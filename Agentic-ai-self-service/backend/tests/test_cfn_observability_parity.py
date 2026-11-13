"""Observability controls must agree across exported env, IAM, and runtime code."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_observability import CfnObservabilityError, build_export_observability_env
from app.services.cfn_template_generator import CfnExportUnsupportedError, CfnTemplateGenerator
from app.services.runtime_artifact import STRANDS_BUNDLE_KEY, classify_runtime_artifact

MODEL_ID = "us.anthropic.claude-sonnet-5"
# Synthetic recipient-account reference for offline IAM tests; this is not a credential.
SECRET_ARN = (
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-otel/export-abcdef"  # pragma: allowlist secret
)


def _request(*, nested=None, **kwargs) -> DeployRequest:
    return DeployRequest(
        nodeId="observability-export",
        config=RuntimeConfig(
            name="observability_export",
            model={"modelId": MODEL_ID},
            observability=nested,
        ),
        **kwargs,
    )


def _bundle(request):
    bundle = CfnTemplateGenerator().generate(request)
    return bundle, yaml.safe_load(bundle.template_yaml)


def _env(template):
    return template["Resources"]["AgentCoreRuntime"]["Properties"]["EnvironmentVariables"]


def _statements(template):
    policies = template["Resources"]["RuntimeExecutionRole"]["Properties"]["Policies"]
    return [statement for policy in policies for statement in policy["PolicyDocument"]["Statement"]]


def _exporter():
    return {
        "name": "captured-node",
        "enableOtel": True,
        "provider": "custom",
        "otlpEndpoint": "https://telemetry.example.test",
        "otlpProtocol": "http/protobuf",
        "serviceName": "developer-owned-agent",
        "sampleRate": 0,
        "resourceAttributes": {"environment": "staging", "team": "research"},
        "authHeaderSecretArn": SECRET_ARN,
    }


def test_top_level_observability_is_preserved_without_a_connected_marker():
    request = _request(observabilityConfig=_exporter())
    before = request.model_dump()
    bundle, template = _bundle(request)
    env = _env(template)

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://telemetry.example.test"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert env["OTEL_SERVICE_NAME"] == "developer-owned-agent"
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.0"
    assert env["OTEL_AUTH_SECRET_ARN"] == SECRET_ARN
    assert "environment=staging" in env["OTEL_RESOURCE_ATTRIBUTES"]
    assert "team=research" in env["OTEL_RESOURCE_ATTRIBUTES"]
    assert "deployment.id=observability-export" in env["OTEL_RESOURCE_ATTRIBUTES"]
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env
    assert "OTEL_EXPORTER_OTLP_EXTRA_HEADERS" not in env
    statements = {statement["Sid"]: statement for statement in _statements(template)}
    assert statements["OtelAuthHeaderSecret"]["Resource"] == [SECRET_ARN]
    assert statements["OtelAuthHeaderSecret"]["Action"] == ["secretsmanager:GetSecretValue"]
    decrypt = statements["OtelAuthHeaderDecrypt"]
    assert decrypt["Action"] == ["kms:Decrypt"]
    assert decrypt["Condition"]["StringEquals"]["kms:EncryptionContext:SecretARN"] == SECRET_ARN
    assert decrypt["Condition"]["StringEquals"]["kms:ViaService"] == {
        "Fn::Sub": "secretsmanager.${AWS::Region}.${AWS::URLSuffix}"
    }
    assert "_otel_bootstrap()" in bundle.agent_code
    assert "get_secret_value(SecretId=secret_arn)" in bundle.agent_code
    assert "_otel_force_flush()" in bundle.agent_code
    assert classify_runtime_artifact(bundle.agent_code).bundle_key == STRANDS_BUNDLE_KEY
    assert "aws-opentelemetry-distro" in bundle.build_bundle_sh
    assert "opentelemetry-exporter-otlp-proto-http" in bundle.build_bundle_sh
    assert request.model_dump() == before, "export must not mutate the saved request"


def test_nonempty_top_level_configuration_wins_over_nested_configuration():
    nested = {**_exporter(), "otlpEndpoint": "https://nested.example.test", "serviceName": "nested-agent"}
    bundle, template = _bundle(_request(nested=nested, observabilityConfig=_exporter()))

    assert _env(template)["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://telemetry.example.test"
    assert _env(template)["OTEL_SERVICE_NAME"] == "developer-owned-agent"
    assert "nested.example.test" not in bundle.template_yaml


def test_empty_top_level_block_matches_runtime_consumer_fallback_to_nested():
    request = _request(nested=_exporter(), observabilityConfig={})
    assert build_export_observability_env(request, runtime_name="agent")["OTEL_AUTH_SECRET_ARN"] == SECRET_ARN


@pytest.mark.parametrize("shape", ["top-level", "nested"])
def test_external_opt_out_preserves_native_usage_without_exporter_or_secret_permissions(shape):
    disabled = {**_exporter(), "enableOtel": False}
    request = (
        _request(nested=_exporter(), observabilityConfig=disabled, connectedTools=["observability"])
        if shape == "top-level"
        else _request(nested=disabled, connectedTools=["observability"])
    )
    request.config.enable_otel = True
    bundle, template = _bundle(request)

    env = _env(template)
    assert env["AGENT_OBSERVABILITY_ENABLED"] == "true"
    assert env["OTEL_SERVICE_NAME"] == "developer-owned-agent"
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.0"
    assert "team=research" in env["OTEL_RESOURCE_ATTRIBUTES"]
    assert not any(key.startswith("OTEL_EXPORTER_") or key == "OTEL_AUTH_SECRET_ARN" for key in env)
    assert not any(statement["Sid"].startswith("OtelAuth") for statement in _statements(template))
    assert "_otel_bootstrap()" in bundle.agent_code
    assert SECRET_ARN not in bundle.template_yaml


def test_false_on_either_enabled_spelling_cannot_be_overridden_by_alias_precedence():
    request = _request(observabilityConfig={"enabled": False, "enableOtel": True})
    assert build_export_observability_env(request, runtime_name="agent") == {
        "AGENT_OBSERVABILITY_ENABLED": "true",
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
    }


@pytest.mark.parametrize("disable_key", ["enabled", "enableOtel", "enable_otel"])
def test_explicit_external_opt_out_on_any_spelling_wins_over_enabling_aliases(disable_key):
    settings = {**_exporter(), "enabled": True, "enableOtel": True, "enable_otel": True, disable_key: False}
    env = build_export_observability_env(_request(observabilityConfig=settings), runtime_name="agent")
    assert env["AGENT_OBSERVABILITY_ENABLED"] == "true"
    assert not any(key.startswith("OTEL_EXPORTER_") or key == "OTEL_AUTH_SECRET_ARN" for key in env)


def test_raw_saved_node_external_opt_out_preserves_native_metadata_without_mutating_request():
    request = _request(
        observabilityConfig={
            "component_type": "observability",
            "name": "Release Telemetry",
            "enable_otel": False,
            "otlp_endpoint": "https://unused.example.test",
            "auth_header_secret_arn": SECRET_ARN,
            "service_name": "raw-saved-node",
            "sample_rate": 0.25,
            "resource_attributes": {"team_name": "research"},
        }
    )
    before = request.model_dump()
    env = build_export_observability_env(request, runtime_name="agent")
    assert env["AGENT_OBSERVABILITY_ENABLED"] == "true"
    assert env["OTEL_SERVICE_NAME"] == "raw-saved-node"
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.25"
    assert "team_name=research" in env["OTEL_RESOURCE_ATTRIBUTES"]
    assert not any(key.startswith("OTEL_EXPORTER_") or key == "OTEL_AUTH_SECRET_ARN" for key in env)
    assert request.model_dump() == before


@pytest.mark.parametrize("top_level", [None, {}])
def test_truly_absent_observability_does_not_initialize_native_instrumentation(top_level):
    bundle, template = _bundle(_request(observabilityConfig=top_level))
    assert not any(key.startswith("OTEL_") or key == "AGENT_OBSERVABILITY_ENABLED" for key in _env(template))
    assert "_otel_bootstrap()" not in bundle.agent_code
    assert not any(statement["Sid"].startswith("OtelAuth") for statement in _statements(template))


@pytest.mark.parametrize("marker", ["none", "node", "legacy"])
def test_unconfigured_enabling_markers_match_the_platform_native_bootstrap(marker):
    request = _request(connectedTools=["observability"] if marker == "node" else [])
    request.config.enable_otel = marker == "legacy"
    env = build_export_observability_env(request, runtime_name="agent")

    if marker == "none":
        assert env == {}
    else:
        assert env == {
            "AGENT_OBSERVABILITY_ENABLED": "true",
            "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        }


@pytest.mark.parametrize("protocol,exporter_name", [("http/protobuf", "otlp_proto_http"), ("grpc", "otlp_proto_grpc")])
def test_external_protocol_reaches_the_actual_sdk_exporter_selection(monkeypatch, protocol, exporter_name):
    from opentelemetry.sdk._configuration import _get_exporter_names

    request = _request(observabilityConfig={**_exporter(), "otlpProtocol": protocol})
    bundle, template = _bundle(request)
    env = _env(template)
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == protocol
    # The direct HTTP exporter in the bootstrap belongs ONLY to the X-Ray
    # trace-endpoint branch, not the configured custom collector branch.
    assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" not in env
    assert "elif endpoint and not adot_configured:" in bundle.agent_code
    assert "_OtelBatchSpanProcessor(_OtelExternalSpanExporter())" in bundle.agent_code
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", env["OTEL_EXPORTER_OTLP_PROTOCOL"])
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", raising=False)
    assert _get_exporter_names("traces") == [exporter_name]
    # The generated recipe includes the pinned AWS distro. Its exporter
    # closure contains both protocols; the same constraints govern that build.
    assert "aws-opentelemetry-distro" in bundle.build_bundle_sh
    constraints = (Path(__file__).resolve().parents[1] / "agentcore-deps-constraints.txt").read_text()
    assert "aws-opentelemetry-distro==0.22.0" in constraints
    assert "opentelemetry-exporter-otlp-proto-grpc==1.45.1" in constraints
    assert "opentelemetry-exporter-otlp-proto-http==1.45.1" in constraints


@pytest.mark.parametrize("unknown", ["samplingRate", "dualExportNative", "sessionReplay"])
def test_unknown_top_level_controls_are_refused_not_silently_ignored(unknown):
    with pytest.raises(CfnExportUnsupportedError, match=unknown):
        _bundle(_request(observabilityConfig={**_exporter(), unknown: True}))


@pytest.mark.parametrize("sample_rate", [-0.1, 1.1, "PRIVATE-INVALID-VALUE"])
def test_invalid_sampler_errors_do_not_echo_the_configured_value(sample_rate):
    with pytest.raises(CfnExportUnsupportedError, match="sample_rate") as exc:
        _bundle(_request(observabilityConfig={**_exporter(), "sampleRate": sample_rate}))
    assert "PRIVATE-INVALID-VALUE" not in str(exc.value)


@pytest.mark.parametrize("shape", ["top-level", "nested"])
def test_plaintext_authentication_headers_are_never_exported(shape):
    settings = {**_exporter(), "extraHeaders": {"Authorization": "Bearer PRIVATE-HEADER-VALUE"}}
    request = _request(observabilityConfig=settings) if shape == "top-level" else _request(nested=settings)
    # Test the helper itself as well as the full boundary; neither spelling
    # may depend on the old validator's top-level-only raw-header scan.
    with pytest.raises(CfnObservabilityError, match="authHeaderSecretArn") as exc:
        build_export_observability_env(request, runtime_name="agent")
    assert "PRIVATE-HEADER-VALUE" not in str(exc.value)
    with pytest.raises(ValueError) as exc:
        _bundle(request)
    assert "PRIVATE-HEADER-VALUE" not in str(exc.value)


def test_credential_bearing_resource_attributes_are_refused():
    request = _request(observabilityConfig={**_exporter(), "resourceAttributes": {"apiKey": "PRIVATE-ATTRIBUTE"}})
    with pytest.raises(ValueError, match="resourceAttributes") as exc:
        _bundle(request)
    assert "PRIVATE-ATTRIBUTE" not in str(exc.value)


def test_native_resource_attributes_preserve_an_explicit_deployment_identifier():
    request = _request(
        observabilityConfig={"enableOtel": True, "resourceAttributes": {"deployment.id": "customer-release"}}
    )
    env = build_export_observability_env(request, runtime_name="agent")
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == "deployment.id=customer-release"


def _generated_bootstrap(source: str) -> str:
    """Extract the actual emitted bootstrap, not the generator's constant."""
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


