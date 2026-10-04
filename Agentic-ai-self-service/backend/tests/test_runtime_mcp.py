"""Product MCP route and bounded AgentCore JSON-RPC contract tests."""

from __future__ import annotations

import io
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.routers import runtime_mcp as router_module
from app.routers.runtime_mcp import router
from app.services import runtime_mcp
from app.services import runtime_target_context as target_context
from app.services.auth import _LOCAL_DEV_SUB, get_caller_sub
from app.services.runtime_target_context import OwnedRuntimeTarget
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

DEPLOYMENT_ID = "5bb2084b-d586-46d6-a5f3-494cd24cfc89"
RUNTIME_ID = "mcp_runtime_AbCdEf1234"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:eu-west-1:222222222222:runtime/{RUNTIME_ID}"
TARGET_ROLE = "arn:aws:iam::222222222222:role/AgentFactoryDeploymentRole"


class _McpClient:
    def __init__(
        self,
        *,
        sse: bool = False,
        malformed: bool = False,
        remote_error: bool = False,
    ) -> None:
        self.calls: list[dict] = []
        self.sse = sse
        self.malformed = malformed
        self.remote_error = remote_error

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["payload"])
        method = payload["method"]
        request_id = payload.get("id")
        session_id = kwargs.get("mcpSessionId") or "mcp-session-1"

        if self.malformed:
            return {
                "statusCode": 200,
                "mcpProtocolVersion": runtime_mcp.MCP_PROTOCOL_VERSION,
                "response": io.BytesIO(b"not-json"),
            }
        if method == "notifications/initialized":
            return {
                "statusCode": 202,
                "mcpProtocolVersion": runtime_mcp.MCP_PROTOCOL_VERSION,
                "mcpSessionId": session_id,
                "response": io.BytesIO(b""),
            }
        if self.remote_error and method == "tools/call":
            body = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "sensitive runtime detail"},
            }
        elif method == "initialize":
            body = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": runtime_mcp.MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "standalone-mcp",
                        "version": "1.0",
                    },
                },
            }
        elif method == "tools/list":
            cursor = payload.get("params", {}).get("cursor")
            if cursor:
                tools = [
                    {
                        "name": "fetch_url",
                        "description": "Fetch a public URL",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"url": {"type": "string"}},
                            "required": ["url"],
                        },
                    }
                ]
                result = {"tools": tools}
            else:
                tools = [
                    {
                        "name": "get_weather",
                        "description": "Read current weather",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                    {
                        "name": "search_web",
                        "description": "Search the public web",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                ]
                result = {"tools": tools, "nextCursor": "page-2"}
            body = {"jsonrpc": "2.0", "id": request_id, "result": result}
        elif method == "tools/call":
            body = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (f"weather-canary:{payload['params']['arguments'].get('city', '')}"),
                        }
                    ],
                    "structuredContent": {"source": "controlled-test"},
                    "isError": False,
                },
            }
        else:  # pragma: no cover - the allowed method set is pinned below
            raise AssertionError(method)

        encoded = json.dumps(body).encode()
        if self.sse:
            encoded = b"event: message\n" + b"data: " + encoded + b"\n\n"
        return {
            "statusCode": 200,
            "contentType": "text/event-stream" if self.sse else "application/json",
            "mcpProtocolVersion": runtime_mcp.MCP_PROTOCOL_VERSION,
            "mcpSessionId": session_id,
            "response": io.BytesIO(encoded),
        }


class _LiveSessionAgentCore(_McpClient):
    """AgentCore as measured live: a session follows runtimeSessionId, never Mcp-Session-Id alone.

    A request without a runtime session id lands in a fresh runtime session, and the reply's
    Mcp-Session-Id is that new session's id. The plain fake above echoes Mcp-Session-Id back,
    which is why a proxy that never pinned the runtime session looked continuous in tests.
    """

    def invoke_agent_runtime(self, **kwargs):
        runtime_session = kwargs.get("runtimeSessionId") or str(uuid.uuid4())
        reply = super().invoke_agent_runtime(**{**kwargs, "mcpSessionId": runtime_session})
        self.calls[-1] = kwargs
        return {**reply, "mcpSessionId": runtime_session, "runtimeSessionId": runtime_session}


