"""Comprehensive preservation property tests — MUST PASS on unfixed code.

These tests capture the baseline behavior of non-buggy paths that must be
preserved during the comprehensive platform fix. They cover:

- Step Functions pipeline code generation (code_generator.py)
- deployment.py generate_agent_code() SDK patterns
- System prompt escaping
- Templates 2 & 3 metadata
- Frontend retry logic structure
- Router endpoint patterns
- Gateway deployer boto3 usage
- codegen_step.py bundle resolution

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10**
"""

import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.deployment_models import RuntimeConfig
from app.models.enums import AgentFramework, ModelProvider
from app.models.components import RuntimeConfiguration, ModelConfiguration
from app.services import code_generator
from app.services.deployment import (
    generate_agent_code as deployment_generate_agent_code,
)
from app.services.code_generator import (
    generate_requirements as cg_generate_requirements,
)
from app.step_handlers.codegen_step import _needs_strands_bundle


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


def _make_runtime_configuration(framework: AgentFramework, **overrides) -> RuntimeConfiguration:
    """Build a minimal RuntimeConfiguration for deployment.py tests."""
    defaults = {
        "name": "test-agent",
        "framework": framework,
        "model": ModelConfiguration(
            provider=ModelProvider.ANTHROPIC,
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        ),
        "system_prompt": "You are a helpful assistant.",
    }
    defaults.update(overrides)
    return RuntimeConfiguration(**defaults)


# Strategies
_all_frameworks = st.sampled_from(list(AgentFramework))

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
    ]
)

_GATEWAY_CREDS = {
    "url": "https://example.com/gateway",
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "token_endpoint": "https://example.com/oauth2/token",
    "scope": "agentcore/*",
}

_GATEWAY_CONFIG = {
    "gateway_url": _GATEWAY_CREDS["url"],
    "client_info": {
        "client_id": _GATEWAY_CREDS["client_id"],
        "client_secret": _GATEWAY_CREDS["client_secret"],
        "token_endpoint": _GATEWAY_CREDS["token_endpoint"],
        "scope": _GATEWAY_CREDS["scope"],
    },
}


# ============================================================================
# Step Functions Pipeline Preservation (Req 3.1, 3.2, 3.9)
# ============================================================================


class TestCodeGeneratorPreservation:
    """code_generator.py generate_agent_code() produces correct BedrockAgentCoreApp
    SDK code for all template_id values and tool combinations.

    **Validates: Requirements 3.1, 3.2, 3.9**
    """

    _TEMPLATE_IDS = st.sampled_from(["web-search-agent", "strands-gateway-agent", "customer-support-assistant"])
    _TOOL_COMBOS = st.sampled_from(
        [
            [],
            ["browser"],
            ["code_interpreter"],
            ["browser", "code_interpreter"],
            ["gateway"],
        ]
    )

    @given(template_id=_TEMPLATE_IDS)
    @settings(max_examples=3)
    def test_property_template_code_has_sdk_pattern(self, template_id):
        """**Validates: Requirements 3.1, 3.2**

        For any template_id, generate_agent_code() MUST produce code with
        BedrockAgentCoreApp, @app.entrypoint, and app.run().
        """
        config = _make_runtime_config()
        code = code_generator.generate_agent_code(
            config,
            tools=[],
            gateway_config=_GATEWAY_CONFIG,
            template_id=template_id,
        )
        assert "BedrockAgentCoreApp" in code, f"Template {template_id} missing BedrockAgentCoreApp"
        assert "@app.entrypoint" in code, f"Template {template_id} missing @app.entrypoint"
        assert "app.run()" in code, f"Template {template_id} missing app.run()"

    @given(tools=_TOOL_COMBOS)
    @settings(max_examples=5)
    def test_property_tool_code_has_sdk_pattern(self, tools):
        """**Validates: Requirements 3.1**

        For any tool combination (no template), generate_agent_code() MUST
        produce code with BedrockAgentCoreApp SDK pattern.
        """
        config = _make_runtime_config()
        gw_config = _GATEWAY_CONFIG if "gateway" in tools else None
        code = code_generator.generate_agent_code(
            config,
            tools=tools,
            gateway_config=gw_config,
        )
        assert "BedrockAgentCoreApp" in code
        assert "@app.entrypoint" in code
        assert "app.run()" in code

    def test_langchain_web_search_template_content(self):
        """**Validates: Requirements 3.1**

        web-search-agent template MUST produce boto3 Converse API web search code.
        """
        config = _make_runtime_config()
        code = code_generator.generate_agent_code(config, template_id="web-search-agent")
        assert "duckduckgo_search" in code
        assert "fetch_webpage" in code
        assert "_converse_loop" in code

    # Removed: test_strands_gateway_template_content asserted the legacy
    # _mcp_request / _to_bedrock_tools JSON-RPC helpers. Migrated to the official
    # Strands MCPClient pattern (per tasks/lessons.md Bug 13: gateway agent must use
    # MCPClient + streamablehttp_client, not hand-rolled MCP helpers).

    def test_customer_support_template_content(self):
        """**Validates: Requirements 3.2**

        customer-support-assistant template MUST produce Strands + MCP support code.
        """
        config = _make_runtime_config()
        code = code_generator.generate_agent_code(
            config,
            gateway_config=_GATEWAY_CONFIG,
            template_id="customer-support-assistant",
        )
        assert "_mcp_request" in code or "MCP" in code


