"""Preservation property tests — MUST PASS on unfixed code.

These tests capture the baseline behavior of non-buggy paths. After the fix
is implemented, these same tests will be re-run to verify no regressions.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**

Property 2: Preservation — Framework Logic, Gateway/MCP, Tools, Requirements,
and Prompt Escaping Preserved
"""

import json
import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.deployment_models import RuntimeConfig
from app.models.enums import AgentFramework, StrandsModelProvider
from app.models.components import RuntimeConfiguration, ModelConfiguration
from app.services import code_generator
from app.services.deployment import (
    generate_agent_code as deployment_generate_agent_code,
    generate_requirements as deployment_generate_requirements,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runtime_config(**overrides) -> RuntimeConfig:
    """Build a minimal RuntimeConfig for code_generator.py tests."""
    defaults = {
        "name": "test-agent",
        "framework": "strands_agents",
        "model": {
            "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "provider": "anthropic",
        },
        "systemPrompt": "You are a helpful assistant.",
    }
    defaults.update(overrides)
    return RuntimeConfig(**defaults)


def _make_runtime_configuration(
    provider: StrandsModelProvider = StrandsModelProvider.BEDROCK, **overrides
) -> RuntimeConfiguration:
    """Build a minimal RuntimeConfiguration for deployment.py tests."""
    defaults = {
        "name": "test-agent",
        "framework": AgentFramework.STRANDS_AGENTS,
        "model": ModelConfiguration(
            provider=provider,
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        ),
        "system_prompt": "You are a helpful assistant.",
    }
    defaults.update(overrides)
    return RuntimeConfiguration(**defaults)


_GATEWAY_CREDS = {
    "url": "https://example.com/gateway",
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "token_endpoint": "https://example.com/oauth2/token",
    "scope": "agentcore/*",
}

# Strategy for safe system prompts (no triple quotes that break f-string embedding)
_safe_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z"), blacklist_characters="\\\"'{}`"),
    min_size=1,
    max_size=200,
)

