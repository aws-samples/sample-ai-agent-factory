"""Regression: guardrailConfig must reach boto3 ``converse()`` templates as a
VALID single-brace dict splat — never literal ``{{`` double braces.

Bug (brace doubling): the converse host generators
(``_generate_langchain_web_search``, ``_generate_mcp_server_runtime``,
``_generate_default_agent``) are f-strings, so their ``{{``/``}}`` already
collapse to single braces in the RETURNED code. ``_inject_guardrails`` then
splices ``guardrailConfig`` via a plain ``str.replace`` — NOT ``.format`` — so a
double-braced replacement string lands LITERAL ``{{...}}`` in the deployed file.
Python parses ``{{...}}`` as a *set literal containing a dict*, which raises
``TypeError: unhashable type: 'dict'`` the moment the guarded converse template
runs with a guardrail configured. The converse path was previously uncovered,
which is why the brace bug slipped through.

These tests RENDER the converse templates with a guardrail and assert: (a) the
output compiles and carries NO literal ``{{``; (b) it contains the valid
``**({"guardrailConfig": {...}} if GUARDRAIL_ID else {})`` splat; (c) a
no-guardrail render is unchanged; (d) injection is idempotent.

Run:
    cd backend && python3 -m pytest tests/test_guardrail_converse_wiring.py -q
"""

from __future__ import annotations

import ast
import sys

import pytest

sys.path.insert(0, "src")

from app.models.deployment_models import RuntimeConfig  # noqa: E402
from app.services.code_generator import (  # noqa: E402
    _generate_default_agent,
    _inject_guardrails,
    generate_agent_code,
)

# The exact, valid single-brace splat that MUST appear in the deployed code.
_VALID_SPLAT = (
    '**({"guardrailConfig": {"guardrailIdentifier": GUARDRAIL_ID, '
    '"guardrailVersion": GUARDRAIL_VERSION}} if GUARDRAIL_ID else {})'
)

# Converse-based templates routed through generate_agent_code().
_CONVERSE_TEMPLATES = ["web-search-agent", "mcp-server-runtime"]


def _cfg() -> RuntimeConfig:
    return RuntimeConfig.model_validate(
        {
            "name": "cvgr00001",
            "model": {
                "provider": "bedrock",
                "modelId": "us.anthropic.claude-sonnet-5",
            },
            "systemPrompt": "You are a search agent.",
        }
    )


@pytest.mark.parametrize("template_id", _CONVERSE_TEMPLATES)
def test_converse_template_emits_valid_single_brace_splat(template_id):
    """A guardrail render of a converse template must compile, must NOT contain
    literal ``{{`` (the set-of-dict crash), and must carry the valid splat."""
    code = generate_agent_code(_cfg(), tools=["guardrails"], template_id=template_id)

    # (a) No literal OPENING double braces (the set-of-dict crash signature),
    # and the code compiles. NOTE: ``}}`` legitimately appears in the valid
    # splat (two dicts closing back-to-back), so we only forbid ``{{``.
    assert "{{" not in code, (
        f"{template_id}: literal '{{{{' double braces landed in deployed code — "
        "Python would parse this as a set of an unhashable dict and crash"
    )
    ast.parse(code)
    compile(code, f"<{template_id}>", "exec")

    # (b) The valid single-brace guardrailConfig splat is present, exactly once.
    assert _VALID_SPLAT in code, f"{template_id}: valid guardrailConfig splat missing"
    assert code.count("guardrailConfig") == 1


@pytest.mark.parametrize("template_id", _CONVERSE_TEMPLATES)
def test_converse_template_without_guardrail_is_unchanged(template_id):
    """A no-guardrail render must carry no guardrail wiring at all."""
    code = generate_agent_code(_cfg(), template_id=template_id)
    assert "guardrailConfig" not in code
    assert "GUARDRAIL_ID" not in code
    assert "{{" not in code
    ast.parse(code)


@pytest.mark.parametrize("template_id", _CONVERSE_TEMPLATES)
def test_converse_injection_is_idempotent(template_id):
    """Re-running _inject_guardrails must not double-inject the env block or the
    guardrailConfig splat."""
    once = generate_agent_code(_cfg(), tools=["guardrails"], template_id=template_id)
    twice = _inject_guardrails(once)
    assert twice.count("guardrailConfig") == 1
    assert twice.count('GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID"') == 1
    assert "{{" not in twice
    ast.parse(twice)


def test_no_tool_converse_template_also_wires_guardrail():
    """The lightweight no-tools converse template has no toolConfig anchor; it
    must still wire guardrails via its inferenceConfig line (low-risk coverage
    gap), and the result must compile with single braces."""
    code = _inject_guardrails(_generate_default_agent("You are an agent.", "model-id", "us-east-1"))
    assert "{{" not in code
    assert _VALID_SPLAT in code
    assert code.count("guardrailConfig") == 1
    ast.parse(code)


def test_no_tool_converse_template_without_guardrail_is_unchanged():
    """The bare (un-injected) no-tools template carries no guardrail wiring."""
    code = _generate_default_agent("You are an agent.", "model-id", "us-east-1")
    assert "guardrailConfig" not in code
    assert "GUARDRAIL_ID" not in code
    assert "{{" not in code
    ast.parse(code)
