"""Production contract for the standalone MCP gallery runtime."""

from __future__ import annotations

import sys
import types
import zipfile
from pathlib import Path
from unittest import mock

import pytest
import yaml
from app.models.deployment_models import DeploymentState, DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import CfnTemplateGenerator
from app.services.code_generator import CodeGenerationUnsupportedError, generate_agent_code
from app.services.runtime_artifact import (
    BASE_BUNDLE_KEY,
    MCP_LEAN_BUNDLE_KEY,
    STRANDS_BUNDLE_KEY,
    RuntimeArtifactError,
    classify_runtime_artifact,
)
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config(**overrides) -> RuntimeConfig:
    # The standalone FastMCP runtime is model-free: it carries no model and no
    # provider. DeployRequest._mcp_protocol_admission now REJECTS an explicit
    # model/modelProvider on templateId 'mcp-server-runtime' rather than accepting
    # and silently ignoring it (see test_mcp_protocol_admission_contract.py), so
    # the default config here must not supply either. Tests that deliberately probe
    # that rejection pass them explicitly via **overrides.
    values = {
        "name": "standalone_mcp",
        "protocol": "MCP",
        "enableOtel": False,
    }
    values.update(overrides)
    return RuntimeConfig.model_validate(values)


def _source(**config_overrides) -> str:
    return generate_agent_code(
        _config(**config_overrides),
        template_id="mcp-server-runtime",
    )


def _cfn_bundle():
    return CfnTemplateGenerator().generate(
        DeployRequest(
            nodeId="standalone-mcp",
            config=_config(),
            templateId="mcp-server-runtime",
        )
    )


def _gateway_mcp_cfn_bundle():
    return CfnTemplateGenerator().generate(
        DeployRequest.model_validate(
            {
                "nodeId": "mcp-gateway-chain",
                "config": {
                    "name": "mcp_gateway_chain",
                    "model": {"modelId": "us.anthropic.claude-sonnet-5"},
                    "modelProvider": "bedrock",
                    "protocol": "HTTP",
                    "enableOtel": False,
                },
                "templateId": "mcp-server-gateway-target",
                "gatewayConfig": {
                    "gateway_provider": "agentcore",
                    "targetType": "lambda",
                },
                "mcpServerConfig": {
                    "name": "order_tools",
                    "tools": [],
                },
            }
        )
    )


def _installed_packages(build_script: str) -> list[str]:
    """The package arguments of a build script's one ``run_pip install``.

    Read from the install command, not the whole script: every recipe carries the
    platform's full constraint file, as the platform's own build passes it to every
    bundle, so a pin line like ``strands-agents==1.56.0`` appears in the lean script
    without installing anything.
    """
    lines = build_script.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("run_pip install"))
    packages: list[str] = []
    for line in lines[start + 1 :]:
        token = line.strip().removesuffix("\\").strip()
        if token.startswith('"') and not token.startswith('"$'):
            packages.append(token.strip('"'))
        if not line.rstrip().endswith("\\"):
            break
    assert packages, "the build script's install names no packages"
    return packages


def _role_actions(template: dict, logical_id: str) -> set[str]:
    actions: set[str] = set()
    policies = template["Resources"][logical_id]["Properties"].get("Policies", [])
    for policy in policies:
        for statement in policy["PolicyDocument"]["Statement"]:
            value = statement.get("Action", [])
            actions.update(value if isinstance(value, list) else [value])
    return actions


class _FakeFastMCP:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tools: dict[str, object] = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register

    def run(self, **_kwargs):
        raise AssertionError("module import must not start the server")


def _execute_with_fake_fastmcp(source: str) -> dict:
    fake_module = types.ModuleType("mcp.server.fastmcp")
    fake_module.FastMCP = _FakeFastMCP
    with mock.patch.dict(
        sys.modules,
        {
            "mcp": types.ModuleType("mcp"),
            "mcp.server": types.ModuleType("mcp.server"),
            "mcp.server.fastmcp": fake_module,
        },
    ):
        namespace = {"__name__": "generated_standalone_mcp"}
        exec(compile(source, "<standalone-mcp>", "exec"), namespace)  # noqa: S102
    return namespace


