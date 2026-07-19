"""CloudWatch Alarms (LAMBDA-011) — SNS topic, Lambda/DDB/SFN alarms, RBAC metric filter."""

import aws_cdk as cdk
from aws_cdk import Duration
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_stepfunctions as sfn

from .config import PlatformConfig
from .tables import Tables


def build_lambda_alarms(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    *,
    workflow_lambda: _lambda.Function,
    deployment_lambda: _lambda.Function,
    step_lambdas: dict[str, _lambda.Function],
    tables: Tables,
    state_machine: sfn.StateMachine,
) -> sns.Topic:
    """Create CloudWatch alarms for Lambdas + the new governance surfaces.

    Production hardening (Part B): an SNS alarm topic routes EVERY alarm to
    operators; DynamoDB throttle alarms cover the governance stores; a Step
    Functions ExecutionsFailed alarm covers the deploy pipeline; and a metric
    filter on the RBAC "would-deny" advisory log line lets an admin SEE what
    enforcement WOULD block before flipping RBAC_ENFORCE=true.
    """
    # --- SNS alarm topic: single fan-out for all alarms ---------------
    alarm_topic = sns.Topic(
        stack,
        "AlarmTopic",
        topic_name=f"{cfg.project}-{cfg.env}-alarms",
        display_name="AgentCore platform alarms",
        # Security best practice (cdk-nag SNS2/SNS3): SSE at rest + enforce
        # TLS in transit. AWS-managed SNS key keeps it zero-ops.
        master_key=kms.Alias.from_alias_name(stack, "SnsManagedKey", "alias/aws/sns"),
        enforce_ssl=True,
    )
    _action = cw_actions.SnsAction(alarm_topic)

    def _wire(alarm: cloudwatch.Alarm) -> None:
        alarm.add_alarm_action(_action)
        alarm.add_ok_action(_action)

    # --- Lambda error + throttle alarms (all functions) ---------------
    all_fns: dict[str, _lambda.Function] = {
        "workflow": workflow_lambda,
        "deployment": deployment_lambda,
        **{f"step-{k}": v for k, v in step_lambdas.items()},
    }
    for name, fn in all_fns.items():
        slug = name.replace("_", "-")
        _wire(
            fn.metric_errors(period=Duration.minutes(5)).create_alarm(
                stack,
                f"Alarm-{slug}-errors",
                alarm_name=f"{cfg.project}-{cfg.env}-{slug}-errors",
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
        )
        _wire(
            fn.metric_throttles(period=Duration.minutes(5)).create_alarm(
                stack,
                f"Alarm-{slug}-throttles",
                alarm_name=f"{cfg.project}-{cfg.env}-{slug}-throttles",
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
        )

    # --- p99 latency on the two user-facing API Lambdas ---------------
    for name, fn in (("workflow", workflow_lambda), ("deployment", deployment_lambda)):
        _wire(
            fn.metric_duration(period=Duration.minutes(5), statistic="p99").create_alarm(
                stack,
                f"Alarm-{name}-p99",
                alarm_name=f"{cfg.project}-{cfg.env}-{name}-p99-latency",
                threshold=25000,  # ms — below the 29s API-GW ceiling
                evaluation_periods=3,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
        )

    # --- DynamoDB throttle alarms on the governance stores ------------
    _ddb_tables = {
        "workflows": tables.workflows,
        "deployments": tables.deployments,
        "tag-policy": tables.tag_policy,
        "budget": tables.budget,
        "audit": tables.audit,
    }
    for tname, table in _ddb_tables.items():
        _wire(
            table.metric("ThrottledRequests", period=Duration.minutes(5), statistic="Sum").create_alarm(
                stack,
                f"Alarm-ddb-{tname}-throttle",
                alarm_name=f"{cfg.project}-{cfg.env}-ddb-{tname}-throttle",
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
        )

    # --- Step Functions: deploy pipeline failures ---------------------
    if state_machine is not None:
        _wire(
            state_machine.metric_failed(period=Duration.minutes(5)).create_alarm(
                stack,
                "Alarm-sfn-deploy-failed",
                alarm_name=f"{cfg.project}-{cfg.env}-deploy-executions-failed",
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
        )

    # --- RBAC advisory "would-deny" metric filter --------------------
    # services/rbac.py logs "RBAC advisory (would-deny): ..." in advisory
    # mode. This metric filter surfaces it as a CloudWatch metric so an admin
    # can quantify what RBAC_ENFORCE=true WOULD block before enabling it
    # (safe rollout — see docs/RBAC_ROLLOUT.md). No alarm (informational).
    if workflow_lambda is not None and workflow_lambda.log_group is not None:
        logs.MetricFilter(
            stack,
            "RbacWouldDenyMetricFilter",
            log_group=workflow_lambda.log_group,
            metric_namespace=f"{cfg.project}/{cfg.env}/rbac",
            metric_name="WouldDeny",
            filter_pattern=logs.FilterPattern.literal('"RBAC advisory (would-deny)"'),
            metric_value="1",
            default_value=0,
        )

    return alarm_topic