@pytest.mark.parametrize("protocol", ["http/protobuf", "grpc"])
def test_emitted_bootstrap_constructs_the_selected_exporter_and_resolves_auth_at_runtime(protocol):
    request = _request(observabilityConfig={**_exporter(), "otlpProtocol": protocol, "sampleRate": 1})
    bundle, template = _bundle(request)
    runtime_env = _env(template)
    # Replace only remote exporter transports and the secret client. The
    # emitted bootstrap, real SDK provider, sampler, batching, resource, and
    # Strands telemetry run unchanged without AWS or collector access.
    setup = """
import os
import sys
import types
import boto3
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

constructed = []
exported = []
secret_reads = []
def make_exporter(protocol):
    class RecordingExporter(SpanExporter):
        def __init__(self):
            constructed.append({
                "protocol": protocol,
                "endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
                "headers": os.environ.get("OTEL_EXPORTER_OTLP_HEADERS"),
            })
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

class SecretClient:
    def get_secret_value(self, *, SecretId):
        secret_reads.append(SecretId)
        # Synthetic marker returned only by this mock; no live credential is read.
        return {"SecretString": "Authorization=Bearer PRIVATE-RUNTIME-AUTH"}  # pragma: allowlist secret
def client(service):
    assert service == "secretsmanager"
    return SecretClient()
boto3.client = client
instrumentor = types.ModuleType("opentelemetry.instrumentation.botocore")
class BotocoreInstrumentor:
    def instrument(self):
        pass
instrumentor.BotocoreInstrumentor = BotocoreInstrumentor
sys.modules[instrumentor.__name__] = instrumentor
"""
    assertions = f"""
assert constructed == [{{
    "protocol": {protocol!r},
    "endpoint": "https://telemetry.example.test",
    "headers": "Authorization=Bearer PRIVATE-RUNTIME-AUTH",
}}], constructed
assert secret_reads == [{SECRET_ARN!r}]
assert _otel_provider.resource.attributes["service.name"] == "developer-owned-agent"
assert _otel_provider.resource.attributes["team"] == "research"
tracer = _otel_provider.get_tracer("actual-export-constructor")
with tracer.start_as_current_span("proof") as span:
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.request.model", "collector-test-model")
    span.set_attribute("gen_ai.usage.input_tokens", 7)
    span.set_attribute("gen_ai.usage.output_tokens", 3)
_otel_force_flush()
assert any(span.name == "proof" for span in exported)
assert len(constructed) == 1, "duplicate exporters would duplicate external spans"
assert all("PRIVATE-RUNTIME-AUTH" not in repr(dict(span.attributes)) for span in exported)
"""
    process_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OTEL_") and key != "AGENT_OBSERVABILITY_ENABLED"
    }
    process_env.update({key: value for key, value in runtime_env.items() if isinstance(value, str)})
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
    assert "PRIVATE-RUNTIME-AUTH" not in output
    assert "PRIVATE-RUNTIME-AUTH" not in bundle.template_yaml
    assert "PRIVATE-RUNTIME-AUTH" not in bundle.agent_code


