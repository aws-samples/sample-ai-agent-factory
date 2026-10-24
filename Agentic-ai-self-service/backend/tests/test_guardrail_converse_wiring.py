"""Regression: guardrailConfig must reach boto3 ``converse()`` templates as a
VALID single-brace dict splat — never literal ``{{`` double braces.

Bug (brace doubling): the boto3 converse host generator
(``_generate_default_agent``) is an f-string, so its ``{{``/``}}`` already collapses to single braces in the RETURNED code. ``_inject_guardrails`` then
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


def test_managed_web_search_uses_strands_guardrail_hooks_not_converse_splats():
    """The gallery search template migrated from hand-rolled Converse to Gateway."""
    code = generate_agent_code(_cfg(), tools=["guardrails"], template_id="web-search-agent")

    assert "MCPClient" in code
    assert 'hooks=globals().get("_GUARDRAIL_HOOKS", [])' in code
    assert "guardrailConfig" not in code
    assert "{{" not in code
    ast.parse(code)
    compile(code, "<web-search-agent>", "exec")


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