def _target(client: _McpClient) -> OwnedRuntimeTarget:
    session = MagicMock()
    session.client.return_value = client
    return OwnedRuntimeTarget(
        runtime_id=RUNTIME_ID,
        version_id="v1",
        deployment_id=DEPLOYMENT_ID,
        region="eu-west-1",
        account_id="222222222222",
        role_arn=TARGET_ROLE,
        session=session,
        runtime_arn=RUNTIME_ARN,
        protocol="MCP",
    )


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_caller_sub] = lambda: _LOCAL_DEV_SUB
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def test_tools_route_performs_full_handshake_and_paginates(
    client: TestClient,
    monkeypatch,
):
    agentcore = _McpClient(sse=True)
    target = _target(agentcore)
    resolver = MagicMock(return_value=target)
    monkeypatch.setattr(
        router_module,
        "resolve_owned_deployment_runtime_target",
        resolver,
    )

    response = client.post(
        "/api/test-mcp-runtime/tools",
        json={"deploymentId": DEPLOYMENT_ID},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["protocolVersion"] == runtime_mcp.MCP_PROTOCOL_VERSION
    assert body["sessionId"] == "mcp-session-1"
    assert body["serverInfo"] == {"name": "standalone-mcp", "version": "1.0"}
    assert [tool["name"] for tool in body["tools"]] == [
        "get_weather",
        "search_web",
        "fetch_url",
    ]
    resolver.assert_called_once_with(
        DEPLOYMENT_ID,
        _LOCAL_DEV_SUB,
        required_protocol="MCP",
    )
    methods = [json.loads(call["payload"])["method"] for call in agentcore.calls]
    assert methods == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/list",
    ]
    for call in agentcore.calls:
        assert call["agentRuntimeArn"] == RUNTIME_ARN
        assert call["runtimeUserId"] == _LOCAL_DEV_SUB
        assert call["mcpProtocolVersion"] == runtime_mcp.MCP_PROTOCOL_VERSION
        assert call["accept"] == "application/json, text/event-stream"
        assert call["qualifier"] == "DEFAULT"
    assert agentcore.calls[1]["mcpSessionId"] == "mcp-session-1"
    assert agentcore.calls[2]["mcpSessionId"] == "mcp-session-1"


class _DeniedClient:
    """AgentCore refusing the invoke: the platform role lacks the for-user action."""

    def __init__(self, code: str = "AccessDeniedException") -> None:
        self.calls: list[dict] = []
        self.code = code

    def invoke_agent_runtime(self, **kwargs):
        from botocore.exceptions import ClientError

        self.calls.append(kwargs)
        raise ClientError(
            {"Error": {"Code": self.code, "Message": "User: arn:aws:sts::1:assumed-role/x is not authorized"}},
            "InvokeAgentRuntime",
        )


def test_a_denied_invoke_is_a_platform_misconfiguration_not_a_transient(
    client: TestClient,
    monkeypatch,
):
    """Live, 2026-09-28: the runtime was READY and IAM allowed InvokeAgentRuntime on both ARN
    forms, but every discovery died AccessDeniedException because the call carries
    ``runtimeUserId`` and AgentCore authorises that as InvokeAgentRuntimeForUser. The route
    answered 503 "temporarily unavailable" with a retry button no retry could satisfy."""
    agentcore = _DeniedClient()
    monkeypatch.setattr(
        router_module,
        "resolve_owned_deployment_runtime_target",
        MagicMock(return_value=_target(agentcore)),
    )

    response = client.post(
        "/api/test-mcp-runtime/tools",
        json={"deploymentId": DEPLOYMENT_ID},
    )

    assert response.status_code == 500, response.text
    detail = response.json()["detail"]
    assert "bedrock-agentcore:InvokeAgentRuntimeForUser" in detail
    assert "temporarily" not in detail.lower()
    assert "arn:aws:sts" not in detail  # the AWS message stays out of the response
    assert len(agentcore.calls) == 1  # initialize was attempted exactly once


def test_other_client_errors_still_read_as_unavailable(client: TestClient, monkeypatch):
    """The classification is exact: only AccessDeniedException names the missing action."""
    agentcore = _DeniedClient(code="ThrottlingException")
    monkeypatch.setattr(
        router_module,
        "resolve_owned_deployment_runtime_target",
        MagicMock(return_value=_target(agentcore)),
    )

    response = client.post(
        "/api/test-mcp-runtime/tools",
        json={"deploymentId": DEPLOYMENT_ID},
    )

    assert response.status_code == 503, response.text
    assert "temporarily unavailable" in response.json()["detail"]


