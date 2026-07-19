"""Platform OTEL — env vars + IAM for every platform Lambda.

We deliberately do NOT use the AWS-managed ADOT Python Lambda layer.
Verified live on 2026-05-15 (tasks/lessons.md Bug 22): the ADOT
exec-wrapper at /opt/otel-instrument calls Python __import__() on the
handler string, which fails for slash-form handlers like
`src/app/lambda_handler.handler` used here. The ADOT layer also bundles
an older `typing_extensions` that shadows /var/task/lib/typing_extensions/
and breaks pydantic_core import.

Instead, services/_otel_platform.py manually builds an OTLP exporter
at module import time using the SDK packages bundled by
requirements-lambda.txt. Each handler module imports it FIRST.
"""

from dataclasses import dataclass

from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda


@dataclass(frozen=True)
class OtelConfig:
    """Platform OTEL defaults — feature is enabled iff both endpoint and
    secret ARN are provided. Lambdas + agents inherit these."""

    endpoint: str
    auth_secret_arn: str
    sample_rate: str
    service_name_prefix: str
    env: str

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint and self.auth_secret_arn)

    def env_vars(self) -> dict[str, str]:
        """Standard OTEL env vars for platform Lambdas.

        Empty dict when platform OTEL is not configured — caller can blindly
        merge into the Lambda's environment without conditionals.
        """
        if not self.enabled:
            return {}
        return {
            "OTEL_EXPORTER_OTLP_ENDPOINT": self.endpoint,
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
            "OTEL_TRACES_SAMPLER_ARG": self.sample_rate,
            "OTEL_RESOURCE_ATTRIBUTES": (f"service.namespace=agentcore-platform,deployment.environment={self.env}"),
            "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
            # Tighten span export so a slow Langfuse endpoint doesn't burn 10s
            # of Lambda CPU per failed export. Live test 2026-05-16 saw repeated
            # 10s read timeouts; tasks/lessons.md Bug 30.
            "OTEL_EXPORTER_OTLP_TIMEOUT": "2000",
            "OTEL_BSP_SCHEDULE_DELAY": "1000",
            "OTEL_BSP_EXPORT_TIMEOUT": "5000",
            # Resolved to OTEL_EXPORTER_OTLP_HEADERS at module import by
            # services/_otel_platform.py.
            "OTEL_AUTH_SECRET_ARN": self.auth_secret_arn,
        }

    def apply(self, fn: _lambda.Function, fn_purpose: str) -> None:
        """Add OTEL env vars + scoped Secrets Manager perms to a Lambda.

        Idempotent and no-op when platform OTEL is not configured (so callers
        don't have to gate on self.enabled). When configured, the Lambda
        will:
          - Receive the OTLP endpoint + auth-secret ARN as env vars
          - Resolve the auth-header secret at module import (via _otel_platform.py)
          - Emit spans tagged service.name={fn_purpose}, service.namespace=agentcore-platform
        """
        if not self.enabled:
            return

        for k, v in self.env_vars().items():
            fn.add_environment(k, v)
        # Per-Lambda service.name so each shows up distinctly in Langfuse.
        fn.add_environment("OTEL_SERVICE_NAME", f"{self.service_name_prefix}-{fn_purpose}")

        # Scoped GetSecretValue on the platform OTEL auth secret only.
        fn.role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="PlatformOtelAuthSecretRead",
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.auth_secret_arn],
            )
        )
