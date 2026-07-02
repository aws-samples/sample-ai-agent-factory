"""OTLP observability env-var construction.

Single source of truth for translating an ObservabilityConfig into the OTEL_*
environment variables consumed by the AgentCore Runtime. Used by:
  - services/deployment.py (direct deploy path)
  - step_handlers/runtime_configure_step.py (Step Functions path)
  - services/cfn_template_generator.py (CloudFormation export path)

Keeping this in one place prevents drift across the three deploy paths
(see tasks/lessons.md Bug 9).

When platform-level OTEL is enabled (SSM keys under /agentcore-workflow/{env}/otel/*)
build_otel_env_vars() merges in those defaults and locks endpoint/secret/sample
to the platform values — per-canvas configs cannot override them. Per-canvas
resource_attributes are still merged additively.
"""

import logging
import os
import re
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)


# Critic Finding 1 (BLOCKER) — cross-tenant Secrets Manager exfiltration.
# User-supplied auth_header_secret_arn ARNs are written into the runtime IAM
# role's secretsmanager:GetSecretValue Resource list and into the runtime's
# OTEL_AUTH_SECRET_ARN env var. Without a namespace check, tenant A could
# point at tenant B's secret and have the runtime POST it to a tenant-A-
# controlled OTLP endpoint as an Authorization header. We pin user-supplied
# ARNs to the agentcore-otel/ prefix that store_credentials writes into.
# Platform defaults (admin-managed via SSM) bypass this check intentionally.
_USER_OTEL_SECRET_ARN_RE = re.compile(
    r"^arn:aws:secretsmanager:[a-z0-9-]+:\d{12}:secret:agentcore-otel/[A-Za-z0-9_/-]+"
)


def _validate_user_otel_secret_arn(arn: str) -> str:
    """Reject user-supplied OTEL secret ARNs outside the agentcore-otel/ namespace.

    Returns the ARN unchanged when it matches the expected shape. Raises
    ``ValueError`` otherwise. ONLY apply to per-canvas (tenant-supplied)
    ARNs — platform_defaults come from SSM and are operator-managed.
    """
    if not isinstance(arn, str) or not _USER_OTEL_SECRET_ARN_RE.match(arn):
        raise ValueError(
            "auth_header_secret_arn must be in the agentcore-otel/ namespace"
        )
    return arn


def _redact_arn(arn: Optional[str]) -> str:
    """Redact a Secrets Manager ARN for logging.

    The ARN is a pointer, not credential material, but it carries the AWS
    account ID and the secret name — both of which we don't want in
    CloudWatch where a less-privileged operator could read them. Returns a
    fixed-shape string that's still useful for debugging (you can tell
    whether an ARN was supplied) without leaking either field.
    """
    if not arn:
        return "<none>"
    return "<redacted-secrets-manager-arn>"


def _redact_endpoint(endpoint: Optional[str]) -> str:
    """Redact OTLP endpoint values for logging."""
    if not endpoint:
        return "<none>"
    return "<redacted-otlp-endpoint>"


# Provider -> default OTLP endpoint when caller leaves it blank.
# Most providers require account-specific URLs, so only the truly fixed ones
# are defaulted here. Custom and most clouds force the user to supply one.
#
# NOTE on agentcore_native: AgentCore Runtime does NOT currently ship an OTLP
# sidecar at localhost:4318. Selecting this provider without an explicit
# endpoint produces no traces — spans are silently dropped at connect time.
# Verified via live deploy 2026-05-15 (see tasks/lessons.md Bug 18).
# We deliberately do NOT default an endpoint for it; the caller must supply one.
_PROVIDER_DEFAULT_ENDPOINTS: dict[str, str] = {
    "langfuse": "https://cloud.langfuse.com/api/public/otel",
}


