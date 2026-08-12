# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Amazon Bedrock Guardrails helper for the gateway interceptors.

Wraps the ``bedrock-runtime:ApplyGuardrail`` API so request/response interceptors
can enforce a **managed** guardrail (content filters, denied topics, PII
detection/anonymization, word filters) in addition to the local regex rules.

Design:
  * **Opt-in / env-gated.** If ``GUARDRAIL_ID`` is unset the helper is a no-op, so
    the stack works with or without a guardrail and existing behavior is unchanged.
  * **Fail-safe + fail-open choice is explicit.** On an API error we DON'T silently
    allow sensitive data through on the response path: callers decide. The request
    path treats a hard guardrail ``BLOCKED`` as a block; the response path uses the
    guardrail's anonymized output when present.
  * boto3 is the Lambda runtime SDK (``bedrock-runtime`` has had ``apply_guardrail``
    since 2024); no extra dependency.

Returns a small normalized dict so the interceptors stay simple:
  {"action": "NONE"|"BLOCKED"|"ANONYMIZED",
   "outputText": <possibly-masked text or None>,
   "reasons": [str, ...]}
"""
from __future__ import annotations

import os
from typing import Optional

try:  # boto3 is present in the Lambda runtime
    import boto3
except Exception:  # pragma: no cover
    boto3 = None

GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

_client = None


def enabled() -> bool:
    return bool(GUARDRAIL_ID) and boto3 is not None


def _runtime():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION"))
    return _client


def apply(text: str, source: str) -> dict:
    """Run text through the configured guardrail.

    source: 'INPUT' (request path) or 'OUTPUT' (response path).
    Returns {"action", "outputText", "reasons"}. No-op result if not enabled.
    """
    if not enabled() or not text:
        return {"action": "NONE", "outputText": text, "reasons": []}
    try:
        resp = _runtime().apply_guardrail(
            guardrailIdentifier=GUARDRAIL_ID,
            guardrailVersion=GUARDRAIL_VERSION,
            source=source,
            content=[{"text": {"text": text}}],
        )
    except Exception as e:  # surfaced to the caller as a soft error
        return {"action": "ERROR", "outputText": text, "reasons": [f"{type(e).__name__}: {e}"]}

    gr_action = resp.get("action", "NONE")  # 'GUARDRAIL_INTERVENED' | 'NONE'
    reasons = _reasons(resp)
    # ApplyGuardrail returns masked/anonymized text in outputs[].text when it
    # rewrites content (e.g. PII anonymization); otherwise outputs may be empty.
    out_text = text
    outputs = resp.get("outputs") or []
    if outputs and isinstance(outputs[0], dict):
        out_text = outputs[0].get("text", text)

    if gr_action != "GUARDRAIL_INTERVENED":
        return {"action": "NONE", "outputText": out_text, "reasons": []}

    # Distinguish a hard block (filters/topics/words) from anonymization. If the
    # output text still carries content (masked), treat as ANONYMIZED; if the
    # guardrail blocked outright it returns its blocked-message as outputText.
    blocked = _is_hard_block(resp)
    return {
        "action": "BLOCKED" if blocked else "ANONYMIZED",
        "outputText": out_text,
        "reasons": reasons,
    }


def _is_hard_block(resp: dict) -> bool:
    """True if any content-filter / topic / word policy hit at BLOCK strength.

    PII anonymization (ANONYMIZE) is NOT a hard block; PII at BLOCK action is.
    """
    for assessment in resp.get("assessments", []) or []:
        cp = assessment.get("contentPolicy", {}).get("filters", []) or []
        if any(f.get("action") == "BLOCKED" for f in cp):
            return True
        tp = assessment.get("topicPolicy", {}).get("topics", []) or []
        if any(t.get("action") == "BLOCKED" for t in tp):
            return True
        wp = assessment.get("wordPolicy", {})
        if any(w.get("action") == "BLOCKED" for w in wp.get("customWords", []) or []):
            return True
        if any(w.get("action") == "BLOCKED" for w in wp.get("managedWordLists", []) or []):
            return True
        sp = assessment.get("sensitiveInformationPolicy", {})
        if any(p.get("action") == "BLOCKED" for p in sp.get("piiEntities", []) or []):
            return True
        if any(r.get("action") == "BLOCKED" for r in sp.get("regexes", []) or []):
            return True
    return False


def _reasons(resp: dict) -> list:
    reasons: list = []
    for assessment in resp.get("assessments", []) or []:
        for f in assessment.get("contentPolicy", {}).get("filters", []) or []:
            reasons.append(f"content:{f.get('type')}:{f.get('action')}")
        for t in assessment.get("topicPolicy", {}).get("topics", []) or []:
            reasons.append(f"topic:{t.get('name')}:{t.get('action')}")
        sp = assessment.get("sensitiveInformationPolicy", {})
        for p in sp.get("piiEntities", []) or []:
            reasons.append(f"pii:{p.get('type')}:{p.get('action')}")
    return reasons
