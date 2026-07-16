"""Step handler: Create AgentCore Online Evaluation config.

Creates an online evaluation configuration attached to a deployed runtime.
Runs AFTER runtime launch since it needs the runtime ARN.

References:
- https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-tutorials/09-AgentCore-E2E/lab-05-agentcore-evals.ipynb
- https://github.com/aws/bedrock-agentcore-starter-toolkit (operations/evaluation/)
"""
# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import re

import json
import logging
import os
import time

import boto3

from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services import step_clients
from app.services.deployment_state_store import DeploymentStateStore

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def handler(event: dict, context) -> dict:
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(
            deployment_id,
            DeploymentStepName.EVALUATION,
            DeploymentStatusEnum.IN_PROGRESS,
        )

        evaluation_config = event.get("evaluation_config") or {}
        region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))
        runtime_arn = event.get("runtime_arn", "")
        runtime_id = event.get("runtime_id", "")

        # Only run online evaluation if explicitly enabled.
        # An observability node with enableOtel=false is NOT an evaluation request.
        if not evaluation_config.get("enabled", False):
            return {
                **event,
                "evaluation_result": {
                    "success": True,
                    "message": "Evaluation not explicitly enabled, skipping",
                },
            }

        if not runtime_arn:
            return {
                **event,
                "evaluation_result": {
                    "success": False,
                    "message": "No runtime_arn available for evaluation",
                },
            }

        agentcore_ctrl = step_clients.client(event, "bedrock-agentcore-control")

        # Extract agent_id from runtime ARN
        # Format: arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}
        agent_id = runtime_id or runtime_arn.split("/")[-1]

        # Name must match [a-zA-Z][a-zA-Z0-9_]{0,47} — no hyphens allowed
        raw_name = evaluation_config.get("name", f"eval_{agent_id}")
        config_name = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name)[:48]
        if not config_name or not config_name[0].isalpha():
            config_name = f"e{config_name}"[:48]
        sampling_rate = evaluation_config.get("samplingRate", 100)

        # Default evaluators
        evaluator_list = evaluation_config.get(
            "evaluators",
            [
                "Builtin.GoalSuccessRate",
                "Builtin.Correctness",
                "Builtin.ToolSelectionAccuracy",
            ],
        )

        # Create IAM role for evaluation
        iam_client = step_clients.client(event, "iam")
        eval_role_name = f"AgentCoreEval-{agent_id[:32]}"
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
        try:
            role_resp = iam_client.create_role(
                RoleName=eval_role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description=f"Evaluation execution role for {agent_id}",
            )
            eval_role_arn = role_resp["Role"]["Arn"]
            iam_client.put_role_policy(
                RoleName=eval_role_name,
                PolicyName="EvaluationPolicy",
                PolicyDocument=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": [
                                    "bedrock:InvokeModel",
                                    "bedrock:InvokeModelWithResponseStream",
                                    "bedrock-agentcore:*",
                                    "bedrock-agentcore-control:*",
                                    "logs:StartQuery",
                                    "logs:GetQueryResults",
                                    "logs:GetLogEvents",
                                    "logs:DescribeLogGroups",
                                    "logs:DescribeLogStreams",
                                    "logs:FilterLogEvents",
                                    "logs:CreateLogGroup",
                                    "logs:PutLogEvents",
                                    "logs:CreateLogStream",
                                    # AgentCore Online Evaluation reads X-Ray
                                    # spans (aws/spans index) and CloudWatch
                                    # Application Signals to extract per-step
                                    # traces. Without these, CreateOnlineEvaluationConfig
                                    # returns AccessDeniedException with
                                    # "Access denied when accessing index policy
                                    # for aws/spans". See lessons.md Bug 119.
                                    "xray:GetIndexingRules",
                                    "xray:GetTraceSummaries",
                                    "xray:BatchGetTraces",
                                    "xray:GetTraceGraph",
                                    "xray:GetGroup",
                                    "xray:GetGroups",
                                    "xray:GetServiceGraph",
                                    "xray:GetSamplingRules",
                                    "application-signals:Get*",
                                    "application-signals:List*",
                                    "application-signals:BatchGet*",
                                ],
                                "Resource": "*",
                            }
                        ],
                    }
                ),
            )
            time.sleep(10)
        except iam_client.exceptions.EntityAlreadyExistsException:
            eval_role_arn = iam_client.get_role(RoleName=eval_role_name)["Role"]["Arn"]

        # Build evaluator configs — list of dicts with evaluatorId key
        evaluators = [{"evaluatorId": ev} for ev in evaluator_list]

        # Build log group name for the runtime. Bug 139: AgentCore Runtime emits
        # its invocation logs (incl. gen_ai.* spans) to the "-DEFAULT" endpoint log
        # group — the same group cost + the dashboard read. Without the suffix the
        # eval config watched an empty group and the evaluations panel stayed blank.
        log_group_name = f"/aws/bedrock-agentcore/runtimes/{agent_id}-DEFAULT"

        # Create online evaluation config
        try:
            create_params = {
                "onlineEvaluationConfigName": config_name,
                "rule": {"samplingConfig": {"samplingPercentage": sampling_rate}},
                "dataSourceConfig": {
                    "cloudWatchLogs": {
                        "logGroupNames": [log_group_name],
                        "serviceNames": [agent_id],
                    }
                },
                "evaluators": evaluators,
                "evaluationExecutionRoleArn": eval_role_arn,
                "enableOnCreate": True,
            }
            resp = agentcore_ctrl.create_online_evaluation_config(**create_params)
            config_id = resp.get("onlineEvaluationConfigId", "")
            logger.info("Created online evaluation config: %s", config_id)
        except Exception as e:
            if "ConflictException" in str(e) or "already exists" in str(e):
                logger.info("Evaluation config already exists, looking up")
                configs = agentcore_ctrl.list_online_evaluation_configs(agentId=agent_id)
                config_id = ""
                for cfg in configs.get("onlineEvaluationConfigs", configs.get("items", [])):
                    if cfg.get("onlineEvaluationConfigName") == config_name:
                        config_id = cfg.get("onlineEvaluationConfigId", "")
                        break
                if not config_id:
                    logger.warning("Could not find existing eval config: %s", e)
            else:
                raise

        return {
            **event,
            "evaluation_result": {
                "success": True,
                "config_id": config_id,
                "config_name": config_name,
            },
        }

    except Exception:
        logger.exception("Evaluation step failed for deployment %s", deployment_id)
        raise
