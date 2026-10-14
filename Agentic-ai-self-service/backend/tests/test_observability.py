"""Tests for OTLP env-var construction and codegen injection.

Covers the three paths that must stay in sync (Bug 9 in tasks/lessons.md):
  1) build_otel_env_vars() output
  2) _inject_otel() codegen post-processor
  3) Backward compat with the legacy enable_otel boolean
"""

import json
import os
import subprocess
import sys

import pytest
from app.models.deployment_models import RuntimeConfig
from app.services.code_generator import OTEL_BOOTSTRAP, _inject_otel, generate_agent_code
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
    env = build_otel_env_vars(None, runtime_name="agent-x", enable_otel_legacy=True)
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
    assert (
        env["OTEL_AUTH_SECRET_ARN"]
        == "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/langfuse/u-camel-aabbccdd"
    )


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


def test_no_endpoint_env_and_codegen_form_a_complete_usage_path():
    """Pin the two Bug 194 halves together.

    The env-only fix previously passed while generated code returned before
    initializing telemetry. A no-endpoint deployment must now carry both the
    native posture variables and the usage-log exporter.
    """
    env = build_otel_env_vars(
        {"enabled": True, "provider": "custom"},
        runtime_name="agent",
    )
    config = RuntimeConfig(
        name="agent",
        framework="strands_agents",
        model={"modelId": "us.anthropic.claude-sonnet-5"},
        system_prompt="Help the user.",
        deployment_type="direct_code_deploy",
        python_runtime="PYTHON_3_12",
    )
    generated = generate_agent_code(config, observability_enabled=True)

    assert env["AGENT_OBSERVABILITY_ENABLED"] == "true"
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    assert "class _UsageLogExporter" in generated
    assert "AGENTCORE_USAGE" in generated
    assert "if not endpoint:\n        return" not in generated


def test_langfuse_default_with_secret_uses_default_endpoint():
    """When the caller supplies credentials for langfuse, the provider default
    endpoint IS usable, so we fall back to it (no explicit endpoint needed)."""
    env = build_otel_env_vars(
        {
            "enabled": True,
            "provider": "langfuse",
            "auth_header_secret_arn": (
                "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/langfuse/u-creds-abcdef123456"
            ),
        },
        runtime_name="agent",
    )
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == ("https://cloud.langfuse.com/api/public/otel")


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
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == ("https://cloud.langfuse.com/api/public/otel")


