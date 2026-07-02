"""Tests for OTLP env-var construction and codegen injection.

Covers the three paths that must stay in sync (Bug 9 in tasks/lessons.md):
  1) build_otel_env_vars() output
  2) _inject_otel() codegen post-processor
  3) Backward compat with the legacy enable_otel boolean
"""

import pytest

from app.services.code_generator import _inject_otel
from app.services.observability import (
    _validate_user_otel_secret_arn,
    build_otel_env_vars,
)


def test_disabled_returns_empty():
    """Truly-disabled stays {} — the Bug 194 native-enable path must NOT
    force observability on when the user explicitly turned it off."""
    assert build_otel_env_vars(None, runtime_name="agent") == {}
    assert build_otel_env_vars({"enabled": False}, runtime_name="agent") == {}


def test_legacy_enable_otel_without_observability_returns_empty():
    """Legacy enable_otel=True with no observability config now returns {}.

    Live deploy 2026-05-15 confirmed AgentCore Runtime does NOT ship a
    localhost:4318 OTLP sidecar — falling back to it produced silent span
    drops. Treat the legacy flag without explicit config as disabled.
    See tasks/lessons.md Bug 18.
    """
    env = build_otel_env_vars(
        None, runtime_name="agent-x", enable_otel_legacy=True
    )
    assert env == {}


def test_langfuse_provider_emits_full_envset():
    env = build_otel_env_vars(
        {
            "enabled": True,
            "provider": "langfuse",
            "otlp_endpoint": "https://cloud.langfuse.com/api/public/otel",
            "service_name": "myagent",
            "sample_rate": 0.5,
            "auth_header_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/langfuse/foo-abcdef123456",
            "resource_attributes": {"env": "prod", "team": "ai"},
        },
        runtime_name="myagent",
        deployment_id="dep-123",
    )
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://cloud.langfuse.com/api/public/otel"
    assert env["OTEL_SERVICE_NAME"] == "myagent"
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.5"
    assert env["OTEL_AUTH_SECRET_ARN"].startswith("arn:aws:secretsmanager:")
    assert "deployment.id=dep-123" in env["OTEL_RESOURCE_ATTRIBUTES"]
    assert "env=prod" in env["OTEL_RESOURCE_ATTRIBUTES"]
    # Strands rich GenAI conventions opt-in
    assert env["OTEL_SEMCONV_STABILITY_OPT_IN"] == "gen_ai_latest_experimental"
    # Flush tuning so spans land before idle stop
    assert env["OTEL_BSP_SCHEDULE_DELAY"] == "1000"


def test_camelcase_aliases_work():
    """Frontend payload may arrive in camelCase. Helper must accept both."""
    env = build_otel_env_vars(
        {
            "enabled": True,
            "provider": "langfuse",
            "otlpEndpoint": "https://cloud.langfuse.com/api/public/otel",
            "serviceName": "agent",
            "sampleRate": 0.25,
            "authHeaderSecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/langfuse/u-camel-aabbccdd",
        },
        runtime_name="agent",
    )
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://cloud.langfuse.com/api/public/otel"
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.25"
    assert env["OTEL_AUTH_SECRET_ARN"] == "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/langfuse/u-camel-aabbccdd"


def test_dual_export_native_field_is_ignored():
    """dual_export_native used to wire a localhost:4318 native sidecar.

    Removed because AgentCore Runtime does not provide that sidecar —
    spans were dropped at connect time. The flag is now ignored. See
    tasks/lessons.md Bug 18.
    """
    env = build_otel_env_vars(
        {
            "enabled": True,
            "provider": "langfuse",
            "otlp_endpoint": "https://cloud.langfuse.com/api/public/otel",
            "dual_export_native": True,
        },
        runtime_name="agent",
    )
    assert "OTEL_DUAL_EXPORT_NATIVE_ENDPOINT" not in env


def test_no_endpoint_enables_native_observability():
    """Custom provider, enabled, no 3rd-party endpoint = enable AgentCore-native
    ADOT capture (Bug 194), NOT empty env.

    Returning {} here used to leave the runtime with NO TracerProvider, so it
    emitted zero gen_ai.usage spans and GET /cost by_model stayed {}. The fix
    turns on AGENT_OBSERVABILITY_ENABLED so spans land in the -DEFAULT log group
    the cost rollup reads — without injecting an OTLP endpoint (no localhost
    sidecar, Bug 18).
    """
    env = build_otel_env_vars(
        {"enabled": True, "provider": "custom"},
        runtime_name="agent",
    )
    assert env == {
        "AGENT_OBSERVABILITY_ENABLED": "true",
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
    }
    # No 3rd-party/localhost endpoint is injected on the native path.
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env


