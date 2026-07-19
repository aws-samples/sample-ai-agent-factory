"""Async (slow-class) runtime teardown — DELETE /api/runtime/{id}.

KB-backed teardowns (managed KB cascade + S3-Vectors/OSS backing stores)
exceed API Gateway's 29s integration cap and used to 503 even though the
Lambda finished. handle_delete_runtime now classifies deletes:

* FAST class (no KB / KB-adjacent resources): runs _run_delete_cleanup
  inline — unchanged behavior, no self-invoke.
* SLOW class (knowledge_base_result.created_by_flow OR any created_resources
  entry of type knowledge_base / oss_collection / s3_vectors_bucket):
  writes delete_status="deleting", self-invokes with an _async_delete
  sentinel, and returns immediately with a poll-for-status message.

_handle_async_delete runs the same cleanup body in the background invoke and
records delete_status = "deleted" | "delete_failed" (+ delete_message).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

import app.deployment_handler as dh  # noqa: E402
from app.models.deployment_models import DeleteResponse  # noqa: E402

FAST_RECORD = {
    "deployment_id": "dep-fast-1",
    "runtime_id": "rt_fast_1",
    "user_id": "tester",
    "created_resources": [
        {"type": "agent_runtime", "id": "rt_fast_1", "region": "us-east-1"},
        {"type": "memory", "id": "mem-1", "region": "us-east-1"},
    ],
}

SLOW_RECORD_KB_RESULT = {
    "deployment_id": "dep-slow-1",
    "runtime_id": "rt_slow_1",
    "user_id": "tester",
    "knowledge_base_result": {"created_by_flow": True, "kb_id": "KB123"},
}

SLOW_RECORD_MANIFEST = {
    "deployment_id": "dep-slow-2",
    "runtime_id": "rt_slow_2",
    "user_id": "tester",
    "created_resources": [
        {"type": "agent_runtime", "id": "rt_slow_2", "region": "us-east-1"},
        {"type": "s3_vectors_bucket", "name": "kb-vectors-abc", "region": "us-east-1"},
    ],
}


@pytest.fixture
def delete_client(monkeypatch):
    """TestClient with the record lookup, cleanup body, status writes and the
    Lambda self-invoke all mocked, so tests can assert dispatch behavior."""
    state: dict = {"record": None}

    monkeypatch.setattr(dh, "_lookup_deployment_record", lambda rid: state["record"])
    monkeypatch.setattr(dh, "_get_user_id", lambda req: "tester")

    cleanup_mock = MagicMock(return_value=DeleteResponse(success=True, message="inline cleanup done"))
    monkeypatch.setattr(dh, "_run_delete_cleanup", cleanup_mock)

    status_mock = MagicMock()
    monkeypatch.setattr(dh, "_set_delete_status", status_mock)

    lambda_client = MagicMock()
    monkeypatch.setattr(dh.boto3, "client", lambda *a, **k: lambda_client)

    client = TestClient(dh.deployment_app)
    client._state = state  # type: ignore[attr-defined]
    client._cleanup = cleanup_mock  # type: ignore[attr-defined]
    client._status = status_mock  # type: ignore[attr-defined]
    client._lambda = lambda_client  # type: ignore[attr-defined]
    return client


# ---------------------------------------------------------------------------
# Fast class — stays inline, no self-invoke
# ---------------------------------------------------------------------------


def test_fast_delete_runs_inline_without_self_invoke(delete_client):
    delete_client._state["record"] = FAST_RECORD
    resp = delete_client.delete("/api/runtime/rt_fast_1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["message"] == "inline cleanup done"
    delete_client._cleanup.assert_called_once_with("rt_fast_1", "tester")
    delete_client._lambda.invoke.assert_not_called()
    delete_client._status.assert_not_called()


def test_missing_record_delete_runs_inline(delete_client):
    """No deployment record at all (external / already-purged) → fast class."""
    delete_client._state["record"] = None
    resp = delete_client.delete("/api/runtime/rt_unknown")
    assert resp.status_code == 200, resp.text
    delete_client._cleanup.assert_called_once()
    delete_client._lambda.invoke.assert_not_called()


def test_cross_tenant_delete_404s_before_any_cleanup(delete_client):
    delete_client._state["record"] = {**SLOW_RECORD_KB_RESULT, "user_id": "someone-else"}
    resp = delete_client.delete("/api/runtime/rt_slow_1")
    assert resp.status_code == 404
    delete_client._cleanup.assert_not_called()
    delete_client._lambda.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# Slow class — dispatches the Event self-invoke, returns background message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("record", [SLOW_RECORD_KB_RESULT, SLOW_RECORD_MANIFEST])
def test_slow_delete_dispatches_background_invoke(delete_client, record):
    delete_client._state["record"] = record
    resp = delete_client.delete(f"/api/runtime/{record['runtime_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert "background" in body["message"]
    assert "delete_status" in body["message"]

    # delete_status="deleting" written before dispatch
    delete_client._status.assert_called_once_with(record["deployment_id"], "deleting")

    # Event self-invoke with the sentinel payload; no inline cleanup ran
    delete_client._lambda.invoke.assert_called_once()
    kwargs = delete_client._lambda.invoke.call_args.kwargs
    assert kwargs["InvocationType"] == "Event"
    import json as _json

    payload = _json.loads(kwargs["Payload"].decode())
    assert payload["_async_delete"] is True
    assert payload["runtime_id"] == record["runtime_id"]
    assert payload["caller_sub"] == "tester"
    delete_client._cleanup.assert_not_called()


def test_slow_delete_falls_back_inline_when_invoke_fails(delete_client):
    """Better slow than dropped: if the Event invoke raises, run inline."""
    delete_client._state["record"] = SLOW_RECORD_KB_RESULT
    delete_client._lambda.invoke.side_effect = Exception("AccessDenied")
    resp = delete_client.delete("/api/runtime/rt_slow_1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "inline cleanup done"
    delete_client._cleanup.assert_called_once_with("rt_slow_1", "tester")


# ---------------------------------------------------------------------------
# _handle_async_delete — records delete_status on success and failure
# ---------------------------------------------------------------------------


def test_async_delete_writes_deleted_on_success(monkeypatch):
    monkeypatch.setattr(dh, "_lookup_deployment_record", lambda rid: {"deployment_id": "dep-slow-1"})
    monkeypatch.setattr(
        dh,
        "_run_delete_cleanup",
        MagicMock(return_value=DeleteResponse(success=True, message="all torn down")),
    )
    status_mock = MagicMock()
    monkeypatch.setattr(dh, "_set_delete_status", status_mock)

    out = dh._handle_async_delete({"_async_delete": True, "runtime_id": "rt_slow_1", "caller_sub": "tester"})
    assert out == {"success": True}
    status_mock.assert_called_once_with("dep-slow-1", "deleted", "all torn down")


def test_async_delete_writes_delete_failed_on_partial_cleanup(monkeypatch):
    monkeypatch.setattr(dh, "_lookup_deployment_record", lambda rid: {"deployment_id": "dep-slow-1"})
    monkeypatch.setattr(
        dh,
        "_run_delete_cleanup",
        MagicMock(return_value=DeleteResponse(success=False, message="Cleanup failures in: knowledge_base")),
    )
    status_mock = MagicMock()
    monkeypatch.setattr(dh, "_set_delete_status", status_mock)

    out = dh._handle_async_delete({"_async_delete": True, "runtime_id": "rt_slow_1", "caller_sub": "tester"})
    assert out == {"success": False}
    status_mock.assert_called_once_with("dep-slow-1", "delete_failed", "Cleanup failures in: knowledge_base")


def test_async_delete_writes_delete_failed_on_exception(monkeypatch):
    monkeypatch.setattr(dh, "_lookup_deployment_record", lambda rid: {"deployment_id": "dep-slow-1"})
    monkeypatch.setattr(dh, "_run_delete_cleanup", MagicMock(side_effect=RuntimeError("boom")))
    status_mock = MagicMock()
    monkeypatch.setattr(dh, "_set_delete_status", status_mock)

    out = dh._handle_async_delete({"_async_delete": True, "runtime_id": "rt_slow_1", "caller_sub": "tester"})
    assert out == {"success": False}
    status_mock.assert_called_once()
    args = status_mock.call_args.args
    assert args[0] == "dep-slow-1"
    assert args[1] == "delete_failed"
    assert "boom" in args[2]


def test_handler_routes_async_delete_sentinel(monkeypatch):
    """handler() must intercept _async_delete before Mangum."""
    called = {}

    def _fake_async_delete(event):
        called["event"] = event
        return {"success": True}

    monkeypatch.setattr(dh, "_handle_async_delete", _fake_async_delete)
    out = dh.handler({"_async_delete": True, "runtime_id": "rt_x", "caller_sub": "s"}, None)
    assert out == {"success": True}
    assert called["event"]["runtime_id"] == "rt_x"


def test_is_slow_delete_classification():
    assert dh._is_slow_delete(SLOW_RECORD_KB_RESULT) is True
    assert dh._is_slow_delete(SLOW_RECORD_MANIFEST) is True
    assert (
        dh._is_slow_delete(
            {"created_resources": [{"type": "oss_collection", "name": "coll-1"}]},
        )
        is True
    )
    assert dh._is_slow_delete(FAST_RECORD) is False
    assert dh._is_slow_delete(None) is False
    # existing (not flow-created) KB stays fast — nothing slow to tear down
    assert dh._is_slow_delete({"knowledge_base_result": {"created_by_flow": False, "kb_id": "KB1"}}) is False
