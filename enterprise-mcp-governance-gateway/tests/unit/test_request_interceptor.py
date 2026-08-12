# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the REQUEST interceptor Lambda.

Imports the handler directly from lambdas/request-interceptor/index.py by adding
that directory to sys.path. The path is resolved relative to this test file so the
tests pass regardless of the current working directory.
"""
import importlib.util
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_LAMBDA_DIR = os.path.join(_REPO_ROOT, "lambdas", "request-interceptor")
if _LAMBDA_DIR not in sys.path:
    sys.path.insert(0, _LAMBDA_DIR)

# Load index.py under a unique module name. Both interceptor lambdas ship a module
# called `index`, so a plain `from index import handler` would collide in sys.modules
# when both unit-test files run in the same pytest session.
_spec = importlib.util.spec_from_file_location(
    "request_interceptor_index", os.path.join(_LAMBDA_DIR, "index.py")
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
handler = _module.handler


def make_event(method="tools/call", tool_name="DocsAPI___get_page", arguments=None, headers=None):
    """Construct a valid REQUEST interceptor input event (interceptorInputVersion 1.0)."""
    return {
        "interceptorInputVersion": "1.0",
        "mcp": {
            "rawGatewayRequest": {"body": "{}"},
            "gatewayRequest": {
                "path": "/mcp",
                "httpMethod": "POST",
                "headers": headers
                or {
                    # Unsigned demo JWT; payload decodes to {"sub": "test@example.com"}
                    "Authorization": "Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
                    "eyJzdWIiOiAidGVzdEBleGFtcGxlLmNvbSJ9.abc"
                },
                "body": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": {"name": tool_name, "arguments": arguments or {}},
                },
            },
        },
    }


def test_passthrough_normal_request():
    """Normal request should pass through unchanged."""
    event = make_event(tool_name="DocsAPI___get_page", arguments={"pageId": "arch-overview"})
    result = handler(event, None)

    assert result["interceptorOutputVersion"] == "1.0"
    assert "transformedGatewayRequest" in result["mcp"]
    assert "transformedGatewayResponse" not in result["mcp"]


def test_blocks_dangerous_sql():
    """SQL with DROP TABLE should be blocked with a JSON-RPC error."""
    event = make_event(
        tool_name="DatabaseAPI___execute_query",
        arguments={"query": "DROP TABLE users"},
    )
    result = handler(event, None)

    assert result["interceptorOutputVersion"] == "1.0"
    assert "transformedGatewayResponse" in result["mcp"]
    response = result["mcp"]["transformedGatewayResponse"]
    assert response["body"]["error"]["code"] == -32600
    assert "dangerous SQL" in response["body"]["error"]["message"]


def test_blocks_sensitive_tool_outside_hours():
    """Sensitive tools are blocked outside 09:00-17:00 UTC; allowed during."""
    from datetime import datetime, timezone

    current_hour = datetime.now(timezone.utc).hour
    event = make_event(
        tool_name="DatabaseAPI___query_audit_logs",
        arguments={"startDate": "2026-01-01"},
    )
    result = handler(event, None)

    if current_hour < 9 or current_hour >= 17:
        assert "transformedGatewayResponse" in result["mcp"]
        msg = result["mcp"]["transformedGatewayResponse"]["body"]["error"]["message"]
        assert "business hours" in msg
    else:
        assert "transformedGatewayRequest" in result["mcp"]


def test_allows_safe_sql():
    """Safe SELECT query should pass through."""
    event = make_event(
        tool_name="DatabaseAPI___execute_query",
        arguments={"query": "SELECT * FROM users WHERE id = 1"},
    )
    result = handler(event, None)

    assert "transformedGatewayRequest" in result["mcp"]
    assert "transformedGatewayResponse" not in result["mcp"]


if __name__ == "__main__":
    test_passthrough_normal_request()
    test_blocks_dangerous_sql()
    test_blocks_sensitive_tool_outside_hours()
    test_allows_safe_sql()
    print("All request interceptor tests passed!")