class TestCodeGeneratorRequirementsPreservation:
    """code_generator.py generate_requirements() returns empty string (deps pre-bundled).

    **Validates: Requirements 3.1**
    """

    @pytest.mark.parametrize(
        "template_id",
        [
            "web-search-agent",
            "strands-gateway-agent",
            "customer-support-assistant",
            None,
        ],
    )
    def test_requirements_returns_empty_for_all_templates(self, template_id):
        """**Validates: Requirements 3.1**

        generate_requirements() MUST return empty string (deps are pre-bundled).
        """
        config = _make_runtime_config()
        result = code_generator.generate_requirements(config, template_id=template_id)
        assert result == "", f"Expected empty string for template_id={template_id}, got {result!r}"

    @pytest.mark.parametrize("tools", [[], ["browser"], ["code_interpreter"], ["gateway"]])
    def test_requirements_returns_empty_for_all_tools(self, tools):
        """**Validates: Requirements 3.1**

        generate_requirements() MUST return empty string (deps are pre-bundled).
        """
        config = _make_runtime_config()
        result = code_generator.generate_requirements(config, tools=tools)
        assert result == "", f"Expected empty string for tools={tools}, got {result!r}"


class TestBundleAndRequirementsPreservation:
    """codegen_step.py _needs_strands_bundle() and generate_requirements().

    **Validates: Requirements 3.9**
    """

    def test_boto3_only_code_uses_base_bundle(self):
        """**Validates: Requirements 3.9**"""
        code = "import boto3\nfrom bedrock_agentcore.runtime import BedrockAgentCoreApp"
        assert _needs_strands_bundle(code) is False

    def test_strands_code_uses_strands_bundle(self):
        """**Validates: Requirements 3.9**"""
        code = "from strands import Agent\nfrom bedrock_agentcore.runtime import BedrockAgentCoreApp"
        assert _needs_strands_bundle(code) is True

    def test_strands_models_import_detected(self):
        """**Validates: Requirements 3.9**"""
        code = "from strands.models import BedrockModel\nimport strands"
        assert _needs_strands_bundle(code) is True

    @pytest.mark.parametrize(
        "template_id",
        [
            "web-search-agent",
            "strands-gateway-agent",
            "customer-support-assistant",
            "mcp-server-runtime",
        ],
    )
    def test_requirements_empty_for_known_templates(self, template_id):
        """**Validates: Requirements 3.9** — deps are pre-bundled, not in requirements.txt"""
        config = _make_runtime_config()
        result = cg_generate_requirements(config, template_id=template_id)
        assert result == ""

    @pytest.mark.parametrize(
        "tools",
        [
            [],
            ["gateway"],
            ["browser"],
            ["code_interpreter"],
            ["browser", "code_interpreter"],
        ],
    )
    def test_requirements_empty_for_tools(self, tools):
        """**Validates: Requirements 3.9** — deps are pre-bundled, not in requirements.txt"""
        config = _make_runtime_config()
        result = cg_generate_requirements(config, tools=tools)
        assert result == ""

    def test_gateway_tools_requirements_empty(self):
        """**Validates: Requirements 3.9** — deps are pre-bundled, not in requirements.txt"""
        config = _make_runtime_config()
        result = cg_generate_requirements(config, tools=["gateway"])
        assert result == ""

    @given(
        template_id=st.sampled_from(
            [
                "web-search-agent",
                "strands-gateway-agent",
                "customer-support-assistant",
                "mcp-server-runtime",
            ]
        ),
        tools=st.lists(
            st.sampled_from(["gateway", "browser", "code_interpreter", "memory"]),
            max_size=3,
        ),
    )
    @settings(max_examples=10)
    def test_property_all_templates_requirements_empty(self, template_id, tools):
        """**Validates: Requirements 3.9** — deps are pre-bundled, not in requirements.txt"""
        config = _make_runtime_config()
        result = cg_generate_requirements(config, tools=tools, template_id=template_id)
        assert result == ""


