"""Authenticated, bounded OpenAPI fixture for Gateway-governance live gates.

Deployed by the gateway-governance live scenario as a private Lambda behind a Function URL and wired
into a product gateway connector. It exposes seven POST operations the scenario drives through the
product runtime to exercise the guardrail, Cedar-policy, and connector controls. Bearer auth (a
one-time SSM SecureString), a strict marker format, and a bounded value keep it inert to anything but
this run. Configuration arrives from a sibling ``fixture_config.json`` the scenario writes into the
deployment package, so this module is candidate- and account-agnostic.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from pathlib import Path
from typing import Any

import boto3

_CONFIG = json.loads(Path(__file__).with_name("fixture_config.json").read_text(encoding="utf-8"))
if set(_CONFIG) != {"region", "secret_parameter", "fixture_id", "blocked_result"}:
    raise RuntimeError("fixture configuration schema differs")
_REGION = str(_CONFIG["region"])
_SECRET_PARAMETER = str(_CONFIG["secret_parameter"])
_FIXTURE_ID = str(_CONFIG["fixture_id"])
_BLOCKED_RESULT = str(_CONFIG["blocked_result"])

_LOG = logging.getLogger("acfpr.gateway_governance_fixture")
_SSM = None
_SECRET: str | None = None
_MARKER = re.compile(r"^gg-[0-9a-f]{12}-[a-z0-9-]{3,48}$")
_PATHS = {
    "/guardrail-safe-echo": "guardrail_safe_echo",
    "/guardrail-blocked-result": "guardrail_blocked_result",
    "/policy-allowed": "policy_allowed_probe",
    "/policy-denied": "policy_denied_probe",
    "/connector-allowed": "connector_allowed_probe",
    "/connector-denied": "connector_denied_probe",
    "/connector-guardrail-denied": "connector_guardrail_denied",
}


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        },
        "body": json.dumps(body, separators=(",", ":"), sort_keys=True),
    }


def _secret() -> str:
    global _SECRET, _SSM
    if _SECRET is None:
        if _SSM is None:
            _SSM = boto3.client("ssm", region_name=_REGION)
        value = _SSM.get_parameter(Name=_SECRET_PARAMETER, WithDecryption=True)["Parameter"]["Value"]
        if not isinstance(value, str) or not value:
            raise RuntimeError("fixture credential is empty")
        _SECRET = value
    return _SECRET


def _headers(event: dict[str, Any]) -> dict[str, str]:
    raw = event.get("headers") or {}
    return {str(key).casefold(): str(value) for key, value in raw.items()}


def _arguments(event: dict[str, Any]) -> dict[str, str]:
    raw = event.get("queryStringParameters") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    expected = f"Bearer {_secret()}"
    supplied = _headers(event).get("authorization", "")
    if not hmac.compare_digest(supplied, expected):
        return _response(401, {"error": "unauthorized"})

    path = str(event.get("rawPath") or "")
    operation = _PATHS.get(path)
    if operation is None or str(event.get("requestContext", {}).get("http", {}).get("method")) != "POST":
        return _response(404, {"error": "not_found"})

    arguments = _arguments(event)
    marker = arguments.get("marker")
    value = arguments.get("value", "")
    if not isinstance(marker, str) or _MARKER.fullmatch(marker) is None:
        return _response(400, {"error": "invalid_marker"})
    if not isinstance(value, str) or len(value.encode("utf-8")) > 256:
        return _response(400, {"error": "invalid_value"})

    _LOG.warning(
        json.dumps(
            {"event": "gateway-governance-fixture", "fixtureId": _FIXTURE_ID, "marker": marker, "operation": operation},
            separators=(",", ":"),
            sort_keys=True,
        )
    )

    if operation == "guardrail_safe_echo":
        return _response(200, {"kind": "guardrail-safe", "marker": marker, "value": value})
    if operation == "guardrail_blocked_result":
        return _response(200, {"kind": "guardrail-result", "marker": marker, "value": _BLOCKED_RESULT})
    if operation == "policy_allowed_probe":
        return _response(200, {"kind": "policy-allowed", "marker": marker})
    if operation == "policy_denied_probe":
        return _response(200, {"kind": "policy-denied-unexpected", "marker": marker})
    if operation == "connector_allowed_probe":
        return _response(200, {"kind": "connector-allowed", "marker": marker})
    if operation == "connector_denied_probe":
        return _response(200, {"kind": "connector-denied-unexpected", "marker": marker})
    return _response(200, {"kind": "connector-guardrail", "marker": marker, "value": value})
