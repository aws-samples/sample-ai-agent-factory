"""SSM Parameters under /agentcore-workflow/{env}/."""

import aws_cdk as cdk
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ssm as ssm

from .config import PlatformConfig
from .otel import OtelConfig


def build_ssm_parameters(
    stack: cdk.Stack,
    cfg: PlatformConfig,
    otel: OtelConfig,
    *,
    workflows_table: dynamodb.Table,
    deployments_table: dynamodb.Table,
    flows_table: dynamodb.Table,
) -> None:
    """Create SSM parameters under /agentcore-workflow/{env}/ path.

    Requirements: 7.5
    """
    prefix = f"/agentcore-workflow/{cfg.env}"

    ssm.StringParameter(
        stack,
        "CorsOriginsParam",
        parameter_name=f"{prefix}/cors-origins",
        string_value="http://localhost:5173",
        description="Allowed CORS origins for the backend API",
    )

    ssm.StringParameter(
        stack,
        "AwsRegionParam",
        parameter_name=f"{prefix}/aws-region",
        string_value=stack.region,
        description="AWS region for the platform",
    )

    ssm.StringParameter(
        stack,
        "WorkflowsTableNameParam",
        parameter_name=f"{prefix}/dynamodb-table-name",
        string_value=workflows_table.table_name,
        description="DynamoDB table name for workflow storage",
    )

    ssm.StringParameter(
        stack,
        "DeploymentsTableNameParam",
        parameter_name=f"{prefix}/deployments-table-name",
        string_value=deployments_table.table_name,
        description="DynamoDB table name for deployment state",
    )

    ssm.StringParameter(
        stack,
        "FlowsTableNameParam",
        parameter_name=f"{prefix}/dynamodb-flows-table-name",
        string_value=flows_table.table_name,
        description="DynamoDB table name for flow persistence",
    )

    # Platform OTEL defaults — only written when the feature is enabled.
    # Backend reads these via services.observability.get_platform_observability_defaults().
    if otel.enabled:
        ssm.StringParameter(
            stack,
            "OtelEndpointParam",
            parameter_name=f"{prefix}/otel/endpoint",
            string_value=otel.endpoint,
            description="Platform-default OTLP endpoint for all deployed agents",
        )
        ssm.StringParameter(
            stack,
            "OtelAuthSecretArnParam",
            parameter_name=f"{prefix}/otel/auth-secret-arn",
            string_value=otel.auth_secret_arn,
            description="Platform-default OTLP auth header Secrets Manager ARN",
        )
        ssm.StringParameter(
            stack,
            "OtelSampleRateParam",
            parameter_name=f"{prefix}/otel/sample-rate",
            string_value=otel.sample_rate,
            description="Platform-default OTLP trace sample rate (0.0-1.0)",
        )
        ssm.StringParameter(
            stack,
            "OtelServiceNamePrefixParam",
            parameter_name=f"{prefix}/otel/service-name-prefix",
            string_value=otel.service_name_prefix,
            description="Platform-default OTEL service.name prefix",
        )


def build_runtime_ssm_parameters(stack: cdk.Stack, cfg: PlatformConfig, api: apigwv2.HttpApi) -> None:
    """Create SSM parameters that depend on runtime resources (API GW URL)."""
    prefix = f"/agentcore-workflow/{cfg.env}"

    ssm.StringParameter(
        stack,
        "ApiGatewayUrlParam",
        parameter_name=f"{prefix}/api-gateway-url",
        string_value=api.url or "",
        description="API Gateway HTTP API URL",
    )