# ============================================================================
# deployment.py generate_agent_code() Preservation (Req 3.3, 3.10)
# ============================================================================


class TestDeploymentGenerateAgentCodePreservation:
    """For all 9 frameworks, deployment.py generate_agent_code() produces code
    containing BedrockAgentCoreApp, @app.entrypoint, lazy-loaded imports, and app.run().

    **Validates: Requirements 3.3, 3.10**
    """

    @given(framework=_all_frameworks)
    @settings(max_examples=9)
    def test_property_all_frameworks_have_sdk_pattern(self, framework):
        """**Validates: Requirements 3.3**

        For ANY framework, deployment.py generate_agent_code() MUST produce code
        with BedrockAgentCoreApp SDK pattern.
        """
        config = _make_runtime_configuration(framework)
        code = deployment_generate_agent_code(config)
        assert "BedrockAgentCoreApp" in code, f"{framework.value} missing BedrockAgentCoreApp"
        assert "@app.entrypoint" in code, f"{framework.value} missing @app.entrypoint"
        assert "app.run()" in code, f"{framework.value} missing app.run()"

    @given(framework=_all_frameworks)
    @settings(max_examples=9)
    def test_property_all_frameworks_have_strands_imports(self, framework):
        """**Validates: Requirements 3.3**

        For Strands-only, generated code MUST import strands at top level
        (single framework — no lazy-loading needed).
        """
        config = _make_runtime_configuration(framework)
        code = deployment_generate_agent_code(config)
        lines = code.split("\n")
        top_level_imports = [
            line.strip()
            for line in lines
            if line.strip().startswith(("import ", "from ")) and not line.startswith(" ") and not line.startswith("\t")
        ]
        # Strands import must be present at top level
        has_strands = any("strands" in imp for imp in top_level_imports)
        assert has_strands, f"Framework {framework.value} missing strands import"
        # Old frameworks must NOT appear
        for imp in top_level_imports:
            assert not any(
                fw_pkg in imp
                for fw_pkg in [
                    "langgraph",
                    "langchain",
                    "crewai",
                    "llama_index",
                ]
            ), f"Framework {framework.value} has unexpected import: {imp}"

    @given(framework=_all_frameworks, model_id=_model_ids)
    @settings(max_examples=5)
    def test_property_all_frameworks_embed_model_id(self, framework, model_id):
        """**Validates: Requirements 3.3**

        For ANY framework, generated code MUST embed the model ID.
        """
        config = _make_runtime_configuration(
            framework,
            model=ModelConfiguration(provider=ModelProvider.ANTHROPIC, model_id=model_id),
        )
        code = deployment_generate_agent_code(config)
        assert model_id in code

    # Removed: test_property_all_frameworks_have_invoke_handler asserted the legacy
    # `async def invoke(payload, context):` signature. The current
    # BedrockAgentCoreApp pattern uses synchronous `def invoke(payload):` decorated
    # with @app.entrypoint (per amazon-bedrock-agentcore-samples).

    @given(framework=_all_frameworks)
    @settings(max_examples=9)
    def test_property_all_frameworks_return_response(self, framework):
        """**Validates: Requirements 3.3**

        For ANY framework, generated code MUST return a dict with "response" key.
        """
        config = _make_runtime_configuration(framework)
        code = deployment_generate_agent_code(config)
        assert '"response"' in code


# ============================================================================
# System Prompt Escaping Preservation (Req 3.10)
# ============================================================================


