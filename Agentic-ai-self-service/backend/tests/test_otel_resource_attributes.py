"""Telemetry metadata must survive the real SDK parser and emitted bootstrap."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest
import yaml
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_observability import CfnObservabilityError, build_export_observability_env
from app.services.cfn_template_generator import CfnTemplateGenerator
from app.services.observability import build_otel_env_vars
from opentelemetry.sdk.resources import Resource

RESOURCE_ATTRIBUTES = {
    "teamName": "research,security",
    "team_name": "release=ready=value",
    "CustomerRelease": "100% complete; %2C stays literal",
    "deployment.environment.name": "staging",
    "Unicode": "Café – 東京",
    "NulValue": "left\x00right",
    "Padding": "  padded metadata  ",
    "Empty": "",
    "CaseSensitive": "UPPER",
    "casesensitive": "lower",
    "Équipe.所有者": "équipe",
    "customer%25key": "literal-key-percent",
    "LooksLikeEnvironment": "data,OTEL_EXPORTER_OTLP_ENDPOINT=https://injected.invalid",
}
RUNTIME_NAME = "metadata-export"
SERVICE_NAME = "metadata-exactness"
EXTERNAL_ENDPOINT = "https://telemetry.example.test"
UNREPRESENTABLE_KEYS = (
    "",
    " leading",
    "trailing ",
    "\tkey",
    "comma,key",
    "equals=key",
    "customer\x00key",
    "customer\ud800key",
)


def _settings(mode: str, attributes: dict[str, str] | None = None) -> dict:
    settings = {
        "enableOtel": mode != "native",
        "provider": "custom",
        "serviceName": SERVICE_NAME,
        "sampleRate": 1,
        "resourceAttributes": dict(RESOURCE_ATTRIBUTES if attributes is None else attributes),
    }
    if mode != "native":
        settings.update(otlpEndpoint=EXTERNAL_ENDPOINT, otlpProtocol=mode)
    return settings


def _request(settings: dict) -> DeployRequest:
    return DeployRequest(
        nodeId=RUNTIME_NAME,
        config=RuntimeConfig(
            name=RUNTIME_NAME,
            model={"modelId": "us.anthropic.claude-sonnet-5"},
        ),
        observabilityConfig=settings,
    )


def _assert_sdk_metadata(env: dict[str, str], caplog, *, expect_service_name: bool = True) -> None:
    with patch.dict(os.environ, env, clear=True):
        resource = Resource.create({})
    expected = {**RESOURCE_ATTRIBUTES, "deployment.id": RUNTIME_NAME}
    actual = {key: resource.attributes.get(key) for key in expected}
    assert actual == expected
    if expect_service_name:
        assert resource.attributes["service.name"] == SERVICE_NAME
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in resource.attributes
    assert not any("Invalid key value resource attribute pair" in record.message for record in caplog.records)
    # Every emitted pair must name one real attribute. An unescaped comma can
    # otherwise invent another attribute or quietly discard part of its value.
    pairs = env["OTEL_RESOURCE_ATTRIBUTES"].split(",")
    assert len(pairs) == len(expected)
    assert {pair.split("=", 1)[0] for pair in pairs} == set(expected)


@pytest.mark.parametrize("mode", ["native", "http/protobuf", "grpc"])
def test_shared_env_survives_the_actual_sdk_resource_parser(mode, caplog):
    settings = _settings(mode)
    # The shared platform builder's native mode is enabled without a remote
    # endpoint; the export helper separately interprets external opt-out.
    settings["enableOtel"] = True
    before = json.loads(json.dumps(settings))
    env = build_otel_env_vars(settings, runtime_name=RUNTIME_NAME, deployment_id=RUNTIME_NAME)

    _assert_sdk_metadata(env, caplog, expect_service_name=mode != "native")
    if mode == "native":
        assert env["AGENT_OBSERVABILITY_ENABLED"] == "true"
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    else:
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == EXTERNAL_ENDPOINT
        assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == mode
    assert settings == before


@pytest.mark.parametrize("mode", ["native", "http/protobuf", "grpc"])
def test_export_env_survives_the_actual_sdk_resource_parser(mode, caplog):
    request = _request(_settings(mode))
    before = request.model_dump()
    env = build_export_observability_env(request, runtime_name=RUNTIME_NAME)

    _assert_sdk_metadata(env, caplog)
    if mode == "native":
        assert env["AGENT_OBSERVABILITY_ENABLED"] == "true"
        assert not any(key.startswith("OTEL_EXPORTER_") or key == "OTEL_AUTH_SECRET_ARN" for key in env)
    else:
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == EXTERNAL_ENDPOINT
        assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == mode
    assert request.model_dump() == before


@pytest.mark.parametrize("mode", ["native", "http/protobuf", "grpc"])
@pytest.mark.parametrize("key", UNREPRESENTABLE_KEYS)
def test_shared_serializer_refuses_unrepresentable_keys_without_echoing_input(mode, key):
    settings = _settings(mode, {key: "PRIVATE-METADATA-VALUE"})
    settings["enableOtel"] = True
    with pytest.raises(ValueError, match="(?i)resource") as exc:
        build_otel_env_vars(settings, runtime_name=RUNTIME_NAME)

    assert "PRIVATE-METADATA-VALUE" not in str(exc.value)
    if key:
        assert key not in str(exc.value)


@pytest.mark.parametrize("mode", ["native", "http/protobuf", "grpc"])
def test_shared_serializer_refuses_invalid_unicode_values_without_echoing_input(mode):
    value = "private\ud800marker"
    settings = _settings(mode, {"CustomerAttribute": value})
    settings["enableOtel"] = True
    with pytest.raises(ValueError, match="(?i)resource") as exc:
        build_otel_env_vars(settings, runtime_name=RUNTIME_NAME)

    assert value not in str(exc.value)
    assert "private" not in str(exc.value)


@pytest.mark.parametrize("mode", ["native", "http/protobuf", "grpc"])
def test_export_refuses_invalid_unicode_values_without_echoing_input(mode):
    value = "private\ud800marker"
    request = _request(_settings(mode, {"CustomerAttribute": value}))
    with pytest.raises(CfnObservabilityError, match="(?i)resource") as exc:
        build_export_observability_env(request, runtime_name=RUNTIME_NAME)

    assert value not in str(exc.value)
    assert "private" not in str(exc.value)


@pytest.mark.parametrize("mode", ["native", "http/protobuf", "grpc"])
@pytest.mark.parametrize("key", UNREPRESENTABLE_KEYS)
def test_export_refuses_unrepresentable_keys_without_echoing_input(mode, key):
    request = _request(_settings(mode, {key: "PRIVATE-METADATA-VALUE"}))
    with pytest.raises(CfnObservabilityError, match="(?i)resource") as exc:
        build_export_observability_env(request, runtime_name=RUNTIME_NAME)

    assert "PRIVATE-METADATA-VALUE" not in str(exc.value)
    if key:
        assert key not in str(exc.value)


def _generated_bootstrap(source: str) -> str:
    """Run the bootstrap actually present in the downloadable source."""
    tree = ast.parse(source)
    call = next(
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_otel_bootstrap"
    )
    beginning = source.index("# OTEL observability bootstrap")
    return "\n".join(source.splitlines()[source[:beginning].count("\n") : call.end_lineno]) + "\n"


@pytest.mark.parametrize("mode", ["native", "http/protobuf", "grpc"])
def test_emitted_bootstrap_preserves_exact_metadata_without_duplicate_or_native_export(mode):
    request = _request(_settings(mode))
    before = request.model_dump()
    bundle = CfnTemplateGenerator().generate(request)
    template = yaml.safe_load(bundle.template_yaml)
    runtime_env = template["Resources"]["AgentCoreRuntime"]["Properties"]["EnvironmentVariables"]
    # Keep the installed SDK provider/resource/sampler/batching and Strands
    # telemetry real. Only exporter transports and AWS clients are replaced.
    setup = """