# P-PLAT-010 / Bug 194: env that enables AgentCore-native ADOT observability
# WITHOUT injecting a 3rd-party/localhost OTLP endpoint. This flag is REQUIRED
# for gen_ai.usage spans to reach the runtime's
# /aws/bedrock-agentcore/runtimes/{rid}-DEFAULT log group — which
# cost_tracking.summarize_from_logs() reads for GET /cost. Without it the
# managed deploy path creates NO TracerProvider at all (StrandsTelemetry only
# runs when OTEL_EXPORTER_OTLP_ENDPOINT is set), so NO spans are emitted
# anywhere and by_model stays {} (Bug 194 root cause).
# CAVEAT: prior live testing showed this flag ALONE did not produce spans —
# the managed runtime container never starts OTEL auto-instrumentation (the
# entrypoint is not launched under opentelemetry-instrument), so the deeper
# fix lives in the runtime container bootstrap. See tasks/lessons.md Bug 194.
# OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental is required for the
# token-count attributes to appear on those spans (Bug 17). We deliberately do
# NOT set OTEL_EXPORTER_OTLP_ENDPOINT here — ADOT supplies the AgentCore
# collector endpoint itself; injecting one would route spans off to a 3rd party
# (or a non-existent localhost:4318 sidecar, Bug 18) instead of -DEFAULT.
# Account-level prerequisite (CloudWatch Transaction Search trace-segment
# destination = CloudWatchLogs) is operator-managed and confirmed enabled.
_AGENTCORE_NATIVE_OBSERVABILITY_ENV: dict[str, str] = {
    "AGENT_OBSERVABILITY_ENABLED": "true",
    "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
}


@lru_cache(maxsize=1)
def get_platform_observability_defaults() -> Optional[dict]:
    """Read platform-level OTEL defaults from SSM Parameter Store.

    Returns:
        A dict shaped like an ObservabilityConfig (with snake_case keys) when
        the platform admin has configured OTEL via deploy.sh. Returns None
        when no platform default is configured — callers should fall back to
        per-canvas-only behavior.

    Cached per process (runs once per Lambda cold start). Resilient: on any
    SSM error, returns None and logs a warning rather than raising.

    SSM keys read (under /agentcore-workflow/{env}/otel/):
        - endpoint               (required to enable the feature)
        - auth-secret-arn        (required)
        - sample-rate            (optional, default "1.0")
        - service-name-prefix    (optional, default project_name)
    """
    env = os.environ.get("ENVIRONMENT") or os.environ.get("ENVIRONMENT_NAME") or "dev"
    prefix = f"/agentcore-workflow/{env}/otel"
    region = os.environ.get("APP_AWS_REGION") or os.environ.get("AWS_REGION") or "us-east-1"

    try:
        import boto3
        ssm = boto3.client("ssm", region_name=region)
        resp = ssm.get_parameters_by_path(Path=prefix, Recursive=False)
    except Exception as e:
        logger.warning("Could not read platform OTEL SSM params: %s", e)
        return None

    params = {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in resp.get("Parameters", [])}
    endpoint = params.get("endpoint", "").strip()
    secret_arn = params.get("auth-secret-arn", "").strip()
    if not endpoint or not secret_arn:
        return None

    try:
        sample_rate = float(params.get("sample-rate", "1.0"))
    except ValueError:
        sample_rate = 1.0

    return {
        "enabled": True,
        "provider": "custom",
        "otlp_endpoint": endpoint,
        "auth_header_secret_arn": secret_arn,
        "sample_rate": sample_rate,
        "service_name_prefix": params.get("service-name-prefix", "").strip() or None,
        "resource_attributes": {},
    }


def _resource_attributes_string(attrs: dict[str, str], deployment_id: str) -> str:
    """Build the OTEL_RESOURCE_ATTRIBUTES value (comma-separated key=value)."""
    merged = dict(attrs or {})
    if deployment_id and "deployment.id" not in merged:
        merged["deployment.id"] = deployment_id
    return ",".join(f"{k}={v}" for k, v in merged.items() if v)


