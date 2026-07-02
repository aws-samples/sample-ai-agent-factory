"""Unit tests for the guardrails_step content-filter config builder.

P-PLAT-012 regression: PROMPT_ATTACK is an input-only filter; Bedrock
CreateGuardrail rejects any non-NONE outputStrength for it. The live deploy
path (_build_content_filter_config) must force outputStrength="NONE" for
PROMPT_ATTACK while leaving every other category with input==output, matching
the CFN-export path.

Run:
    cd backend && python3 -m pytest tests/test_guardrails_step.py -x -q
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from app.step_handlers.guardrails_step import (  # noqa: E402
    _build_content_filter_config,
)


def _by_type(cfg: dict) -> dict:
    return {f["type"]: f for f in cfg["filtersConfig"]}


def test_prompt_attack_output_strength_forced_to_none():
    cfg = _build_content_filter_config({"prompt_attack": "HIGH"})
    entry = _by_type(cfg)["PROMPT_ATTACK"]
    assert entry["inputStrength"] == "HIGH"
    assert entry["outputStrength"] == "NONE"


def test_normal_category_keeps_input_equals_output():
    cfg = _build_content_filter_config({"hate": "HIGH"})
    entry = _by_type(cfg)["HATE"]
    assert entry["inputStrength"] == "HIGH"
    assert entry["outputStrength"] == "HIGH"


def test_mixed_categories_only_prompt_attack_is_special_cased():
    cfg = _build_content_filter_config(
        {"hate": "MEDIUM", "prompt_attack": "MEDIUM"}
    )
    by_type = _by_type(cfg)
    assert by_type["HATE"]["inputStrength"] == "MEDIUM"
    assert by_type["HATE"]["outputStrength"] == "MEDIUM"
    assert by_type["PROMPT_ATTACK"]["inputStrength"] == "MEDIUM"
    assert by_type["PROMPT_ATTACK"]["outputStrength"] == "NONE"
