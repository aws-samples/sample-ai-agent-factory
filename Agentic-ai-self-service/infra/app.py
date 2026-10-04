#!/usr/bin/env python3
"""CDK app entry point for the AgentCore Visual Workflow Platform.

Reads configuration from CDK context parameters and instantiates the
PlatformStack with the appropriate environment settings.

CDK-NAG (AwsSolutionsChecks) runs during synthesis to flag security
best-practice violations. Suppressions document conscious trade-offs.

Requirements: 1.1, 1.4
"""

import importlib.metadata
import re
import sys
from pathlib import Path

import aws_cdk as cdk
import cdk_nag
from stacks.platform_stack import PlatformStack


def _assert_pinned_cdk_dependencies() -> None:
    """Fail before synth if the CLI launched a different Python environment.

    ``npx`` prepends npm directories to ``PATH`` before it starts the CDK app.
    On a machine with multiple Python installations that can make ``python3``
    resolve differently inside CDK than it did when requirements were installed.
    Synthesizing with stale CDK libraries is especially dangerous because it can
    produce a valid-looking but materially different template.
    """
    requirements = Path(__file__).with_name("requirements.txt")
    pins = {}
    for raw_line in requirements.read_text().splitlines():
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^#\s]+)", raw_line.strip())
        if match:
            pins[match.group(1)] = match.group(2)

    mismatches = []
    for package in ("aws-cdk-lib", "constructs", "cdk-nag"):
        expected = pins.get(package)
        if expected is None:
            mismatches.append(f"{package} has no exact pin in {requirements.name}")
            continue
        try:
            installed = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            installed = "<not installed>"
        if installed != expected:
            mismatches.append(f"{package}: installed {installed}, required {expected}")

    if mismatches:
        detail = "; ".join(mismatches)
        raise RuntimeError(
            "CDK dependency environment does not match infra/requirements.txt "
            f"({detail}). The app is running with {sys.executable}. Install the "
            "requirements with that interpreter, or set CDK_PYTHON to the "
            "interpreter where the pinned dependencies are installed."
        )


_assert_pinned_cdk_dependencies()


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
