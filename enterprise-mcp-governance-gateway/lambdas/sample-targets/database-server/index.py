# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Gateway Lambda Target: Database server.

Runtime contract (AgentCore Lambda target):
  - ``event`` is the FLAT tool-arguments dict, NOT a JSON-RPC envelope.
  - The invoked tool name is provided via
    ``context.client_context.custom['bedrockAgentCoreToolName']`` formatted as
    ``DatabaseAPI___<tool_name>`` -- split on the triple underscore.
  - Return the result DIRECTLY (no statusCode/body wrapper).

All data below is SYNTHETIC. Emails use the RFC 2606 reserved 'example.com'
domain, phone numbers use the reserved 555-01xx range, and the SSN/credit-card
values are well-known test placeholders (123-45-6789 is a documented invalid
sample SSN; 4111-1111-1111-1111 is the standard Visa test card number). No real
PII is present.

Compliance note: If adapting this for real data, HIPAA controls are required for
PHI, PCI-DSS for payment data, and GDPR for EU user data. This sample uses
synthetic test data only.

Note: ``export_pii_report`` intentionally returns text containing a sample SSN
and test credit-card number so the RESPONSE interceptor's redaction can be proven.
"""
import json

SAMPLE_DATA = [
    {"id": 1, "name": "Alice Johnson", "email": "alice@example.com", "phone": "555-010-0101"},
    {"id": 2, "name": "Bob Smith", "email": "bob@example.com", "phone": "555-010-0102"},
    {"id": 3, "name": "Charlie Brown", "email": "charlie@example.com", "phone": "555-010-0103"},
]


def _text(text: str) -> dict:
    """Wrap a string in the MCP text-content result envelope."""
    return {"content": [{"type": "text", "text": text}]}


def _resolve_tool_name(context) -> str:
    """Extract the bare tool name from the AgentCore client context."""
    custom = {}
    client_context = getattr(context, "client_context", None)
    if client_context is not None:
        raw = getattr(client_context, "custom", None)
        if isinstance(raw, str):
            try:
                custom = json.loads(raw or "{}")
            except json.JSONDecodeError:
                custom = {}
        elif isinstance(raw, dict):
            custom = raw
    full_tool_name = custom.get("bedrockAgentCoreToolName", "___unknown")
    return full_tool_name.split("___", 1)[-1]


def _coerce_int(value, default: int) -> int:
    """Best-effort int coercion for caller-supplied numeric arguments."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def handler(event, context):
    tool_name = _resolve_tool_name(context)
    args = event if isinstance(event, dict) else {}

    if tool_name == "execute_query":
        max_rows = max(0, _coerce_int(args.get("maxRows", 10), 10))
        return _text(json.dumps(SAMPLE_DATA[:max_rows], indent=2))

    if tool_name == "drop_table":
        return _text(f"Dropped table: {args.get('tableName', 'unknown')}")

    if tool_name == "truncate_table":
        return _text(f"Truncated table: {args.get('tableName', 'unknown')}")

    if tool_name == "delete_records":
        return _text(
            f"Deleted from {args.get('table', '?')} where {args.get('where', '?')}"
        )

    if tool_name == "bulk_export":
        return _text(json.dumps(SAMPLE_DATA, indent=2))

    if tool_name == "query_audit_logs":
        return _text(
            json.dumps(
                [
                    {
                        "ts": "2026-05-29T10:00:00Z",
                        "action": "login",
                        "user": "admin@example.com",
                    }
                ]
            )
        )

    if tool_name == "export_pii_report":
        # Intentional PII so the RESPONSE interceptor's redaction is provable.
        department = args.get("department", "all")
        return _text(
            f"PII report for department '{department}':\n"
            "Name: Alice Johnson, SSN: 123-45-6789, "
            "Credit Card: 4111-1111-1111-1111, Email: alice@example.com, "
            "Phone: 555-010-0101"
        )

    return _text(f"Unknown tool: {tool_name}")