def test_generated_artifact_is_a_real_fastmcp_server():
    source = _source()
    compile(source, "<standalone-mcp>", "exec")

    assert "from mcp.server.fastmcp import FastMCP" in source
    assert 'host="0.0.0.0"' in source
    assert 'os.environ.get("PORT", "8000")' in source
    assert "stateless_http=True" in source
    assert 'mcp.run(transport="streamable-http")' in source
    assert source.count("@mcp.tool()") == 3
    for name in ("get_weather", "search_web", "fetch_url"):
        assert f"def {name}(" in source

    for forbidden in (
        "BedrockAgentCoreApp",
        "bedrock-runtime",
        ".converse(",
        "MODEL_ID",
        "SYSTEM_PROMPT",
        "@app.entrypoint",
    ):
        assert forbidden not in source


def test_generated_tools_register_and_call_the_canonical_implementations():
    namespace = _execute_with_fake_fastmcp(_source())
    server = namespace["mcp"]
    assert set(server.tools) == {"get_weather", "search_web", "fetch_url"}
    assert server.kwargs["port"] == 8000
    assert server.kwargs["stateless_http"] is True

    namespace["_do_weather"] = lambda city: f"weather:{city}"
    namespace["_do_duckduckgo_search"] = lambda query: f"search:{query}"
    namespace["_do_fetch_webpage"] = lambda url: f"fetch:{url}"
    namespace["_tool_safe"] = lambda function, value: function(value)

    assert server.tools["get_weather"]("Dublin") == "weather:Dublin"
    assert server.tools["search_web"]("MCP") == "search:MCP"
    assert server.tools["fetch_url"]("https://example.com") == "fetch:https://example.com"


def test_generated_server_keeps_the_canonical_ssrf_guard():
    source = _source()
    assert "_FETCH_BLOCKED_NETS" in source
    assert "getaddrinfo" in source
    assert "169.254.0.0/16" in source
    assert "_do_fetch_webpage" in source


@pytest.mark.parametrize(
    "source,kind,key,provider_applicable",
    [
        ("import boto3\n", "base", BASE_BUNDLE_KEY, False),
        ("from strands import Agent\n", "strands", STRANDS_BUNDLE_KEY, True),
        ("from mcp.server.fastmcp import FastMCP\n", "mcp", MCP_LEAN_BUNDLE_KEY, False),
        (
            "from strands import Agent\nfrom mcp.client.streamable_http import streamablehttp_client\n",
            "strands",
            STRANDS_BUNDLE_KEY,
            True,
        ),
    ],
)
def test_shared_bundle_classifier(source, kind, key, provider_applicable):
    artifact = classify_runtime_artifact(source)
    assert (artifact.kind, artifact.bundle_key, artifact.model_provider_applicable) == (
        kind,
        key,
        provider_applicable,
    )


def test_bundle_classifier_fails_closed_on_invalid_generated_python():
    with pytest.raises(RuntimeArtifactError, match="not valid Python"):
        classify_runtime_artifact("def broken(:")


def test_shipping_mcp_bundle_matches_the_generated_v1_import_contract():
    # The bundles are build artifacts of scripts/install-agentcore-deps.sh, gitignored, so a
    # fresh checkout (CI) has none. Same rule as test_runtime_bundle_pins_match_the_test_environment:
    # nothing built is a skip, a partial build is a failure, because a missing mcp-lean.zip next
    # to a present base.zip is exactly the drift this test exists to catch.
    deps_dir = REPO_ROOT / "backend" / "agentcore-deps"
    archive = deps_dir / "mcp-lean.zip"
    if not archive.exists():
        built = sorted(p.name for p in deps_dir.glob("*.zip")) if deps_dir.is_dir() else []
        if not built:
            pytest.skip(f"no bundles built in {deps_dir}; run scripts/install-agentcore-deps.sh")
        pytest.fail(f"{deps_dir.name}/ is partially built -- {archive.name} is missing but {built} exist")
    with zipfile.ZipFile(archive) as bundle:
        metadata_name = next(
            name for name in bundle.namelist() if name.startswith("mcp-") and name.endswith(".dist-info/METADATA")
        )
        metadata = bundle.read(metadata_name).decode()
        version_line = next(line for line in metadata.splitlines() if line.startswith("Version: "))
        major = int(version_line.removeprefix("Version: ").split(".", 1)[0])
        assert major == 1
        assert "mcp/server/fastmcp/__init__.py" in bundle.namelist()


