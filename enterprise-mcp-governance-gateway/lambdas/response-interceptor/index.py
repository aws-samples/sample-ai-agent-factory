# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Gateway RESPONSE Interceptor.

Runs AFTER the target tool executes, BEFORE the response reaches the caller.
Responsibilities:
  1. Redact PII (email, US phone, SSN, credit card) from text content.
  2. Truncate oversized text payloads to a 50KB limit.
  3. Emit a structured JSON audit record to stdout (CloudWatch Logs).

Compliance note: This interceptor provides data minimization but does not
constitute full regulatory compliance. If handling PHI, additional HIPAA controls
are required. If processing payment data, PCI-DSS requirements apply. If handling
EU user data, GDPR considerations are necessary.

I/O schema (per AgentCore Gateway interceptor contract):

  Input:
    {
      "interceptorInputVersion": "1.0",
      "mcp": {
        "gatewayRequest":  { "headers": {...}, "body": { ...tools/call... } },
        "gatewayResponse": {
          "statusCode": 200,
          "headers": {...},
          "body": {
            "jsonrpc": "2.0",
            "id": 1,
            "result": { "content": [{"type": "text", "text": "..."}] }
          }
        }
      }
    }

  Output:
    {
      "interceptorOutputVersion": "1.0",
      "mcp": {
        "transformedGatewayResponse": {
          "statusCode": <statusCode>,
          "body": { ...modified response... }
        }
      }
    }
"""
import json
import logging
import os
import re

import guardrail  # Bedrock Guardrails helper (no-op unless GUARDRAIL_ID is set)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

OUTPUT_VERSION = "1.0"

# Order matters: SSN and credit card are matched before the generic phone
# pattern so that more specific structures are redacted with the right label.
PII_PATTERNS = (
    ("email", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")),
    (
        "credit_card",
        re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    ),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    (
        "phone",
        re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    ),
)

# 50KB cap on a single text item's length (characters).
MAX_TEXT_SIZE = 50_000


def redact_pii(text: str):
    """Redact PII from text. Returns (redacted_text, {pii_type: count})."""
    counts = {}
    for pii_type, pattern in PII_PATTERNS:
        # Use a counter closure so we tally only genuine substitutions.
        n = [0]

        def _sub(_match, _label=pii_type, _n=n):
            _n[0] += 1
            return f"[REDACTED_{_label.upper()}]"

        text = pattern.sub(_sub, text)
        if n[0]:
            counts[pii_type] = n[0]
    return text, counts


def _redact_content(response_body: dict):
    """Redact PII across every text content item in a JSON-RPC result.

    Mutates response_body in place. Returns aggregate redaction counts.
    """
    totals = {}
    result = response_body.get("result")
    if not isinstance(result, dict):
        return totals
    content = result.get("content")
    if not isinstance(content, list):
        return totals
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
            redacted, counts = redact_pii(item["text"])
            item["text"] = redacted
            for key, value in counts.items():
                totals[key] = totals.get(key, 0) + value
    return totals


def _truncate_content(response_body: dict) -> bool:
    """Truncate any text content item exceeding MAX_TEXT_SIZE. Returns whether
    truncation occurred. Mutates response_body in place.
    """
    truncated = False
    result = response_body.get("result")
    if not isinstance(result, dict):
        return truncated
    content = result.get("content")
    if not isinstance(content, list):
        return truncated
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            if isinstance(text, str) and len(text) > MAX_TEXT_SIZE:
                item["text"] = (
                    text[:MAX_TEXT_SIZE]
                    + "\n\n[TRUNCATED: response exceeded 50KB limit]"
                )
                truncated = True
    return truncated


def _guardrail_content(response_body: dict) -> dict:
    """Run each text content item through the Bedrock Guardrail (OUTPUT source).

    Mutates response_body in place, replacing text with the guardrail's anonymized
    output when it intervenes. No-op unless GUARDRAIL_ID is set. Returns a small
    summary {applied, intervened, reasons}.
    """
    summary = {"applied": False, "intervened": 0, "reasons": []}
    if not guardrail.enabled():
        return summary
    result = response_body.get("result")
    if not isinstance(result, dict):
        return summary
    content = result.get("content")
    if not isinstance(content, list):
        return summary
    summary["applied"] = True
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            gr = guardrail.apply(item["text"], source="OUTPUT")
            if gr["action"] in ("ANONYMIZED", "BLOCKED"):
                item["text"] = gr["outputText"]
                summary["intervened"] += 1
                summary["reasons"].extend(gr["reasons"])
            elif gr["action"] == "ERROR":
                logger.error(json.dumps({"event": "guardrail_error", "where": "response",
                                         "reasons": gr["reasons"]}))
    return summary


def handler(event, context):
    """RESPONSE interceptor entry point."""
    mcp_data = event.get("mcp", {}) if isinstance(event, dict) else {}
    gateway_request = mcp_data.get("gatewayRequest", {}) or {}
    gateway_response = mcp_data.get("gatewayResponse", {}) or {}

    request_body = gateway_request.get("body", {}) or {}
    response_body = gateway_response.get("body", {}) or {}
    response_status = gateway_response.get("statusCode", 200)

    tool_name = (request_body.get("params", {}) or {}).get("name", "unknown")

    # --- MANAGED GUARDRAIL (Bedrock) — anonymize/block first, so the local regex
    #     redaction below is a defense-in-depth backstop for anything it misses. ---
    guardrail_summary = _guardrail_content(response_body)

    # --- PII REDACTION (before truncation, so redaction labels survive) ---
    redaction_counts = _redact_content(response_body)

    # --- TRUNCATION ---
    truncated = _truncate_content(response_body)

    # --- AUDIT LOG ---
    logger.info(
        json.dumps(
            {
                "event": "response_audit",
                "tool": tool_name,
                "status_code": response_status,
                "response_size": len(json.dumps(response_body, default=str)),
                "pii_redacted": redaction_counts,
                "truncated": truncated,
                "guardrail": guardrail_summary,
            }
        )
    )

    # --- RETURN MODIFIED RESPONSE ---
    return {
        "interceptorOutputVersion": OUTPUT_VERSION,
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": response_status,
                "body": response_body,
            }
        },
    }
