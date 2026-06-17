"""Gap 2C — standalone unit tests for the pure guardrail config-builders.

These run NOW with zero AWS/boto3/moto dependency and WITHOUT any shared
manifest edit applied. They pin the exact Bedrock policy shapes plus all the
clamping / normalization / drop rules described in the design.

Run:
    cd backend && python3 -m pytest tests/test_guardrail_builders.py -x -q
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from app.services.guardrail_builders import (  # noqa: E402
    build_contextual_grounding_config,
    build_regex_filters,
)


# ---------------------------------------------------------------------------
# build_contextual_grounding_config
# ---------------------------------------------------------------------------


def test_contextual_grounding_exact_shape():
    assert build_contextual_grounding_config(0.7, 0.7) == {
        "filtersConfig": [
            {"type": "GROUNDING", "threshold": 0.7},
            {"type": "RELEVANCE", "threshold": 0.7},
        ]
    }


def test_contextual_grounding_order_is_grounding_then_relevance():
    cfg = build_contextual_grounding_config(0.1, 0.9)
    types = [f["type"] for f in cfg["filtersConfig"]]
    assert types == ["GROUNDING", "RELEVANCE"]


def test_contextual_grounding_clamps_high_to_099():
    cfg = build_contextual_grounding_config(1.5, 5)
    assert cfg["filtersConfig"][0]["threshold"] == 0.99
    assert cfg["filtersConfig"][1]["threshold"] == 0.99


def test_contextual_grounding_rejects_exactly_one_clamps_to_099():
    # Bedrock rejects 1.0 — must clamp to 0.99, not 1.0.
    cfg = build_contextual_grounding_config(1.0, 1.0)
    assert cfg["filtersConfig"][0]["threshold"] == 0.99
    assert cfg["filtersConfig"][1]["threshold"] == 0.99


def test_contextual_grounding_clamps_low_to_zero():
    cfg = build_contextual_grounding_config(-0.2, -100)
    assert cfg["filtersConfig"][0]["threshold"] == 0.0
    assert cfg["filtersConfig"][1]["threshold"] == 0.0


def test_contextual_grounding_none_threshold_omits_that_filter():
    cfg = build_contextual_grounding_config(None, 0.5)
    assert cfg == {"filtersConfig": [{"type": "RELEVANCE", "threshold": 0.5}]}

    cfg2 = build_contextual_grounding_config(0.5, None)
    assert cfg2 == {"filtersConfig": [{"type": "GROUNDING", "threshold": 0.5}]}


def test_contextual_grounding_both_none_returns_empty():
    assert build_contextual_grounding_config(None, None) == {}


def test_contextual_grounding_non_numeric_treated_as_absent():
    assert build_contextual_grounding_config("abc", None) == {}
    # one valid, one garbage
    assert build_contextual_grounding_config("not-a-number", 0.3) == {
        "filtersConfig": [{"type": "RELEVANCE", "threshold": 0.3}]
    }


def test_contextual_grounding_numeric_strings_coerce():
    cfg = build_contextual_grounding_config("0.6", "0.4")
    assert cfg == {
        "filtersConfig": [
            {"type": "GROUNDING", "threshold": 0.6},
            {"type": "RELEVANCE", "threshold": 0.4},
        ]
    }


# ---------------------------------------------------------------------------
# build_regex_filters
# ---------------------------------------------------------------------------


def test_regex_basic_shape():
    out = build_regex_filters(
        [{"name": "ticket", "pattern": r"TICKET-\d+", "action": "BLOCK"}]
    )
    assert out == {
        "regexesConfig": [
            {"name": "ticket", "pattern": r"TICKET-\d+", "action": "BLOCK"}
        ]
    }


def test_regex_defaults_action_to_anonymize_when_missing():
    out = build_regex_filters([{"name": "x", "pattern": "foo"}])
    assert out["regexesConfig"][0]["action"] == "ANONYMIZE"


def test_regex_defaults_action_to_anonymize_when_invalid():
    out = build_regex_filters([{"name": "x", "pattern": "foo", "action": "NUKE"}])
    assert out["regexesConfig"][0]["action"] == "ANONYMIZE"


def test_regex_action_case_insensitive():
    out = build_regex_filters([{"name": "x", "pattern": "foo", "action": "block"}])
    assert out["regexesConfig"][0]["action"] == "BLOCK"


def test_regex_drops_empty_name():
    out = build_regex_filters([{"name": "   ", "pattern": "foo"}])
    assert out == {}


def test_regex_drops_empty_pattern():
    out = build_regex_filters([{"name": "x", "pattern": "   "}])
    assert out == {}


def test_regex_drops_uncompilable_pattern():
    # unbalanced paren -> re.error -> dropped, never raises
    out = build_regex_filters([{"name": "bad", "pattern": "([a-z"}])
    assert out == {}


def test_regex_drops_name_over_100_chars():
    out = build_regex_filters([{"name": "n" * 101, "pattern": "foo"}])
    assert out == {}


def test_regex_keeps_name_exactly_100_chars():
    out = build_regex_filters([{"name": "n" * 100, "pattern": "foo"}])
    assert len(out["regexesConfig"]) == 1


def test_regex_empty_list_returns_empty():
    assert build_regex_filters([]) == {}


def test_regex_none_returns_empty():
    assert build_regex_filters(None) == {}


def test_regex_includes_optional_description():
    out = build_regex_filters(
        [{"name": "x", "pattern": "foo", "description": "match foo"}]
    )
    assert out["regexesConfig"][0]["description"] == "match foo"


def test_regex_omits_blank_description():
    out = build_regex_filters([{"name": "x", "pattern": "foo", "description": "  "}])
    assert "description" not in out["regexesConfig"][0]


def test_regex_keeps_valid_drops_invalid_in_mixed_batch():
    out = build_regex_filters(
        [
            {"name": "good1", "pattern": r"\d{3}", "action": "BLOCK"},
            {"name": "", "pattern": "x"},          # dropped: empty name
            {"name": "bad", "pattern": "([a-z"},   # dropped: uncompilable
            {"name": "good2", "pattern": "abc"},   # kept, default action
        ]
    )
    names = [r["name"] for r in out["regexesConfig"]]
    assert names == ["good1", "good2"]
    assert out["regexesConfig"][1]["action"] == "ANONYMIZE"


def test_regex_ignores_non_dict_entries():
    out = build_regex_filters(["not-a-dict", 42, {"name": "ok", "pattern": "y"}])
    assert [r["name"] for r in out["regexesConfig"]] == ["ok"]