def test_standalone_cfn_runtime_is_model_free_and_uses_the_mcp_bundle():
    bundle = _cfn_bundle()
    template = yaml.safe_load(bundle.template_yaml)

    assert template["Parameters"]["DependencyBundleKey"]["Default"] == MCP_LEAN_BUNDLE_KEY
    assert "ModelId" not in template["Parameters"]

    runtimes = [
        resource for resource in template["Resources"].values() if resource["Type"] == "AWS::BedrockAgentCore::Runtime"
    ]
    assert len(runtimes) == 1
    assert runtimes[0]["Properties"]["ProtocolConfiguration"] == "MCP"
    assert "MODEL_ID" not in runtimes[0]["Properties"].get("EnvironmentVariables", {})

    runtime_role = template["Resources"]["RuntimeExecutionRole"]
    statements = runtime_role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    actions = {
        action
        for statement in statements
        for action in (
            statement.get("Action", []) if isinstance(statement.get("Action", []), list) else [statement["Action"]]
        )
    }
    assert "bedrock:InvokeModel" not in actions
    assert "bedrock:InvokeModelWithResponseStream" not in actions

    assert 'PARAM_OVERRIDES+=("ModelId=$MODEL_ID")' not in bundle.deploy_sh


def test_standalone_cfn_readme_documents_the_mcp_protocol_not_an_agent_prompt():
    readme = _cfn_bundle().readme

    assert "## Invoking the MCP Runtime" in readme
    for method in (
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ):
        assert method in readme

    assert "## Invoking the Agent" not in readme
    assert '{"prompt": "hello"}' not in readme


def test_gateway_target_export_keeps_client_and_mcp_server_bundle_authority_separate():
    bundle = _gateway_mcp_cfn_bundle()
    template = yaml.safe_load(bundle.template_yaml)
    parameters = template["Parameters"]
    resources = template["Resources"]

    assert parameters["DependencyBundleKey"]["Default"] == STRANDS_BUNDLE_KEY
    assert parameters["McpServerDependencyBundleKey"]["Default"] == MCP_LEAN_BUNDLE_KEY
    assert resources["AgentCodePackage"]["Properties"]["DependencyBundleKey"] == {"Ref": "DependencyBundleKey"}
    assert resources["McpServerCodePackage"]["Properties"]["DependencyBundleKey"] == {
        "Ref": "McpServerDependencyBundleKey"
    }
    assert resources["McpServerCodePackage"]["Properties"]["BundleDigest"] == {"Ref": "McpServerDependencyBundleDigest"}

    mcp_runtime = resources["McpServerRuntime"]["Properties"]
    assert mcp_runtime["ProtocolConfiguration"] == "MCP"
    assert "MODEL_ID" not in mcp_runtime.get("EnvironmentVariables", {})
    assert "bedrock:InvokeModel" not in _role_actions(template, "McpServerRole")
    assert "bedrock:InvokeModelWithResponseStream" not in _role_actions(template, "McpServerRole")

    assert "strands-agents" in _installed_packages(bundle.build_bundle_sh)
    assert "strands-agents" not in _installed_packages(bundle.build_mcp_bundle_sh)
    assert _installed_packages(bundle.build_mcp_bundle_sh) == ["bedrock-agentcore", "boto3", "mcp<2"]
    assert 'bundle_digest_in_bucket "$MCP_BUNDLE_KEY"' in bundle.deploy_sh
    assert 'PARAM_OVERRIDES+=("McpServerDependencyBundleKey=$MCP_BUNDLE_KEY")' in (bundle.deploy_sh)
    assert 'PARAM_OVERRIDES+=("McpServerDependencyBundleDigest=$MCP_BUNDLE_DIGEST")' in bundle.deploy_sh


def test_gateway_target_export_documents_both_retained_dependency_bundles():
    bundle = _gateway_mcp_cfn_bundle()

    for token in (
        "build-dependency-bundle.sh",
        "build-mcp-server-bundle.sh",
        "DependencyBundleKey",
        "McpServerDependencyBundleKey",
        "McpServerDependencyBundleDigest",
        STRANDS_BUNDLE_KEY,
        MCP_LEAN_BUNDLE_KEY,
    ):
        assert token in bundle.readme

    assert STRANDS_BUNDLE_KEY in bundle.teardown_sh
    assert MCP_LEAN_BUNDLE_KEY in bundle.teardown_sh


