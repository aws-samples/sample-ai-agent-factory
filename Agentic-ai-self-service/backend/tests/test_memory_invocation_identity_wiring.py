"""F-56: every runtime surface must pass one tenant-bound identity to Memory.

The pure ``resolve_invocation_identity`` tests prove the identity itself is
valid.  They do not prove any production route calls it.  These tests exercise
the three runtime invocation surfaces and inspect the actual AgentCore request:

* ``POST /api/test-runtime``;
* ``POST /api/test-runtime-stream`` (the browser's API Gateway route);
* ``stream_handler._stream_invoke`` (the Function URL route).

For a memory-aware deployment, one resolved session must be used for both
AgentCore routing and the generated agent payload, and the payload must carry
the tenant-bound actor.  Omitting either payload field makes the generated
agent fall back to the global ``session_id="default"`` / ``actor_id="user"``
namespace even if ``runtimeSessionId`` itself is correct.
"""

from __future__ import annotations

import json
import re

import pytest
from app import deployment_handler as dh
from app import stream_handler as sh
from app.services import step_clients
from app.services.resource_ownership import owner_sub_hash
from fastapi.testclient import TestClient

OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
RUNTIME = "memory_runtime_abc123"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/{RUNTIME}"
EXPLICIT_SESSION = "client-session_" + ("x" * 40)
INVALID_MEMORY_SESSION = "too-short"


def _record(*, memory: bool = True, owner: str | None = OWNER) -> dict:
    record = {
        "deployment_id": "5bb2084b-d586-46d6-a5f3-494cd24cfc89",
        "runtime_id": RUNTIME,
        "runtime_arn": RUNTIME_ARN,
        "user_id": owner,
        "deployment_mode": "runtime",
        "status": "succeeded",
    }
    if memory:
        record["memory_result"] = {
            "memory_id": "memory-AbCdEf1234",
            "ready": True,
        }
    return record


class _Store:
    _table = object()


class _Agentcore:
    def __init__(self) -> None:
        self.invocations: list[dict] = []

    def invoke_agent_runtime(self, **kwargs):
        self.invocations.append(kwargs)
        return {
            "response": json.dumps({"response": "ok"}).encode(),
            "runtimeSessionId": kwargs.get("runtimeSessionId"),
            "statusCode": 200,
        }


class _Session:
    def __init__(self, client: _Agentcore) -> None:
        self._client = client

    def client(self, service, **_kwargs):
        assert service == "bedrock-agentcore"
        return self._client


def _client(sub: str = OWNER) -> TestClient:
    event = {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": sub,
                        "cognito:groups": ["g-users-default"],
                    }
                }
            }
        }
    }

    async def _inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await dh.deployment_app(scope, receive, send)

    return TestClient(_inject)


@pytest.fixture
def api_runtime(monkeypatch):
    client = _Agentcore()
    monkeypatch.setattr(dh, "_get_state_store", lambda: _Store())
    monkeypatch.setattr(
        dh,
        "_scan_for_runtime",
        lambda _table, runtime_id: _record() if runtime_id == RUNTIME else None,
    )
    monkeypatch.setattr(
        step_clients,
        "session_for_event",
        lambda _event: _Session(client),
    )
    monkeypatch.setattr(dh, "_maybe_promote_policy", lambda *args, **kwargs: False)
    return client


def _payload(invocation: dict) -> dict:
    return json.loads(invocation["payload"])


def _assert_memory_identity(invocation: dict) -> str:
    payload = _payload(invocation)
    session_id = invocation["runtimeSessionId"]

    assert payload["session_id"] == session_id
    assert payload["actor_id"] == owner_sub_hash(OWNER)
    assert 33 <= len(session_id) <= 100
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", session_id)
    return session_id


def _sse_events(body: str) -> list[dict]:
    return [json.loads(line.removeprefix("data: ")) for line in body.splitlines() if line.startswith("data: ")]


def _assert_safe_session_error(message: str) -> None:
    lowered = message.lower()
    assert "session" in lowered
    assert "traceback" not in lowered
    assert "invocationidentity" not in lowered


def test_sync_route_generates_and_returns_one_memory_identity(api_runtime):
    response = _client().post(
        "/api/test-runtime",
        json={"runtimeId": RUNTIME, "input": "hi"},
    )

    assert response.status_code == 200, response.text
    assert len(api_runtime.invocations) == 1
    session_id = _assert_memory_identity(api_runtime.invocations[0])
    assert response.json()["sessionId"] == session_id


def test_browser_sse_route_passes_an_explicit_session_to_routing_and_memory(
    api_runtime,
):
    response = _client().post(
        "/api/test-runtime-stream",
        json={
            "runtimeId": RUNTIME,
            "input": "hi",
            "sessionId": EXPLICIT_SESSION,
        },
    )

    assert response.status_code == 200, response.text
    assert len(api_runtime.invocations) == 1
    invocation = api_runtime.invocations[0]
    assert invocation["runtimeSessionId"] == EXPLICIT_SESSION
    assert _payload(invocation) == {
        "prompt": "hi",
        "session_id": EXPLICIT_SESSION,
        "actor_id": owner_sub_hash(OWNER),
    }
    done = [event for event in _sse_events(response.text) if event["type"] == "done"]
    assert done and done[-1]["session_id"] == EXPLICIT_SESSION


