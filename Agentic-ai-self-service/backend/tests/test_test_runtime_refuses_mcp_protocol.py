"""POST /api/test-runtime{,-stream} is the wrong door for an MCP-protocol runtime.

An MCP-server runtime speaks JSON-RPC over the bounded MCP data plane and is
exercised through /api/test-mcp-runtime (routers/runtime_mcp.py). The HTTP test
paths speak the agent request/response envelope. Invoking an MCP runtime through
them would send a shape it does not understand, so both refuse a persisted
``runtime_protocol == "MCP"`` deployment BEFORE any side effect and only AFTER the
ownership check -- so the protocol is never disclosed to a non-owner and the
endpoint stays a non-oracle.

This suite is deliberately NOT refusal-only (see the a-suite-of-refusal-tests-
hides-a-dead-happy-path lesson): it pins that an HTTP runtime and a legacy record
with no protocol still invoke, so a mutant that refuses everything fails here.
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "src")

import pytest
from app import deployment_handler as dh
from app.services import step_clients
from fastapi.testclient import TestClient

OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
OTHER = "54381418-7021-708e-4f3b-30505a2b82ec"
RUNTIME = "mcp_explorer_88a6981e-qE8xnm3uaM"
ANSWER = "the http agent answered"


def _client(sub: str | None) -> TestClient:
    """A TestClient carrying ``sub`` the way the HTTP API's JWT authorizer delivers it."""
    claims = {"cognito:groups": ["g-users-default"], **({"sub": sub} if sub else {})}
    event = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}

    async def _inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await dh.deployment_app(scope, receive, send)

    return TestClient(_inject)


class _Table:
    pass


class _Store:
    _table = _Table()


@pytest.fixture
def env(monkeypatch):
    """Wire a store whose record is chosen per test, capturing invoke + promote calls.

    Returns a small handle: ``env.set(record)`` installs the deployment record the
    scan returns for RUNTIME, ``env.invoked`` and ``env.promoted`` record the two
    side effects the refusal must precede.
    """
    state: dict = {"record": None}
    invoked: list[dict] = []
    promoted: list[tuple] = []

    class _Agentcore:
        def invoke_agent_runtime(self, **kwargs):
            invoked.append(kwargs)
            return {"runtimeSessionId": "sess-1", "response": json.dumps({"response": ANSWER}).encode()}

    class _Session:
        def client(self, service, **_kwargs):
            assert service == "bedrock-agentcore"
            return _Agentcore()

    def _promote(*a, **k):
        promoted.append((a, k))
        return False

    monkeypatch.setattr(dh, "_get_state_store", lambda: _Store())
    monkeypatch.setattr(
        dh,
        "_scan_for_runtime",
        lambda table, rid: dict(state["record"]) if state["record"] and rid == RUNTIME else None,
    )
    monkeypatch.setattr(step_clients, "session_for_event", lambda _event: _Session())
    monkeypatch.setattr(dh, "_maybe_promote_policy", _promote)

    class _Env:
        def __init__(self):
            self.invoked = invoked
            self.promoted = promoted

        def set(self, record):
            state["record"] = record

    return _Env()


def _record(**overrides) -> dict:
    record = {
        "deployment_id": "5bb2084b-d586-46d6-a5f3-494cd24cfc89",
        "runtime_id": RUNTIME,
        "runtime_arn": f"arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/{RUNTIME}",
        "user_id": OWNER,
        "deployment_mode": "runtime",
        "status": "succeeded",
    }
    record.update(overrides)
    return record


def _post(sub, path="/api/test-runtime"):
    return _client(sub).post(path, json={"runtime_id": RUNTIME, "input": "hi"})


# --------------------------------------------------------------------------- #
# Sync route
# --------------------------------------------------------------------------- #


def test_the_owner_of_an_mcp_runtime_gets_409_naming_the_mcp_door(env):
    env.set(_record(runtime_protocol="MCP"))
    resp = _post(OWNER)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "MCP" in detail
    assert "/api/test-mcp-runtime" in detail


def test_the_mcp_refusal_precedes_both_side_effects(env):
    env.set(_record(runtime_protocol="MCP"))
    assert _post(OWNER).status_code == 409
    assert env.invoked == [], "invoke_agent_runtime must not run for an MCP runtime"
    assert env.promoted == [], "policy promotion must not run before the refusal"


def test_an_http_runtime_still_invokes(env):
    """Not refusal-only: the happy path must survive, or a refuse-everything mutant passes."""
    env.set(_record(runtime_protocol="HTTP"))
    resp = _post(OWNER)
    assert resp.status_code == 200, resp.text
    assert resp.json()["response"] == ANSWER
    assert len(env.invoked) == 1


def test_a_legacy_record_without_a_protocol_is_treated_as_http(env):
    env.set(_record())  # no runtime_protocol key at all
    resp = _post(OWNER)
    assert resp.status_code == 200, resp.text
    assert len(env.invoked) == 1


def test_a_non_owner_gets_404_not_409_so_the_protocol_stays_private(env):
    """The MCP refusal sits AFTER ownership, so a stranger cannot use it to learn
    that a runtime exists or that it is MCP -- the refusal is 404, identical to any
    other unowned/absent runtime."""
    env.set(_record(runtime_protocol="MCP"))
    resp = _post(OTHER)
    assert resp.status_code == 404, resp.text
    assert env.invoked == []


# --------------------------------------------------------------------------- #
# Stream route (SSE)
# --------------------------------------------------------------------------- #


def _errors(body: str) -> tuple[list[str], list[str]]:
    tokens, errors = [], []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        evt = json.loads(line[6:])
        if evt.get("type") == "token":
            tokens.append(evt.get("token", ""))
        elif evt.get("type") == "error":
            errors.append(evt.get("error", ""))
    return tokens, errors


def test_the_stream_route_refuses_an_mcp_runtime_with_an_error_frame(env):
    env.set(_record(runtime_protocol="MCP"))
    resp = _post(OWNER, path="/api/test-runtime-stream")
    assert resp.status_code == 200  # SSE body carries the refusal, not the HTTP status
    tokens, errors = _errors(resp.text)
    assert tokens == [], "an MCP runtime must yield no answer tokens on the HTTP stream"
    assert len(errors) == 1
    assert "/api/test-mcp-runtime" in errors[0]
    assert env.invoked == []
    assert env.promoted == []


def test_the_stream_route_still_streams_an_http_runtime(env):
    env.set(_record(runtime_protocol="HTTP"))
    resp = _post(OWNER, path="/api/test-runtime-stream")
    assert resp.status_code == 200
    tokens, errors = _errors(resp.text)
    assert errors == []
    assert "".join(tokens).strip() == ANSWER
    assert len(env.invoked) == 1
