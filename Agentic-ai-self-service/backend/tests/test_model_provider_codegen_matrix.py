"""Every reachable Strands code-generation path honours the selected provider.

The original default and multi-agent generators were provider-aware, but the
gateway, memory, built-in-tool, template, and A2A branches each hard-coded a
Bedrock model. Those canvases deployed successfully and then either called the
wrong provider or passed a third-party model id to Bedrock.
"""

import pytest
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.models.enums import StrandsModelProvider
from app.services.code_generator import (
    CodeGenerationUnsupportedError,
    _get_model_init_code,
    generate_agent_code,
)
from pydantic import ValidationError

PROVIDERS = [provider.value for provider in StrandsModelProvider]


def _config(provider: str) -> RuntimeConfig:
    model_id = "us.anthropic.claude-sonnet-5" if provider == "bedrock" else "provider-model"
    return RuntimeConfig(
        name="provider-matrix",
        model={"modelId": model_id},
        modelProvider=provider,
        systemPrompt="Use the configured model.",
    )


SUPPORTED_PATHS = [
    pytest.param([], None, None, None, id="default"),
    pytest.param(["gateway"], None, {}, None, id="gateway"),
    pytest.param(["memory"], None, None, None, id="memory"),
    pytest.param(["memory", "gateway"], None, {}, None, id="memory-gateway"),
    pytest.param(["browser"], None, None, None, id="browser"),
    pytest.param(["code_interpreter"], None, None, None, id="code-interpreter"),
    pytest.param(["knowledge_base"], None, None, {}, id="knowledge-base"),
    pytest.param([], "strands-gateway-agent", {}, None, id="gateway-template"),
    pytest.param([], "mcp-server-gateway-target", {}, None, id="mcp-gateway-template"),
    pytest.param([], "customer-support-assistant", {}, None, id="customer-template"),
    pytest.param(["a2a"], None, None, None, id="a2a"),
]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("tools,template_id,gateway_config,kb_config", SUPPORTED_PATHS)
def test_every_supported_path_emits_the_selected_provider(
    provider,
    tools,
    template_id,
    gateway_config,
    kb_config,
):
    config = _config(provider)
    code = generate_agent_code(
        config=config,
        tools=tools,
        template_id=template_id,
        gateway_config=gateway_config,
        kb_config=kb_config,
        portable=True,
    )

    expected_import, _ = _get_model_init_code(
        provider,
        "us.anthropic.claude-sonnet-5" if provider == "bedrock" else "provider-model",
        "us-east-1",
    )
    assert expected_import in code
    compile(code, f"<{provider}:{template_id or ','.join(tools) or 'default'}>", "exec")


def test_direct_bedrock_template_refuses_a_different_provider():
    with pytest.raises(CodeGenerationUnsupportedError, match="refusing to silently substitute Bedrock"):
        generate_agent_code(
            config=_config("openai"),
            template_id="web-search-agent",
            portable=True,
        )


def test_api_boundary_refuses_bedrock_only_template_before_deploy():
    with pytest.raises(ValidationError, match="will not silently substitute Bedrock"):
        DeployRequest(
            nodeId="provider-matrix",
            config=_config("openai"),
            templateId="web-search-agent",
        )


def test_model_free_mcp_template_refuses_a_provider_instead_of_ignoring_it():
    config = _config("openai")
    config.protocol = "MCP"
    with pytest.raises(CodeGenerationUnsupportedError, match="model-free MCP tool server"):
        generate_agent_code(
            config=config,
            template_id="mcp-server-runtime",
            portable=True,
        )
    with pytest.raises(ValidationError, match="model-free FastMCP tool server"):
        DeployRequest(
            nodeId="provider-matrix",
            config=config,
            templateId="mcp-server-runtime",
        )


def test_non_bedrock_guardrail_is_refused_before_it_can_be_unenforced():
    with pytest.raises(ValidationError, match="Bedrock Guardrails require"):
        DeployRequest(
            nodeId="provider-matrix",
            config=_config("openai"),
            connectedTools=["guardrails"],
        )


def test_harness_request_is_not_subject_to_generated_code_restrictions():
    request = DeployRequest(
        nodeId="provider-matrix",
        config=_config("openai"),
        deploymentMode="harness",
        templateId="web-search-agent",
    )
    assert request.deployment_mode == "harness"