def test_langfuse_default_without_credentials_enables_native_observability():
    """P-PLAT-010 / Bug 194: enabled (incl. by default) but no caller endpoint
    and no credentials must NOT inject a 3rd-party OTLP endpoint, and must NOT
    return {}.

    Injecting the langfuse cloud URL with no auth would route gen_ai.usage spans
    off to an unauthenticated endpoint (401, dropped) instead of the -DEFAULT
    log group cost reads. But Bug 194 proved leaving OTEL_* entirely unset emits
    NO spans at all — the managed deploy path creates no TracerProvider. So the
    native path enables AgentCore-native ADOT observability (which feeds
    -DEFAULT) without an endpoint. AGENT_OBSERVABILITY_ENABLED is what turns
    native export on; an unset env does NOT preserve any native export.
    """
    native = {
        "AGENT_OBSERVABILITY_ENABLED": "true",
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
    }
    env = build_otel_env_vars(
        {"enabled": True},  # provider defaults to 'langfuse', no endpoint/creds
        runtime_name="agent",
    )
    assert env == native

    # Explicit langfuse provider, still no credentials -> same native env.
    env2 = build_otel_env_vars(
        {"enabled": True, "provider": "langfuse"},
        runtime_name="agent",
    )
    assert env2 == native


def test_langfuse_default_with_secret_uses_default_endpoint():
    """When the caller supplies credentials for langfuse, the provider default
    endpoint IS usable, so we fall back to it (no explicit endpoint needed)."""
    env = build_otel_env_vars(
        {
            "enabled": True,
            "provider": "langfuse",
            "auth_header_secret_arn": (
                "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
                "agentcore-otel/langfuse/u-creds-abcdef123456"
            ),
        },
        runtime_name="agent",
    )
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
        "https://cloud.langfuse.com/api/public/otel"
    )


def test_langfuse_default_with_extra_headers_uses_default_endpoint():
    """Auth via extra headers also makes the provider default endpoint usable."""
    env = build_otel_env_vars(
        {
            "enabled": True,
            "provider": "langfuse",
            "extra_headers": {"Authorization": "Basic abc"},
        },
        runtime_name="agent",
    )
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
        "https://cloud.langfuse.com/api/public/otel"
    )


def test_inject_otel_adds_bootstrap_after_app_init():
    src = '''"""Test"""
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """hi"""

@app.entrypoint
def invoke(payload):
    return {"response": "ok"}

if __name__ == "__main__":
    app.run()
'''
    out = _inject_otel(src)
    # Bootstrap inserted right after app = BedrockAgentCoreApp()
    assert "_otel_bootstrap()" in out
    assert "OTEL_AUTH_SECRET_ARN" in out
    # invoke wrapped so spans flush before idle stop
    assert "_otel_invoke_wrap" in out
    assert "_otel_force_flush()" in out


def test_inject_otel_resilient_when_no_app_marker():
    """Should still produce valid Python even if the marker isn't present."""
    out = _inject_otel("print('no app here')\n")
    # Doesn't crash and doesn't leave the file empty.
    assert "print" in out


# ---------------------------------------------------------------------------
# Platform defaults (Reading A: admin-set OTEL backend for all agents)
# ---------------------------------------------------------------------------


_PLATFORM_DEFAULTS = {
    "enabled": True,
    "provider": "custom",
    "otlp_endpoint": "https://platform.langfuse.example/api/public/otel",
    "auth_header_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/platform/dev-platform0001",
    "sample_rate": 0.5,
    "service_name_prefix": "myplatform",
    "resource_attributes": {"env": "prod-platform", "owner": "ops"},
}


def test_platform_defaults_inject_when_no_canvas_config():
    """Agents without an Observability node still get traced when platform defaults exist."""
    env = build_otel_env_vars(
        None,
        runtime_name="agent-x",
        platform_defaults=_PLATFORM_DEFAULTS,
    )
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://platform.langfuse.example/api/public/otel"
    assert env["OTEL_AUTH_SECRET_ARN"] == "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/platform/dev-platform0001"
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.5"
    assert env["OTEL_SERVICE_NAME"] == "myplatform-agent-x"
    # Platform-supplied resource attributes flow through.
    assert "env=prod-platform" in env["OTEL_RESOURCE_ATTRIBUTES"]
    assert "owner=ops" in env["OTEL_RESOURCE_ATTRIBUTES"]


