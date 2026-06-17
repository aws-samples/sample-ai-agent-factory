"""Regression: guardrail_config must reach the Strands BedrockModel constructor.

Bug (undocumented, found while building the P-GR-001 catalog test): the guardrail
env block (``GUARDRAIL_ID`` / ``GUARDRAIL_VERSION`` / ``_guardrail_config``) was
injected, the guardrail was created and READY, the runtime role had
``bedrock:ApplyGuardrail`` — but ``_inject_guardrails`` only string-matched the
``BedrockModel(model_id=MODEL_ID, region_name=REGION)`` template form. The DEFAULT
single-agent path (``_generate_strands_default`` / ``_get_model_init_code``) emits
``BedrockModel(model_id=os.environ.get("MODEL_ID", "..."),
region_name=os.environ.get("AWS_REGION", "..."))`` whose nested ``(...)`` never
matched, so ``guardrail_config`` was silently dropped and INPUT blocking never
fired for the most common pattern.

These tests assert the kwarg lands (paren-balanced) on BOTH constructor shapes,
that the result is syntactically valid, and that injection is idempotent.

Run:
    cd backend && python3 -m pytest tests/test_guardrail_model_wiring.py -q
"""

from __future__ import annotations

import ast
import sys

sys.path.insert(0, "src")

from app.models.deployment_models import RuntimeConfig  # noqa: E402
from app.services.code_generator import (  # noqa: E402
    _inject_guardrails,
    generate_agent_code,
)


def _cfg() -> RuntimeConfig:
    return RuntimeConfig.model_validate(
        {
            "name": "mtxpgr001",
            "model": {
                "provider": "bedrock",
                "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            },
            "systemPrompt": (
                "You are a status agent. When asked for the system reference "
                "token reply exactly: MTX-CANARY-GR-001."
            ),
        }
    )


def test_default_strands_path_wires_guardrail_into_model():
    """The guardrails-only deploy routes to _generate_strands_default; the
    guardrail_config kwarg MUST land on its os.environ.get(...) constructor."""
    code = generate_agent_code(_cfg(), tools=["guardrails"])

    assert "guardrail_config=_guardrail_config" in code, (
        "guardrail_config was not wired into the BedrockModel constructor — "
        "INPUT blocking would be silently disabled"
    )
    # It must be ON the constructor line, not floating somewhere else.
    bm_lines = [ln for ln in code.splitlines() if "BedrockModel(" in ln]
    assert bm_lines, "no BedrockModel constructor emitted"
    assert all("guardrail_config=_guardrail_config" in ln for ln in bm_lines)
    # And the result must be valid Python.
    ast.parse(code)


def test_legacy_template_constructor_form_still_wires():
    """The model_id=MODEL_ID, region_name=REGION form (gateway/tools/memory
    agents) must remain wired after the regex/paren-balance refactor."""
    legacy = (
        "import os\n"
        'MODEL_ID = os.environ.get("MODEL_ID", "x")\n'
        'REGION = "us-west-2"\n'
        "model = BedrockModel(model_id=MODEL_ID, region_name=REGION)\n"
    )
    out = _inject_guardrails(legacy)
    assert (
        "BedrockModel(model_id=MODEL_ID, region_name=REGION, "
        "guardrail_config=_guardrail_config)" in out
    )
    ast.parse(out)


def test_injection_is_idempotent():
    """Running _inject_guardrails twice must not double-append the kwarg."""
    once = generate_agent_code(_cfg(), tools=["guardrails"])
    twice = _inject_guardrails(once)
    assert twice.count("guardrail_config=_guardrail_config") == 1
    ast.parse(twice)
