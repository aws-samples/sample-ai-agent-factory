# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Gateway REQUEST Interceptor.

Runs BEFORE a request reaches the target tool. Responsibilities:
  1. Emit a structured JSON audit record to stdout (CloudWatch Logs).
  2. Block SQL-injection / destructive SQL patterns in any tool argument.
  3. Block sensitive tools outside business hours (09:00-17:00 UTC).
  4. Pass everything else through unchanged.

I/O schema (per AgentCore Gateway interceptor contract):

  Input:
    {
      "interceptorInputVersion": "1.0",
      "mcp": {
        "gatewayRequest": {
          "headers": { "Authorization": "Bearer ...", "Mcp-Session-Id": "..." },
          "body": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": { "name": "Target___tool", "arguments": {...} }
          }
        }
      }
    }

  Output (pass-through):
    {
      "interceptorOutputVersion": "1.0",
      "mcp": { "transformedGatewayRequest": { "body": {...} } }
    }

  Output (block / short-circuit):
    {
      "interceptorOutputVersion": "1.0",
      "mcp": {
        "transformedGatewayResponse": {
          "statusCode": 200,
          "body": { "jsonrpc": "2.0", "id": <id>, "error": {...} }
        }
      }
    }
"""
import base64
import binascii
import json
import logging
import os
import re
from datetime import datetime, timezone

import guardrail  # Bedrock Guardrails helper (no-op unless GUARDRAIL_ID is set)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

INPUT_VERSION = "1.0"
OUTPUT_VERSION = "1.0"

# JSON-RPC error code for an invalid request that the gateway rejects.
JSONRPC_INVALID_REQUEST = -32600

# Destructive / injection SQL patterns. Matches statement separators followed by
# dangerous verbs, classic tautologies, and inline comment terminators.
DANGEROUS_SQL = re.compile(
    r"(\b(DROP\s+TABLE|DROP\s+DATABASE|DELETE\s+FROM|TRUNCATE(\s+TABLE)?|"
    r"ALTER\s+TABLE|GRANT|REVOKE|INSERT\s+INTO|UPDATE\s+\w+\s+SET|EXEC(UTE)?|"
    r"UNION\s+(ALL\s+)?SELECT)\b"
    r"|--|;\s*\w"
    r"|\bOR\s+['\"]?\d+['\"]?\s*=\s*['\"]?\d+"
    r"|\bUNION\b)",
    re.IGNORECASE,
)

# String argument keys that may contain a raw SQL statement.
SQL_ARG_KEYS = ("query", "where", "sql", "statement", "filter")

# Tools that may only be invoked during business hours (UTC).
SENSITIVE_TOOLS = frozenset(
    {"DatabaseAPI___query_audit_logs", "DatabaseAPI___export_pii_report"}
)
BUSINESS_HOUR_START = int(os.environ.get("BUSINESS_HOUR_START", "9"))
BUSINESS_HOUR_END = int(os.environ.get("BUSINESS_HOUR_END", "17"))


def _decode_jwt_segment(segment: str) -> dict:
    """Base64url-decode a JWT segment into a dict, tolerating missing padding."""
    padded = segment + "=" * (-len(segment) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    return json.loads(raw)


def extract_user_from_jwt(headers: dict) -> str:
    """Best-effort user identity from the Authorization Bearer JWT.

    The signature is NOT verified here -- the gateway already validated the JWT
    before invoking this interceptor. This is purely for audit attribution.
    """
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return "unknown"
    try:
        token = auth.split(" ", 1)[1]
        parts = token.split(".")
        if len(parts) < 2:
            return "unknown"
        claims = _decode_jwt_segment(parts[1])
    except (ValueError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        return "unknown"
    return (
        claims.get("sub")
        or claims.get("email")
        or claims.get("username")
        or claims.get("client_id")
        or "unknown"
    )


def _iter_string_args(arguments: dict):
    """Yield (key, value) pairs for string arguments worth scanning for SQL."""
    if not isinstance(arguments, dict):
        return
    for key, value in arguments.items():
        if isinstance(value, str):
            yield key, value


def _find_dangerous_sql(arguments: dict):
    """Return (key, value) of the first argument containing a dangerous pattern."""
    for key, value in _iter_string_args(arguments):
        if key.lower() in SQL_ARG_KEYS and DANGEROUS_SQL.search(value):
            return key, value
    return None


def _block(request_id, message: str) -> dict:
    """Build a short-circuit JSON-RPC error response."""
    return {
        "interceptorOutputVersion": OUTPUT_VERSION,
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "body": {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": JSONRPC_INVALID_REQUEST,
                        "message": message,
                    },
                },
            }
        },
    }


def _passthrough(body: dict) -> dict:
    """Build a pass-through response that forwards the (unmodified) body."""
    return {
        "interceptorOutputVersion": OUTPUT_VERSION,
        "mcp": {"transformedGatewayRequest": {"body": body}},
    }


def handler(event, context):
    """REQUEST interceptor entry point."""
    mcp_data = event.get("mcp", {}) if isinstance(event, dict) else {}
    gateway_request = mcp_data.get("gatewayRequest", {}) or {}
    headers = gateway_request.get("headers", {}) or {}
    body = gateway_request.get("body", {}) or {}

    method = body.get("method", "")
    params = body.get("params", {}) or {}
    tool_name = params.get("name", "unknown")
    tool_args = params.get("arguments", {}) or {}
    request_id = body.get("id", 0)

    user_id = extract_user_from_jwt(headers)

    # --- AUDIT LOG ---
    # PII-safe by construction: we log only METADATA — the caller, the method, the tool,
    # the argument KEY NAMES (`arguments_keys`, never the values) and the session id.
    # Argument VALUES are deliberately excluded because they can carry customer data
    # (queries, page content, PII). Do not add `tool_args` itself to this record.
    logger.info(
        json.dumps(
            {
                "event": "request_audit",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "user": user_id,
                "method": method,
                "tool": tool_name,
                "arguments_keys": sorted(tool_args.keys())
                if isinstance(tool_args, dict)
                else [],
                "session_id": headers.get("Mcp-Session-Id")
                or headers.get("mcp-session-id")
                or "none",
            }
        )
    )

    # Only enforce guardrails on actual tool invocations.
    if method == "tools/call":
        # --- SQL injection / destructive SQL detection ---
        hit = _find_dangerous_sql(tool_args)
        if hit is not None:
            arg_key, arg_val = hit
            # Log WHICH argument tripped the rule, not its content: a blocked payload can
            # still contain customer data (e.g. a legitimate query with PII plus an
            # injected clause). Length is safe and useful for triage; the value is not.
            logger.warning(
                json.dumps(
                    {
                        "event": "request_blocked",
                        "reason": "dangerous_sql_pattern",
                        "user": user_id,
                        "tool": tool_name,
                        "argument": arg_key,
                        "value_length": len(arg_val) if isinstance(arg_val, str) else None,
                    }
                )
            )
            return _block(
                request_id,
                f"Request blocked: dangerous SQL pattern detected in tool "
                f"'{tool_name}' (argument '{arg_key}').",
            )

        # --- BUSINESS HOURS CHECK ---
        if tool_name in SENSITIVE_TOOLS:
            current_hour = datetime.now(timezone.utc).hour
            if current_hour < BUSINESS_HOUR_START or current_hour >= BUSINESS_HOUR_END:
                logger.warning(
                    json.dumps(
                        {
                            "event": "request_blocked",
                            "reason": "outside_business_hours",
                            "user": user_id,
                            "tool": tool_name,
                            "current_hour_utc": current_hour,
                        }
                    )
                )
                return _block(
                    request_id,
                    f"Tool '{tool_name}' is only available during business hours "
                    f"({BUSINESS_HOUR_START:02d}:00-{BUSINESS_HOUR_END:02d}:00 UTC).",
                )

        # --- BEDROCK GUARDRAILS (managed) ---
        # Run the request's string arguments through the configured guardrail
        # (prompt-attack / denied-topic / managed-word / PII filters). No-op unless
        # GUARDRAIL_ID is set, so the local rules above remain the baseline.
        if guardrail.enabled():
            scan_text = "\n".join(v for _, v in _iter_string_args(tool_args))
            if scan_text:
                gr = guardrail.apply(scan_text, source="INPUT")
                if gr["action"] == "BLOCKED":
                    logger.warning(
                        json.dumps(
                            {
                                "event": "request_blocked",
                                "reason": "bedrock_guardrail",
                                "user": user_id,
                                "tool": tool_name,
                                "guardrail_reasons": gr["reasons"],
                            }
                        )
                    )
                    return _block(
                        request_id,
                        f"Request blocked by Bedrock Guardrail in tool '{tool_name}' "
                        f"({', '.join(gr['reasons']) or 'policy'}).",
                    )
                if gr["action"] == "ERROR":
                    # Don't fail the request on a guardrail outage; log and continue
                    # (the local SQL/PII controls still apply). Tune to fail-closed
                    # for stricter environments.
                    logger.error(json.dumps({"event": "guardrail_error", "where": "request",
                                              "reasons": gr["reasons"]}))

    # --- PASS THROUGH ---
    return _passthrough(body)
