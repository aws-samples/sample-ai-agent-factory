"""Unit tests for the Python project exporter (Phase 3 Gap 3G).

Pure tests — no AWS, no moto. The exporter is a pure builder; S3 / presigning
lives in the deployment_handler endpoint and is out of scope here.

Run:
    cd backend && python3 -m pytest tests/test_python_exporter.py -x -q
"""

import io
import json
import os
import subprocess
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
        "model": {"modelId": "us.anthropic.claude-sonnet-5"},
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
    "run-docker.sh",
}


# ---------------------------------------------------------------------------
# build_python_project — file set
# ---------------------------------------------------------------------------


def test_build_python_project_returns_expected_files():
    config = _make_runtime_config()
    req = _make_deploy_request(config)

    files = build_python_project(req)

    # The gap spec requires at least these five; we also ship launchers.
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
    for expected in ("agent.py", "requirements.txt", "Dockerfile", "README.md", "run-docker.sh"):
        assert f"{prefix}/{expected}" in names, f"{expected} not in zip under {prefix}/"


def test_zip_project_standalone_matches_build():
    config = _make_runtime_config()
    files = build_python_project(_make_deploy_request(config))
    zip_bytes = zip_project(files, "myname")

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
    assert "myname-python/agent.py" in names
    assert all(n.startswith("myname-python/") for n in names)


def test_zip_marks_launchers_executable_but_not_source_files():
    files = build_python_project(_make_deploy_request(_make_runtime_config()))
    zip_bytes = zip_project(files, "permissions")

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert zf.getinfo("permissions-python/run.sh").external_attr >> 16 & 0o111
        assert zf.getinfo("permissions-python/run-docker.sh").external_attr >> 16 & 0o111
        assert not zf.getinfo("permissions-python/agent.py").external_attr >> 16 & 0o111


def test_docker_launcher_preserves_values_and_does_not_inherit_omitted_host_secrets(tmp_path):
    marker = tmp_path / "docker-launcher-must-not-execute"
    dangerous = f"value $(touch${{IFS}}{marker})"
    files = build_python_project(
        _make_deploy_request(
            _make_runtime_config(),
            gatewayConfig={
                "gatewayProvider": "agentcore",
                "gatewayUrl": dangerous,
            },
            identityConfig={
                "provider": "okta",
                "clientId": dangerous,
                "clientSecretRef": "oauth/client-ref",
                "scopes": ["gateway/read"],
            },
        )
    )

    (tmp_path / ".env").write_text(files[".env.example"])
    launcher = tmp_path / "run-docker.sh"
    launcher.write_text(files["run-docker.sh"])
    launcher.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        'names = ("GATEWAY_URL", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET_REF", "GUARDRAIL_ID")\n'
        'record = {"args": sys.argv[1:], "env": {name: os.environ.get(name) for name in names}}\n'
        'with open(os.environ["DOCKER_ARGS_LOG"], "a", encoding="utf-8") as stream:\n'
        '    stream.write(json.dumps(record) + "\\n")\n'
    )
    docker.chmod(0o755)
    log_path = tmp_path / "docker-calls.jsonl"
    process_env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "DOCKER_ARGS_LOG": str(log_path),
        # This optional variable is absent from the generated .env. The launcher
        # must clear it rather than accidentally forwarding a host secret.
        "GUARDRAIL_ID": "host-secret-must-not-reach-container",
    }

    subprocess.run(
        ["bash", str(launcher), "exported-agent"],
        cwd=tmp_path,
        env=process_env,
        check=True,
        capture_output=True,
        text=True,
    )

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert records[0]["args"] == ["build", "-t", "exported-agent", "."]
    assert records[1]["args"][:5] == ["run", "--rm", "-p", "8080:8080", "--env"]
    assert records[1]["args"][-1] == "exported-agent"
    assert "GATEWAY_URL" in records[1]["args"]
    assert "OAUTH_CLIENT_ID" in records[1]["args"]
    assert "OAUTH_CLIENT_SECRET_REF" in records[1]["args"]
    assert "GUARDRAIL_ID" not in records[1]["args"]
    assert records[1]["env"]["GATEWAY_URL"] == dangerous
    assert records[1]["env"]["OAUTH_CLIENT_ID"] == dangerous
    assert records[1]["env"]["OAUTH_CLIENT_SECRET_REF"] == "oauth/client-ref"
    assert records[1]["env"]["GUARDRAIL_ID"] is None
    assert not marker.exists()
    assert "./run-docker.sh my-agent" in files["README.md"]
    assert "\ndocker run --rm -p 8080:8080 --env-file" not in files["README.md"]


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


