"""Unit tests for the Python project exporter (Phase 3 Gap 3G).

Pure tests — no AWS, no moto. The exporter is a pure builder; S3 / presigning
lives in the deployment_handler endpoint and is out of scope here.

Run:
    cd backend && python3 -m pytest tests/test_python_exporter.py -x -q
"""

import io
import zipfile

from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.code_generator import generate_agent_code
from app.services.python_exporter import (
    build_and_zip,
    build_env_example,
    build_python_project,
    build_requirements,
    zip_project,
)


# ---------------------------------------------------------------------------
# Helpers — mirror tests/test_comprehensive_preservation.py::_make_runtime_config
# ---------------------------------------------------------------------------


def _make_runtime_config(**overrides) -> RuntimeConfig:
    defaults = {
        "name": "test-agent",
        "framework": "strands_agents",
        "model": {"modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"},
        "systemPrompt": "You are a helpful assistant.",
    }
    defaults.update(overrides)
    return RuntimeConfig(**defaults)


def _make_deploy_request(config: RuntimeConfig, **overrides) -> DeployRequest:
    defaults = {
        "nodeId": "node-1",
        "config": config,
    }
    defaults.update(overrides)
    return DeployRequest(**defaults)


_EXPECTED_FILES = {
    "agent.py",
    "requirements.txt",
    "Dockerfile",
    "README.md",
    ".env.example",
    "run.sh",
}


# ---------------------------------------------------------------------------
# build_python_project — file set
# ---------------------------------------------------------------------------


def test_build_python_project_returns_expected_files():
    config = _make_runtime_config()
    req = _make_deploy_request(config)

    files = build_python_project(req)

    # The gap spec requires at least these five; we also ship run.sh.
    for name in ("agent.py", "requirements.txt", "Dockerfile", "README.md", ".env.example"):
        assert name in files, f"missing {name} in exported project"
    assert set(files.keys()) == _EXPECTED_FILES


def test_all_file_contents_are_non_empty_strings():
    config = _make_runtime_config()
    files = build_python_project(_make_deploy_request(config))
    for name, content in files.items():
        assert isinstance(content, str), f"{name} should be a str"
        assert content.strip(), f"{name} should not be empty"


# ---------------------------------------------------------------------------
# zip_project — archive shape (mirrors CfnBundle.to_zip)
# ---------------------------------------------------------------------------


def test_zip_project_contains_prefixed_files():
    config = _make_runtime_config(name="My Cool Agent")
    req = _make_deploy_request(config)

    zip_bytes, deployment_name = build_and_zip(req)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())

    prefix = f"{deployment_name}-python"
    for expected in ("agent.py", "requirements.txt", "Dockerfile", "README.md"):
        assert f"{prefix}/{expected}" in names, f"{expected} not in zip under {prefix}/"


def test_zip_project_standalone_matches_build():
    config = _make_runtime_config()
    files = build_python_project(_make_deploy_request(config))
    zip_bytes = zip_project(files, "myname")

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
    assert "myname-python/agent.py" in names
    assert all(n.startswith("myname-python/") for n in names)


# ---------------------------------------------------------------------------
# agent.py is the REAL generated source (byte-identical to generate_agent_code)
# ---------------------------------------------------------------------------


def test_agent_code_is_verbatim_generated_source():
    config = _make_runtime_config()
    req = _make_deploy_request(config)

    expected = generate_agent_code(
        config=config,
        tools=[],
        gateway_config=None,
        template_id=None,
        gateway_tools=[],
        custom_tools=[],
        portable=True,
        observability_enabled=False,
    )

    files = build_python_project(req)
    assert files["agent.py"] == expected


# ---------------------------------------------------------------------------
# requirements.txt — real deps derived from PROVIDER_PACKAGES
# ---------------------------------------------------------------------------


def test_bedrock_requirements_include_strands_and_agentcore():
    config = _make_runtime_config()  # default provider == bedrock
    reqs = build_requirements(config)
    lines = set(reqs.splitlines())

    assert "strands-agents" in lines
    assert "strands-agents-tools" in lines
    # bedrock-agentcore is NOT in PROVIDER_PACKAGES but the generated code
    # imports it — the exporter must add it explicitly (gap risk #1).
    assert "bedrock-agentcore" in lines
    assert "boto3" in lines


def test_openai_provider_adds_openai_package():
    config = _make_runtime_config(modelProvider="openai")
    reqs = build_requirements(config)
    lines = set(reqs.splitlines())

    assert "openai" in lines
    assert "strands-agents" in lines
    assert "bedrock-agentcore" in lines


def test_unknown_provider_falls_back_to_bedrock_packages():
    # model_provider is a Literal, so simulate an out-of-map value by mutating
    # the attribute directly (bypassing validation) to prove .get() fallback.
    config = _make_runtime_config()
    object.__setattr__(config, "model_provider", "totally-unknown-provider")
    reqs = build_requirements(config)
    lines = set(reqs.splitlines())

    assert "strands-agents" in lines
    assert "bedrock-agentcore" in lines  # no KeyError, fell back gracefully


def test_observability_adds_otel_distro():
    config = _make_runtime_config(enableOtel=True)
    reqs = build_requirements(config, connected_tools=[])
    assert "aws-opentelemetry-distro" in reqs.splitlines()


def test_no_observability_omits_otel_distro():
    config = _make_runtime_config()
    reqs = build_requirements(config, connected_tools=[])
    assert "aws-opentelemetry-distro" not in reqs.splitlines()


def test_requirements_sorted_and_deduped():
    config = _make_runtime_config()
    reqs = build_requirements(config)
    lines = [l for l in reqs.splitlines() if l]
    assert lines == sorted(lines)
    assert len(lines) == len(set(lines))


# ---------------------------------------------------------------------------
# .env.example — placeholders only, never real secrets
# ---------------------------------------------------------------------------


def test_env_example_has_placeholders_no_secrets():
    config = _make_runtime_config()
    env = build_env_example(config)

    assert "MODEL_ID=" in env
    assert "AWS_REGION=" in env
    # The model id placeholder may carry the (non-secret) model id, but there
    # must be no secret-looking value: no provider key, no OTLP auth header.
    assert "PROVIDER_API_KEY=" not in env  # bedrock has no provider key line
    # No real secret values: every var line ends with "=" or a non-secret value.
    for line in env.splitlines():
        if line.startswith("PROVIDER_API_KEY") or "OTLP_HEADERS" in line or "OTEL_EXPORTER_OTLP_HEADERS" in line:
            assert line.strip().endswith("="), f"secret-bearing line not blank: {line!r}"


def test_env_example_non_bedrock_adds_blank_provider_key():
    config = _make_runtime_config(modelProvider="anthropic")
    env = build_env_example(config)
    # Provider key placeholder present and BLANK.
    assert "PROVIDER_API_KEY=" in env
    for line in env.splitlines():
        if line.startswith("PROVIDER_API_KEY"):
            assert line.strip() == "PROVIDER_API_KEY="


def test_env_example_observability_adds_blank_otlp_vars():
    config = _make_runtime_config(enableOtel=True)
    env = build_env_example(config, connected_tools=[])
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=" in env
    # Auth header must be a blank placeholder, never a real secret value.
    for line in env.splitlines():
        if line.startswith("OTEL_EXPORTER_OTLP_HEADERS"):
            assert line.strip() == "OTEL_EXPORTER_OTLP_HEADERS="


def test_env_example_model_id_is_the_configured_id():
    config = _make_runtime_config(
        model={"modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"}
    )
    env = build_env_example(config)
    assert "MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0" in env