@pytest.mark.parametrize("remove_bootstrap", [False, True])
@pytest.mark.parametrize("external_export_enabled", [False, True])
def test_emitted_native_bootstrap_consumes_resources_and_zero_sampling_without_losing_usage(
    remove_bootstrap, external_export_enabled
):
    settings = {
        "enableOtel": external_export_enabled,
        "provider": "custom",
        "serviceName": "native-export-service",
        "sampleRate": 0,
        "resourceAttributes": {"team": "native-research"},
    }
    if not external_export_enabled:
        # Saved external collector settings must not be consumed when their
        # toggle is off, but local usage metering must still run unchanged.
        settings.update(otlpEndpoint="https://unused.example.test", authHeaderSecretArn=SECRET_ARN)
    request = _request(observabilityConfig=settings)
    bundle, template = _bundle(request)
    runtime_env = _env(template)
    assert runtime_env["AGENT_OBSERVABILITY_ENABLED"] == "true"
    assert runtime_env["OTEL_TRACES_SAMPLER_ARG"] == "0.0"
    assert not any(key.startswith("OTEL_EXPORTER_") or key == "OTEL_AUTH_SECRET_ARN" for key in runtime_env)
    bootstrap = _generated_bootstrap(bundle.agent_code)
    if remove_bootstrap:
        bootstrap = bootstrap.replace("\n_otel_bootstrap()", "\n# bootstrap removed", 1)
    script = (
        """
import logging
import sys
import types
import boto3
logging.basicConfig(level=logging.WARNING, format="%(message)s")
remote_exporter_calls = []
secret_client_calls = []
class ForbiddenRemoteExporter:
    def __init__(self, *args, **kwargs):
        remote_exporter_calls.append("unexpected exporter")
        raise AssertionError("native usage must not initialize a remote exporter")
for name in (
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
):
    module = types.ModuleType(name)
    module.OTLPSpanExporter = ForbiddenRemoteExporter
    sys.modules[name] = module
def forbidden_client(service, *args, **kwargs):
    secret_client_calls.append(service)
    raise AssertionError("native usage must not read external exporter credentials")
boto3.client = forbidden_client
"""
        + bootstrap
        + """
from opentelemetry.sdk.trace.sampling import Decision
assert _otel_provider is not None, "the exported source did not initialize tracing"
assert _otel_provider.resource.attributes["service.name"] == "native-export-service"
assert _otel_provider.resource.attributes["team"] == "native-research"
assert _otel_provider.resource.attributes["deployment.id"] == "observability-export"
decision = _otel_provider.sampler.should_sample(None, 123, "sampling-proof")
assert decision.decision is Decision.RECORD_ONLY, decision
tracer = _otel_provider.get_tracer("exported-runtime-regression")
with tracer.start_as_current_span("model") as span:
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.request.model", "exported-test-model")
    span.set_attribute("gen_ai.usage.input_tokens", 31)
    span.set_attribute("gen_ai.usage.output_tokens", 13)
    span.set_attribute("gen_ai.prompt", "PRIVATE-PROMPT")
    span.set_attribute("gen_ai.response", "PRIVATE-RESPONSE")
_otel_force_flush()
assert remote_exporter_calls == [], remote_exporter_calls
assert secret_client_calls == [], secret_client_calls
"""
    )
    process_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OTEL_") and key != "AGENT_OBSERVABILITY_ENABLED"
    }
    process_env.update({key: value for key, value in runtime_env.items() if isinstance(value, str)})
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=process_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    output = completed.stdout + completed.stderr
    if remove_bootstrap:
        assert completed.returncode != 0, "removing runtime initialization escaped the regression"
        return
    assert completed.returncode == 0, output
    records = [
        json.loads(line.split("AGENTCORE_USAGE ", 1)[1]) for line in output.splitlines() if "AGENTCORE_USAGE " in line
    ]
    assert records == [
        {
            "gen_ai.request.model": "exported-test-model",
            "gen_ai.usage.input_tokens": 31,
            "gen_ai.usage.output_tokens": 13,
        }
    ]
    assert "PRIVATE-PROMPT" not in output
    assert "PRIVATE-RESPONSE" not in output