def _pkg_names(reqs: str) -> set[str]:
    """Package names from a requirements body, dropping comment lines + any
    '>=' / '==' version specifier (exporter now applies version floors)."""
    import re

    out = set()
    for line in reqs.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(re.split(r"[><=]", line, maxsplit=1)[0])
    return out


def test_bedrock_requirements_include_strands_and_agentcore():
    config = _make_runtime_config()  # default provider == bedrock
    pkgs = _pkg_names(build_requirements(config))

    assert "strands-agents" in pkgs
    assert "strands-agents-tools" in pkgs
    # bedrock-agentcore is NOT in PROVIDER_PACKAGES but the generated code
    # imports it — the exporter must add it explicitly (gap risk #1).
    assert "bedrock-agentcore" in pkgs
    assert "boto3" in pkgs


def test_openai_provider_adds_openai_package():
    config = _make_runtime_config(modelProvider="openai")
    pkgs = _pkg_names(build_requirements(config))

    assert "openai" in pkgs
    assert "strands-agents" in pkgs
    assert "bedrock-agentcore" in pkgs


def test_unknown_provider_falls_back_to_bedrock_packages():
    # model_provider is a Literal, so simulate an out-of-map value by mutating
    # the attribute directly (bypassing validation) to prove .get() fallback.
    config = _make_runtime_config()
    object.__setattr__(config, "model_provider", "totally-unknown-provider")
    pkgs = _pkg_names(build_requirements(config))

    assert "strands-agents" in pkgs
    assert "bedrock-agentcore" in pkgs  # no KeyError, fell back gracefully


def test_observability_adds_otel_distro():
    config = _make_runtime_config(enableOtel=True)
    assert "aws-opentelemetry-distro" in _pkg_names(build_requirements(config, connected_tools=[]))


def test_no_observability_omits_otel_distro():
    config = _make_runtime_config()
    assert "aws-opentelemetry-distro" not in _pkg_names(build_requirements(config, connected_tools=[]))


def test_requirements_are_version_floored_with_header():
    """Holmes supply-chain finding: known packages carry a '>=' floor and the
    body leads with a 'pin exact versions for production' header."""
    config = _make_runtime_config()
    reqs = build_requirements(config)
    assert reqs.lstrip().startswith("#")  # header present
    assert "boto3>=" in reqs and "bedrock-agentcore>=" in reqs


def test_requirements_package_lines_sorted_and_deduped():
    import re

    config = _make_runtime_config()
    pkg_lines = [ln for ln in build_requirements(config).splitlines() if ln and not ln.startswith("#")]
    # Lines are ordered by PACKAGE NAME (the version specifier is appended after).
    names = [re.split(r"[><=]", ln, maxsplit=1)[0] for ln in pkg_lines]
    assert names == sorted(names)
    assert len(pkg_lines) == len(set(pkg_lines))


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
        if line.startswith("PROVIDER_API_KEY="):
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
    config = _make_runtime_config(model={"modelId": "us.anthropic.claude-sonnet-5"})
    env = build_env_example(config)
    assert "MODEL_ID=us.anthropic.claude-sonnet-5" in env


def test_non_bedrock_env_documents_key_reference_and_base_url():
    key_ref = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:agentcore-provider/example-AbCdEf"
    config = _make_runtime_config(
        model={"modelId": "openai/gpt-4o"},
        modelProvider="litellm",
        providerApiKeyRef=key_ref,
        providerBaseUrl="https://models.example.internal/v1",
    )
    env = build_env_example(config)
    assert "PROVIDER_API_KEY=" in env
    assert f"PROVIDER_API_KEY_SECRET_ARN={key_ref}" in env
    assert "PROVIDER_BASE_URL=https://models.example.internal/v1" in env


