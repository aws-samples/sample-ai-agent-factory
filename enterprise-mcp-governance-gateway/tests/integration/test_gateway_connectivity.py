# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Integration tests for the deployed AgentCore Gateway.

Requires the gateway to be deployed and these environment variables set:
  GATEWAY_URL   the full MCP endpoint returned by create_gateway (gatewayUrl).
  AUTH_TOKEN    a Bearer JWT for inbound auth (run `source scripts/get-token.sh`).

Run with:  GATEWAY_URL=... AUTH_TOKEN=... pytest tests/integration -v
If GATEWAY_URL is unset the whole module is skipped (so unit-test runs are clean).

These tests assert real, server-side governance behaviour. They never mock AWS.

Note: Tests validate PII/PHI redaction and sensitive data governance. For
production with regulated data, HIPAA controls are required for PHI, PCI-DSS for
payment data, and GDPR for EU user data. See AWS compliance documentation.
"""
import json
import os

import pytest

try:
    import requests
except ImportError:  # pragma: no cover - requests is a documented prerequisite
    requests = None

GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")

pytestmark = [
    pytest.mark.skipif(not GATEWAY_URL, reason="GATEWAY_URL not set; gateway not deployed"),
    pytest.mark.skipif(requests is None, reason="requests not installed"),
]


def mcp_request(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    """Send an MCP JSON-RPC request to the Gateway and return the parsed result.

    The Gateway speaks Streamable HTTP; it may answer with application/json or an
    SSE (text/event-stream) frame. Handle both.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        # 2025-11-25 enables MCP URL-mode elicitation, which the per-user 3LO
        # connectors (e.g. Atlassian) use; the gateway also supports 2025-03-26.
        "Mcp-Protocol-Version": "2025-11-25",
    }
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"

    payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    resp = requests.post(GATEWAY_URL, json=payload, headers=headers, timeout=30)

    ctype = resp.headers.get("Content-Type", "")
    if "text/event-stream" in ctype:
        # Parse the last `data:` line of the SSE stream as the JSON-RPC message.
        last = None
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                last = line[len("data:"):].strip()
        if last:
            return json.loads(last)
        raise AssertionError(f"No SSE data frame in response: {resp.text!r}")
    return resp.json()


def test_tools_list():
    """Gateway should return tools from the registered targets."""
    result = mcp_request("tools/list")
    assert "result" in result, f"Expected result, got: {result}"
    tools = result["result"]["tools"]
    tool_names = [t["name"] for t in tools]

    assert any("get_page" in name for name in tool_names), f"Missing docs tools. Got: {tool_names}"
    assert any(
        "execute_query" in name for name in tool_names
    ), f"Missing db tools. Got: {tool_names}"


def test_tool_call_allowed():
    """Calling an allowed tool (DocsAPI___get_page) should succeed."""
    result = mcp_request(
        "tools/call",
        {"name": "DocsAPI___get_page", "arguments": {"pageId": "arch-overview"}},
        request_id=2,
    )
    assert "result" in result, f"Expected result, got: {result}"
    content = result["result"]["content"]
    assert len(content) > 0


def test_tool_call_blocked_by_policy():
    """Calling a Cedar-forbidden tool (DatabaseAPI___drop_table) should be denied.

    A DENY may surface either as a JSON-RPC `error` or as an MCP result flagged
    `isError`. Accept either; the call must NOT succeed normally.
    """
    result = mcp_request(
        "tools/call",
        {"name": "DatabaseAPI___drop_table", "arguments": {"tableName": "users"}},
        request_id=3,
    )
    denied = "error" in result or result.get("result", {}).get("isError") is True
    assert denied, f"Expected policy DENY, got: {result}"


def test_sql_injection_blocked():
    """A SQL-injection payload should be blocked by the REQUEST interceptor."""
    result = mcp_request(
        "tools/call",
        {"name": "DatabaseAPI___execute_query", "arguments": {"query": "DROP TABLE users; --"}},
        request_id=4,
    )

    message = ""
    if "error" in result:
        message = result["error"].get("message", "")
    elif "result" in result:
        # Interceptor may surface the block inside the result content.
        content = result["result"].get("content", [])
        message = " ".join(item.get("text", "") for item in content)

    assert "dangerous SQL" in message or result.get("result", {}).get("isError") is True, (
        f"Expected interceptor block, got: {result}"
    )


