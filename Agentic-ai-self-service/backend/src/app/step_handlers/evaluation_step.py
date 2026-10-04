"""Step handler: Create AgentCore Online Evaluation config.

Creates an online evaluation configuration attached to a deployed runtime.
Runs AFTER runtime launch since it needs the runtime ARN.

References:
- https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-tutorials/09-AgentCore-E2E/lab-05-agentcore-evals.ipynb
- https://github.com/aws/bedrock-agentcore-starter-toolkit (operations/evaluation/)
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import json
import logging
import os
import re
import time

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services import step_clients
from app.services.aws_errors import is_error
from app.services.aws_pagination import list_all
from app.services.deployment_state_store import DeploymentStateStore
from app.services.iam_boundary import create_role_kwargs, ensure_role_boundary
from app.services.naming import regional_iam_role_name
from app.services.resource_ownership import assert_this_deployment_may_mutate
from app.services.resource_tagging import governed_tag_list

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _find_online_evaluation_config_id(
    agentcore_ctrl,
    config_name: str,
    *,
    retries: int = 3,
) -> str:
    """Find an existing config by name across every page.

    ``ListOnlineEvaluationConfigs`` has no agent/runtime filter in the SDK, so
    passing ``agentId`` here is a client-side validation error rather than a
    server-side filter. Scan the account/region listing and allow a short
    eventual-consistency window after a conflicting create.
    """
    for attempt in range(max(1, retries)):
        configs = list_all(
            agentcore_ctrl,
            "list_online_evaluation_configs",
            item_keys=("onlineEvaluationConfigs", "items"),
            request={"maxResults": 50},
        )
        for config in configs:
            if config.get("onlineEvaluationConfigName") == config_name:
                return str(config.get("onlineEvaluationConfigId") or "")
        if attempt + 1 < max(1, retries):
            time.sleep(2)
    return ""


def _config_targets_runtime(agentcore_ctrl, config_id: str, agent_id: str, log_group_name: str) -> bool:
    """True only when the existing config's CloudWatch data source names THIS runtime.

    Read, not inferred: a name match is a hypothesis, the data source is the fact. Any read
    failure answers False, so an unprovable adoption is recorded as not ours.
    """
    try:
        cfg = agentcore_ctrl.get_online_evaluation_config(onlineEvaluationConfigId=config_id)
    except Exception:  # noqa: BLE001
        logger.warning("Could not read evaluation config %s to prove its data source", config_id)
        return False
    logs_cfg = ((cfg.get("dataSourceConfig") or {}).get("cloudWatchLogs")) or {}
    service_names = {str(n) for n in (logs_cfg.get("serviceNames") or [])}
    log_groups = {str(n) for n in (logs_cfg.get("logGroupNames") or [])}
    return agent_id in service_names or log_group_name in log_groups


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
        runtime_arn = event.get("runtime_arn", "")
        runtime_id = event.get("runtime_id", "")
        region = event.get("target_region") or _get_env(
            "APP_AWS_REGION",
            _get_env("AWS_REGION", "us-east-1"),
        )

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
        default_config_name = re.sub(r"[^a-zA-Z0-9_]", "_", f"eval_{agent_id}")[:48]
        config_created_by_deployment = False
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
        eval_role_name = regional_iam_role_name(
            f"AgentCoreEval-{agent_id[:32]}",
            region,
        )
        eval_role_created = True
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
                # This role carried no tags at all, so a later deploy had no way to
                # tell its own eval role from an identically-named foreign one. See
                # services/resource_ownership.py (peer finding F-7).
                Tags=governed_tag_list(region, event.get("resource_tags")),
                **create_role_kwargs(),
            )
        except iam_client.exceptions.EntityAlreadyExistsException:
            eval_role_created = False
            # Passing a colliding foreign role to AgentCore is itself a confused
            # deputy: if that role trusts the service, this deployment can cause the
            # service to exercise somebody else's permissions even though we never
            # edit the role. Refuse unless live tags prove this stack owns it.
            _existing_eval_role = iam_client.get_role(RoleName=eval_role_name)["Role"]
            eval_role_arn = _existing_eval_role["Arn"]
            assert_this_deployment_may_mutate(
                f"IAM role {eval_role_name}",
                _existing_eval_role.get("Tags"),
                region,
            )
            # F-06: retrofit the permissions boundary once ownership is proven.
            ensure_role_boundary(iam_client, eval_role_name, role=_existing_eval_role)
            store.record_resource(
                deployment_id,
                {
                    "type": "iam_role",
                    "name": eval_role_name,
                    "region": region,
                    "created_by_deployment": False,
                },
            )
        else:
            eval_role_arn = role_resp["Role"]["Arn"]
            # Journal immediately after CreateRole acknowledges the resource. Policy
            # attachment and IAM propagation can still fail; teardown must already
            # know the exact account-global role name if either does.
            store.record_resource(
                deployment_id,
                {
                    "type": "iam_role",
                    "name": eval_role_name,
                    "region": region,
                    "created_by_deployment": True,
                },
            )
            iam_client.put_role_policy(
                RoleName=eval_role_name,
                PolicyName="EvaluationPolicy",
                PolicyDocument=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                # LLM-judge evaluator model calls. Resource "*"
                                # required: cross-region inference profiles route
                                # to foundation-model ARNs in other regions and
                                # the evaluator model is service-selected.
                                "Sid": "EvaluatorModelAccess",
                                "Effect": "Allow",
                                "Action": [
                                    "bedrock:InvokeModel",
                                    "bedrock:InvokeModelWithResponseStream",
                                ],
                                "Resource": "*",
                            },
                            {
                                # Exact AgentCore evaluation verbs (previously
                                # bedrock-agentcore:* + -control:* — HIGH IAM
                                # finding). Eval-config/evaluator ids are minted
                                # by the service, so ARNs are unknowable here —
                                # scoped by the exact action list instead.
                                "Sid": "AgentCoreEvaluation",
                                "Effect": "Allow",
                                "Action": [
                                    "bedrock-agentcore:Evaluate",
                                    "bedrock-agentcore:GetOnlineEvaluationConfig",
                                    "bedrock-agentcore:ListOnlineEvaluationConfigs",
                                    "bedrock-agentcore:ListEvaluators",
                                    "bedrock-agentcore:GetEvaluator",
                                ],
                                "Resource": "*",
                            },
                            {
                                # Read runtime spans/logs + write eval results —
                                # scoped to the AgentCore runtime log-group
                                # namespace plus the aws/spans trace-index group
                                # (Bug 119/139) instead of "*".
                                "Sid": "EvaluationLogsScoped",
                                "Effect": "Allow",
                                "Action": [
                                    "logs:StartQuery",
                                    "logs:GetQueryResults",
                                    "logs:GetLogEvents",
                                    "logs:DescribeLogStreams",
                                    "logs:FilterLogEvents",
                                    "logs:CreateLogGroup",
                                    "logs:PutLogEvents",
                                    "logs:CreateLogStream",
                                ],
                                "Resource": [
                                    "arn:aws:logs:*:*:log-group:/aws/bedrock-agentcore/*",
                                    "arn:aws:logs:*:*:log-group:/aws/bedrock-agentcore/*:log-stream:*",
                                    "arn:aws:logs:*:*:log-group:aws/spans",
                                    "arn:aws:logs:*:*:log-group:aws/spans:log-stream:*",
                                ],
                            },
                            {
                                # Account-level discovery/trace APIs with no
                                # resource-ARN form. AgentCore Online Evaluation
                                # reads X-Ray spans (aws/spans index) and
                                # CloudWatch Application Signals to extract
                                # per-step traces. Without these,
                                # CreateOnlineEvaluationConfig returns
                                # AccessDeniedException "Access denied when
                                # accessing index policy for aws/spans". See
                                # lessons.md Bug 119.
                                "Sid": "EvaluationTraceDiscovery",
                                "Effect": "Allow",
                                "Action": [
                                    "logs:DescribeLogGroups",
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
                            },
                        ],
                    }
                ),
            )
            time.sleep(10)

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
            config_created_by_deployment = True
        except Exception as e:
            # "already exists" fallback kept: conflicts can surface as a
            # ValidationException whose message says "already exists".
            if is_error(e, "ConflictException") or "already exists" in str(e):
                logger.info("Evaluation config already exists, looking up")
                config_id = _find_online_evaluation_config_id(
                    agentcore_ctrl,
                    config_name,
                )
                if not config_id:
                    raise RuntimeError(
                        f"Online evaluation config {config_name!r} conflicted but "
                        "could not be found after a complete paginated lookup."
                    ) from e
                # Adopted: ours only if the name is the default derived from THIS runtime id AND
                # the existing config's data source actually names this runtime -- a same-named
                # config sampling another runtime's log group is someone else's, whatever it is
                # called. A user-chosen name can be shared, so it is never adopted as ours.
                config_created_by_deployment = config_name == default_config_name and _config_targets_runtime(
                    agentcore_ctrl, config_id, agent_id, log_group_name
                )
            else:
                raise

        # Journal the config (and, by id, its eval-results log group) so teardown deletes it by
        # exact id from the manifest instead of guessing from a name. See Bug 124 / F-sidecars.
        store.record_resource(
            deployment_id,
            {
                "type": "online_evaluation_config",
                "id": config_id,
                "name": config_name,
                "region": region,
                "created_by_deployment": config_created_by_deployment,
            },
        )
        return {
            **event,
            "evaluation_result": {
                "success": True,
                "config_id": config_id,
                "config_name": config_name,
                "role_name": eval_role_name,
                "role_created_by_deployment": eval_role_created,
            },
        }

    except Exception:
        logger.exception("Evaluation step failed for deployment %s", deployment_id)
        raise