@pytest.mark.parametrize(
    ("bundle_key", "mcp_bundle_key"),
    [
        ("agentcore-deps/unknown-main.zip", None),
        (STRANDS_BUNDLE_KEY, "agentcore-deps/unknown-mcp.zip"),
    ],
)
def test_generated_readme_refuses_an_unknown_dependency_bundle_recipe(
    bundle_key,
    mcp_bundle_key,
):
    """Deployment instructions must not lie when classifier and recipes drift.

    The build-script generator already fails closed on an unknown key. The README is
    part of the same executable artifact contract, so silently describing that key as
    the Strands or lean bundle would leave Terraform users with believable instructions
    for packages the emitted template never selected.
    """
    template = {
        "Parameters": {},
        "Resources": {
            "AgentCoreRuntime": {
                "Type": "AWS::BedrockAgentCore::Runtime",
            },
        },
    }

    with pytest.raises(RuntimeArtifactError, match="dependency-bundle recipe"):
        CfnTemplateGenerator()._generate_readme(
            "bundle-authority",
            "mcp-server-runtime",
            _config(),
            False,
            False,
            False,
            bool(mcp_bundle_key),
            bundle_key=bundle_key,
            mcp_bundle_key=mcp_bundle_key,
            template=template,
        )


def test_codegen_refuses_a_false_http_protocol():
    with pytest.raises(CodeGenerationUnsupportedError, match="requires config.protocol='MCP'"):
        generate_agent_code(
            _config(protocol="HTTP"),
            template_id="mcp-server-runtime",
        )


def test_codegen_refuses_model_provider_and_http_agent_postprocessors():
    with pytest.raises(CodeGenerationUnsupportedError, match="model-free MCP tool server"):
        generate_agent_code(
            _config(modelProvider="openai", model={"modelId": "gpt-5"}),
            template_id="mcp-server-runtime",
        )
    with pytest.raises(CodeGenerationUnsupportedError, match="cannot honour guardrails"):
        generate_agent_code(
            _config(),
            tools=["guardrails"],
            template_id="mcp-server-runtime",
        )
    with pytest.raises(CodeGenerationUnsupportedError, match="generic agent observability"):
        generate_agent_code(
            _config(),
            template_id="mcp-server-runtime",
            observability_enabled=True,
        )


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"connectedTools": ["memory"]}, "Memory"),
        ({"gatewayConfig": {}}, "Gateway"),
        ({"customTools": [{"toolName": "x", "description": "x", "lambdaCode": "x"}]}, "custom Gateway tools"),
        ({"externalMcpServers": [{"server_id": "x"}]}, "external MCP servers"),
        ({"guardrailsConfig": {}}, "Guardrails"),
        ({"evaluationConfig": {}}, "evaluationConfig"),
        ({"policyConfig": {}}, "policyConfig"),
        ({"mcpServerConfig": {}}, "mcpServerConfig"),
        ({"observabilityConfig": {}}, "generic agent observability"),
        ({"a2aConfig": {}}, "A2A"),
    ],
)
def test_api_boundary_refuses_unsupported_standalone_mcp_combinations(override, expected):
    # Build config the way a real caller (and the frontend runtimeRequestConfig
    # helper) does for the model-free MCP template: a MINIMAL dict, not a full
    # model_dump. A dump re-emits every field including model-only defaults, and
    # DeployRequest._mcp_protocol_admission rejects an explicitly-present model-only
    # field before the unsupported-capability check runs — which would mask the
    # capability rejection this test exists to prove.
    body = {
        "nodeId": "standalone-mcp",
        "config": {"name": "standalone_mcp", "protocol": "MCP", "enableOtel": False},
        "templateId": "mcp-server-runtime",
        **override,
    }
    with pytest.raises(ValidationError, match=expected):
        DeployRequest.model_validate(body)


def test_api_boundary_requires_mcp_and_rejects_provider_credentials():
    with pytest.raises(ValidationError, match="requires config.protocol='MCP'"):
        DeployRequest(
            nodeId="standalone-mcp",
            config=_config(protocol="HTTP"),
            templateId="mcp-server-runtime",
        )
    with pytest.raises(ValidationError, match="providerApiKeyRef"):
        DeployRequest(
            nodeId="standalone-mcp",
            config=_config(providerApiKeyRef="arn:aws:secretsmanager:us-east-1:111122223333:secret:x"),
            templateId="mcp-server-runtime",
        )


def test_deployment_state_protocol_is_public_and_legacy_safe():
    from datetime import datetime, timezone

    legacy = DeploymentState(
        deployment_id="legacy",
        started_at=datetime.now(timezone.utc),
    )
    assert legacy.runtime_protocol == "HTTP"
    assert legacy.model_dump(mode="json")["runtime_protocol"] == "HTTP"

    mcp = legacy.model_copy(update={"runtime_protocol": "MCP"})
    assert mcp.model_dump(mode="json")["runtime_protocol"] == "MCP"
