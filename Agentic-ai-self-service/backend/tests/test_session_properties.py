"""Property-based tests for session ID passthrough on /api/test-runtime.

Property: when the caller supplies a session_id, the boto3
`invoke_agent_runtime` call must receive that exact value in BOTH the
top-level `runtimeSessionId` parameter (for AgentCore-side routing) AND
inside the JSON-encoded payload as `session_id` (so the agent's invoke
body can read it for memory persistence — see lessons.md Bug 29).

When the caller omits session_id, neither placement is present.
"""

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

sys.path.insert(0, "src")

# deployment_handler imports mangum at module level; stub it so the module
# can be loaded in test environments where mangum is not installed.
if "mangum" not in sys.modules:
    sys.modules["mangum"] = types.ModuleType("mangum")
    sys.modules["mangum"].Mangum = MagicMock()  # type: ignore[attr-defined]

from fastapi.testclient import TestClient

from app.deployment_handler import deployment_app


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# TestRequest.session_id is `Optional[str]` with `max_length=256`.
session_id_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "S")),
    min_size=1,
    max_size=256,
).filter(lambda s: s.strip() != "")

prompt_st = st.text(min_size=1, max_size=200)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_invoke_kwargs():
    """Patch _create_agentcore_client to return a mock that captures the
    last invoke_agent_runtime kwargs. Returns (patcher, captured_dict).

    The captured_dict will be populated with the kwargs dict on each call.
    """
    captured = {}
    mock_client = MagicMock()

    def _capture(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return {
            "response": "ok",
            "runtimeSessionId": kwargs.get("runtimeSessionId", ""),
            "statusCode": 200,
        }

    mock_client.invoke_agent_runtime.side_effect = _capture
    patcher = patch(
        "app.deployment_handler._create_agentcore_client",
        return_value=mock_client,
    )
    return patcher, captured


def _post_test_runtime(client: TestClient, *, session_id, prompt: str = "hi"):
    body = {
        "input": prompt,
        "runtimeId": "diag_runtime_abc123",
    }
    if session_id is not None:
        body["sessionId"] = session_id
    return client.post("/api/test-runtime", json=body)


# ---------------------------------------------------------------------------
# Property — session_id supplied
# ---------------------------------------------------------------------------


class TestSessionIDPassthrough:
    """For any non-empty session_id, the boto3 invoke call carries the exact
    value in BOTH `runtimeSessionId` AND `payload['session_id']`.
    """

    @given(session_id=session_id_st, prompt=prompt_st)
    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_runtime_session_id_param_matches_exact(self, session_id: str, prompt: str):
        """`runtimeSessionId` kwarg equals the supplied session_id verbatim."""
        patcher, captured = _capture_invoke_kwargs()
        with patcher:
            client = TestClient(deployment_app)
            resp = _post_test_runtime(client, session_id=session_id, prompt=prompt)
            assert resp.status_code == 200, resp.text
            assert captured.get("runtimeSessionId") == session_id

    @given(session_id=session_id_st, prompt=prompt_st)
    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_payload_body_session_id_matches_exact(self, session_id: str, prompt: str):
        """The JSON `payload` body carries `session_id` with the exact value."""
        patcher, captured = _capture_invoke_kwargs()
        with patcher:
            client = TestClient(deployment_app)
            resp = _post_test_runtime(client, session_id=session_id, prompt=prompt)
            assert resp.status_code == 200, resp.text
            payload = json.loads(captured.get("payload", "{}"))
            assert payload.get("session_id") == session_id

    @given(session_id=session_id_st, prompt=prompt_st)
    @settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_both_placements_agree(self, session_id: str, prompt: str):
        """The two placements never drift — same value in both spots, always."""
        patcher, captured = _capture_invoke_kwargs()
        with patcher:
            client = TestClient(deployment_app)
            resp = _post_test_runtime(client, session_id=session_id, prompt=prompt)
            assert resp.status_code == 200, resp.text
            payload = json.loads(captured.get("payload", "{}"))
            assert captured.get("runtimeSessionId") == payload.get("session_id") == session_id


# ---------------------------------------------------------------------------
# Property — session_id omitted
# ---------------------------------------------------------------------------


class TestSessionIDOmittedNoLeakage:
    """When the caller omits session_id, neither placement appears."""

    def test_omitted_session_id_no_runtime_session_id_kwarg(self):
        patcher, captured = _capture_invoke_kwargs()
        with patcher:
            client = TestClient(deployment_app)
            resp = _post_test_runtime(client, session_id=None)
            assert resp.status_code == 200, resp.text
            assert "runtimeSessionId" not in captured

    def test_omitted_session_id_no_payload_session_key(self):
        patcher, captured = _capture_invoke_kwargs()
        with patcher:
            client = TestClient(deployment_app)
            resp = _post_test_runtime(client, session_id=None)
            assert resp.status_code == 200, resp.text
            payload = json.loads(captured.get("payload", "{}"))
            assert "session_id" not in payload


# ---------------------------------------------------------------------------
# Property — no cross-request leakage
# ---------------------------------------------------------------------------


class TestSessionIDNoCrossRequestLeakage:
    """A request without session_id immediately following one with session_id
    must not inherit the prior value — each request stands alone.
    """

    @given(session_id=session_id_st)
    @settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_subsequent_request_without_session_id_is_clean(self, session_id: str):
        patcher, captured = _capture_invoke_kwargs()
        with patcher:
            client = TestClient(deployment_app)

            # Request 1: with session_id
            resp1 = _post_test_runtime(client, session_id=session_id)
            assert resp1.status_code == 200
            assert captured.get("runtimeSessionId") == session_id

            # Request 2: without session_id — must not carry over
            resp2 = _post_test_runtime(client, session_id=None)
            assert resp2.status_code == 200
            assert "runtimeSessionId" not in captured
            payload2 = json.loads(captured.get("payload", "{}"))
            assert "session_id" not in payload2


# Pytest plugin sanity: ensure deployment_app is exported
def test_deployment_app_is_importable():
    assert deployment_app is not None
