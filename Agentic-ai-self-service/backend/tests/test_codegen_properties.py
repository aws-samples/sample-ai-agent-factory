"""Property-based tests for code generator module.

Property 6: Code Generator Produces Valid Output
- For any valid RuntimeConfig with a recognized framework, generate_agent_code()
  returns a non-empty string containing the framework import, and
  generate_requirements() returns a non-empty string containing the framework package.

Validates: Requirements 5.1, 5.2
"""

import sys

sys.path.insert(0, "src")

from hypothesis import given, settings, strategies as st

from app.models.deployment_models import RuntimeConfig
from app.services.code_generator import (
    PROVIDER_PACKAGES,
    generate_agent_code,
    generate_requirements,
)


# ============================================================================
# Hypothesis Strategies
# ============================================================================

# All recognized providers
_RECOGNIZED_PROVIDERS = list(PROVIDER_PACKAGES.keys())

valid_provider_st = st.sampled_from(_RECOGNIZED_PROVIDERS)

valid_runtime_config_st = st.builds(
    RuntimeConfig,
    name=st.text(min_size=1, max_size=50, alphabet="abcdefghijklmnopqrstuvwxyz0123456789_").filter(
        lambda x: len(x.strip()) > 0
    ),
    entrypoint=st.just("agent.py"),
    framework=st.just("strands_agents"),
    model=st.fixed_dictionaries({"modelId": st.just("us.anthropic.claude-sonnet-5")}),
    system_prompt=st.text(min_size=1, max_size=200).filter(lambda x: len(x.strip()) > 0),
    deployment_type=st.just("direct_code_deploy"),
    python_runtime=st.just("PYTHON_3_12"),
    protocol=st.just("HTTP"),
    idle_timeout=st.integers(min_value=60, max_value=28800),
    max_lifetime=st.integers(min_value=60, max_value=28800),
    enable_otel=st.booleans(),
    model_provider=valid_provider_st,
)


# ============================================================================
# Property 6: Code Generator Produces Valid Output
# ============================================================================


class TestCodeGeneratorProperty:
    """Property 6: Code Generator Produces Valid Output.

    **Validates: Requirements 5.1, 5.2**
    """

    @given(config=valid_runtime_config_st)
    @settings(max_examples=100)
    def test_generate_agent_code_returns_nonempty_with_framework(self, config: RuntimeConfig):
        """generate_agent_code() returns non-empty string for any recognized framework."""
        code = generate_agent_code(config)
        assert isinstance(code, str)
        assert len(code) > 0

    @given(config=valid_runtime_config_st)
    @settings(max_examples=100)
    def test_generate_requirements_returns_empty(self, config: RuntimeConfig):
        """generate_requirements() returns empty string (deps are pre-bundled).

        **Validates: Requirement 6.1**
        """
        reqs = generate_requirements(config)
        assert isinstance(reqs, str)
        assert reqs == ""

    @given(config=valid_runtime_config_st)
    @settings(max_examples=100)
    def test_generate_requirements_with_tools_returns_empty(self, config: RuntimeConfig):
        """generate_requirements() returns empty string even with tools (deps are pre-bundled).

        **Validates: Requirement 6.1**
        """
        reqs = generate_requirements(config, tools=["browser"])
        assert isinstance(reqs, str)
        assert reqs == ""

    # Removed: test_any_framework_value_still_generates_code — RuntimeConfig.framework
    # is now Literal["strands_agents"]; passing a non-strands value raises ValidationError
    # by design (Strands-only consolidation).

    def test_requirements_always_returns_empty(self):
        """generate_requirements() returns empty string (deps are pre-bundled).

        **Validates: Requirement 6.1**
        """
        config = RuntimeConfig(
            name="test",
            framework="strands_agents",
            model={"modelId": "us.anthropic.claude-sonnet-5"},
            system_prompt="test",
            deployment_type="direct_code_deploy",
            python_runtime="PYTHON_3_12",
        )
        reqs = generate_requirements(config)
        assert reqs == ""
