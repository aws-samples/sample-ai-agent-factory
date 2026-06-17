#!/usr/bin/env python3
"""CDK app entry point for the AgentCore Visual Workflow Platform.

Reads configuration from CDK context parameters and instantiates the
PlatformStack with the appropriate environment settings.

CDK-NAG (AwsSolutionsChecks) runs during synthesis to flag security
best-practice violations. Suppressions document conscious trade-offs.

Requirements: 1.1, 1.4
"""

import aws_cdk as cdk
import cdk_nag

from stacks.platform_stack import PlatformStack


def get_context_value(app: cdk.App, key: str, default: str | None = None) -> str:
    """Read a value from CDK context, falling back to a default."""
    value = app.node.try_get_context(key)
    if value is None:
        if default is not None:
            return default
        raise ValueError(f"Missing required CDK context parameter: '{key}'. Pass it with -c {key}=<value>")
    return value


app = cdk.App()

environment_name = get_context_value(app, "environment_name", default="dev")
aws_region = get_context_value(app, "aws_region", default="us-east-1")
project_name = get_context_value(app, "project_name", default="agentcore-workflow")

# Optional platform OTEL defaults. When otel_endpoint is provided, every agent
# the platform deploys (and the platform Lambdas themselves via ADOT) emit
# OTLP spans to this backend. Per-canvas Observability node config is locked
# to platform values for endpoint/secret/sampling; only resource_attributes
# can be added per agent.
otel_endpoint = get_context_value(app, "otel_endpoint", default="")
otel_auth_secret_arn = get_context_value(app, "otel_auth_secret_arn", default="")
otel_sample_rate = get_context_value(app, "otel_sample_rate", default="1.0")
otel_service_name_prefix = get_context_value(app, "otel_service_name_prefix", default=project_name)

stack = PlatformStack(
    app,
    f"{project_name}-{environment_name}",
    env=cdk.Environment(region=aws_region),
    environment_name=environment_name,
    project_name=project_name,
    otel_endpoint=otel_endpoint,
    otel_auth_secret_arn=otel_auth_secret_arn,
    otel_sample_rate=otel_sample_rate,
    otel_service_name_prefix=otel_service_name_prefix,
)

# ---------------------------------------------------------------------------
# CDK-NAG: AWS Solutions security checks
# ---------------------------------------------------------------------------
cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(verbose=True))

# Audit issue #4: suppressions are now applied per-construct inside
# PlatformStack._apply_nag_suppressions() so each rule is scoped to the
# specific resource that legitimately needs it. Stack-wide suppressions
# previously hid any new wildcard added in unrelated constructs.

app.synth()