def test_pii_redaction():
    """PII in a response should be redacted by the RESPONSE interceptor.

    If the user is not authorised for export_pii_report, the call is denied by
    policy instead — also acceptable, since the raw PII is still never returned.
    """
    result = mcp_request(
        "tools/call",
        {"name": "DatabaseAPI___export_pii_report", "arguments": {"department": "engineering"}},
        request_id=5,
    )

    if "result" in result and not result["result"].get("isError"):
        text = result["result"]["content"][0]["text"]
        assert "123-45-6789" not in text, "SSN was not redacted!"
        # Redaction may come from the local regex interceptor ("[REDACTED_SSN]") or the
        # managed Bedrock Guardrail ANONYMIZE token ("{US_SOCIAL_SECURITY_NUMBER}").
        # The control is "raw PII is never returned" — accept either marker format.
        assert "[REDACTED_SSN]" in text or "{US_SOCIAL_SECURITY_NUMBER}" in text, (
            f"expected an SSN redaction marker, got: {text}"
        )
    else:
        # Denied by policy (e.g. caller is not security-admin) — raw PII not exposed.
        assert "error" in result or result["result"].get("isError") is True


# ---------------------------------------------------------------------------
# Atlassian connector (per-user 3LO target) — read allowed, writes role-gated.
#
# These run only when the Atlassian connector is deployed (skipped otherwise).
# They are consent-AGNOSTIC: the caller may not have completed the one-time
# Atlassian OAuth consent, so a read may return live data OR a -32042 consent
# elicitation — both prove Cedar PERMITS the read. Writes are Cedar-denied
# BEFORE any 3LO, so that outcome is deterministic regardless of consent.
# ---------------------------------------------------------------------------
def _atlassian_tool_names() -> list[str]:
    result = mcp_request("tools/list")
    return [t["name"] for t in result.get("result", {}).get("tools", [])]


def _require_atlassian() -> list[str]:
    names = _atlassian_tool_names()
    if not any(n.startswith("Atlassian___") for n in names):
        pytest.skip("Atlassian connector not deployed (no Atlassian___ tools)")
    return names


def _is_policy_denied(result: dict) -> bool:
    """True if the gateway denied the call via Cedar (not a 3LO consent prompt)."""
    if result.get("result", {}).get("isError") is True:
        text = " ".join(
            i.get("text", "") for i in result["result"].get("content", [])
        ).lower()
        return "policy" in text or "denied" in text or "not allowed" in text
    err = result.get("error") or {}
    if err.get("code") == -32042:  # URL elicitation = permitted, awaiting consent
        return False
    msg = str(err.get("message", "")).lower()
    return "policy" in msg or "denied" in msg or "not allowed" in msg


def test_atlassian_tools_filtered_by_cedar():
    """Read tools are visible; role-gated write tools are filtered from tools/list."""
    names = _require_atlassian()
    assert "Atlassian___getVisibleJiraProjects" in names, (
        f"Atlassian read tool missing from tools/list. Got: {names}"
    )
    assert "Atlassian___createJiraIssue" not in names, (
        f"Atlassian write tool should be Cedar-hidden, but appeared. Got: {names}"
    )


def test_atlassian_read_permitted():
    """A read (getVisibleJiraProjects) is Cedar-permitted: live data OR -32042 consent."""
    _require_atlassian()
    result = mcp_request(
        "tools/call",
        {"name": "Atlassian___getVisibleJiraProjects", "arguments": {}},
        request_id=6,
    )
    assert not _is_policy_denied(result), f"Atlassian read was policy-denied: {result}"
    # Must be either a -32042 consent prompt or a successful (non-error) result.
    ok = (result.get("error", {}) or {}).get("code") == -32042 or (
        "result" in result and not result["result"].get("isError")
    )
    assert ok, f"Unexpected Atlassian read outcome: {result}"


def test_atlassian_write_denied():
    """A role-gated write (createJiraIssue) is Cedar-denied (default-deny; no issue created)."""
    _require_atlassian()
    result = mcp_request(
        "tools/call",
        {"name": "Atlassian___createJiraIssue",
         "arguments": {"projectKey": "TEST", "summary": "integration-test (should be denied)"}},
        request_id=7,
    )
    assert _is_policy_denied(result), f"Expected Cedar DENY for Atlassian write, got: {result}"


if __name__ == "__main__":
    if not GATEWAY_URL:
        raise SystemExit("Set GATEWAY_URL (and AUTH_TOKEN) to run integration tests.")
    test_tools_list()
    print("tools/list OK")
    test_tool_call_allowed()
    print("allowed call OK")
    test_tool_call_blocked_by_policy()
    print("policy deny OK")
    test_sql_injection_blocked()
    print("sql injection block OK")
    test_pii_redaction()
    print("pii redaction OK")

    # Atlassian connector (skipped if not deployed).
    for label, fn in [
        ("atlassian tools filtered", test_atlassian_tools_filtered_by_cedar),
        ("atlassian read permitted", test_atlassian_read_permitted),
        ("atlassian write denied", test_atlassian_write_denied),
    ]:
        try:
            fn()
            print(f"{label} OK")
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "Skipped":
                print(f"{label} SKIPPED ({exc})")
            else:
                raise

    print("\nAll integration tests passed!")
