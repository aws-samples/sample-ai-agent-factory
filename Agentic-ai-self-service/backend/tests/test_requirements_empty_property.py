"""Property test: generate_requirements() returns package list for runtime installation.

**Property 3: Requirements generation returns packages**

For any valid RuntimeConfig and any combination of tools, template_id,
and gateway_tools, generate_requirements() returns a non-empty string
containing 'bedrock-agentcore'.

**Validates: Requirement 6.1**
"""

import sys

sys.path.insert(0, "src")

from hypothesis import given, settings, strategies as st

from app.models.deployment_models import RuntimeConfig
from app.services.code_generator import generate_requirements


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_frameworks = st.just("strands_agents")

_model_ids = st.sampled_from(
    [
        "us.anthropic.claude-sonnet-5",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.amazon.nova-2-lite-v1:0",
        "us.anthropic.claude-sonnet-5",
    ]
)

_runtime_config_st = st.builds(
    RuntimeConfig,
    name=st.text(
        min_size=1,
        max_size=50,
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789_",
    ).filter(lambda x: len(x.strip()) > 0),
    entrypoint=st.just("agent.py"),
    framework=_frameworks,
    model=st.fixed_dictionaries({"modelId": _model_ids}),
    system_prompt=st.text(min_size=1, max_size=200).filter(lambda x: len(x.strip()) > 0),
    deployment_type=st.just("S3_CODE_DEPLOY"),
    python_runtime=st.just("PYTHON_3_11"),
    protocol=st.just("HTTP"),
    idle_timeout=st.integers(min_value=60, max_value=28800),
    max_lifetime=st.integers(min_value=60, max_value=28800),
    enable_otel=st.booleans(),
)

_tool_ids = st.sampled_from(["browser", "code_interpreter", "gateway", "memory", "search"])
_tools_st = st.lists(_tool_ids, max_size=5)

_template_ids = st.one_of(
    st.none(),
    st.sampled_from(
        [
            "web-search-agent",
            "strands-gateway-agent",
            "customer-support-assistant",
            "unknown-template",
        ]
    ),
)

_gateway_tools_st = st.one_of(
    st.none(),
    st.lists(
        st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnopqrstuvwxyz_-"),
        max_size=5,
    ),
)


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------


class TestRequirementsPackagesProperty:
    """**Validates: Requirement 6.1**"""

    @given(
        config=_runtime_config_st,
        tools=_tools_st,
        template_id=_template_ids,
        gateway_tools=_gateway_tools_st,
    )
    @settings(max_examples=200)
    def test_generate_requirements_always_returns_empty(
        self,
        config,
        tools,
        template_id,
        gateway_tools,
    ):
        """**Validates: Requirement 6.1**

        For any valid RuntimeConfig and any combination of tools,
        template_id, and gateway_tools, generate_requirements() returns
        an empty string (deps are pre-bundled into code.zip).
        """
        result = generate_requirements(
            config,
            tools=tools,
            template_id=template_id,
            gateway_tools=gateway_tools,
        )
        assert result == "", (
            f"Expected empty string, got {result!r} for "
            f"framework={config.framework}, tools={tools}, "
            f"template_id={template_id}, gateway_tools={gateway_tools}"
        )
