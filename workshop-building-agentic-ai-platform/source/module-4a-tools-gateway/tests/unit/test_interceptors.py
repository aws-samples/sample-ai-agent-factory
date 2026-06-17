# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for AgentCore Gateway interceptor Lambda handlers.

Validates request interceptor (audit logging) and response interceptor
(field sanitization + Bedrock Guardrails).
"""

import base64
import json
import os
from unittest.mock import MagicMock, patch

import pytest


def _make_fake_jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


def _wrap_gateway_request(headers: dict, body) -> dict:
    return {"mcp": {"gatewayRequest": {"headers": headers, "body": body}}}


def _wrap_gateway_response(body, headers: dict | None = None, request_headers: dict | None = None) -> dict:
    envelope: dict = {"body": body}
    if headers:
        envelope["headers"] = headers
    event = {"mcp": {"gatewayResponse": envelope}}
    if request_headers:
        event["mcp"]["gatewayRequest"] = {"headers": request_headers}
    return event


@pytest.fixture(autouse=True)
def env_setup():
    os.environ["AUDIT_TABLE_NAME"] = "tools-audit-log"
    os.environ["AWS_REGION"] = "us-west-2"
    yield
    os.environ.pop("AUDIT_TABLE_NAME", None)
    os.environ.pop("AWS_REGION", None)


class TestRequestInterceptor:

    @patch("handlers.interceptors.boto3")
    def test_call_tool_request_logged(self, mock_boto):
        from handlers.interceptors import request_interceptor_handler

        mock_table = MagicMock()
        mock_boto.resource.return_value.Table.return_value = mock_table

        token = _make_fake_jwt({"sub": "user-abc", "cognito:groups": ["developers"]})
        event = _wrap_gateway_request(
            headers={"Authorization": f"Bearer {token}"},
            body={"jsonrpc": "2.0", "id": "req-1", "method": "tools/call",
                  "params": {"name": "get_customer_orders", "arguments": {}}},
        )

        result = request_interceptor_handler(event, None)

        assert result["interceptorOutputVersion"] == "1.0"
        transformed = result["mcp"]["transformedGatewayRequest"]
        assert transformed["body"]["method"] == "tools/call"
        mock_table.put_item.assert_called_once()
        audit_item = mock_table.put_item.call_args.kwargs["Item"]
        assert audit_item["toolId"] == "gateway:get_customer_orders"
        assert audit_item["actor"] == "user-abc"

    @patch("handlers.interceptors.boto3")
    def test_list_tools_request_logged(self, mock_boto):
        from handlers.interceptors import request_interceptor_handler

        mock_table = MagicMock()
        mock_boto.resource.return_value.Table.return_value = mock_table

        token = _make_fake_jwt({"sub": "user-xyz"})
        event = _wrap_gateway_request(
            headers={"Authorization": f"Bearer {token}"},
            body={"jsonrpc": "2.0", "id": "req-2", "method": "tools/list"},
        )

        result = request_interceptor_handler(event, None)
        assert result["interceptorOutputVersion"] == "1.0"
        mock_table.put_item.assert_called_once()
        assert mock_table.put_item.call_args.kwargs["Item"]["action"] == "GATEWAY_TOOLS_LIST"

    @patch("handlers.interceptors.boto3")
    def test_non_tool_request_passes_through(self, mock_boto):
        from handlers.interceptors import request_interceptor_handler

        mock_table = MagicMock()
        mock_boto.resource.return_value.Table.return_value = mock_table

        event = _wrap_gateway_request(
            headers={},
            body={"jsonrpc": "2.0", "id": "req-3", "method": "ping"},
        )

        result = request_interceptor_handler(event, None)
        assert result["interceptorOutputVersion"] == "1.0"
        mock_table.put_item.assert_not_called()

    @patch("handlers.interceptors.boto3")
    def test_audit_failure_does_not_crash(self, mock_boto):
        from handlers.interceptors import request_interceptor_handler

        mock_table = MagicMock()
        mock_table.put_item.side_effect = Exception("DynamoDB unavailable")
        mock_boto.resource.return_value.Table.return_value = mock_table

        token = _make_fake_jwt({"sub": "user-fail"})
        event = _wrap_gateway_request(
            headers={"Authorization": f"Bearer {token}"},
            body={"method": "tools/call", "params": {"name": "failing_tool"}},
        )

        result = request_interceptor_handler(event, None)
        assert result["interceptorOutputVersion"] == "1.0"

    @patch("handlers.interceptors.boto3")
    def test_m2m_token_extracts_client_id(self, mock_boto):
        from handlers.interceptors import request_interceptor_handler

        mock_table = MagicMock()
        mock_boto.resource.return_value.Table.return_value = mock_table

        token = _make_fake_jwt({"client_id": "m2m-client-123"})
        event = _wrap_gateway_request(
            headers={"Authorization": f"Bearer {token}"},
            body={"method": "tools/call", "params": {"name": "m2m_tool"}},
        )

        result = request_interceptor_handler(event, None)
        assert mock_table.put_item.call_args.kwargs["Item"]["actor"] == "m2m-client-123"

    def test_flat_format_fallback(self):
        from handlers.interceptors import request_interceptor_handler

        token = _make_fake_jwt({"sub": "flat-user"})
        event = {
            "headers": {"Authorization": f"Bearer {token}"},
            "body": {"method": "tools/call", "params": {"name": "flat_tool"}},
        }

        with patch("handlers.interceptors.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table
            result = request_interceptor_handler(event, None)

        assert result["interceptorOutputVersion"] == "1.0"
        mock_table.put_item.assert_called_once()


class TestRequestInterceptorAccessControl:
    """Tests for group-based access control in request interceptor."""

    POLICY = json.dumps({
        "gateway-admins": ["*"],
        "gateway-developers": ["product-*", "order-*"],
    })

    @patch("handlers.interceptors.TOOL_ACCESS_POLICY", POLICY)
    @patch("handlers.interceptors.boto3")
    def test_admin_can_call_any_tool(self, mock_boto):
        from handlers.interceptors import request_interceptor_handler

        mock_boto.resource.return_value.Table.return_value = MagicMock()
        token = _make_fake_jwt({"sub": "admin-1", "cognito:groups": ["gateway-admins"]})
        event = _wrap_gateway_request(
            headers={"Authorization": f"Bearer {token}"},
            body={"jsonrpc": "2.0", "id": "1", "method": "tools/call",
                  "params": {"name": "secret-internal-tool"}},
        )

        result = request_interceptor_handler(event, None)
        body = result["mcp"]["transformedGatewayRequest"]["body"]
        assert "error" not in body

    @patch("handlers.interceptors.TOOL_ACCESS_POLICY", POLICY)
    @patch("handlers.interceptors.boto3")
    def test_developer_can_call_allowed_tool(self, mock_boto):
        from handlers.interceptors import request_interceptor_handler

        mock_boto.resource.return_value.Table.return_value = MagicMock()
        token = _make_fake_jwt({"sub": "dev-1", "cognito:groups": ["gateway-developers"]})
        event = _wrap_gateway_request(
            headers={"Authorization": f"Bearer {token}"},
            body={"jsonrpc": "2.0", "id": "1", "method": "tools/call",
                  "params": {"name": "product-info"}},
        )

        result = request_interceptor_handler(event, None)
        body = result["mcp"]["transformedGatewayRequest"]["body"]
        assert "error" not in body

    @patch("handlers.interceptors.TOOL_ACCESS_POLICY", POLICY)
    @patch("handlers.interceptors.boto3")
    def test_developer_blocked_from_unauthorized_tool(self, mock_boto):
        from handlers.interceptors import request_interceptor_handler

        mock_boto.resource.return_value.Table.return_value = MagicMock()
        token = _make_fake_jwt({"sub": "dev-1", "cognito:groups": ["gateway-developers"]})
        event = _wrap_gateway_request(
            headers={"Authorization": f"Bearer {token}"},
            body={"jsonrpc": "2.0", "id": "1", "method": "tools/call",
                  "params": {"name": "secret-internal-tool"}},
        )

        result = request_interceptor_handler(event, None)
        body = result["mcp"]["transformedGatewayRequest"]["body"]
        assert body["error"]["code"] == -32600
        assert "Access denied" in body["error"]["message"]

    @patch("handlers.interceptors.TOOL_ACCESS_POLICY", POLICY)
    @patch("handlers.interceptors.boto3")
    def test_no_groups_blocked(self, mock_boto):
        from handlers.interceptors import request_interceptor_handler

        mock_boto.resource.return_value.Table.return_value = MagicMock()
        token = _make_fake_jwt({"sub": "anon-user"})
        event = _wrap_gateway_request(
            headers={"Authorization": f"Bearer {token}"},
            body={"jsonrpc": "2.0", "id": "1", "method": "tools/call",
                  "params": {"name": "product-info"}},
        )

        result = request_interceptor_handler(event, None)
        body = result["mcp"]["transformedGatewayRequest"]["body"]
        assert body["error"]["code"] == -32600

    @patch("handlers.interceptors.TOOL_ACCESS_POLICY", "")
    @patch("handlers.interceptors.boto3")
    def test_no_policy_allows_all(self, mock_boto):
        from handlers.interceptors import request_interceptor_handler

        mock_boto.resource.return_value.Table.return_value = MagicMock()
        token = _make_fake_jwt({"sub": "anyone"})
        event = _wrap_gateway_request(
            headers={"Authorization": f"Bearer {token}"},
            body={"jsonrpc": "2.0", "id": "1", "method": "tools/call",
                  "params": {"name": "any-tool"}},
        )

        result = request_interceptor_handler(event, None)
        body = result["mcp"]["transformedGatewayRequest"]["body"]
        assert "error" not in body

    @patch("handlers.interceptors.TOOL_ACCESS_POLICY", POLICY)
    @patch("handlers.interceptors.boto3")
    def test_tools_list_passes_through_request_interceptor(self, mock_boto):
        """tools/list is not blocked at request time — filtered at response time."""
        from handlers.interceptors import request_interceptor_handler

        mock_boto.resource.return_value.Table.return_value = MagicMock()
        token = _make_fake_jwt({"sub": "dev-1", "cognito:groups": ["gateway-developers"]})
        event = _wrap_gateway_request(
            headers={"Authorization": f"Bearer {token}"},
            body={"jsonrpc": "2.0", "id": "1", "method": "tools/list"},
        )

        result = request_interceptor_handler(event, None)
        body = result["mcp"]["transformedGatewayRequest"]["body"]
        assert "error" not in body
        assert body["method"] == "tools/list"


class TestResponseInterceptor:

    def test_strips_internal_fields_from_tools_list(self):
        from handlers.interceptors import response_interceptor_handler

        event = _wrap_gateway_response(body={
            "jsonrpc": "2.0", "id": "resp-1",
            "result": {"tools": [{
                "name": "visible_tool", "description": "A good tool",
                "gatewayTargetId": "target-secret-123",
                "embedding": [0.1, 0.2], "securityScanResult": {"passed": True},
                "healthCheckMessage": "OK", "lastHealthCheck": "2026-01-01",
                "createdBy": "user-secret-456",
            }]},
        })

        result = response_interceptor_handler(event, None)
        tool = result["mcp"]["transformedGatewayResponse"]["body"]["result"]["tools"][0]
        assert tool["name"] == "visible_tool"
        for field in ["gatewayTargetId", "embedding", "securityScanResult",
                      "healthCheckMessage", "lastHealthCheck", "createdBy"]:
            assert field not in tool

    def test_non_tools_response_passes_through(self):
        from handlers.interceptors import response_interceptor_handler

        event = _wrap_gateway_response(
            body={"jsonrpc": "2.0", "id": "resp-3", "result": {"status": "ok"}},
        )
        result = response_interceptor_handler(event, None)
        assert result["mcp"]["transformedGatewayResponse"]["body"]["result"]["status"] == "ok"

    def test_invalid_body_passes_through(self):
        from handlers.interceptors import response_interceptor_handler

        event = _wrap_gateway_response(body="not-json{{{")
        result = response_interceptor_handler(event, None)
        assert result["mcp"]["transformedGatewayResponse"]["body"] == {}


class TestResponseInterceptorAccessControl:
    """Tests for group-based tool filtering in response interceptor."""

    POLICY = json.dumps({
        "gateway-admins": ["*"],
        "gateway-developers": ["product-*", "order-*"],
    })

    @patch("handlers.interceptors.TOOL_ACCESS_POLICY", POLICY)
    def test_admin_sees_all_tools(self):
        from handlers.interceptors import response_interceptor_handler

        token = _make_fake_jwt({"sub": "admin-1", "cognito:groups": ["gateway-admins"]})
        event = _wrap_gateway_response(
            body={
                "jsonrpc": "2.0", "id": "1",
                "result": {"tools": [
                    {"name": "product-info", "description": "Product info"},
                    {"name": "secret-internal", "description": "Internal tool"},
                    {"name": "order-status", "description": "Order status"},
                ]},
            },
            request_headers={"Authorization": f"Bearer {token}"},
        )

        result = response_interceptor_handler(event, None)
        tools = result["mcp"]["transformedGatewayResponse"]["body"]["result"]["tools"]
        assert len(tools) == 3

    @patch("handlers.interceptors.TOOL_ACCESS_POLICY", POLICY)
    def test_developer_sees_only_allowed_tools(self):
        from handlers.interceptors import response_interceptor_handler

        token = _make_fake_jwt({"sub": "dev-1", "cognito:groups": ["gateway-developers"]})
        event = _wrap_gateway_response(
            body={
                "jsonrpc": "2.0", "id": "1",
                "result": {"tools": [
                    {"name": "product-info", "description": "Product info"},
                    {"name": "secret-internal", "description": "Internal tool"},
                    {"name": "order-status", "description": "Order status"},
                ]},
            },
            request_headers={"Authorization": f"Bearer {token}"},
        )

        result = response_interceptor_handler(event, None)
        tools = result["mcp"]["transformedGatewayResponse"]["body"]["result"]["tools"]
        names = [t["name"] for t in tools]
        assert "product-info" in names
        assert "order-status" in names
        assert "secret-internal" not in names
        assert len(tools) == 2

    @patch("handlers.interceptors.TOOL_ACCESS_POLICY", POLICY)
    def test_no_request_headers_skips_filtering(self):
        """If response event doesn't include request headers, skip filtering (fail-open)."""
        from handlers.interceptors import response_interceptor_handler

        event = _wrap_gateway_response(
            body={
                "jsonrpc": "2.0", "id": "1",
                "result": {"tools": [
                    {"name": "product-info"},
                    {"name": "secret-internal"},
                ]},
            },
        )

        result = response_interceptor_handler(event, None)
        tools = result["mcp"]["transformedGatewayResponse"]["body"]["result"]["tools"]
        assert len(tools) == 2

    @patch("handlers.interceptors.TOOL_ACCESS_POLICY", "")
    def test_no_policy_shows_all_tools(self):
        from handlers.interceptors import response_interceptor_handler

        token = _make_fake_jwt({"sub": "dev-1", "cognito:groups": ["gateway-developers"]})
        event = _wrap_gateway_response(
            body={
                "jsonrpc": "2.0", "id": "1",
                "result": {"tools": [
                    {"name": "tool-a"},
                    {"name": "tool-b"},
                ]},
            },
            request_headers={"Authorization": f"Bearer {token}"},
        )

        result = response_interceptor_handler(event, None)
        tools = result["mcp"]["transformedGatewayResponse"]["body"]["result"]["tools"]
        assert len(tools) == 2