def test_function_url_uses_the_deployment_owner_for_an_iam_callers_memory(
    monkeypatch,
):
    client = _Agentcore()
    monkeypatch.setattr(sh, "_get_state_store", lambda: _Store())
    monkeypatch.setattr(
        sh,
        "_scan_for_runtime",
        lambda _table, runtime_id: _record() if runtime_id == RUNTIME else None,
    )
    monkeypatch.setattr(
        sh.step_clients,
        "session_for_event",
        lambda _event: _Session(client),
    )

    written: list[bytes] = []
    sh._stream_invoke(
        written.append,
        {"runtimeId": RUNTIME, "input": "hi"},
        "iam:AROATEST:operator",
    )

    assert len(client.invocations) == 1
    session_id = _assert_memory_identity(client.invocations[0])
    done = [event for event in _sse_events(b"".join(written).decode()) if event["type"] == "done"]
    assert done and done[-1]["session_id"] == session_id


def test_sync_route_rejects_an_invalid_memory_session_before_invoking(api_runtime):
    response = _client().post(
        "/api/test-runtime",
        json={
            "runtimeId": RUNTIME,
            "input": "hi",
            "sessionId": INVALID_MEMORY_SESSION,
        },
    )

    assert response.status_code < 500, response.text
    assert api_runtime.invocations == []
    body = response.json()
    _assert_safe_session_error(str(body.get("detail") or body.get("error") or body))


def test_browser_sse_rejects_an_invalid_memory_session_before_invoking(
    api_runtime,
):
    response = _client().post(
        "/api/test-runtime-stream",
        json={
            "runtimeId": RUNTIME,
            "input": "hi",
            "sessionId": INVALID_MEMORY_SESSION,
        },
    )

    assert response.status_code == 200, response.text
    assert api_runtime.invocations == []
    errors = [event for event in _sse_events(response.text) if event["type"] == "error"]
    assert errors
    _assert_safe_session_error(errors[-1]["error"])


def test_function_url_rejects_an_invalid_memory_session_before_invoking(
    monkeypatch,
):
    client = _Agentcore()
    monkeypatch.setattr(sh, "_get_state_store", lambda: _Store())
    monkeypatch.setattr(
        sh,
        "_scan_for_runtime",
        lambda _table, runtime_id: _record() if runtime_id == RUNTIME else None,
    )
    monkeypatch.setattr(
        sh.step_clients,
        "session_for_event",
        lambda _event: _Session(client),
    )

    written: list[bytes] = []
    sh._stream_invoke(
        written.append,
        {
            "runtimeId": RUNTIME,
            "input": "hi",
            "sessionId": INVALID_MEMORY_SESSION,
        },
        "iam:AROATEST:operator",
    )

    assert client.invocations == []
    errors = [event for event in _sse_events(b"".join(written).decode()) if event["type"] == "error"]
    assert errors
    _assert_safe_session_error(errors[-1]["error"])


def test_sync_non_memory_runtime_does_not_invent_a_memory_identity(
    monkeypatch,
    api_runtime,
):
    monkeypatch.setattr(
        dh,
        "_scan_for_runtime",
        lambda _table, runtime_id: _record(memory=False) if runtime_id == RUNTIME else None,
    )

    response = _client().post(
        "/api/test-runtime",
        json={"runtimeId": RUNTIME, "input": "hi"},
    )

    assert response.status_code == 200, response.text
    assert len(api_runtime.invocations) == 1
    invocation = api_runtime.invocations[0]
    assert "runtimeSessionId" not in invocation
    payload = _payload(invocation)
    assert "session_id" not in payload
    assert "actor_id" not in payload


def test_browser_sse_non_memory_runtime_does_not_invent_a_memory_identity(
    monkeypatch,
    api_runtime,
):
    monkeypatch.setattr(
        dh,
        "_scan_for_runtime",
        lambda _table, runtime_id: _record(memory=False) if runtime_id == RUNTIME else None,
    )

    response = _client().post(
        "/api/test-runtime-stream",
        json={"runtimeId": RUNTIME, "input": "hi"},
    )

    assert response.status_code == 200, response.text
    assert len(api_runtime.invocations) == 1
    invocation = api_runtime.invocations[0]
    assert "runtimeSessionId" not in invocation
    payload = _payload(invocation)
    assert "session_id" not in payload
    assert "actor_id" not in payload


def test_function_url_non_memory_runtime_does_not_invent_a_memory_identity(
    monkeypatch,
):
    client = _Agentcore()
    monkeypatch.setattr(sh, "_get_state_store", lambda: _Store())
    monkeypatch.setattr(
        sh,
        "_scan_for_runtime",
        lambda _table, runtime_id: _record(memory=False) if runtime_id == RUNTIME else None,
    )
    monkeypatch.setattr(
        sh.step_clients,
        "session_for_event",
        lambda _event: _Session(client),
    )

    written: list[bytes] = []
    sh._stream_invoke(
        written.append,
        {"runtimeId": RUNTIME, "input": "hi"},
        "iam:AROATEST:operator",
    )

    assert len(client.invocations) == 1
    invocation = client.invocations[0]
    assert "runtimeSessionId" not in invocation
    payload = _payload(invocation)
    assert "session_id" not in payload
    assert "actor_id" not in payload
