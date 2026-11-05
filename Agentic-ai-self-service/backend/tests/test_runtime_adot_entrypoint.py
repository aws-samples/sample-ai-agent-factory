"""Production evaluation keeps Python direct code on agent.py and boots ADOT in-process."""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import CfnTemplateGenerator
from app.services.runtime_deployer import _create_code_zip, create_agent_runtime

ROOT = Path(__file__).resolve().parents[2]
BUNDLES = ROOT / "backend" / "agentcore-deps"
INSTALLER = ROOT / "scripts" / "install-agentcore-deps.sh"


class _Control:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "agentRuntimeId": "runtime_observable_123",
            "agentRuntimeArn": ("arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/runtime_observable_123"),
            "status": "CREATING",
        }


def _create(*, instrument: bool) -> dict:
    control = _Control()
    create_agent_runtime(
        agentcore_ctrl=control,
        runtime_name="observable_runtime",
        role_arn="arn:aws:iam::123456789012:role/AgentCoreRuntime-observable",
        s3_bucket="artifacts",
        s3_key="code.zip",
        entrypoint="agent.py",
        instrument_observability=instrument,
        region="eu-west-1",
    )
    assert len(control.calls) == 1
    return control.calls[0]


def test_instrumented_python_runtime_keeps_the_python_direct_code_entrypoint() -> None:
    request = _create(instrument=True)
    assert request["agentRuntimeArtifact"]["codeConfiguration"]["entryPoint"] == [
        "agent.py",
    ]


def test_uninstrumented_runtime_keeps_the_plain_entrypoint() -> None:
    request = _create(instrument=False)
    assert request["agentRuntimeArtifact"]["codeConfiguration"]["entryPoint"] == ["agent.py"]


@pytest.mark.parametrize("mode", ["native", "http/protobuf", "grpc"])
@pytest.mark.parametrize("remove_bootstrap", [False, True], ids=["complete-startup", "bootstrap-removed"])
def test_exported_observable_python_runtime_keeps_the_python_entrypoint(mode, remove_bootstrap) -> None:
    config = RuntimeConfig(
        name="observable-export",
        model={"modelId": "us.anthropic.claude-sonnet-5"},
        observability={
            "enableOtel": mode != "native",
            "provider": "custom",
            "otlpEndpoint": "https://otel.example/v1/traces",
            "otlpProtocol": "grpc" if mode == "grpc" else "http/protobuf",
            "serviceName": "whole-module-startup",
            "sampleRate": 1,
        },
    )
    bundle = CfnTemplateGenerator().generate(DeployRequest(nodeId="node-1", config=config))
    template = yaml.safe_load(bundle.template_yaml)

    runtime = template["Resources"]["AgentCoreRuntime"]["Properties"]
    assert runtime["AgentRuntimeArtifact"]["CodeConfiguration"]["EntryPoint"] == ["agent.py"]

    tree = ast.parse(bundle.agent_code)
    bootstrap_calls = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_otel_bootstrap"
    ]
    assert len(bootstrap_calls) == 1, "telemetry must execute once at module startup"
    bootstrap = bootstrap_calls[0]
    registrations = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.FunctionDef)
            and any(
                isinstance(decorator, ast.Attribute)
                and isinstance(decorator.value, ast.Name)
                and decorator.value.id == "app"
                and decorator.attr == "entrypoint"
                for decorator in node.decorator_list
            )
        )
        or (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "app"
            and node.value.func.attr == "entrypoint"
        )
    ]
    assert registrations, "the generated module must register its invocation handler"
    assert all(bootstrap.end_lineno < registration.lineno for registration in registrations)

    source = bundle.agent_code
    if remove_bootstrap:
        # Remove the actual executed AST statement, not its function definition
        # or a constructor-like string in a prompt/comment.
        lines = source.splitlines(keepends=True)
        lines[bootstrap.lineno - 1 : bootstrap.end_lineno] = ["# module bootstrap removed by regression mutation\n"]
        source = "".join(lines)

    setup = """
import inspect
import logging
import os
import sys
import types
import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
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
    raise AssertionError("whole-module startup must not call AWS")
boto3.client = forbidden_client
def forbidden_server(*args, **kwargs):
    raise AssertionError("importing the generated module must not start a server")
BedrockAgentCoreApp.run = forbidden_server
instrumentor = types.ModuleType("opentelemetry.instrumentation.botocore")
class BotocoreInstrumentor:
    def instrument(self):
        pass
instrumentor.BotocoreInstrumentor = BotocoreInstrumentor
sys.modules[instrumentor.__name__] = instrumentor
"""
    assertions = f"""
provider = namespace["_otel_provider"]
assert provider is not None, "missing-bootstrap: whole-module startup did not initialize telemetry"
assert provider.resource.attributes["service.name"] == "whole-module-startup"
app = namespace["app"]
registered = app.handlers["main"]
assert registered is namespace["invoke"] is namespace["_otel_invoke_wrap"]
assert list(inspect.signature(registered).parameters) == ["payload", "context"]
assert namespace["_otel_inner_invoke"] is not registered
assert namespace["_otel_inner_invoke"].__name__ == "invoke"
assert {mode!r} == "native" or constructed == [({mode!r}, "https://otel.example/v1/traces")], constructed
if {mode!r} == "native":
    assert constructed == [], constructed
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in os.environ

# Substitute only the model-work callback after the entire generated module
# has started. The SDK/Strands telemetry, app registration, invoke wrapper,
# span producer, batching and force-flush all remain real.
real_flush = namespace["_otel_force_flush"]
events = []
def observed_flush():
    events.append("flush")
    return real_flush()
namespace["_otel_force_flush"] = observed_flush
def model_work(payload):
    events.append("model")
    with provider.get_tracer("whole-module-entrypoint").start_as_current_span("model-proof") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", "whole-module-test-model")
        span.set_attribute("gen_ai.usage.input_tokens", 7)
        span.set_attribute("gen_ai.usage.output_tokens", 3)
    return {{"response": payload["prompt"]}}
namespace["_otel_inner_invoke"] = model_work
assert registered({{"prompt": "whole-module proof"}}) == {{"response": "whole-module proof"}}
assert events == ["model", "flush"], events
if {mode!r} == "native":
    assert exported == [], exported
else:
    assert len(constructed) == 1, constructed
    # The real wrapper intentionally emits one correlation span around the
    # model span. Each logical span must be exported once, not duplicated.
    assert sorted(span.name for span in exported) == ["invoke_agent", "model-proof"]
    assert len({{span.context.span_id for span in exported}}) == 2
    assert all("gen_ai.task.input" not in span.attributes for span in exported)
    assert all("gen_ai.task.output" not in span.attributes for span in exported)

# The registered wrapper must flush on an invocation error as well.
events.clear()
def failing_model_work(payload):
    events.append("model-error")
    raise RuntimeError("deliberate-model-error")
namespace["_otel_inner_invoke"] = failing_model_work
try:
    registered({{"prompt": "error-path proof"}})
except RuntimeError as error:
    assert str(error) == "deliberate-model-error"
else:
    raise AssertionError("the registered handler swallowed its invocation error")
assert events == ["model-error", "flush"], events
if {mode!r} != "native":
    assert sorted(span.name for span in exported) == ["invoke_agent", "invoke_agent", "model-proof"]
    assert len({{span.context.span_id for span in exported}}) == 3
"""
    script = (
        setup
        + "\nnamespace = {'__name__': 'generated_runtime_import'}\n"
        + f"exec(compile({source!r}, 'exported-agent.py', 'exec'), namespace)\n"
        + assertions
    )
    process_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OTEL_") and key != "AGENT_OBSERVABILITY_ENABLED"
    }
    process_env.update({key: value for key, value in runtime["EnvironmentVariables"].items() if isinstance(value, str)})
    process_env["AWS_EC2_METADATA_DISABLED"] = "true"
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
        assert completed.returncode != 0, "an inert/comment-only bootstrap escaped startup verification"
        assert "missing-bootstrap: whole-module startup did not initialize telemetry" in output, output
        return
    assert completed.returncode == 0, output
    records = [
        json.loads(line.split("AGENTCORE_USAGE ", 1)[1]) for line in output.splitlines() if "AGENTCORE_USAGE " in line
    ]
    assert records == [
        {
            "gen_ai.request.model": "whole-module-test-model",
            "gen_ai.usage.input_tokens": 7,
            "gen_ai.usage.output_tokens": 3,
        }
    ]