def test_prefilled_env_values_cannot_execute_shell_syntax(tmp_path):
    marker = tmp_path / "should-not-exist"
    dangerous_url = f"https://models.example/$(touch${{IFS}}{marker})"
    config = _make_runtime_config(
        model={"modelId": "openai/gpt-4o"},
        modelProvider="openai",
        providerBaseUrl=dangerous_url,
    )
    env_path = tmp_path / ".env"
    env_path.write_text(build_env_example(config))

    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -a; source "$1"; printf "%s" "$PROVIDER_BASE_URL"',
            "bash",
            str(env_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == dangerous_url
    assert not marker.exists(), "sourcing the exported .env executed customer-controlled shell syntax"


def test_litellm_gateway_is_inferred_and_fully_wired_without_connected_tools():
    raw_key = "sk-must-never-be-exported"
    key_ref = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:litellm-key"
    request = _make_deploy_request(
        _make_runtime_config(),
        gatewayConfig={
            "gatewayProvider": "litellm",
            "litellmBaseUrl": "https://litellm.example.internal",
            "litellmServers": ["github"],
            "litellmApiKey": raw_key,
            "litellmApiKeyRef": key_ref,
        },
    )

    files = build_python_project(request)

    assert "MCPClient" in files["agent.py"]
    assert "GATEWAY_URL=https://litellm.example.internal/github/mcp" in files[".env.example"]
    assert "GATEWAY_AUTH_MODE=static_bearer" in files[".env.example"]
    assert "GATEWAY_MCP_SERVERS=github" in files[".env.example"]
    assert f"GATEWAY_API_KEY_SECRET_ARN={key_ref}" in files[".env.example"]
    assert all(raw_key not in content for content in files.values())


def test_deployed_litellm_result_shape_keeps_static_bearer_auth():
    key_ref = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:litellm-key"
    request = _make_deploy_request(
        _make_runtime_config(),
        gatewayConfig={
            "gateway_url": "https://litellm.example/github/mcp",
            "client_info": {
                "provider": "litellm",
                "api_key_ref": key_ref,
            },
        },
    )

    env = build_python_project(request)[".env.example"]

    assert "GATEWAY_AUTH_MODE=static_bearer" in env
    assert "GATEWAY_URL=https://litellm.example/github/mcp" in env
    assert f"GATEWAY_API_KEY_SECRET_ARN={key_ref}" in env


def test_agentcore_gateway_export_lists_the_oauth_inputs_it_needs():
    request = _make_deploy_request(
        _make_runtime_config(),
        gatewayConfig={"gatewayProvider": "agentcore", "targetType": "lambda"},
    )
    env = build_python_project(request)[".env.example"]

    assert "GATEWAY_URL=" in env
    assert "GATEWAY_AUTH_MODE=oauth2" in env
    assert "COGNITO_CLIENT_ID=" in env
    assert "COGNITO_USER_POOL_ID=" in env
    assert "COGNITO_TOKEN_ENDPOINT=" in env
    assert "OAUTH_CLIENT_SECRET_REF=" in env


def test_component_configs_are_not_silently_dropped_when_tool_ids_are_omitted():
    memory = _make_deploy_request(
        _make_runtime_config(),
        memoryConfig={"name": "existing-memory", "memoryId": "mem-0123456789"},
    )
    memory_files = build_python_project(memory)
    assert "MemoryClient" in memory_files["agent.py"]
    assert "MEMORY_ID=mem-0123456789" in memory_files[".env.example"]

    knowledge_base = _make_deploy_request(
        _make_runtime_config(),
        knowledgeBaseConfig={"kbMode": "existing", "knowledgeBaseId": "KB12345678"},
    )
    kb_files = build_python_project(knowledge_base)
    assert "retrieve_from_kb" in kb_files["agent.py"]
    assert "KB_ID=KB12345678" in kb_files[".env.example"]

    a2a = _make_deploy_request(
        _make_runtime_config(),
        a2aConfig={
            "capabilities": ["summarize"],
            "peerAllowlist": ["https://peer.example"],
        },
    )
    a2a_files = build_python_project(a2a)
    assert '"summarize"' in a2a_files["agent.py"]
    assert '"https://peer.example"' in a2a_files["agent.py"]
    assert "A2A_SELF_URL=" in a2a_files[".env.example"]


def test_existing_guardrail_external_identity_and_otel_settings_survive_export():
    raw_header = "Authorization=Bearer must-never-be-exported"
    oauth_secret_ref = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:oauth/client-AbCdEf"
    otel_secret_ref = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:agentcore-otel/team-AbCdEf"
    request = _make_deploy_request(
        _make_runtime_config(),
        gatewayConfig={"gatewayProvider": "agentcore", "targetType": "lambda"},
        identityConfig={
            "provider": "okta",
            "clientId": "client-for-export",
            "clientSecretRef": oauth_secret_ref,
            "discoveryUrl": "https://idp.example/.well-known/openid-configuration",
            "scopes": ["gateway/read", "gateway/write"],
        },
        guardrailsConfig={
            "mode": "existing",
            "guardrailId": "gr-0123456789",
            "guardrailVersion": "7",
        },
        observabilityConfig={
            "enabled": True,
            "otlpEndpoint": "https://otel.example/v1/traces",
            "otlpProtocol": "http/protobuf",
            "serviceName": "support agent",
            "sampleRate": 0.25,
            "resourceAttributes": {"environment": "test", "team": "agent platform"},
            "authHeaderSecretArn": otel_secret_ref,
            "extraHeaders": {"Authorization": "Bearer must-never-be-exported"},
        },
    )

    files = build_python_project(request)
    env = files[".env.example"]

    assert "GUARDRAIL_ID=gr-0123456789" in env
    assert "GUARDRAIL_VERSION=7" in env
    assert "COGNITO_CLIENT_ID=" in env
    assert "OAUTH_CLIENT_ID=client-for-export" in env
    assert f"OAUTH_CLIENT_SECRET_REF={oauth_secret_ref}" in env
    assert "OAUTH_SCOPE='gateway/read gateway/write'" in env
    assert "OAUTH_TOKEN_ENDPOINT=" in env
    assert "https://idp.example/.well-known/openid-configuration" not in env
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=https://otel.example/v1/traces" in env
    assert "OTEL_SERVICE_NAME='support agent'" in env
    assert "OTEL_TRACES_SAMPLER_ARG=0.25" in env
    assert "OTEL_RESOURCE_ATTRIBUTES='environment=test,team=agent platform'" in env
    assert f"OTEL_AUTH_SECRET_ARN={otel_secret_ref}" in env
    assert all(raw_header not in content for content in files.values())
    assert "OTEL_EXPORTER_OTLP_HEADERS=" in env
    assert "OTEL_EXPORTER_OTLP_EXTRA_HEADERS=" in env


def test_create_new_component_names_are_not_misrepresented_as_existing_ids():
    request = _make_deploy_request(
        _make_runtime_config(),
        memoryConfig={"name": "memory-to-create"},
        knowledgeBaseConfig={
            "kbMode": "create_new",
            "kbName": "kb-to-create",
            "dataSourceType": "s3",
        },
        guardrailsConfig={
            "mode": "create_new",
            "name": "guardrail-to-create",
            "guardrailId": "must-not-be-used",
        },
    )

    env = build_python_project(request)[".env.example"]

    assert "\nMEMORY_ID=\n" in env
    assert "\nKB_ID=\n" in env
    assert "\nGUARDRAIL_ID=\n" in env
    assert "\nGUARDRAIL_VERSION=\n" in env
    assert "memory-to-create" not in env
    assert "kb-to-create" not in env
    assert "must-not-be-used" not in env


def test_mcp_server_target_implies_gateway_but_readme_says_it_is_not_provisioned():
    request = _make_deploy_request(
        _make_runtime_config(),
        mcpServerConfig={
            "name": "support-mcp",
            "tools": ["order_status"],
        },
    )

    files = build_python_project(request)

    assert "MCPClient" in files["agent.py"]
    assert "GATEWAY_URL=" in files[".env.example"]
    assert "separate\nMCP-server runtime" in files["README.md"]


def test_every_new_prefilled_env_value_is_shell_quoted(tmp_path):
    marker = tmp_path / "must-not-exist"
    dangerous = f"value $(touch${{IFS}}{marker})"
    env_path = tmp_path / ".env"
    env_path.write_text(
        build_env_example(
            _make_runtime_config(enableOtel=True),
            connected_tools=["gateway", "memory", "knowledge_base", "guardrails", "observability"],
            gateway_config={
                "gatewayProvider": "agentcore",
                "gatewayUrl": dangerous,
            },
            identity_config={
                "provider": "okta",
                "clientId": dangerous,
                "clientSecretRef": dangerous,
                "scopes": [dangerous],
            },
            memory_config={"memoryId": dangerous},
            knowledge_base_config={"kbMode": "existing", "knowledgeBaseId": dangerous},
            guardrails_config={
                "mode": "existing",
                "guardrailId": dangerous,
                "guardrailVersion": dangerous,
            },
            observability_config={
                "otlpEndpoint": dangerous,
                "serviceName": dangerous,
                "resourceAttributes": {"unsafe": dangerous},
                "authHeaderSecretArn": dangerous,
            },
        )
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                'set -a; source "$1"; '
                'printf "%s\\n" "$GATEWAY_URL" "$OAUTH_CLIENT_ID" '
                '"$OAUTH_CLIENT_SECRET_REF" "$OAUTH_SCOPE" "$MEMORY_ID" "$KB_ID" '
                '"$GUARDRAIL_ID" "$GUARDRAIL_VERSION" "$OTEL_EXPORTER_OTLP_ENDPOINT" '
                '"$OTEL_SERVICE_NAME" "$OTEL_AUTH_SECRET_ARN"'
            ),
            "bash",
            str(env_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [dangerous] * 11
    assert not marker.exists(), "sourcing the exported .env executed customer-controlled shell syntax"
