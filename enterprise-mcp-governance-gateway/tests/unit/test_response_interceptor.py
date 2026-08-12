# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the RESPONSE interceptor Lambda.

Imports the handler directly from lambdas/response-interceptor/index.py by adding
that directory to sys.path. The path is resolved relative to this test file so the
tests pass regardless of the current working directory.

Note: Tests validate PII redaction patterns. For production with regulated data,
HIPAA controls are required for PHI, PCI-DSS for payment data, and GDPR for EU
user data. See AWS compliance documentation.
"""
import importlib.util
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_LAMBDA_DIR = os.path.join(_REPO_ROOT, "lambdas", "response-interceptor")
if _LAMBDA_DIR not in sys.path:
    sys.path.insert(0, _LAMBDA_DIR)

# Load index.py under a unique module name. Both interceptor lambdas ship a module
# called `index`, so a plain `from index import handler` would collide in sys.modules
# when both unit-test files run in the same pytest session.
_spec = importlib.util.spec_from_file_location(
    "response_interceptor_index", os.path.join(_LAMBDA_DIR, "index.py")
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
handler = _module.handler


def make_event(tool_name="DatabaseAPI___execute_query", response_text="Hello world"):
    """Construct a valid RESPONSE interceptor input event (interceptorInputVersion 1.0)."""
    return {
        "interceptorInputVersion": "1.0",
        "mcp": {
            "rawGatewayRequest": {"body": "{}"},
            "gatewayRequest": {
                "path": "/mcp",
                "httpMethod": "POST",
                "headers": {},
                "body": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": {}},
                },
            },
            "gatewayResponse": {
                "statusCode": 200,
                "headers": {"Mcp-Session-Id": "test-session"},
                "body": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "text", "text": response_text}]},
                },
            },
        },
    }


def test_redacts_email():
    """Emails in the response should be redacted."""
    event = make_event(response_text="Contact alice@example.com for details")
    result = handler(event, None)

    body = result["mcp"]["transformedGatewayResponse"]["body"]
    text = body["result"]["content"][0]["text"]
    assert "alice@example.com" not in text
    assert "[REDACTED_EMAIL]" in text


def test_redacts_ssn():
    """SSNs in the response should be redacted."""
    event = make_event(response_text="SSN: 123-45-6789")
    result = handler(event, None)

    body = result["mcp"]["transformedGatewayResponse"]["body"]
    text = body["result"]["content"][0]["text"]
    assert "123-45-6789" not in text
    assert "[REDACTED_SSN]" in text


def test_redacts_credit_card():
    """Credit card numbers should be redacted."""
    event = make_event(response_text="Card: 4111-1111-1111-1111")
    result = handler(event, None)

    body = result["mcp"]["transformedGatewayResponse"]["body"]
    text = body["result"]["content"][0]["text"]
    assert "4111-1111-1111-1111" not in text
    assert "[REDACTED_CREDIT_CARD]" in text


def test_truncates_large_response():
    """Responses over the size limit should be truncated."""
    large_text = "x" * 60_000
    event = make_event(response_text=large_text)
    result = handler(event, None)

    body = result["mcp"]["transformedGatewayResponse"]["body"]
    text = body["result"]["content"][0]["text"]
    assert len(text) <= 55_000  # 50K + truncation message
    assert "[TRUNCATED" in text


def test_passthrough_clean_response():
    """A clean response without PII should pass through unchanged."""
    event = make_event(response_text="The weather is sunny, 72F")
    result = handler(event, None)

    assert result["interceptorOutputVersion"] == "1.0"
    body = result["mcp"]["transformedGatewayResponse"]["body"]
    assert body["result"]["content"][0]["text"] == "The weather is sunny, 72F"


if __name__ == "__main__":
    test_redacts_email()
    test_redacts_ssn()
    test_redacts_credit_card()
    test_truncates_large_response()
    test_passthrough_clean_response()
    print("All response interceptor tests passed!")