_model_ids = st.sampled_from(
    [
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.amazon.nova-2-lite-v1:0",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    ]
)

_all_providers = st.sampled_from(list(StrandsModelProvider))

# Strands-specific imports expected in deployment.py generated code
_DEPLOYMENT_STRANDS_MARKERS = [
    "from bedrock_agentcore.runtime import BedrockAgentCoreApp",
    "from strands import Agent",
]


# ============================================================================
# Property 3: Framework-Specific Agent Logic Preserved (deployment.py)
# ============================================================================


class TestDeploymentFrameworkLogicPreservation:
    """Preservation tests for deployment.py generate_agent_code() — Strands-only.

    **Validates: Requirements 3.1, 3.6**

    All generated code MUST contain Strands + BedrockAgentCoreApp imports.
    """

    def test_strands_markers_present(self):
        """**Validates: Requirements 3.1, 3.6**

        Generated code MUST contain Strands-specific markers.
        """
        config = _make_runtime_configuration()
        code = deployment_generate_agent_code(config)
        for marker in _DEPLOYMENT_STRANDS_MARKERS:
            assert marker in code, f"Strands generated code missing expected marker: {marker}"

    def test_strands_agents_has_agent_creation(self):
        """**Validates: Requirements 3.1**

        Strands template MUST create Agent inside invoke() (per official
        bedrock-agentcore-starter-toolkit pattern).
        """
        config = _make_runtime_configuration()
        code = deployment_generate_agent_code(config)
        assert "agent = Agent(" in code
        assert "from strands import Agent" in code

    @given(provider=_all_providers, model_id=_model_ids)
    @settings(max_examples=5)
    def test_property_all_providers_embed_model_and_prompt(self, provider, model_id):
        """**Validates: Requirements 3.1, 3.6**

        For ANY provider, the generated Strands code MUST embed the model ID and
        system prompt.
        """
        config = _make_runtime_configuration(
            provider=provider,
            model=ModelConfiguration(provider=provider, model_id=model_id),
        )
        code = deployment_generate_agent_code(config)
        assert model_id in code
        assert "You are a helpful assistant." in code

    # Removed: test_property_all_providers_have_invoke asserted the legacy
    # `async def invoke(payload, context):` signature. Migrated to
    # synchronous `def invoke(payload):` decorated with @app.entrypoint
    # (BedrockAgentCoreApp pattern from amazon-bedrock-agentcore-samples).

    @given(provider=_all_providers)
    @settings(max_examples=5)
    def test_property_all_providers_return_response_key(self, provider):
        """**Validates: Requirements 3.1, 3.6**

        For ANY provider, the generated code MUST return {"response": ...}.
        """
        config = _make_runtime_configuration(provider=provider)
        code = deployment_generate_agent_code(config)
        assert '"response"' in code


# ============================================================================
# Property 3: Framework-Specific Agent Logic Preserved (code_generator.py)
# ============================================================================


class TestCodeGeneratorFrameworkLogicPreservation:
    """Preservation tests for code_generator.py template functions.

    **Validates: Requirements 3.1, 3.2, 3.3**

    Each template function MUST contain its framework-specific logic.
    """

    def test_langchain_web_search_has_langgraph_imports(self):
        """**Validates: Requirements 3.1**

        _generate_langchain_web_search MUST contain web search + tool-calling logic.
        Uses lightweight boto3 Converse API loop instead of LangChain/LangGraph.
        """
        code = code_generator._generate_langchain_web_search(
            "You are a search agent.",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "us-east-1",
        )
        assert "duckduckgo_search" in code
        assert "fetch_webpage" in code
        assert "_converse_loop" in code
        assert "BedrockAgentCoreApp" in code

    # Removed: test_customer_support_has_gateway_mcp and test_gateway_agent_has_mcp_protocol
    # asserted the legacy `_mcp_request` / `_get_gateway_token` JSON-RPC helpers.
    # Both generators now delegate to _generate_strands_gateway, which uses the
    # official Strands MCPClient + streamablehttp_client pattern (per
    # tasks/lessons.md Bug 13).

    def test_default_agent_has_boto3_converse(self):
        """**Validates: Requirements 3.1**

        _generate_default_agent MUST use boto3 Bedrock Converse API.
        """
        code = code_generator._generate_default_agent(
            "You are a helpful assistant.",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "us-east-1",
        )
        assert "import boto3" in code
        assert ".converse(" in code or "bedrock-runtime" in code


# ============================================================================
# Property 4: Gateway/MCP and Built-in Tools Logic Preserved
# ============================================================================


class TestGatewayMCPPreservation:
    """Preservation tests for gateway/MCP template logic.

    **Validates: Requirements 3.2, 3.3**
    """

    def test_strands_gateway_has_cognito_oauth(self):
        """**Validates: Requirements 3.2**

        _generate_strands_gateway MUST contain Cognito OAuth token acquisition.
        """
        code = code_generator._generate_strands_gateway(
            "You are a gateway agent.",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            _GATEWAY_CREDS,
        )
        assert "def _get_gateway_token():" in code
        assert "grant_type" in code
        assert "client_credentials" in code
        assert "access_token" in code

    # Removed: test_strands_gateway_has_mcp_protocol asserted hand-rolled
    # `_mcp_request`/`_list_gateway_tools`/`_call_gateway_tool`/`_to_bedrock_tools`
    # JSON-RPC helpers. Migrated to MCPClient + streamablehttp_client (per
    # tasks/lessons.md Bug 13).
    #
    # Removed: test_strands_gateway_has_agentic_loop asserted boto3 Converse
    # agentic loop (`.converse(`, `tool_use`, `toolResult`, `max_turns`).
    # Strands Agent now handles the agentic loop natively.
    #
    # Removed: test_strands_gateway_embeds_credentials asserted that gateway
    # credentials are embedded in source. Migrated to env-var-only injection
    # at deploy time (per tasks/lessons.md Bug 13 — credentials must NOT be
    # in generated code).

    def test_customer_support_has_cognito_oauth(self):
        """**Validates: Requirements 3.2**

        _generate_customer_support MUST contain Cognito OAuth token acquisition.
        """
        code = code_generator._generate_customer_support(
            "You are a support agent.",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            _GATEWAY_CREDS,
        )
        assert "def _get_gateway_token():" in code
        assert "grant_type" in code
        assert "client_credentials" in code

    # Removed: test_customer_support_has_mcp_helpers, test_gateway_agent_has_oauth_and_mcp,
    # and test_property_strands_gateway_mcp_helpers_always_present asserted the
    # legacy `_mcp_request` / `_list_gateway_tools` / `_call_gateway_tool` /
    # `_to_bedrock_tools` JSON-RPC helpers. The generator now uses Strands MCPClient
    # with streamablehttp_client (per tasks/lessons.md Bug 13). OAuth via
    # _get_gateway_token() remains and is covered by the existing
    # test_strands_gateway_has_cognito_oauth and test_customer_support_has_cognito_oauth.


class TestBuiltInToolsPreservation:
    """Preservation tests for built-in tools template.

    **Validates: Requirements 3.3**
    """

    def test_tools_agent_produces_valid_code(self):
        """**Validates: Requirements 3.3**

        _generate_tools_agent MUST produce valid Python with Strands Agent.
        Updated: tools agent migrated from boto3 Converse to Strands Agent +
        BedrockModel (single-framework consolidation).
        """
        code = code_generator._generate_tools_agent(
            "You are a tools agent.",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "us-east-1",
            has_browser=True,
            has_code_interpreter=False,
        )
        assert "from strands import Agent" in code
        assert "BedrockAgentCoreApp" in code
        compile(code, "<test>", "exec")

    # Removed: test_tools_agent_uses_boto3 asserted boto3 usage in the tools agent.
    # The tools agent now uses Strands Agent + BedrockModel (single-framework
    # consolidation); see test_tools_agent_produces_valid_code above.


# ============================================================================
# Property 5: Framework-Specific Requirements Preserved
# ============================================================================


class TestRequirementsPreservation:
    """Preservation tests for generate_requirements() in both files.

    **Validates: Requirements 3.4, 3.5**
    """

    # --- code_generator.py requirements ---

    def test_codegen_langchain_web_search_requirements(self):
        """**Validates: Requirements 6.1**

        generate_requirements() returns empty string (deps pre-bundled).
        """
        config = _make_runtime_config()
        reqs = code_generator.generate_requirements(config, tools=[], template_id="web-search-agent")
        assert reqs == ""

    def test_codegen_customer_support_requirements(self):
        """**Validates: Requirements 6.1**"""
        config = _make_runtime_config()
        reqs = code_generator.generate_requirements(config, tools=[], template_id="customer-support-assistant")
        assert reqs == ""

    def test_codegen_strands_gateway_requirements(self):
        """**Validates: Requirements 6.1**"""
        config = _make_runtime_config()
        reqs = code_generator.generate_requirements(config, tools=[], template_id="strands-gateway-agent")
        assert reqs == ""

    def test_codegen_tools_with_browser_requirements(self):
        """**Validates: Requirements 6.1**"""
        config = _make_runtime_config()
        reqs = code_generator.generate_requirements(config, tools=["browser"])
        assert reqs == ""

    def test_codegen_tools_with_gateway_requirements(self):
        """**Validates: Requirements 6.1**"""
        config = _make_runtime_config()
        reqs = code_generator.generate_requirements(config, tools=["gateway"])
        assert reqs == ""

    def test_codegen_default_requirements(self):
        """**Validates: Requirements 6.1**"""
        config = _make_runtime_config()
        reqs = code_generator.generate_requirements(config, tools=[])
        assert reqs == ""

    # --- deployment.py requirements ---

    def test_deployment_strands_deps(self):
        """**Validates: Requirements 3.4** — deps are pre-bundled, not in requirements.txt"""
        config = _make_runtime_configuration()
        reqs = deployment_generate_requirements(config)
        assert reqs == ""

    @given(provider=_all_providers)
    @settings(max_examples=5)
    def test_property_deployment_requirements_empty(self, provider):
        """**Validates: Requirements 3.4**

        For ANY provider, deployment.py generate_requirements() returns empty string.
        """
        config = _make_runtime_configuration(provider=provider)
        reqs = deployment_generate_requirements(config)
        assert reqs == ""


# ============================================================================
# Property 6: System Prompt Escaping Preserved
# ============================================================================


class TestSystemPromptEscapingPreservation:
    """Preservation tests for system prompt escaping in generated code.

    **Validates: Requirements 3.8**
    """

    def test_escape_triple_quotes_function(self):
        """**Validates: Requirements 3.8**

        _escape_triple_quotes MUST replace triple double-quotes.
        """
        result = code_generator._escape_triple_quotes('Hello """world"""')
        assert '"""' not in result
        assert '\\"\\"\\"' in result

    def test_prompt_with_special_chars_in_default_agent(self):
        """**Validates: Requirements 3.8**

        System prompt with special characters MUST produce valid Python.
        """
        special_prompt = "You are an agent. Handle 'quotes' and \\backslashes\\ carefully."
        code = code_generator._generate_default_agent(
            code_generator._escape_triple_quotes(special_prompt),
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "us-east-1",
        )
        # The generated code should be syntactically valid Python
        try:
            compile(code, "<test>", "exec")
        except SyntaxError as e:
            pytest.fail(f"Generated code has syntax error: {e}")

    def test_prompt_with_special_chars_in_deployment(self):
        """**Validates: Requirements 3.8**

        System prompt with special characters in deployment.py MUST produce valid Python.
        """
        config = _make_runtime_configuration(
            system_prompt="You are an agent. Handle 'quotes' and \\backslashes\\ carefully.",
        )
        code = deployment_generate_agent_code(config)
        try:
            compile(code, "<test>", "exec")
        except SyntaxError as e:
            pytest.fail(f"Generated code has syntax error: {e}")

    @given(system_prompt=_safe_text)
    @settings(max_examples=5)
    def test_property_default_agent_always_valid_python(self, system_prompt):
        """**Validates: Requirements 3.8**

        For ANY safe system prompt, _generate_default_agent MUST produce
        syntactically valid Python code.
        """
        escaped = code_generator._escape_triple_quotes(system_prompt)
        code = code_generator._generate_default_agent(escaped, "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "us-east-1")
        try:
            compile(code, "<test>", "exec")
        except SyntaxError as e:
            pytest.fail(f"Generated code has syntax error for prompt '{system_prompt}': {e}")

    @given(provider=_all_providers, system_prompt=_safe_text)
    @settings(max_examples=5)
    def test_property_deployment_always_valid_python(self, provider, system_prompt):
        """**Validates: Requirements 3.8**

        For ANY provider and safe system prompt, deployment.py generate_agent_code()
        MUST produce syntactically valid Python code.
        """
        config = _make_runtime_configuration(provider=provider, system_prompt=system_prompt)
        code = deployment_generate_agent_code(config)
        try:
            compile(code, "<test>", "exec")
        except SyntaxError as e:
            pytest.fail(
                f"Generated code has syntax error for provider={provider.value}, prompt='{system_prompt[:50]}': {e}"
            )


# ============================================================================
# Template Routing Preservation (code_generator.py)
# ============================================================================


class TestTemplateRoutingPreservation:
    """Preservation tests for generate_agent_code() routing in code_generator.py.

    **Validates: Requirements 3.5**
    """

    def test_routes_langchain_web_search(self):
        """**Validates: Requirements 3.5**

        template_id="web-search-agent" MUST route to _generate_langchain_web_search.
        """
        config = _make_runtime_config()
        code = code_generator.generate_agent_code(config, template_id="web-search-agent")
        assert "Web Search Agent" in code or "duckduckgo_search" in code

    # Removed: test_routes_strands_gateway asserted the legacy `_mcp_request` /
    # `_to_bedrock_tools` JSON-RPC helpers. The strands-gateway-agent template
    # now uses Strands MCPClient + streamablehttp_client (per
    # tasks/lessons.md Bug 13). Routing is still covered by
    # test_property_template_code_has_sdk_pattern in test_comprehensive_preservation.py.

    def test_routes_customer_support(self):
        """**Validates: Requirements 3.5**

        template_id="customer-support-assistant" MUST route to _generate_customer_support.
        """
        config = _make_runtime_config()
        gateway_config = {
            "gateway_url": _GATEWAY_CREDS["url"],
            "client_info": {
                "client_id": _GATEWAY_CREDS["client_id"],
                "client_secret": _GATEWAY_CREDS["client_secret"],
                "token_endpoint": _GATEWAY_CREDS["token_endpoint"],
                "scope": _GATEWAY_CREDS["scope"],
            },
        }
        code = code_generator.generate_agent_code(
            config,
            gateway_config=gateway_config,
            template_id="customer-support-assistant",
        )
        assert "_mcp_request" in code or "Gateway" in code

    # Removed: test_routes_to_gateway_agent_with_gateway_tool asserted the legacy
    # `_mcp_request` JSON-RPC helper. The gateway-tool route now uses Strands
    # MCPClient + streamablehttp_client (per tasks/lessons.md Bug 13).

    def test_routes_to_tools_agent_with_browser(self):
        """**Validates: Requirements 3.5**

        tools=["browser"] MUST route to _generate_tools_agent (Strands-based).
        """
        config = _make_runtime_config()
        code = code_generator.generate_agent_code(config, tools=["browser"])
        # Updated: tools agent migrated from boto3 Converse to Strands Agent
        # (single-framework consolidation).
        assert "from strands import Agent" in code

    def test_routes_to_default_strands_agent(self):
        """**Validates: Requirements 3.5**

        No template_id and no tools MUST route to _generate_strands_default.
        """
        config = _make_runtime_config()
        code = code_generator.generate_agent_code(config, tools=[])
        # Default agent uses Strands Agent + BedrockAgentCoreApp
        assert "from strands import Agent" in code
        assert "from bedrock_agentcore.runtime import BedrockAgentCoreApp" in code

    # Removed: test_unrecognized_framework_still_generates_strands —
    # RuntimeConfig.framework is now Literal["strands_agents"]; passing an
    # unrecognized value raises a Pydantic ValidationError by design (Strands-only
    # consolidation, no longer permits "backward compat" framework strings).


# ============================================================================
# Deployment.py All 9 Frameworks Supported
# ============================================================================


class TestDeploymentAllProvidersSupported:
    """Verify all Strands model providers produce non-empty code.

    **Validates: Requirements 3.6**
    """

    @pytest.mark.parametrize("provider", list(StrandsModelProvider))
    def test_provider_generates_code(self, provider):
        """**Validates: Requirements 3.6**

        Each provider MUST generate non-empty Strands agent code with
        BedrockAgentCoreApp and Strands imports.
        """
        config = _make_runtime_configuration(provider=provider)
        code = deployment_generate_agent_code(config)
        assert len(code) > 100, f"Provider {provider.value} generated too little code"
        assert "SYSTEM_PROMPT" in code
        assert "model_id=" in code or "MODEL_ID" in code
        assert "from bedrock_agentcore.runtime import BedrockAgentCoreApp" in code
        assert "from strands import Agent" in code


# ============================================================================
# _parse_response_body Preservation
# ============================================================================


class TestParseResponseBodyPreservation:
    """Preservation tests for _parse_response_body — valid input handling.

    **Validates: Requirements 3.1**
    """

    def test_parse_valid_json_with_response_key(self):
        """**Validates: Requirements 3.1**"""
        from app.deployment_handler import _parse_response_body

        body = json.dumps({"response": "Hello from the agent!"})
        result = _parse_response_body(body)
        assert result == "Hello from the agent!"

    def test_parse_valid_json_with_output_key(self):
        """**Validates: Requirements 3.1**"""
        from app.deployment_handler import _parse_response_body

        body = json.dumps({"output": "Agent output here"})
        result = _parse_response_body(body)
        assert result == "Agent output here"

    def test_parse_sse_stream_format(self):
        """**Validates: Requirements 3.1**"""
        from app.deployment_handler import _parse_response_body

        sse_body = 'data: {"partial": "chunk1"}\ndata: {"response": "final answer"}'
        result = _parse_response_body(sse_body)
        assert result == "final answer"

    def test_parse_plain_text_fallback(self):
        """**Validates: Requirements 3.1**"""
        from app.deployment_handler import _parse_response_body

        body = "This is just plain text from the agent."
        result = _parse_response_body(body)
        assert result == body

    def test_parse_empty_string(self):
        """**Validates: Requirements 3.1**"""
        from app.deployment_handler import _parse_response_body

        result = _parse_response_body("")
        assert result == ""

    @given(response_text=st.text(min_size=1, max_size=200))
    @settings(max_examples=5)
    def test_property_parse_json_response_key_extraction(self, response_text):
        """**Validates: Requirements 3.1**"""
        from app.deployment_handler import _parse_response_body

        body = json.dumps({"response": response_text})
        result = _parse_response_body(body)
        assert result == response_text


# ============================================================================
# Frontend Retry Logic Preservation
# ============================================================================


class TestFrontendRetryLogicPreservation:
    """Preservation tests for DeployPanel.tsx retry logic.

    **Validates: Requirements 3.7**
    """

    @pytest.fixture
    def deploy_panel_source(self):
        frontend_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "frontend",
            "src",
            "components",
            "deploy",
            "DeployPanel.tsx",
        )
        with open(frontend_path, "r") as f:
            return f.read()

    def _extract_handle_test(self, source: str) -> str:
        start = source.find("const handleTest = useCallback(async ()")
        assert start != -1, "Could not find handleTest in DeployPanel.tsx"
        section = source[start:]
        end = section.find("}, [deploymentStatus.endpoint")
        assert end != -1, "Could not find end of handleTest useCallback"
        return section[:end]

    def test_max_retries_is_five(self, deploy_panel_source):
        """**Validates: Requirements 3.7**"""
        body = self._extract_handle_test(deploy_panel_source)
        assert "MAX_RETRIES = 5" in body

    def test_retry_loop_structure(self, deploy_panel_source):
        """**Validates: Requirements 3.7**"""
        body = self._extract_handle_test(deploy_panel_source)
        assert "for (let attempt = 1; attempt <= MAX_RETRIES; attempt++)" in body

    def test_cold_start_detection_patterns(self, deploy_panel_source):
        """**Validates: Requirements 3.7**"""
        body = self._extract_handle_test(deploy_panel_source)
        expected_patterns = [
            "initialization time exceeded",
            "Runtime initialization",
            "cold start",
            "Read timeout",
            "read timeout",
            "timed out",
        ]
        for pattern in expected_patterns:
            assert pattern in body, f"Missing cold-start detection pattern: {pattern}"
