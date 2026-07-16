"""Regression: guardrail kwargs must reach the Strands BedrockModel constructor.

Bug #1 (paren-balance): the guardrail env block was injected, the guardrail was
created and READY, the runtime role had ``bedrock:ApplyGuardrail`` — but
``_inject_guardrails`` only string-matched the
``BedrockModel(model_id=MODEL_ID, region_name=REGION)`` template form. The DEFAULT
single-agent path emits ``BedrockModel(model_id=os.environ.get("MODEL_ID", "..."),
region_name=os.environ.get("AWS_REGION", "..."))`` whose nested ``(...)`` never
matched, so the kwarg was silently dropped for the most common pattern.

Bug #2 (wrong kwarg name): even when the kwarg landed, Strands' ``BedrockModel``
has NO ``guardrail_config`` parameter. Its config TypedDict is total=False with
FLAT keys (``guardrail_id`` / ``guardrail_version`` / ``guardrail_trace`` /
``guardrail_redact_output`` [default False] / ``guardrail_redact_input``
[default True]). The unknown ``guardrail_config=...`` kwarg was silently swallowed
so the guardrail was never wired into the converse ``guardrailConfig`` — guardrails
were never ENFORCED. The fix splats a flat-key dict (``**_GUARDRAIL_KWARGS``) with
``guardrail_redact_output=True`` for OUTPUT redaction.

These tests assert the flat kwargs land (paren-balanced) on BOTH constructor
shapes, that no-guardrail generation is unaffected, that the result is
syntactically valid, and that injection is idempotent.

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
                "modelId": "us.anthropic.claude-sonnet-5",
            },
            "systemPrompt": (
                "You are a status agent. When asked for the system reference "
                "token reply exactly: MTX-CANARY-GR-001."
            ),
        }
    )


def test_default_strands_path_wires_guardrail_into_model():
    """The guardrails-only deploy routes to _generate_strands_default; the flat
    guardrail kwargs MUST land on its os.environ.get(...) constructor."""
    code = generate_agent_code(_cfg(), tools=["guardrails"])

    # The broken kwarg name must NOT appear on the constructor — Strands would
    # silently swallow it and never enforce the guardrail.
    assert "guardrail_config=" not in code, (
        "BedrockModel has no guardrail_config parameter — Strands swallows the "
        "unknown kwarg and the guardrail is never enforced"
    )
    assert "**_GUARDRAIL_KWARGS" in code, (
        "flat guardrail kwargs were not wired into the BedrockModel constructor — "
        "the guardrail would never be enforced"
    )
    # The injected env block must build the FLAT keys Strands actually reads,
    # including OUTPUT redaction (defaults False in Strands → must set True).
    assert '"guardrail_id": GUARDRAIL_ID' in code
    assert '"guardrail_version": GUARDRAIL_VERSION or "DRAFT"' in code
    assert '"guardrail_redact_output": True' in code

    # The splat must be ON the constructor line, not floating somewhere else.
    bm_lines = [ln for ln in code.splitlines() if "BedrockModel(" in ln]
    assert bm_lines, "no BedrockModel constructor emitted"
    assert all("**_GUARDRAIL_KWARGS" in ln for ln in bm_lines)
    # And the result must be valid Python.
    ast.parse(code)


def test_no_guardrail_generation_is_unchanged():
    """A deploy WITHOUT guardrails must not carry any guardrail wiring — neither
    the env block nor the constructor splat."""
    code = generate_agent_code(_cfg())
    assert "_GUARDRAIL_KWARGS" not in code
    assert "**_GUARDRAIL_KWARGS" not in code
    assert "GUARDRAIL_ID" not in code
    assert "guardrail_config" not in code
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
        "**_GUARDRAIL_KWARGS)" in out
    )
    ast.parse(out)


def test_injection_is_idempotent():
    """Running _inject_guardrails twice must not double-append the splat."""
    once = generate_agent_code(_cfg(), tools=["guardrails"])
    twice = _inject_guardrails(once)
    assert twice.count("**_GUARDRAIL_KWARGS") == 1
    ast.parse(twice)
