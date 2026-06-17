"""Bug condition exploration tests for AgentCore SDK migration.

These tests encode the EXPECTED (correct) behavior: all templates should use
BedrockAgentCoreApp SDK instead of raw http.server.

Updated for Strands-only framework with multi-provider support.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6**
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.deployment_models import RuntimeConfig
from app.models.components import RuntimeConfiguration, ModelConfiguration
from app.models.enums import StrandsModelProvider
from app.services import code_generator
from app.services import deployment as legacy_deployment


# ---------------------------------------------------------------------------
# Helpers — build minimal valid configs for each code path
# ---------------------------------------------------------------------------


def _make_runtime_config(**overrides) -> RuntimeConfig:
    """Build a minimal RuntimeConfig for code_generator.py tests."""
    defaults = {
        "name": "test-agent",
        "framework": "strands_agents",
        "model": {
            "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "provider": "bedrock",
        },
        "systemPrompt": "You are a helpful assistant.",
        "modelProvider": "bedrock",
    }
    defaults.update(overrides)
    return RuntimeConfig(**defaults)


def _make_runtime_configuration(
    provider: StrandsModelProvider = StrandsModelProvider.BEDROCK,
) -> RuntimeConfiguration:
    """Build a minimal RuntimeConfiguration for deployment.py tests."""
    return RuntimeConfiguration(
        name="test-agent",
        model=ModelConfiguration(provider=provider, model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        system_prompt="You are a helpful assistant.",
        model_provider=provider,
    )


_GATEWAY_CREDS = {
    "url": "https://example.com/gateway",
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "token_endpoint": "https://example.com/oauth2/token",
    "scope": "agentcore/*",
}


# ---------------------------------------------------------------------------
# SDK pattern assertions — reusable for all templates
# ---------------------------------------------------------------------------


def _assert_sdk_pattern_present(code: str, template_name: str):
    """Assert that generated code uses BedrockAgentCoreApp SDK pattern."""
    assert "from bedrock_agentcore.runtime import BedrockAgentCoreApp" in code, (
        f"{template_name}: missing 'from bedrock_agentcore.runtime import BedrockAgentCoreApp'"
    )
    assert "app = BedrockAgentCoreApp()" in code, f"{template_name}: missing 'app = BedrockAgentCoreApp()'"
    assert "@app.entrypoint" in code, f"{template_name}: missing '@app.entrypoint' decorator"
    assert 'if __name__ == "__main__"' in code, f"{template_name}: missing 'if __name__ == \"__main__\"' guard"
    assert "app.run()" in code, f"{template_name}: missing 'app.run()'"


def _assert_raw_http_absent(code: str, template_name: str):
    """Assert that generated code does NOT contain raw http.server patterns."""
    assert "import http.server" not in code, f"{template_name}: still contains 'import http.server'"
    assert "class _Handler" not in code, f"{template_name}: still contains 'class _Handler'"
    assert "HTTPServer" not in code, f"{template_name}: still contains 'HTTPServer'"
    assert "serve_forever()" not in code, f"{template_name}: still contains 'serve_forever()'"


# ============================================================================
# code_generator.py — All Template Functions (Expected Behavior Tests)
# ============================================================================


class TestCodeGeneratorSDKPattern:
    """Tests that all code_generator.py templates use BedrockAgentCoreApp."""

    def test_langchain_web_search_uses_sdk(self):
        code = code_generator._generate_langchain_web_search(
            system_prompt="You are a web search assistant.",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            region="us-west-2",
        )
        _assert_sdk_pattern_present(code, "langchain_web_search")
        _assert_raw_http_absent(code, "langchain_web_search")

    def test_strands_gateway_uses_sdk(self):
        code = code_generator._generate_strands_gateway(
            system_prompt="You are a gateway agent.",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            creds=_GATEWAY_CREDS,
        )
        _assert_sdk_pattern_present(code, "strands_gateway")
        _assert_raw_http_absent(code, "strands_gateway")

    def test_default_agent_uses_sdk(self):
        code = code_generator._generate_default_agent(
            system_prompt="You are helpful.",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            region="us-west-2",
        )
        _assert_sdk_pattern_present(code, "default_agent")
        _assert_raw_http_absent(code, "default_agent")

    def test_strands_default_uses_sdk(self):
        code = code_generator._generate_strands_default(
            system_prompt="You are helpful.",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            region="us-west-2",
            provider="bedrock",
        )
        _assert_sdk_pattern_present(code, "strands_default")
        _assert_raw_http_absent(code, "strands_default")
        assert "from strands import Agent" in code
        assert "BedrockModel" in code

    def test_strands_default_openai_provider(self):
        code = code_generator._generate_strands_default(
            system_prompt="You are helpful.",
            model_id="gpt-4o",
            region="us-west-2",
            provider="openai",
        )
        _assert_sdk_pattern_present(code, "strands_default_openai")
        assert "OpenAIModel" in code


# ============================================================================
# code_generator.py — Requirements Include bedrock-agentcore
# ============================================================================


class TestCodeGeneratorRequirementsSDK:
    """Tests that code_generator.py generate_requirements() returns empty string (deps pre-bundled)."""

    @pytest.mark.parametrize(
        "template_id,tools,description",
        [
            ("web-search-agent", [], "web-search-agent template"),
            ("strands-gateway-agent", [], "strands-gateway-agent template"),
            ("customer-support-assistant", [], "customer-support template"),
            (None, ["gateway"], "gateway agent"),
            (None, ["browser"], "tools agent (browser)"),
            (None, ["code_interpreter"], "tools agent (code_interpreter)"),
            (None, [], "default agent"),
        ],
    )
    def test_requirements_returns_empty(self, template_id, tools, description):
        config = _make_runtime_config()
        reqs = code_generator.generate_requirements(
            config,
            tools=tools,
            template_id=template_id,
        )
        assert reqs == "", (
            f"{description}: generate_requirements() should return empty string (deps pre-bundled), got '{reqs}'"
        )


# ============================================================================
# deployment.py — Strands Agent (Expected Behavior Tests)
# ============================================================================


class TestDeploymentSDKPattern:
    """Tests that deployment.py generates Strands agent code."""

    def test_deployment_strands_uses_sdk(self):
        config = _make_runtime_configuration()
        code = legacy_deployment.generate_agent_code(config)
        _assert_sdk_pattern_present(code, "deployment.py/strands")
        _assert_raw_http_absent(code, "deployment.py/strands")
        assert "from strands import Agent" in code

    @pytest.mark.parametrize(
        "provider",
        [
            StrandsModelProvider.BEDROCK,
            StrandsModelProvider.OPENAI,
            StrandsModelProvider.ANTHROPIC,
        ],
    )
    def test_deployment_provider_specific_code(self, provider):
        config = _make_runtime_configuration(provider)
        code = legacy_deployment.generate_agent_code(config)
        _assert_sdk_pattern_present(code, f"deployment.py/{provider.value}")
        assert "from strands import Agent" in code


# ============================================================================
# deployment.py — Requirements
# ============================================================================


class TestDeploymentRequirementsSDK:
    """Tests that deployment.py generate_requirements() returns empty string (deps pre-bundled)."""

    def test_deployment_requirements_empty(self):
        config = _make_runtime_configuration()
        reqs = legacy_deployment.generate_requirements(config)
        assert reqs == ""


# ============================================================================
# Property-Based Exploration Tests (Hypothesis)
# ============================================================================


class TestExplorationProperties:
    """Property-based tests exploring the bug across random inputs."""

    @given(
        system_prompt=st.text(min_size=1, max_size=200),
        model_id=st.sampled_from(
            [
                "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "us.amazon.nova-2-lite-v1:0",
            ]
        ),
    )
    @settings(max_examples=3)
    def test_property_code_generator_default_uses_sdk(self, system_prompt, model_id):
        code = code_generator._generate_default_agent(
            system_prompt=system_prompt,
            model_id=model_id,
            region="us-west-2",
        )
        assert "from bedrock_agentcore.runtime import BedrockAgentCoreApp" in code
        assert "@app.entrypoint" in code
        assert "app.run()" in code
        assert "import http.server" not in code

    @given(
        provider=st.sampled_from([p for p in StrandsModelProvider]),
    )
    @settings(max_examples=5)
    def test_property_deployment_all_providers_use_sdk(self, provider):
        config = _make_runtime_configuration(provider)
        code = legacy_deployment.generate_agent_code(config)
        assert "from bedrock_agentcore.runtime import BedrockAgentCoreApp" in code
        assert "@app.entrypoint" in code
        assert "app.run()" in code
        assert "import http.server" not in code
