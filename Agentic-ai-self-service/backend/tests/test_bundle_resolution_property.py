"""Property tests for bundle resolution and generate_requirements().

Tests cover correctness properties:

- Property 1: _needs_strands_bundle correctly detects strands imports
- Property 2: Requirements always return empty (deps are pre-bundled)
- Property 3: Generated code gets the right bundle

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**
"""

import sys

sys.path.insert(0, "src")

from app.models.deployment_models import RuntimeConfig
from app.services.code_generator import generate_requirements
from app.step_handlers.codegen_step import _needs_strands_bundle
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

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
    framework=st.just("strands_agents"),
    model=st.fixed_dictionaries({"modelId": _model_ids}),
    system_prompt=st.text(min_size=1, max_size=200).filter(lambda x: len(x.strip()) > 0),
)

_any_template_id = st.one_of(
    st.none(),
    st.sampled_from(
        [
            "web-search-agent",
            "strands-gateway-agent",
            "mcp-server-runtime",
            "customer-support-assistant",
            "unknown-template",
        ]
    ),
)

_any_tools_list = st.lists(
    st.text(min_size=1, max_size=50, alphabet="abcdefghijklmnopqrstuvwxyz_"),
    max_size=10,
)


# ---------------------------------------------------------------------------
# Property 1: Bundle detection based on code content
# ---------------------------------------------------------------------------


class TestBundleDetection:
    """**Validates: Requirement 3.5**

    _needs_strands_bundle correctly detects strands imports in generated code.
    """

    def test_strands_import_detected(self):
        code = "from strands import Agent\napp = BedrockAgentCoreApp()"
        assert _needs_strands_bundle(code) is True

    def test_strands_models_import_detected(self):
        code = "from strands.models import BedrockModel\nfrom strands import Agent"
        assert _needs_strands_bundle(code) is True

    def test_boto3_only_not_detected(self):
        code = "import boto3\nfrom bedrock_agentcore.runtime import BedrockAgentCoreApp"
        assert _needs_strands_bundle(code) is False

    def test_gateway_agent_code_not_detected(self):
        code = """from bedrock_agentcore.runtime import BedrockAgentCoreApp
import boto3
import json
import os
import uuid
import urllib.request
import urllib.parse

app = BedrockAgentCoreApp()
"""
        assert _needs_strands_bundle(code) is False

    def test_web_search_agent_not_detected(self):
        code = """from bedrock_agentcore.runtime import BedrockAgentCoreApp
import boto3
import json
"""
        assert _needs_strands_bundle(code) is False


# ---------------------------------------------------------------------------
# Property 2: Requirements always contain bedrock-agentcore
# ---------------------------------------------------------------------------


class TestRequirementsBasePackageInvariant:
    """**Validates: Requirement 3.5** — deps are pre-bundled, not in requirements.txt"""

    @given(
        config=_runtime_config_st,
        template_id=_any_template_id,
        tools=_any_tools_list,
    )
    @settings(max_examples=200)
    def test_requirements_always_empty(self, config, template_id, tools):
        result = generate_requirements(config, tools=tools, template_id=template_id)
        assert result == ""


# ---------------------------------------------------------------------------
# Property 3: Tool-based requirements (deps pre-bundled)
# ---------------------------------------------------------------------------


class TestToolBasedRequirements:
    """**Validates: Requirements 3.2, 3.3, 3.4** — deps are pre-bundled"""

    @given(config=_runtime_config_st, tools=_any_tools_list)
    @settings(max_examples=200)
    def test_always_returns_empty(self, config, tools):
        result = generate_requirements(config, tools=tools)
        assert result == ""
