# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Gateway interceptor Lambda handlers.

Request interceptor: audit logging + group-based access control for tool invocations.
Response interceptor: group-based tool filtering + field sanitization + Bedrock Guardrails.

Access control uses TOOL_ACCESS_POLICY env var — a JSON map of Cognito group names
to lists of allowed tool name patterns. Example:
    {"gateway-admins": ["*"], "gateway-developers": ["product-*", "order-*"]}
Patterns support trailing wildcard (*) and exact match. When a policy IS
configured, a tools/call is allowed only if one of the caller's groups matches a
pattern; unmatched tools are denied. When NO policy is configured the request
interceptor does not enforce access control and requests pass through unchanged
(fail-open) — see request_interceptor_handler, which gates enforcement on a
non-empty policy. (To make absence-of-policy deny-all, drop the `and policy`
guard there so `_is_tool_allowed` runs and returns False on an empty policy.)
"""

import base64
import fnmatch
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)

AUDIT_TABLE_NAME = os.environ.get("AUDIT_TABLE_NAME", "")
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
BEDROCK_GUARDRAIL_ID = os.environ.get("BEDROCK_GUARDRAIL_ID", "")
BEDROCK_GUARDRAIL_VERSION = os.environ.get("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
TOOL_ACCESS_POLICY = os.environ.get("TOOL_ACCESS_POLICY", "")

# Fields to strip from tools/list responses (internal metadata)
_INTERNAL_FIELDS = {
    "gatewayTargetId", "embedding", "securityScanResult",
    "healthCheckMessage", "lastHealthCheck", "createdBy",
}


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without signature verification (for audit logging only).

    WARNING: This MUST NOT be used for authorization decisions. The JWT signature
    is not verified, so claims could be forged. The AgentCore Gateway's CUSTOM_JWT
    authorizer handles actual token validation. This function only extracts claims
    for audit log enrichment.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        # Add padding
        payload += "=" * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def _extract_actor(headers: dict) -> str:
    """Extract actor identity from Authorization header."""
    auth = headers.get("Authorization", headers.get("authorization", ""))
    if not auth.startswith("Bearer "):
        return "unknown"
    token = auth[7:]
    claims = _decode_jwt_payload(token)
    return claims.get("sub", claims.get("client_id", "unknown"))


def _extract_caller_groups(headers: dict) -> list[str]:
    """Extract Cognito groups from JWT Authorization header."""
    auth = headers.get("Authorization", headers.get("authorization", ""))
    if not auth.startswith("Bearer "):
        return []
    claims = _decode_jwt_payload(auth[7:])
    groups = claims.get("cognito:groups", [])
    return groups if isinstance(groups, list) else []


def _load_access_policy() -> dict:
    """Load tool access policy from TOOL_ACCESS_POLICY env var.

    Returns dict mapping group names to lists of tool name patterns.
    Empty dict means no policy (allow all).
    """
    if not TOOL_ACCESS_POLICY:
        return {}
    try:
        policy = json.loads(TOOL_ACCESS_POLICY)
        return policy if isinstance(policy, dict) else {}
    except (json.JSONDecodeError, TypeError):
        logger.warning("Invalid TOOL_ACCESS_POLICY, allowing all access")
        return {}


def _is_tool_allowed(tool_name: str, caller_groups: list[str], policy: dict) -> bool:
    """Check if any of the caller's groups grants access to the named tool.

    Callers gate on a non-empty policy before invoking this (see the handlers),
    so the empty-policy branch below is defensive — in that case this returns
    False (deny). Returns True if any caller group has a matching pattern
    ("*" matches all, "prefix-*" matches prefix); otherwise False.
    """
    if not policy:
        logger.warning("No TOOL_ACCESS_POLICY configured — denying all tool access")
        return False
    for group in caller_groups:
        for pattern in policy.get(group, []):
            if fnmatch.fnmatch(tool_name, pattern):
                return True
    return False


def _extract_request(event: dict) -> tuple[dict, dict]:
    """Extract headers and body from gateway event (envelope or flat)."""
    if "mcp" in event and "gatewayRequest" in event.get("mcp", {}):
        req = event["mcp"]["gatewayRequest"]
        return req.get("headers", {}), req.get("body", {})
    return event.get("headers", {}), event.get("body", {})


def _extract_response(event: dict) -> tuple[dict, dict]:
    """Extract headers and body from gateway response event."""
    if "mcp" in event and "gatewayResponse" in event.get("mcp", {}):
        resp = event["mcp"]["gatewayResponse"]
        return resp.get("headers", {}), resp.get("body", {})
    return event.get("headers", {}), event.get("body", {})


def _parse_body(body) -> dict:
    """Parse body to dict, handling string or dict input."""
    if isinstance(body, str):
        try:
            return json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return {}
    if isinstance(body, dict):
        return body
    return {}


def request_interceptor_handler(event: dict, context) -> dict:
    """Request interceptor: audit log + enforce group-based access control."""
    headers, raw_body = _extract_request(event)
    body = _parse_body(raw_body)

    method = body.get("method", "")
    policy = _load_access_policy()

    # Audit tools/call and tools/list requests
    if method in ("tools/call", "tools/list") and AUDIT_TABLE_NAME:
        actor = _extract_actor(headers)
        tool_name = ""
        if method == "tools/call":
            params = body.get("params", {})
            tool_name = params.get("name", "")

        try:
            dynamo = boto3.resource("dynamodb", region_name=AWS_REGION)
            table = dynamo.Table(AUDIT_TABLE_NAME)
            table.put_item(Item={
                "toolId": f"gateway:{tool_name}" if tool_name else "gateway:list",
                "eventId": str(uuid.uuid4()),
                "action": "GATEWAY_TOOLS_CALL" if method == "tools/call" else "GATEWAY_TOOLS_LIST",
                "actor": actor,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "method": method,
                "toolName": tool_name,
            })
        except Exception as e:
            logger.warning("Audit log write failed (non-fatal): %s", e)

    # Enforce access control on tools/call
    if method == "tools/call" and policy:
        tool_name = body.get("params", {}).get("name", "")
        caller_groups = _extract_caller_groups(headers)

        if not _is_tool_allowed(tool_name, caller_groups, policy):
            logger.warning(
                "Access denied: groups=%s tool=%s", caller_groups, tool_name
            )
            return {
                "interceptorOutputVersion": "1.0",
                "mcp": {
                    "transformedGatewayRequest": {
                        "headers": headers,
                        "body": {
                            "jsonrpc": "2.0",
                            "id": body.get("id", ""),
                            "error": {
                                "code": -32600,
                                "message": f"Access denied: your team does not have permission to call '{tool_name}'",
                            },
                        },
                    }
                },
            }

    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "headers": headers,
                "body": body,
            }
        },
    }


def response_interceptor_handler(event: dict, context) -> dict:
    """Response interceptor: group-based filtering + sanitization + Guardrails."""
    headers, raw_body = _extract_response(event)
    body = _parse_body(raw_body)

    # Extract caller groups from original request (if available in event)
    req_headers = {}
    if "mcp" in event and "gatewayRequest" in event.get("mcp", {}):
        req_headers = event["mcp"]["gatewayRequest"].get("headers", {})

    policy = _load_access_policy()
    caller_groups = _extract_caller_groups(req_headers)

    # Filter + sanitize tools/list responses
    result = body.get("result", {})
    if isinstance(result, dict) and "tools" in result:
        tools = result["tools"]

        # Group-based filtering: only show tools the caller's groups can access
        if policy and req_headers:
            tools = [
                t for t in tools
                if _is_tool_allowed(t.get("name", ""), caller_groups, policy)
            ]
            result["tools"] = tools

        # Strip internal fields
        for tool in tools:
            for field in _INTERNAL_FIELDS:
                tool.pop(field, None)

    # Apply Bedrock Guardrails to tool output content
    if BEDROCK_GUARDRAIL_ID and isinstance(result, dict) and "content" in result:
        content_items = result.get("content", [])
        text_parts = [
            item.get("text", "")
            for item in content_items
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if text_parts:
            combined_text = "\n".join(text_parts)
            try:
                bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
                # Requires IAM permission: bedrock-runtime:ApplyGuardrail on arn:aws:bedrock:${Region}:${Account}:guardrail/${GuardrailId}
                guardrail_resp = bedrock.apply_guardrail(
                    guardrailIdentifier=BEDROCK_GUARDRAIL_ID,
                    guardrailVersion=BEDROCK_GUARDRAIL_VERSION,
                    source="OUTPUT",
                    content=[{"text": {"text": combined_text}}],
                )
                if guardrail_resp.get("action") == "GUARDRAIL_INTERVENED":
                    blocked_text = guardrail_resp["outputs"][0]["text"]
                    for item in content_items:
                        if isinstance(item, dict) and item.get("type") == "text":
                            item["text"] = blocked_text
            except Exception as e:
                # Fail-open: guardrail unavailability should not block tool responses.
                # For production fail-closed behavior, return an error response here instead.
                logger.warning("Guardrail check failed (fail-open): %s", type(e).__name__)
                try:
                    cw = boto3.client("cloudwatch", region_name=AWS_REGION)
                    cw.put_metric_data(
                        Namespace="AgentCoreGateway",
                        MetricData=[{
                            "MetricName": "GuardrailFailure",
                            "Value": 1,
                            "Unit": "Count",
                        }],
                    )
                except Exception:
                    pass  # Best-effort metric

    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "headers": headers,
                "body": body,
            }
        },
    }