import logging
import os
import sys
import types
import boto3
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

logging.basicConfig(level=logging.WARNING, format="%(message)s")
constructed = []
exported = []
def make_exporter(protocol):
    class RecordingExporter(SpanExporter):
        def __init__(self):
            constructed.append((protocol, os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")))
        def export(self, spans):
            exported.extend(spans)
            return SpanExportResult.SUCCESS
        def shutdown(self):
            pass
    return RecordingExporter
for protocol, module_name in (
    ("http/protobuf", "opentelemetry.exporter.otlp.proto.http.trace_exporter"),
    ("grpc", "opentelemetry.exporter.otlp.proto.grpc.trace_exporter"),
):
    module = types.ModuleType(module_name)
    module.OTLPSpanExporter = make_exporter(protocol)
    sys.modules[module_name] = module
def forbidden_client(*args, **kwargs):
    raise AssertionError("metadata preservation must not access AWS")
boto3.client = forbidden_client
instrumentor = types.ModuleType("opentelemetry.instrumentation.botocore")
class BotocoreInstrumentor:
    def instrument(self):
        pass
instrumentor.BotocoreInstrumentor = BotocoreInstrumentor
sys.modules[instrumentor.__name__] = instrumentor
"""
    expected = {**RESOURCE_ATTRIBUTES, "deployment.id": RUNTIME_NAME}
    assertions = f"""
assert _otel_provider is not None, "the emitted bootstrap did not initialize telemetry"
actual = dict(_otel_provider.resource.attributes)
expected = {expected!r}
assert {{key: actual.get(key) for key in expected}} == expected, actual
assert actual["service.name"] == {SERVICE_NAME!r}
assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in actual, actual
assert "OTEL_AUTH_SECRET_ARN" not in os.environ
tracer = _otel_provider.get_tracer("exact-resource-attributes-regression")
with tracer.start_as_current_span("metadata-proof") as span:
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.request.model", "metadata-test-model")
    span.set_attribute("gen_ai.usage.input_tokens", 7)
    span.set_attribute("gen_ai.usage.output_tokens", 3)
_otel_force_flush()
if {mode!r} == "native":
    assert constructed == [], constructed
    assert exported == [], exported
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in os.environ
else:
    assert constructed == [({mode!r}, {EXTERNAL_ENDPOINT!r})], constructed
    assert len(exported) == 1, [span.name for span in exported]
    assert exported[0].name == "metadata-proof"
    exported_resource = dict(exported[0].resource.attributes)
    assert {{key: exported_resource.get(key) for key in expected}} == expected, exported_resource
"""
    process_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OTEL_") and key != "AGENT_OBSERVABILITY_ENABLED"
    }
    process_env.update({key: value for key, value in runtime_env.items() if isinstance(value, str)})
    process_env["AWS_EC2_METADATA_DISABLED"] = "true"
    completed = subprocess.run(
        [sys.executable, "-c", setup + _generated_bootstrap(bundle.agent_code) + assertions],
        env=process_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "Invalid key value resource attribute pair" not in output
    records = [
        json.loads(line.split("AGENTCORE_USAGE ", 1)[1]) for line in output.splitlines() if "AGENTCORE_USAGE " in line
    ]
    assert records == [
        {
            "gen_ai.request.model": "metadata-test-model",
            "gen_ai.usage.input_tokens": 7,
            "gen_ai.usage.output_tokens": 3,
        }
    ]
    assert request.model_dump() == before