def test_call_route_uses_only_the_named_bounded_operation(
    client: TestClient,
    monkeypatch,
):
    agentcore = _McpClient()
    target = _target(agentcore)
    monkeypatch.setattr(
        router_module,
        "resolve_owned_deployment_runtime_target",
        MagicMock(return_value=target),
    )

    response = client.post(
        "/api/test-mcp-runtime/call",
        json={
            "deploymentId": DEPLOYMENT_ID,
            "toolName": "get_weather",
            "arguments": {"city": "Dublin"},
            "sessionId": "mcp-session-1",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "protocolVersion": runtime_mcp.MCP_PROTOCOL_VERSION,
        "sessionId": "mcp-session-1",
        "content": [{"type": "text", "text": "weather-canary:Dublin"}],
        "structuredContent": {"source": "controlled-test"},
        "isError": False,
    }
    assert len(agentcore.calls) == 1
    call = agentcore.calls[0]
    assert call["mcpMethod"] == "tools/call"
    assert call["mcpName"] == "get_weather"
    assert call["mcpSessionId"] == "mcp-session-1"
    assert json.loads(call["payload"]) == {
        "jsonrpc": "2.0",
        "id": 1001,
        "method": "tools/call",
        "params": {
            "name": "get_weather",
            "arguments": {"city": "Dublin"},
        },
    }


def test_a_session_stays_in_the_runtime_session_that_initialize_opened(
    client: TestClient,
    monkeypatch,
):
    agentcore = _LiveSessionAgentCore()
    monkeypatch.setattr(
        router_module,
        "resolve_owned_deployment_runtime_target",
        MagicMock(return_value=_target(agentcore)),
    )

    tools = client.post("/api/test-mcp-runtime/tools", json={"deploymentId": DEPLOYMENT_ID})
    assert tools.status_code == 200, tools.text
    session_id = tools.json()["sessionId"]
    assert len(session_id) == 36
    assert "runtimeSessionId" not in agentcore.calls[0]
    for call in agentcore.calls[1:]:
        assert call["mcpSessionId"] == session_id
        assert call["runtimeSessionId"] == session_id

    for _ in range(2):
        called = client.post(
            "/api/test-mcp-runtime/call",
            json={
                "deploymentId": DEPLOYMENT_ID,
                "toolName": "get_weather",
                "arguments": {"city": "Dublin"},
                "sessionId": session_id,
            },
        )
        assert called.status_code == 200, called.text
        assert called.json()["sessionId"] == session_id
        assert agentcore.calls[-1]["mcpSessionId"] == session_id
        assert agentcore.calls[-1]["runtimeSessionId"] == session_id


@pytest.mark.parametrize("session_id", ["mcp-session-1", "x" * 32, "y" * 257])
def test_an_id_that_cannot_name_a_runtime_session_is_forwarded_as_the_mcp_id_only(
    client: TestClient,
    monkeypatch,
    session_id: str,
):
    agentcore = _McpClient()
    monkeypatch.setattr(
        router_module,
        "resolve_owned_deployment_runtime_target",
        MagicMock(return_value=_target(agentcore)),
    )

    response = client.post(
        "/api/test-mcp-runtime/call",
        json={
            "deploymentId": DEPLOYMENT_ID,
            "toolName": "get_weather",
            "arguments": {"city": "Dublin"},
            "sessionId": session_id,
        },
    )

    assert response.status_code == 200, response.text
    assert agentcore.calls[0]["mcpSessionId"] == session_id
    assert "runtimeSessionId" not in agentcore.calls[0]


def test_call_without_a_session_initializes_before_calling(
    client: TestClient,
    monkeypatch,
):
    agentcore = _McpClient()
    monkeypatch.setattr(
        router_module,
        "resolve_owned_deployment_runtime_target",
        MagicMock(return_value=_target(agentcore)),
    )

    response = client.post(
        "/api/test-mcp-runtime/call",
        json={
            "deploymentId": DEPLOYMENT_ID,
            "toolName": "get_weather",
            "arguments": {"city": "Dublin"},
        },
    )

    assert response.status_code == 200, response.text
    assert [json.loads(call["payload"])["method"] for call in agentcore.calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]


def test_route_accepts_no_caller_selected_aws_target_or_method(
    client: TestClient,
):
    response = client.post(
        "/api/test-mcp-runtime/tools",
        json={
            "deploymentId": DEPLOYMENT_ID,
            "runtimeArn": RUNTIME_ARN,
            "region": "us-east-1",
            "method": "resources/read",
        },
    )
    assert response.status_code == 422


def test_cross_tenant_or_missing_deployment_stays_a_404(
    client: TestClient,
    monkeypatch,
):
    resolver = MagicMock(
        side_effect=HTTPException(status_code=404, detail="Not found"),
    )
    monkeypatch.setattr(
        router_module,
        "resolve_owned_deployment_runtime_target",
        resolver,
    )

    response = client.post(
        "/api/test-mcp-runtime/tools",
        json={"deploymentId": DEPLOYMENT_ID},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


def test_http_runtime_is_refused_before_an_agentcore_client_is_created(
    client: TestClient,
    monkeypatch,
):
    resolver = MagicMock(
        side_effect=HTTPException(
            status_code=409,
            detail="This deployment uses the HTTP runtime protocol.",
        ),
    )
    monkeypatch.setattr(
        router_module,
        "resolve_owned_deployment_runtime_target",
        resolver,
    )

    response = client.post(
        "/api/test-mcp-runtime/tools",
        json={"deploymentId": DEPLOYMENT_ID},
    )
    assert response.status_code == 409


def test_remote_jsonrpc_error_is_sanitized(
    client: TestClient,
    monkeypatch,
):
    agentcore = _McpClient(remote_error=True)
    monkeypatch.setattr(
        router_module,
        "resolve_owned_deployment_runtime_target",
        MagicMock(return_value=_target(agentcore)),
    )

    response = client.post(
        "/api/test-mcp-runtime/call",
        json={
            "deploymentId": DEPLOYMENT_ID,
            "toolName": "does_not_exist",
            "arguments": {},
            "sessionId": "mcp-session-1",
        },
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "The MCP runtime rejected this operation."}
    assert "sensitive" not in response.text


def test_malformed_runtime_response_is_a_sanitized_502(
    client: TestClient,
    monkeypatch,
):
    agentcore = _McpClient(malformed=True)
    monkeypatch.setattr(
        router_module,
        "resolve_owned_deployment_runtime_target",
        MagicMock(return_value=_target(agentcore)),
    )

    response = client.post(
        "/api/test-mcp-runtime/tools",
        json={"deploymentId": DEPLOYMENT_ID},
    )
    assert response.status_code == 502
    assert response.json() == {"detail": "The MCP runtime returned an invalid protocol response."}
    assert RUNTIME_ARN not in response.text


def test_oversized_arguments_are_rejected_before_aws(
    client: TestClient,
    monkeypatch,
):
    agentcore = _McpClient()
    monkeypatch.setattr(
        router_module,
        "resolve_owned_deployment_runtime_target",
        MagicMock(return_value=_target(agentcore)),
    )

    response = client.post(
        "/api/test-mcp-runtime/call",
        json={
            "deploymentId": DEPLOYMENT_ID,
            "toolName": "get_weather",
            "arguments": {"city": "x" * (runtime_mcp.MAX_STRING_BYTES + 1)},
        },
    )
    assert response.status_code == 422
    assert agentcore.calls == []


def test_unexpected_protocol_version_is_not_silently_downgraded():
    agentcore = _McpClient()
    original = agentcore.invoke_agent_runtime

    def wrong_version(**kwargs):
        response = original(**kwargs)
        response["mcpProtocolVersion"] = "2025-03-26"
        return response

    agentcore.invoke_agent_runtime = wrong_version
    with pytest.raises(runtime_mcp.McpProtocolError):
        runtime_mcp.list_tools(
            agentcore,
            runtime_arn=RUNTIME_ARN,
            runtime_user_id=_LOCAL_DEV_SUB,
        )


def test_repeated_pagination_cursor_fails_closed():
    agentcore = _McpClient()
    original = agentcore.invoke_agent_runtime

    def repeated_cursor(**kwargs):
        response = original(**kwargs)
        payload = json.loads(kwargs["payload"])
        if payload["method"] == "tools/list":
            body = json.loads(response["response"].read())
            body["result"]["nextCursor"] = "same"
            response["response"] = io.BytesIO(json.dumps(body).encode())
        return response

    agentcore.invoke_agent_runtime = repeated_cursor
    with pytest.raises(runtime_mcp.McpProtocolError):
        runtime_mcp.list_tools(
            agentcore,
            runtime_arn=RUNTIME_ARN,
            runtime_user_id=_LOCAL_DEV_SUB,
        )


def _deployment(**overrides):
    values = {
        "deployment_id": DEPLOYMENT_ID,
        "version_id": "v1",
        "runtime_id": RUNTIME_ID,
        "runtime_arn": RUNTIME_ARN,
        "runtime_protocol": "MCP",
        "user_id": _LOCAL_DEV_SUB,
        "status": "succeeded",
        "delete_status": None,
        "target_account_id": "222222222222",
        "target_region": "eu-west-1",
        "target_role_arn": TARGET_ROLE,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_deployment_target_resolution_uses_the_frozen_cross_account_session(
    monkeypatch,
):
    store = MagicMock()
    store.get.return_value = _deployment()
    session = MagicMock()
    session_for_event = MagicMock(return_value=session)
    monkeypatch.setattr(target_context, "get_deployment_store", lambda: store)
    monkeypatch.setattr(
        target_context.step_clients,
        "session_for_event",
        session_for_event,
    )

    target = target_context.resolve_owned_deployment_runtime_target(
        DEPLOYMENT_ID,
        _LOCAL_DEV_SUB,
        required_protocol="MCP",
    )

    store.get.assert_called_once_with(DEPLOYMENT_ID, consistent=True)
    session_for_event.assert_called_once_with(
        {
            "target_account_id": "222222222222",
            "target_region": "eu-west-1",
            "target_role_arn": TARGET_ROLE,
        }
    )
    assert target.runtime_arn == RUNTIME_ARN
    assert target.protocol == "MCP"


def test_deployment_target_resolution_checks_owner_before_protocol(
    monkeypatch,
):
    store = MagicMock()
    store.get.return_value = _deployment(
        user_id="another-tenant",
        runtime_protocol="HTTP",
    )
    session_for_event = MagicMock()
    monkeypatch.setattr(target_context, "get_deployment_store", lambda: store)
    monkeypatch.setattr(
        target_context.step_clients,
        "session_for_event",
        session_for_event,
    )

    with pytest.raises(HTTPException) as exc:
        target_context.resolve_owned_deployment_runtime_target(
            DEPLOYMENT_ID,
            _LOCAL_DEV_SUB,
            required_protocol="MCP",
        )

    assert exc.value.status_code == 404
    session_for_event.assert_not_called()


def test_legacy_http_record_is_not_an_mcp_target(monkeypatch):
    store = MagicMock()
    store.get.return_value = _deployment(runtime_protocol=None)
    session_for_event = MagicMock()
    monkeypatch.setattr(target_context, "get_deployment_store", lambda: store)
    monkeypatch.setattr(
        target_context.step_clients,
        "session_for_event",
        session_for_event,
    )

    with pytest.raises(HTTPException) as exc:
        target_context.resolve_owned_deployment_runtime_target(
            DEPLOYMENT_ID,
            _LOCAL_DEV_SUB,
            required_protocol="MCP",
        )

    assert exc.value.status_code == 409
    session_for_event.assert_not_called()


def test_failed_authority_read_is_503_not_an_ambient_session(monkeypatch):
    store = MagicMock()
    store.get.side_effect = RuntimeError("sensitive DynamoDB failure")
    session_for_event = MagicMock()
    monkeypatch.setattr(target_context, "get_deployment_store", lambda: store)
    monkeypatch.setattr(
        target_context.step_clients,
        "session_for_event",
        session_for_event,
    )

    with pytest.raises(HTTPException) as exc:
        target_context.resolve_owned_deployment_runtime_target(
            DEPLOYMENT_ID,
            _LOCAL_DEV_SUB,
            required_protocol="MCP",
        )

    assert exc.value.status_code == 503
    assert "sensitive" not in str(exc.value.detail)
    session_for_event.assert_not_called()