class TestSystemPromptEscapingPreservation:
    """_escape_triple_quotes() handles special characters correctly in both
    code generation paths.

    **Validates: Requirements 3.10**
    """

    def test_escape_triple_quotes_basic(self):
        """**Validates: Requirements 3.10**

        _escape_triple_quotes MUST replace triple double-quotes.
        """
        result = code_generator._escape_triple_quotes('Hello """world"""')
        assert '"""' not in result
        assert '\\"\\"\\"' in result

    def test_escape_triple_quotes_no_change_for_safe_text(self):
        """**Validates: Requirements 3.10**

        _escape_triple_quotes MUST not alter text without triple quotes.
        """
        text = "Hello world, this is safe."
        assert code_generator._escape_triple_quotes(text) == text

    def test_escape_triple_quotes_empty_string(self):
        """**Validates: Requirements 3.10**

        _escape_triple_quotes MUST handle empty string.
        """
        assert code_generator._escape_triple_quotes("") == ""

    @given(system_prompt=_safe_text)
    @settings(max_examples=10)
    def test_property_code_generator_produces_valid_python(self, system_prompt):
        """**Validates: Requirements 3.10**

        For ANY safe system prompt, code_generator._generate_default_agent()
        MUST produce syntactically valid Python.
        """
        escaped = code_generator._escape_triple_quotes(system_prompt)
        code = code_generator._generate_default_agent(
            escaped,
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "us-east-1",
        )
        try:
            compile(code, "<test>", "exec")
        except SyntaxError as e:
            pytest.fail(f"Syntax error for prompt '{system_prompt[:50]}': {e}")

    @given(framework=_all_frameworks, system_prompt=_safe_text)
    @settings(max_examples=10)
    def test_property_deployment_produces_valid_python(self, framework, system_prompt):
        """**Validates: Requirements 3.10**

        For ANY framework and safe system prompt, deployment.py generate_agent_code()
        MUST produce syntactically valid Python.
        """
        config = _make_runtime_configuration(framework, system_prompt=system_prompt)
        code = deployment_generate_agent_code(config)
        try:
            compile(code, "<test>", "exec")
        except SyntaxError as e:
            pytest.fail(f"Syntax error for {framework.value}, prompt '{system_prompt[:50]}': {e}")


# ============================================================================
# Templates 2 & 3 Preservation (Req 3.8)
# ============================================================================


class TestTemplates2And3Preservation:
    """strands-gateway-agent and customer-support-assistant metadata, IDs,
    framework values unchanged.

    **Validates: Requirements 3.8**
    """

    def _read_templates_ts(self) -> str:
        """Read the templates.ts file content."""
        ts_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "frontend",
            "src",
            "data",
            "templates.ts",
        )
        with open(ts_path, "r") as f:
            return f.read()

    def test_template2_id_is_strands_gateway_agent(self):
        """**Validates: Requirements 3.8**

        Template 2 MUST have id 'strands-gateway-agent'.
        """
        content = self._read_templates_ts()
        assert "'strands-gateway-agent'" in content

    def test_template2_framework_is_strands_agents(self):
        """**Validates: Requirements 3.8**

        Template 2 runtime MUST have framework 'strands_agents'.
        """
        content = self._read_templates_ts()
        # Find the strands-gateway-agent section and verify framework
        idx = content.index("'strands-gateway-agent'")
        # Look for framework within the next ~50 lines of that template
        section = content[idx : idx + 2000]
        assert "framework: 'strands_agents'" in section

    def test_template3_id_is_customer_support_assistant(self):
        """**Validates: Requirements 3.8**

        Template 3 MUST have id 'customer-support-assistant'.
        """
        content = self._read_templates_ts()
        assert "'customer-support-assistant'" in content

    def test_template3_framework_is_strands_agents(self):
        """**Validates: Requirements 3.8**

        Template 3 runtime MUST have framework 'strands_agents'.
        """
        content = self._read_templates_ts()
        idx = content.index("'customer-support-assistant'")
        section = content[idx : idx + 2000]
        assert "framework: 'strands_agents'" in section

    def test_template2_has_gateway_component(self):
        """**Validates: Requirements 3.8**

        Template 2 MUST include a gateway node.
        """
        content = self._read_templates_ts()
        idx = content.index("'strands-gateway-agent'")
        # Find the next template boundary or end
        next_template = content.find("id: '", idx + 30)
        section = content[idx:next_template] if next_template > 0 else content[idx:]
        assert "type: 'gateway'" in section

    def test_template3_has_memory_component(self):
        """**Validates: Requirements 3.8**

        Template 3 MUST include a memory node.
        """
        content = self._read_templates_ts()
        idx = content.index("'customer-support-assistant'")
        section = content[idx:]
        assert "type: 'memory'" in section


# ============================================================================
# Frontend Retry Logic Preservation (Req 3.4)
# ============================================================================


