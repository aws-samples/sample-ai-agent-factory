"""Pure config-builders for Gap 2C — Guardrails enhancement.

This module holds the *shape* logic for two new Bedrock Guardrail policy
sections so it can be unit-tested standalone, with **zero** AWS / boto3
dependencies:

  1. Contextual grounding  -> ``contextualGroundingPolicyConfig``
     (blocks hallucinated / off-topic model responses by enforcing a minimum
     grounding score — factual support against a supplied source — and a
     minimum relevance score — on-topic-ness against the user query).

  2. Custom regex filters   -> ``sensitiveInformationPolicyConfig.regexesConfig``
     (named regex patterns with a BLOCK or ANONYMIZE action, wired alongside
     the existing PII entity filters).

Both shapes are *fields on the same Bedrock guardrail* that
``step_handlers/guardrails_step.py`` already creates, so the existing guardrail
lifecycle (create + cascade-delete in ``deployment_handler.destroy_runtime``)
covers them with no new DDB table, router, endpoint, or per-runtime AWS
resource — and therefore no new IAM grant (the runtime already has
``bedrock:ApplyGuardrail``).

IMPORTANT BEDROCK SEMANTICS / GOTCHAS
-------------------------------------
* Contextual-grounding thresholds clamp to ``[0.0, 0.99]``. Bedrock REJECTS a
  threshold of ``1.0`` (only strictly-less-than-one is allowed), so the upper
  clamp is ``0.99``, **not** ``1.0``.
* Contextual grounding is INERT unless the agent passes a ``grounding_source``
  + ``query`` qualifier on its ``ApplyGuardrail`` / converse call at invoke
  time. Merely setting ``contextualGroundingPolicyConfig`` on the guardrail
  does nothing for a non-RAG agent. Gate the UI on a knowledge-base/gateway
  connected agent in a later iteration (out of scope for 2C).
* An uncompilable user regex would make ``create_guardrail`` fail the entire
  deploy, so ``build_regex_filters`` silently DROPS any entry whose pattern
  doesn't ``re.compile`` (and any entry with an empty name / a name longer than
  the Bedrock 100-char limit).

PROMPT-INJECTION DEFENSE NOTE
-----------------------------
The minimal-viable prompt-injection hardening for 2C is (a) the existing
Bedrock ``PROMPT_ATTACK`` content filter (already wired in guardrails_step)
plus (b) a system-prompt hardening line added in code_generator when
guardrails are connected. An *optional* Haiku pre-screen could be layered on
top, but it is intentionally left opt-in (not auto-injected) to avoid
per-invoke latency + token cost; if enabled later it must use the in-window
model ``us.anthropic.claude-haiku-4-5-20251001-v1:0`` (Bedrock model window
Oct-2025 .. May-2026).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# Bedrock contextual-grounding threshold bounds. Upper bound is 0.99 (NOT 1.0):
# Bedrock rejects exactly 1.0.
_GROUNDING_MIN = 0.0
_GROUNDING_MAX = 0.99

# Bedrock caps a regex filter ``name`` at 100 characters.
_REGEX_NAME_MAX = 100

# Allowed regex-filter actions. ANONYMIZE is the safe default (redact rather
# than hard-block) and matches the PII-filter default in guardrails_step.
_REGEX_ACTIONS = ("BLOCK", "ANONYMIZE")
_REGEX_DEFAULT_ACTION = "ANONYMIZE"


def _clamp_threshold(value: Any) -> float | None:
    """Coerce ``value`` to a float clamped to ``[0.0, 0.99]``.

    Returns ``None`` when ``value`` is ``None`` or not coercible to a float, so
    callers can omit that filter entry entirely.
    """
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:  # NaN guard
        return None
    if num < _GROUNDING_MIN:
        return _GROUNDING_MIN
    if num > _GROUNDING_MAX:
        return _GROUNDING_MAX
    return num


def build_contextual_grounding_config(
    grounding_threshold: Any,
    relevance_threshold: Any,
) -> dict:
    """Build a Bedrock ``contextualGroundingPolicyConfig`` dict.

    Args:
        grounding_threshold: Minimum grounding score (factual support against
            the supplied source). Clamped to ``[0.0, 0.99]``. ``None`` /
            non-numeric omits the GROUNDING filter entry.
        relevance_threshold: Minimum relevance score (on-topic-ness against the
            user query). Clamped to ``[0.0, 0.99]``. ``None`` / non-numeric
            omits the RELEVANCE filter entry.

    Returns:
        ``{"filtersConfig": [...]}`` with a GROUNDING entry and/or a RELEVANCE
        entry, in that order. Returns ``{}`` when BOTH thresholds are absent /
        invalid, so the caller can skip setting the policy key entirely (an
        empty filtersConfig would be rejected by Bedrock).
    """
    filters: list[dict] = []

    grounding = _clamp_threshold(grounding_threshold)
    if grounding is not None:
        filters.append({"type": "GROUNDING", "threshold": grounding})

    relevance = _clamp_threshold(relevance_threshold)
    if relevance is not None:
        filters.append({"type": "RELEVANCE", "threshold": relevance})

    if not filters:
        return {}
    return {"filtersConfig": filters}


def _normalize_action(action: Any) -> str:
    """Return a valid regex-filter action, defaulting to ANONYMIZE."""
    candidate = str(action or "").strip().upper()
    if candidate in _REGEX_ACTIONS:
        return candidate
    return _REGEX_DEFAULT_ACTION


def _is_compilable(pattern: str) -> bool:
    """True when ``pattern`` compiles as a regex (so the deploy won't fail)."""
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False


def build_regex_filters(patterns: Iterable[dict] | None) -> dict:
    """Build a partial ``sensitiveInformationPolicyConfig`` with regex filters.

    Each input entry is a dict shaped like::

        {"name": "...", "pattern": "...", "action": "BLOCK"|"ANONYMIZE",
         "description": "..." (optional)}

    Entries are DROPPED (never raise) when:
      * ``name`` is empty / whitespace, or longer than 100 chars
      * ``pattern`` is empty / whitespace, or doesn't ``re.compile``

    ``action`` defaults to ANONYMIZE when missing or invalid.

    Returns:
        ``{"regexesConfig": [...]}`` for the kept entries, or ``{}`` when the
        input is falsy or every entry is dropped — so the caller can MERGE this
        into the existing ``sensitiveInformationPolicyConfig`` (which may
        already hold ``piiEntitiesConfig``) via ``setdefault(...).update(...)``
        and skip the section entirely when empty.
    """
    if not patterns:
        return {}

    regexes: list[dict] = []
    for entry in patterns:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        pattern = str(entry.get("pattern", "")).strip()
        if not name or len(name) > _REGEX_NAME_MAX:
            continue
        if not pattern or not _is_compilable(pattern):
            continue

        regex_entry: dict = {
            "name": name,
            "pattern": pattern,
            "action": _normalize_action(entry.get("action")),
        }
        description = str(entry.get("description", "")).strip()
        if description:
            regex_entry["description"] = description
        regexes.append(regex_entry)

    if not regexes:
        return {}
    return {"regexesConfig": regexes}