def test_platform_defaults_lock_endpoint_against_canvas_override():
    """Per-canvas endpoint/secret/sample are IGNORED when platform defaults exist."""
    env = build_otel_env_vars(
        {
            "enabled": True,
            "provider": "langfuse",
            "otlp_endpoint": "https://canvas-tried-to-override.example/v1/traces",
            "auth_header_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/langfuse/u-canvas-zzzz",
            "sample_rate": 1.0,
        },
        runtime_name="agent-x",
        platform_defaults=_PLATFORM_DEFAULTS,
    )
    # Platform values win.
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://platform.langfuse.example/api/public/otel"
    assert env["OTEL_AUTH_SECRET_ARN"] == "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/platform/dev-platform0001"
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.5"


def test_platform_defaults_resource_attributes_merge_additively():
    """Per-canvas resource_attributes ADD to platform attributes; canvas wins on collision."""
    env = build_otel_env_vars(
        {
            "enabled": True,
            "resource_attributes": {
                "team": "ai-platform",          # adds new key
                "env": "prod-canvas-override",  # collides with platform "env"
            },
        },
        runtime_name="agent-x",
        platform_defaults=_PLATFORM_DEFAULTS,
    )
    attrs = env["OTEL_RESOURCE_ATTRIBUTES"]
    # Platform-only keys preserved.
    assert "owner=ops" in attrs
    # Canvas-only keys added.
    assert "team=ai-platform" in attrs
    # Collision: canvas wins (matches the documented "canvas keys win" merge rule).
    assert "env=prod-canvas-override" in attrs
    assert "env=prod-platform" not in attrs


def test_no_platform_defaults_preserves_per_canvas_behavior():
    """When platform_defaults is None, behavior is identical to pre-platform-OTEL."""
    env = build_otel_env_vars(
        {
            "enabled": True,
            "provider": "langfuse",
            "otlp_endpoint": "https://cloud.langfuse.com/api/public/otel",
            "service_name": "my-agent",
            "sample_rate": 0.5,
        },
        runtime_name="my-agent",
        platform_defaults=None,
    )
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://cloud.langfuse.com/api/public/otel"
    assert env["OTEL_SERVICE_NAME"] == "my-agent"
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.5"


def test_platform_defaults_enable_telemetry_even_with_disabled_canvas():
    """Platform-enforced means telemetry is on regardless of per-canvas enabled flag."""
    env = build_otel_env_vars(
        {"enabled": False},  # canvas tried to opt out
        runtime_name="agent-x",
        platform_defaults=_PLATFORM_DEFAULTS,
    )
    # Platform overrides the disable.
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://platform.langfuse.example/api/public/otel"


# ---------------------------------------------------------------------------
# Critic Finding 1 (BLOCKER) — cross-tenant Secrets Manager exfiltration
# ---------------------------------------------------------------------------


def test_validate_user_otel_secret_arn_accepts_user_namespace():
    """ARNs in agentcore-otel/<provider>/<owner>-<rand> are valid."""
    arn = (
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
        "agentcore-otel/langfuse/u-xyz-abc123"
    )
    assert _validate_user_otel_secret_arn(arn) == arn


def test_validate_user_otel_secret_arn_rejects_other_namespace():
    """An ARN outside agentcore-otel/ must be rejected — this is the
    cross-tenant exfiltration vector the Critic Finding 1 fix closes."""
    with pytest.raises(ValueError, match="agentcore-otel/"):
        _validate_user_otel_secret_arn(
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
            "billing-prod-AbCdEf"
        )


def test_validate_user_otel_secret_arn_accepts_platform_namespace():
    """Platform-managed secrets live under agentcore-otel/platform/ and
    share the same prefix — only the operator can name into this namespace,
    so we deliberately do not block it from the validator. The split between
    user and platform secrets is a naming convention, not a regex split."""
    arn = (
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
        "agentcore-otel/platform/dev-AbCdEf"
    )
    assert _validate_user_otel_secret_arn(arn) == arn


def test_build_otel_env_vars_rejects_non_namespace_arn():
    """Integration: a per-canvas observability config carrying a foreign
    ARN must fail loudly through the public API (not silently propagate to
    the IAM step where it would be granted to the runtime role)."""
    with pytest.raises(ValueError, match="agentcore-otel/"):
        build_otel_env_vars(
            {
                "enabled": True,
                "provider": "langfuse",
                "otlp_endpoint": "https://cloud.langfuse.com/api/public/otel",
                "auth_header_secret_arn": (
                    "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
                    "billing-prod-AbCdEf"
                ),
            },
            runtime_name="agent-x",
        )