class TestFrontendRetryLogicPreservation:
    """DeployPanel.tsx has 5 retries with increasing delays, isTesting reset
    in finally block.

    **Validates: Requirements 3.4**
    """

    def _read_deploy_panel(self) -> str:
        """Read the DeployPanel.tsx file content."""
        tsx_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "frontend",
            "src",
            "components",
            "deploy",
            "DeployPanel.tsx",
        )
        with open(tsx_path, "r") as f:
            return f.read()

    def test_max_retries_is_5(self):
        """**Validates: Requirements 3.4**

        MAX_RETRIES MUST be set to 5.
        """
        content = self._read_deploy_panel()
        assert "const MAX_RETRIES = 5" in content

    def test_retry_loop_uses_max_retries(self):
        """**Validates: Requirements 3.4**

        The retry loop MUST iterate up to MAX_RETRIES.
        """
        content = self._read_deploy_panel()
        assert "attempt <= MAX_RETRIES" in content

    def test_increasing_delay_pattern(self):
        """**Validates: Requirements 3.4**

        Retry delays MUST increase with each attempt.
        """
        content = self._read_deploy_panel()
        # The pattern: 5000 + (attempt - 2) * 5000
        assert "5000 + (attempt - 2) * 5000" in content

    def test_is_testing_reset_in_finally(self):
        """**Validates: Requirements 3.4**

        isTesting MUST be reset to false in the finally block of handleTest.
        """
        content = self._read_deploy_panel()
        # handleTest sets isTesting(true) then must reset it in its own finally.
        # Locate the handleTest function body and check its finally block.
        handle_test_idx = content.index("const handleTest = useCallback")
        handle_test_section = content[handle_test_idx:]
        finally_idx = handle_test_section.index("} finally {")
        after_finally = handle_test_section[finally_idx : finally_idx + 200]
        assert "setIsTesting(false)" in after_finally

    def test_is_testing_set_true_at_start(self):
        """**Validates: Requirements 3.4**

        isTesting MUST be set to true at the start of handleTest.
        """
        content = self._read_deploy_panel()
        assert "setIsTesting(true)" in content


# Removed TestRouterEndpointPreservation: the dead routers/deployment.py file
# this class read was deleted. /api/test-runtime + /api/runtime/{id} are owned
# by the Deployment Lambda's deployment_handler.py now. See tasks/lessons.md Bug 31.


# ============================================================================
# Gateway Deployer Preservation (Req 3.7)
# ============================================================================


class TestGatewayDeployerPreservation:
    """WorkflowExecutor._deploy_gateway() uses boto3 for gateway operations.

    **Validates: Requirements 3.7**
    """

    def _read_deployment_source(self) -> str:
        """Read the services/deployment.py source."""
        path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "src",
            "app",
            "services",
            "deployment.py",
        )
        with open(path, "r") as f:
            return f.read()

    def test_deploy_gateway_uses_boto3_deployer(self):
        """**Validates: Requirements 3.7**

        deploy() MUST use gateway_deployer.deploy_gateway (boto3) for gateway operations.
        """
        source = self._read_deployment_source()
        # Updated: import is now multi-line with an alias to avoid shadowing the
        # generator helper. Match either form (single-line or split across lines).
        assert (
            "from app.services.gateway_deployer import deploy_gateway" in source
            or "from app.services.gateway_deployer import (" in source
            and "deploy_gateway" in source
        )

    def test_deploy_gateway_injects_env_vars(self):
        """**Validates: Requirements 3.7**

        deploy() MUST inject gateway credentials into runtime env vars.
        """
        source = self._read_deployment_source()
        assert 'env_vars["GATEWAY_URL"]' in source
        assert 'env_vars["COGNITO_CLIENT_ID"]' in source

    def test_deploy_gateway_before_runtime(self):
        """**Validates: Requirements 3.7**

        deploy() MUST deploy Gateway BEFORE generating agent code and Runtime.
        """
        source = self._read_deployment_source()
        gw_idx = source.index("Phase 1: Deploy Gateway FIRST")
        code_idx = source.index("Phase 2: Generate agent code")
        runtime_idx = source.index("Phase 4: Create IAM role and Runtime")
        assert gw_idx < code_idx < runtime_idx

    def test_rollback_gateway_uses_boto3(self):
        """**Validates: Requirements 3.7**

        rollback() gateway cleanup MUST use boto3 (list_gateways, delete_gateway_target,
        delete_gateway).
        """
        source = self._read_deployment_source()
        func_idx = source.index("async def rollback")
        # Use a larger window to capture the full rollback method
        func_section = source[func_idx : func_idx + 3500]
        assert "list_gateways()" in func_section
        assert "delete_gateway_target(" in func_section
        assert "delete_gateway(" in func_section