class TestResponseInterceptorGuardrails:

    @patch("handlers.interceptors.BEDROCK_GUARDRAIL_ID", "grn-test123")
    @patch("handlers.interceptors.boto3")
    def test_guardrail_blocks_tool_output(self, mock_boto):
        from handlers.interceptors import response_interceptor_handler

        mock_client = MagicMock()
        mock_boto.client.return_value = mock_client
        mock_client.apply_guardrail.return_value = {
            "action": "GUARDRAIL_INTERVENED",
            "outputs": [{"text": "Content blocked by enterprise guardrail"}],
        }

        event = _wrap_gateway_response(body={
            "jsonrpc": "2.0", "id": "resp-gr-1",
            "result": {"content": [{"type": "text", "text": "Some sensitive output"}]},
        })

        result = response_interceptor_handler(event, None)
        body = result["mcp"]["transformedGatewayResponse"]["body"]
        assert body["result"]["content"][0]["text"] == "Content blocked by enterprise guardrail"

    @patch("handlers.interceptors.BEDROCK_GUARDRAIL_ID", "grn-test123")
    @patch("handlers.interceptors.boto3")
    def test_guardrail_passes_clean_content(self, mock_boto):
        from handlers.interceptors import response_interceptor_handler

        mock_client = MagicMock()
        mock_boto.client.return_value = mock_client
        mock_client.apply_guardrail.return_value = {"action": "NONE"}

        event = _wrap_gateway_response(body={
            "jsonrpc": "2.0", "id": "resp-gr-2",
            "result": {"content": [{"type": "text", "text": "Normal output"}]},
        })

        result = response_interceptor_handler(event, None)
        body = result["mcp"]["transformedGatewayResponse"]["body"]
        assert body["result"]["content"][0]["text"] == "Normal output"

    @patch("handlers.interceptors.BEDROCK_GUARDRAIL_ID", "")
    def test_guardrail_disabled_when_no_env_var(self):
        from handlers.interceptors import response_interceptor_handler

        event = _wrap_gateway_response(body={
            "result": {"content": [{"type": "text", "text": "Should pass through"}]},
        })

        result = response_interceptor_handler(event, None)
        body = result["mcp"]["transformedGatewayResponse"]["body"]
        assert body["result"]["content"][0]["text"] == "Should pass through"

    @patch("handlers.interceptors.BEDROCK_GUARDRAIL_ID", "grn-test123")
    @patch("handlers.interceptors.boto3")
    def test_guardrail_failure_does_not_crash(self, mock_boto):
        from handlers.interceptors import response_interceptor_handler

        mock_client = MagicMock()
        mock_boto.client.return_value = mock_client
        mock_client.apply_guardrail.side_effect = Exception("Bedrock unavailable")

        event = _wrap_gateway_response(body={
            "result": {"content": [{"type": "text", "text": "Original preserved"}]},
        })

        result = response_interceptor_handler(event, None)
        body = result["mcp"]["transformedGatewayResponse"]["body"]
        assert body["result"]["content"][0]["text"] == "Original preserved"