def test_production_evaluation_forces_native_adot_even_when_canvas_disables_otel():
    env = build_otel_env_vars(
        {"enableOtel": False},
        runtime_name="release_bot_deadbeef",
        deployment_id="dep-release",
        production_evaluation_required=True,
        target_region="eu-west-1",
    )

    assert env["AGENT_OBSERVABILITY_ENABLED"] == "true"
    assert env["UNIFIED_TRACES_DESTINATION_ENABLED"] == "false"
    assert env["OTEL_PYTHON_DISTRO"] == "aws_distro"
    assert env["OTEL_PYTHON_CONFIGURATOR"] == "aws_configurator"
    assert env["OTEL_AWS_PYTHON_DEFER_TO_WORKERS_ENABLED"] == "false"
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert env["OTEL_LOGS_EXPORTER"] == "none"
    assert env["OTEL_METRICS_EXPORTER"] == "none"
    assert env["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] == "http/protobuf"
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == ("https://xray.eu-west-1.amazonaws.com/v1/traces")
    assert env["OTEL_SERVICE_NAME"] == "release_bot_deadbeef.DEFAULT"
    assert env["OTEL_TRACES_SAMPLER"] == "always_on"
    assert env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "false"
    assert env["AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT"] == "true"
    assert env["OTEL_SEMCONV_STABILITY_OPT_IN"].endswith("gen_ai_unredacted_attributes=")
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env


def test_required_traces_endpoint_sigv4_signs_a_real_otlp_http_request():
    setup = """
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

seen = {}
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        seen["path"] = self.path
        seen["authorization"] = self.headers.get("Authorization", "")
        seen["token"] = self.headers.get("X-Amz-Security-Token", "")
        seen["content_type"] = self.headers.get("Content-Type", "")
        seen["body"] = self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
os.environ["AWS_REGION"] = "eu-west-1"
os.environ["AWS_ACCESS_KEY_ID"] = "AKIDEXAMPLE"  # pragma: allowlist secret
os.environ["AWS_SECRET_ACCESS_KEY"] = "secret-example"  # pragma: allowlist secret
os.environ["AWS_SESSION_TOKEN"] = "session-example"  # pragma: allowlist secret
os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = (
    f"http://127.0.0.1:{server.server_port}/v1/traces"
)
os.environ["OTEL_SERVICE_NAME"] = "signed-probe.DEFAULT"
os.environ["OTEL_TRACES_SAMPLER"] = "always_on"
"""
    assertion = """
from opentelemetry import trace
import time
with trace.get_tracer("sigv4-test").start_as_current_span("signed-probe") as probe_span:
    trace_hex = format(probe_span.get_span_context().trace_id, "032x")
_otel_force_flush()
server.shutdown()
server.server_close()
thread.join(timeout=2)
assert seen["path"] == "/v1/traces", seen
assert seen["content_type"] == "application/x-protobuf", seen
assert seen["body"], seen
assert len(trace_hex) == 32
assert abs(int(trace_hex[:8], 16) - int(time.time())) < 60, trace_hex
assert seen["token"] == "session-example", seen
assert seen["authorization"].startswith("AWS4-HMAC-SHA256 "), seen
assert "Credential=AKIDEXAMPLE/" in seen["authorization"], seen
assert "/eu-west-1/xray/aws4_request" in seen["authorization"], seen
"""
    env = os.environ.copy()
    for key in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_AUTH_SECRET_ARN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
    ):
        env.pop(key, None)

    positive = subprocess.run(
        [sys.executable, "-c", setup + OTEL_BOOTSTRAP + assertion],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert positive.returncode == 0, positive.stdout + positive.stderr

    signer = "signed_session.auth = _XRaySigV4Auth(signing_region)"
    assert OTEL_BOOTSTRAP.count(signer) == 1
    mutant = OTEL_BOOTSTRAP.replace(signer, "signed_session.auth = None", 1)
    removed = subprocess.run(
        [sys.executable, "-c", setup + mutant + assertion],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert removed.returncode != 0, "Removing X-Ray SigV4 authentication escaped detection"


def test_production_evaluation_trace_endpoint_overrides_external_trace_routing():
    env = build_otel_env_vars(
        {
            "enabled": True,
            "provider": "custom",
            "otlp_endpoint": "https://otel.example/v1",
        },
        runtime_name="release_bot_deadbeef",
        production_evaluation_required=True,
        target_region="eu-west-1",
    )

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otel.example/v1"
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == ("https://xray.eu-west-1.amazonaws.com/v1/traces")
    assert env["OTEL_SERVICE_NAME"] == "release_bot_deadbeef.DEFAULT"


def test_production_evaluation_rejects_an_invalid_target_region():
    with pytest.raises(ValueError, match="valid AWS Region"):
        build_otel_env_vars(
            None,
            runtime_name="release_bot_deadbeef",
            production_evaluation_required=True,
            target_region="not-a-region",
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
    assert "AGENTCORE_USAGE" in out
    # invoke wrapped so spans flush before idle stop
    assert "_otel_invoke_wrap" in out
    assert "_otel_force_flush()" in out
    assert "invoke = app.entrypoint(_otel_invoke_wrap)" in out


def test_registered_agentcore_entrypoint_uses_evaluation_wrapper_and_removal_mutant_fails():
    source = """class BedrockAgentCoreApp:
    def __init__(self):
        self.handlers = {}

    def entrypoint(self, function):
        self.handlers["main"] = function
        return function

app = BedrockAgentCoreApp()

@app.entrypoint
def invoke(payload):
    return dict(payload)
"""
    generated = _inject_otel(source)
    registration = "invoke = app.entrypoint(_otel_invoke_wrap)"
    assert generated.count(registration) == 1
    assertion = """
payload = {
    "prompt": "What is two plus two?",
    "session_id": "staging-session",
    "_agentcore_evaluation_capture": True,
}
result = app.handlers["main"](payload)
assert app.handlers["main"] is invoke
assert "_agentcore_evaluation_capture" not in result
assert result["prompt"] == "What is two plus two?"
"""
    env = os.environ.copy()
    for key in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_AUTH_SECRET_ARN",
    ):
        env.pop(key, None)
    env["AGENT_OBSERVABILITY_ENABLED"] = "true"

    positive = subprocess.run(
        [sys.executable, "-c", generated + assertion],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert positive.returncode == 0, positive.stdout + positive.stderr

    mutant = generated.replace(registration, "invoke = _otel_invoke_wrap", 1)
    removed = subprocess.run(
        [sys.executable, "-c", mutant + assertion],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert removed.returncode != 0, "Removing public entrypoint registration escaped detection"


def test_no_endpoint_bootstrap_emits_allowlisted_usage_without_sensitive_span_data():
    """Execute the generated bootstrap in a fresh interpreter.

    This is the exact Bug 194 shape: observability is enabled but there is no
    external OTLP endpoint. A real chat span must still produce one usage
    record in runtime logs, while the parent/aggregate span and sensitive
    attributes remain absent.
    """
    script = (
        "import logging\n"
        "logging.basicConfig(level=logging.WARNING, format='%(message)s')\n"
        + OTEL_BOOTSTRAP
        + """
from opentelemetry import trace
from strands.telemetry.tracer import get_tracer

tracer = trace.get_tracer("agentcore-usage-regression")
with tracer.start_as_current_span("aggregate") as span:
    span.set_attribute("gen_ai.operation.name", "invoke_agent")
    span.set_attribute("gen_ai.request.model", "must-not-be-counted")
    span.set_attribute("gen_ai.usage.input_tokens", 999)
    span.set_attribute("gen_ai.usage.output_tokens", 999)

strands_tracer = get_tracer()
model_span = strands_tracer.start_model_invoke_span(
    messages=[
        {
            "role": "user",
            "content": [{"text": "PROMPT-MUST-NOT-LEAK"}],
        }
    ],
    model_id="anthropic.claude-test",
    authorization="Bearer TOKEN-MUST-NOT-LEAK",
)
import inspect as _inspect

_end_kwargs = {
    "message": {
        "role": "assistant",
        "content": [{"text": "RESPONSE-MUST-NOT-LEAK"}],
    },
    "usage": {
        "inputTokens": 123,
        "outputTokens": 45,
        "totalTokens": 168,
        "cacheReadInputTokens": 7,
    },
    "stop_reason": "end_turn",
}
# strands changed this signature between the version installed for tests and the
# one the runtime bundle resolves to: 1.56.0 requires `metrics`, 1.9.1 does not
# accept it. Passing it unconditionally raises TypeError on the older one, so the
# kwarg is driven by the installed signature. The privacy property under test is
# the same either way; the only thing version-dependent is how to reach it.
_end_params = _inspect.signature(strands_tracer.end_model_invoke_span).parameters
if "metrics" in _end_params:
    _end_kwargs["metrics"] = {}
strands_tracer.end_model_invoke_span(model_span, **_end_kwargs)

_otel_force_flush()
"""
    )
    env = os.environ.copy()
    for key in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_AUTH_SECRET_ARN",
    ):
        env.pop(key, None)
    env["AGENT_OBSERVABILITY_ENABLED"] = "true"
    # Cost accounting is not trace sampling. Even a 0% external trace sample
    # rate must retain every model call in the local usage ledger.
    env["OTEL_TRACES_SAMPLER"] = "always_off"

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    combined = completed.stdout + completed.stderr

    assert completed.returncode == 0, combined
    usage_lines = [line for line in combined.splitlines() if "AGENTCORE_USAGE " in line]
    assert len(usage_lines) == 1, combined
    record = json.loads(usage_lines[0].split("AGENTCORE_USAGE ", 1)[1])
    # Asserted as a CONTRACT, not as one dependency version's record shape. This
    # test previously demanded `cache_write_input_tokens: 0` exactly, which passes
    # only where strands happens to set that span attribute -- it does on 1.9.1 and
    # does not on 1.56.0, and the runtime bundle pins neither. The three required
    # fields are the ones `cost_tracking.summarize_from_logs` actually parses; the
    # two cache fields are optional by design (see _UsageLogExporter) because an
    # absent attribute means "not reported", not "zero".
    assert record["gen_ai.request.model"] == "anthropic.claude-test"
    assert record["gen_ai.usage.input_tokens"] == 123
    assert record["gen_ai.usage.output_tokens"] == 45
    # cache_read is supplied by this test's own usage dict, so it must survive the
    # exporter on every version; a missing one means the exporter dropped it.
    assert record["gen_ai.usage.cache_read_input_tokens"] == 7
    # cache_write is NOT supplied, so its presence is the instrumentation's choice.
    # Present-and-zero or absent are both correct; any other value is not.
    assert record.get("gen_ai.usage.cache_write_input_tokens", 0) == 0
    # The upper bound is the load-bearing half of this assertion: the record must
    # carry NOTHING beyond these five keys. That is what keeps a future span
    # attribute carrying a prompt, a response or an auth header out of the usage
    # log, and it is why this cannot go back to being a loose subset check.
    assert set(record) <= {
        "gen_ai.request.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.cache_read_input_tokens",
        "gen_ai.usage.cache_write_input_tokens",
    }, record
    assert "must-not-be-counted" not in combined
    assert "PROMPT-MUST-NOT-LEAK" not in combined
    assert "RESPONSE-MUST-NOT-LEAK" not in combined
    assert "TOKEN-MUST-NOT-LEAK" not in combined


def test_evaluation_span_captures_only_marked_staging_content():
    script = (
        "from opentelemetry import trace\n"
        "from opentelemetry.sdk.trace import TracerProvider\n"
        "from opentelemetry.sdk.trace.export import SimpleSpanProcessor\n"
        "from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter\n"
        "exporter = InMemorySpanExporter()\n"
        "provider = TracerProvider()\n"
        "provider.add_span_processor(SimpleSpanProcessor(exporter))\n"
        "trace.set_tracer_provider(provider)\n"
        + OTEL_BOOTSTRAP
        + """
seen = []
def invoke(payload):
    seen.append(dict(payload))
    return {"response": "The answer is 4."}

_otel_invoke_with_evaluation_span(
    invoke,
    {"prompt": "PRODUCTION-MUST-STAY-REDACTED", "session_id": "prod-session"},
)
_otel_invoke_with_evaluation_span(
    invoke,
    {
        "prompt": "What is two plus two?",
        "session_id": "staging-session",
        "_agentcore_evaluation_capture": True,
    },
)
_otel_force_flush()
spans = [span for span in exporter.get_finished_spans() if span.name == "invoke_agent"]
assert len(spans) == 1, spans
attrs = dict(spans[0].attributes)
assert attrs["gen_ai.operation.name"] == "invoke_agent"
assert attrs["session.id"] == "staging-session"
assert attrs["gen_ai.task.input"] == "What is two plus two?"
assert attrs["gen_ai.task.output"] == "The answer is 4."
assert "agentcore.invocation.user_prompt" not in attrs
assert "agentcore.invocation.agent_response" not in attrs
assert "PRODUCTION-MUST-STAY-REDACTED" not in repr(attrs)
assert all("_agentcore_evaluation_capture" not in payload for payload in seen)
assert len(_otel_evaluation_text("x" * 40000)) == 12000
"""
    )
    env = os.environ.copy()
    env.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_inject_otel_resilient_when_no_app_marker():
    """Should still produce valid Python even if the marker isn't present."""
    out = _inject_otel("print('no app here')\n")
    # Doesn't crash and doesn't leave the file empty.
    assert "print" in out


def test_bootstrap_reuses_a_preconfigured_sdk_tracer_provider():
    script = (
        "from opentelemetry import trace\n"
        "from opentelemetry.sdk.trace import TracerProvider\n"
        "provider = TracerProvider()\n"
        "trace.set_tracer_provider(provider)\n" + OTEL_BOOTSTRAP + "\nassert _otel_provider is provider\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Overriding of current TracerProvider is not allowed" not in result.stderr


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
    assert (
        env["OTEL_AUTH_SECRET_ARN"]
        == "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/platform/dev-platform0001"
    )
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
    assert (
        env["OTEL_AUTH_SECRET_ARN"]
        == "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/platform/dev-platform0001"
    )
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.5"


def test_platform_defaults_resource_attributes_merge_additively():
    """Per-canvas resource_attributes ADD to platform attributes; canvas wins on collision."""
    env = build_otel_env_vars(
        {
            "enabled": True,
            "resource_attributes": {
                "team": "ai-platform",  # adds new key
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
    arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/langfuse/u-xyz-abc123"
    assert _validate_user_otel_secret_arn(arn) == arn


def test_validate_user_otel_secret_arn_rejects_other_namespace():
    """An ARN outside agentcore-otel/ must be rejected — this is the
    cross-tenant exfiltration vector the Critic Finding 1 fix closes."""
    with pytest.raises(ValueError, match="agentcore-otel/"):
        _validate_user_otel_secret_arn("arn:aws:secretsmanager:us-east-1:123456789012:secret:billing-prod-AbCdEf")


def test_validate_user_otel_secret_arn_accepts_platform_namespace():
    """Platform-managed secrets live under agentcore-otel/platform/ and
    share the same prefix — only the operator can name into this namespace,
    so we deliberately do not block it from the validator. The split between
    user and platform secrets is a naming convention, not a regex split."""
    arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/platform/dev-AbCdEf"
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
                "auth_header_secret_arn": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:billing-prod-AbCdEf"),
            },
            runtime_name="agent-x",
        )