def build_otel_env_vars(
    observability: Optional[dict],
    *,
    runtime_name: str,
    deployment_id: str = "",
    enable_otel_legacy: bool = False,
    platform_defaults: Optional[dict] = None,
) -> dict[str, str]:
    """Translate an ObservabilityConfig dict into runtime env vars.

    Args:
        observability: Dict from ObservabilityConfig.model_dump() or the raw
            payload from the frontend. May be None.
        runtime_name: Runtime name, used as fallback service.name.
        deployment_id: Deployment UUID, added to OTEL_RESOURCE_ATTRIBUTES.
        enable_otel_legacy: True if the runtime config has the legacy
            ``enable_otel: true`` flag set. Treated as disabled now (verified
            2026-05-15: the localhost sidecar fallback this used to enable
            does not exist).
        platform_defaults: Optional dict from get_platform_observability_defaults().
            When provided, endpoint/secret_arn/sample_rate/service_name are
            LOCKED to platform values and per-canvas overrides are dropped
            (with a WARNING log). resource_attributes merge additively
            (canvas keys win on collision). When None, behaves as before.

    Returns:
        Dict of OTEL_* env vars. Empty when observability is disabled AND no
        platform defaults are configured.
    """
    obs = observability or {}
    plat = platform_defaults or {}

    # Platform defaults present → telemetry is always on for every agent,
    # regardless of whether the user dropped an Observability node on the canvas.
    if plat:
        # Detect override attempts so the operator can tell the per-canvas
        # values were rejected.
        canvas_endpoint = obs.get("otlp_endpoint") or obs.get("otlpEndpoint")
        canvas_secret = obs.get("auth_header_secret_arn") or obs.get("authHeaderSecretArn")
        if canvas_secret:
            # Critic Finding 1: validate per-canvas ARN even when platform
            # defaults will override it, so a malicious canvas config never
            # silently passes through this code path.
            _validate_user_otel_secret_arn(canvas_secret)
        canvas_sample = obs.get("sample_rate") if "sample_rate" in obs else obs.get("sampleRate")
        if (canvas_endpoint and canvas_endpoint != plat.get("otlp_endpoint")) \
                or (canvas_secret and canvas_secret != plat.get("auth_header_secret_arn")) \
                or (canvas_sample is not None and canvas_sample != plat.get("sample_rate", 1.0)):
            # Both endpoint and ARN are redacted before logging — the endpoint
            # is operator-controlled but can carry tenant/project hints, and
            # the ARN carries account ID + secret name. Operators only need
            # to know that an override attempt was rejected, not the values.
            logger.warning(
                "Per-canvas Observability override ignored — platform defaults are locked. "
                "Canvas wanted endpoint=%s secret=%s sample=%r; using platform endpoint=%s.",
                _redact_endpoint(canvas_endpoint),
                _redact_arn(canvas_secret),
                canvas_sample,
                _redact_endpoint(plat.get("otlp_endpoint")),
            )
        # Merge: platform supplies endpoint/secret/sample/service-prefix; canvas
        # supplies any extra resource attributes (and only those).
        platform_attrs = plat.get("resource_attributes") or {}
        canvas_attrs = obs.get("resource_attributes") or obs.get("resourceAttributes") or {}
        merged_attrs = {**platform_attrs, **canvas_attrs}
        prefix = plat.get("service_name_prefix")
        canvas_service_name = obs.get("service_name") or obs.get("serviceName")
        # Service name: platform prefix + canvas/runtime name when prefix set,
        # else just canvas/runtime name. Caller's per-canvas service_name is
        # respected as the suffix because it's still useful for filtering.
        suffix = canvas_service_name or runtime_name
        service_name = f"{prefix}-{suffix}" if prefix else suffix
        obs = {
            "enabled": True,
            "provider": "custom",
            "otlp_endpoint": plat["otlp_endpoint"],
            "otlp_protocol": "http/protobuf",
            "service_name": service_name,
            "sample_rate": plat.get("sample_rate", 1.0),
            "auth_header_secret_arn": plat["auth_header_secret_arn"],
            "resource_attributes": merged_attrs,
            "extra_headers": obs.get("extra_headers") or obs.get("extraHeaders") or {},
        }

    # Backward compat: legacy enable_otel=True with no observability config
    # used to fall back to a localhost AgentCore sidecar that does not exist
    # in production (verified via live deploy 2026-05-15). Treat as disabled.
    if not obs:
        return {}

    if not obs.get("enabled", True):
        return {}

    provider = obs.get("provider", "langfuse")
    caller_endpoint = obs.get("otlp_endpoint") or obs.get("otlpEndpoint") or ""
    caller_secret = (
        obs.get("auth_header_secret_arn") or obs.get("authHeaderSecretArn") or ""
    )
    caller_headers = obs.get("extra_headers") or obs.get("extraHeaders") or {}

    # P-PLAT-010 cost-rollup gap (Bug 194): a 3rd-party provider default
    # endpoint (e.g. langfuse's cloud URL) is only usable when the caller
    # actually supplied credentials for it. If observability is "enabled" by
    # default but the caller gave NO endpoint AND NO credentials (no secret
    # ARN, no auth headers), injecting OTEL_EXPORTER_OTLP_ENDPOINT=<provider-
    # cloud-url> would route the gen_ai.usage spans off to an unauthenticated
    # 3rd-party endpoint (401, dropped) instead of the -DEFAULT log group that
    # cost_tracking.summarize_from_logs() reads, leaving by_model={}.
    #
    # So: only fall back to a provider DEFAULT endpoint when the caller has
    # credentials that make it usable. Otherwise we do NOT inject a 3rd-party
    # endpoint; instead we ENABLE AgentCore-native ADOT observability so the
    # runtime emits gen_ai.usage spans to its -DEFAULT log group (the source
    # GET /cost reads). Bug 194 proved that leaving OTEL_* entirely unset does
    # NOT preserve any native export — the managed deploy path creates no
    # TracerProvider, so nothing is emitted at all. An explicit caller endpoint
    # (or platform default, handled in the `if plat:` block above) is always
    # honoured. This native path does NOT set OTEL_EXPORTER_OTLP_ENDPOINT, so
    # it does not reintroduce the non-existent localhost:4318 sidecar (Bug 18).
    endpoint = caller_endpoint
    if not endpoint:
        default_endpoint = _PROVIDER_DEFAULT_ENDPOINTS.get(provider, "")
        if default_endpoint and (caller_secret or caller_headers):
            endpoint = default_endpoint
    if not endpoint:
        # No usable 3rd-party export target, but observability IS enabled
        # (explicitly or by default). Turn on AgentCore-native ADOT capture so
        # gen_ai.usage spans land in the -DEFAULT log group for cost rollup.
        return dict(_AGENTCORE_NATIVE_OBSERVABILITY_ENV)

    protocol = obs.get("otlp_protocol") or obs.get("otlpProtocol", "http/protobuf")
    service_name = obs.get("service_name") or obs.get("serviceName") or runtime_name
    sample_rate = obs.get("sample_rate") if "sample_rate" in obs \
        else obs.get("sampleRate", 1.0)
    resource_attrs = obs.get("resource_attributes") or obs.get("resourceAttributes") or {}
    secret_arn = caller_secret
    if secret_arn and not plat:
        # Critic Finding 1: per-canvas (tenant-supplied) ARNs MUST live in
        # the agentcore-otel/ namespace. Skip validation when plat is set —
        # in that branch, obs has already been replaced with platform-default
        # values which are admin-managed via SSM (see the `if plat:` block
        # above). We deliberately do not double-check platform defaults.
        _validate_user_otel_secret_arn(secret_arn)
    extra_headers = caller_headers

    env: dict[str, str] = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_PROTOCOL": protocol,
        "OTEL_SERVICE_NAME": service_name,
        "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
        "OTEL_TRACES_SAMPLER_ARG": str(sample_rate),
        # Strands gates rich GenAI attributes behind this opt-in.
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        # Tighten flush so spans land before AgentCore idle-stop kills the runtime.
        "OTEL_BSP_SCHEDULE_DELAY": "1000",
        "OTEL_BSP_EXPORT_TIMEOUT": "5000",
    }

    resource_str = _resource_attributes_string(resource_attrs, deployment_id)
    if resource_str:
        env["OTEL_RESOURCE_ATTRIBUTES"] = resource_str

    if secret_arn:
        # Codegen prologue resolves this and writes OTEL_EXPORTER_OTLP_HEADERS.
        env["OTEL_AUTH_SECRET_ARN"] = secret_arn

    if extra_headers:
        # Non-secret headers go in directly; the prologue merges them with the
        # resolved auth header.
        env["OTEL_EXPORTER_OTLP_EXTRA_HEADERS"] = ",".join(
            f"{k}={v}" for k, v in extra_headers.items() if v
        )

    return env
