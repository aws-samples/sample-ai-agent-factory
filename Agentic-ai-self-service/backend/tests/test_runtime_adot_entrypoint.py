"""Production evaluation keeps Python direct code on agent.py and boots ADOT in-process."""

from __future__ import annotations

import io
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


def test_exported_observable_python_runtime_keeps_the_python_entrypoint() -> None:
    config = RuntimeConfig(
        name="observable-export",
        model={"modelId": "us.anthropic.claude-sonnet-5"},
        observability={
            "enableOtel": True,
            "provider": "custom",
            "otlpEndpoint": "https://otel.example/v1/traces",
        },
    )
    bundle = CfnTemplateGenerator().generate(DeployRequest(nodeId="node-1", config=config))
    template = yaml.safe_load(bundle.template_yaml)

    assert template["Resources"]["AgentCoreRuntime"]["Properties"]["AgentRuntimeArtifact"]["CodeConfiguration"][
        "EntryPoint"
    ] == ["agent.py"]
    assert "_otel_auto_initialize(swallow_exceptions=False)" in bundle.agent_code


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
