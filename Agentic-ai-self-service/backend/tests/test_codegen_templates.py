"""Tests for the canonical deployed-code template package (C1 refactor).

The built-in Gateway tool implementations (search / wikipedia / weather /
SSRF-guarded fetch, customer-support demo handlers, KB RAG Lambda) used to be
TRIPLICATED across gateway_deployer, code_generator, and
cfn_template_generator with drift. They now live once in
``app.services.codegen_templates`` and every consumer loads from there.

These tests pin the contract:
  (a) every template file in the package is syntax-valid Python;
  (b) the deployed Gateway Lambda keeps the hardened SSRF guard
      (``_FETCH_BLOCKED_NETS`` DNS-resolution denylist) and the structured
      ``tool_unavailable`` failure shape;
  (c) generated agent code embedding the tools still compiles;
  (d) the CFN export path ships the same canonical code.

Run:
    cd backend && python3 -m pytest tests/test_codegen_templates.py -q
"""

from __future__ import annotations

import io
import zipfile
from importlib import resources

import pytest
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services import codegen_templates
from app.services.cfn_template_generator import CfnTemplateGenerator
from app.services.code_generator import generate_agent_code

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _template_names() -> list[str]:
    files = resources.files("app.services.codegen_templates")
    return sorted(p.name[:-3] for p in files.iterdir() if p.name.endswith(".py") and p.name != "__init__.py")


def _make_config(**overrides) -> RuntimeConfig:
    defaults = {
        "name": "tmpl-test-agent",
        "framework": "strands_agents",
        "model": {"modelId": "us.anthropic.claude-sonnet-5"},
        "systemPrompt": "You are a helpful assistant.",
    }
    defaults.update(overrides)
    return RuntimeConfig(**defaults)


# ---------------------------------------------------------------------------
# (a) every template in the package compiles — both raw and rendered forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _template_names())
def test_template_file_compiles(name: str):
    src = codegen_templates.load(name)
    compile(src, f"<codegen_templates/{name}.py>", "exec")


@pytest.mark.parametrize("name", _template_names())
def test_rendered_template_compiles(name: str):
    """load_impl (docstring + template-only imports stripped) must stay valid."""
    src = codegen_templates.load_impl(name)
    assert src.strip(), f"{name}: rendered template is empty"
    compile(src, f"<rendered {name}>", "exec")


def test_composed_lambda_sources_compile():
    compile(codegen_templates.dynamic_tools_lambda_source(), "<dynamic_tools.py>", "exec")
    compile(codegen_templates.customer_support_tools_lambda_source(), "<customer_support_tools.py>", "exec")


# ---------------------------------------------------------------------------
# (b) gateway_deployer constants carry the hardened canonical behavior
# ---------------------------------------------------------------------------


def test_dynamic_tools_lambda_has_ssrf_guard_and_structured_errors():
    from app.services.gateway_deployer import DYNAMIC_TOOLS_LAMBDA_CODE

    # DNS-resolution SSRF guard (substring denylists are bypassable via rebinding)
    assert "_FETCH_BLOCKED_NETS" in DYNAMIC_TOOLS_LAMBDA_CODE
    assert "getaddrinfo" in DYNAMIC_TOOLS_LAMBDA_CODE
    # Structured tool-failure contract
    assert "ToolUnavailable" in DYNAMIC_TOOLS_LAMBDA_CODE
    assert "tool_unavailable" in DYNAMIC_TOOLS_LAMBDA_CODE
    compile(DYNAMIC_TOOLS_LAMBDA_CODE, "<DYNAMIC_TOOLS_LAMBDA_CODE>", "exec")


def test_gateway_deployer_constants_compile():
    from app.services.gateway_deployer import (
        CUSTOMER_SUPPORT_LAMBDA_CODE,
        KNOWLEDGE_BASE_LAMBDA_TEMPLATE,
    )

    compile(CUSTOMER_SUPPORT_LAMBDA_CODE, "<CUSTOMER_SUPPORT_LAMBDA_CODE>", "exec")
    compile(KNOWLEDGE_BASE_LAMBDA_TEMPLATE, "<KNOWLEDGE_BASE_LAMBDA_TEMPLATE>", "exec")
    # The KB Lambda's ingestion-eventual-consistency contract must survive.
    assert "still_ingesting" in KNOWLEDGE_BASE_LAMBDA_TEMPLATE


# ---------------------------------------------------------------------------
# (c) generated agent code embedding the canonical tools still compiles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_id", ["web-search-agent", "mcp-server-runtime"])
def test_generated_tool_agent_code_compiles(template_id: str):
    code = generate_agent_code(_make_config(), template_id=template_id)
    compile(code, f"<{template_id}>", "exec")
    # Canonical impl actually landed (marker replaced, hardened fetch present).
    assert "__TOOL_IMPL__" not in code
    assert "_FETCH_BLOCKED_NETS" in code
    assert "duckduckgo_search" in code


def test_web_search_agent_wires_canonical_handlers():
    code = generate_agent_code(_make_config(), template_id="web-search-agent")
    assert "_do_duckduckgo_search" in code
    assert "_do_weather" in code
    assert "_do_fetch_webpage" in code
    # exactly one _http_get definition (no duplicated helper)
    assert code.count("def _http_get") == 1


# ---------------------------------------------------------------------------
# (d) CFN export path ships the canonical code
# ---------------------------------------------------------------------------


def test_cfn_tool_lambda_zip_contains_canonical_code():
    req = DeployRequest(
        nodeId="node-1",
        config=_make_config(),
        connectedTools=["gateway"],
        gatewayTools=["duckduckgo_search", "web_page_fetcher", "get_order"],
    )
    bundle = CfnTemplateGenerator().generate(req)

    assert bundle.tool_lambda_code, "tools pattern must ship a tool-lambdas zip"
    with zipfile.ZipFile(io.BytesIO(bundle.tool_lambda_code)) as zf:
        names = set(zf.namelist())
        assert "dynamic_tools.py" in names
        assert "customer_support_tools.py" in names
        dynamic = zf.read("dynamic_tools.py").decode()
        customer = zf.read("customer_support_tools.py").decode()

    # The exported Lambda is the SAME hardened code the UI deploy path ships.
    assert "_FETCH_BLOCKED_NETS" in dynamic
    assert "tool_unavailable" in dynamic
    compile(dynamic, "<cfn dynamic_tools.py>", "exec")
    assert "_do_process_refund" in customer
    compile(customer, "<cfn customer_support_tools.py>", "exec")

    # Template handler wiring matches the shipped module's entrypoint name.
    assert "dynamic_tools.lambda_handler" in bundle.template_yaml
    assert "customer_support_tools.lambda_handler" in bundle.template_yaml


def test_cfn_kb_template_embeds_canonical_kb_lambda():
    req = DeployRequest(
        nodeId="node-1",
        config=_make_config(),
        connectedTools=["gateway"],
        knowledgeBaseConfig={"kbMode": "existing", "knowledgeBaseId": "KB123456"},
    )
    bundle = CfnTemplateGenerator().generate(req)

    # Canonical KB Lambda (with the retryable still_ingesting signal) is inline
    # in the template YAML...
    assert "still_ingesting" in bundle.template_yaml
    assert "retrieve_and_generate" in bundle.template_yaml
    # ...and stays under the CFN inline ZipFile 4096-byte cap.
    assert len(codegen_templates.load_impl("kb_lambda").encode()) <= 4096