def test_installer_preserves_only_a_path_neutral_adot_console_script() -> None:
    source = INSTALLER.read_text()
    assert 'bin/opentelemetry-instrument"' in source
    assert "#!/usr/bin/env python3" in source
    assert '>"${target_dir:?}/opentelemetry-instrument"' in source
    assert 'chmod 0755 "${target_dir:?}/opentelemetry-instrument"' in source
    assert 'rm -rf "${target_dir:?}/bin"' in source


def test_built_model_runtime_bundles_carry_the_executable_when_present() -> None:
    bundle_paths = [BUNDLES / "base.zip", BUNDLES / "strands-mcp.zip"]
    if not all(path.exists() for path in bundle_paths):
        pytest.skip("generated AgentCore bundles are absent")
    for bundle in bundle_paths:
        with zipfile.ZipFile(bundle) as archive:
            info = archive.getinfo("opentelemetry-instrument")
            assert info.external_attr >> 16 & 0o111
            script = archive.read(info).decode("utf-8")
            assert script.startswith("#!/usr/bin/env python3\n")
            assert not any(name.startswith("bin/") for name in archive.namelist())


def test_final_code_zip_preserves_the_adot_executable_mode() -> None:
    bundle = BUNDLES / "strands-mcp.zip"
    if not bundle.exists():
        pytest.skip("generated AgentCore bundle is absent")

    code_zip = _create_code_zip(
        "print('agent')\n",
        "",
        "agent.py",
        deps_bundle=bundle.read_bytes(),
    )

    with zipfile.ZipFile(io.BytesIO(code_zip)) as archive:
        info = archive.getinfo("opentelemetry-instrument")
        assert info.external_attr >> 16 & 0o111
        assert archive.read(info).startswith(b"#!/usr/bin/env python3\n")

    # Mutation twin: recreating the same bytes by name only drops the execute bit.
    with zipfile.ZipFile(bundle) as source:
        script = source.read("opentelemetry-instrument")
    mutated = io.BytesIO()
    with zipfile.ZipFile(mutated, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("opentelemetry-instrument", script)
    with zipfile.ZipFile(io.BytesIO(mutated.getvalue())) as archive:
        assert archive.getinfo("opentelemetry-instrument").external_attr >> 16 & 0o111 == 0
