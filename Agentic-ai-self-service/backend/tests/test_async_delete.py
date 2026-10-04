"""Async (slow-class) runtime teardown — DELETE /api/runtime/{id}.

KB-backed teardowns (managed KB cascade + S3-Vectors/OSS backing stores)
exceed API Gateway's 29s integration cap and used to 503 even though the
Lambda finished. handle_delete_runtime now classifies deletes:

* FAST class (no KB / KB-adjacent resources): runs _run_delete_cleanup
  inline — unchanged behavior, no self-invoke.
* SLOW class (knowledge_base_result.created_by_flow OR any created_resources
  entry of type knowledge_base / oss_collection / s3_vectors_bucket):
  atomically claims delete_status="deleting", self-invokes with an _async_delete
  sentinel, and returns immediately with a poll-for-status message.

_handle_async_delete runs the same cleanup body in the background invoke and
records delete_status = "deleted" | "delete_failed" (+ delete_message).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, call

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

import app.deployment_handler as dh  # noqa: E402
import app.services.deployment_state_store as state_store_module  # noqa: E402
import app.services.gateway_name_claim as gnc  # noqa: E402
from app.models.deployment_models import DeleteResponse  # noqa: E402

FAST_RECORD = {
    "deployment_id": "dep-fast-1",
    "runtime_id": "rt_fast_1",
    "user_id": "tester",
    "created_resources": [
        {"type": "agent_runtime", "id": "rt_fast_1", "region": "us-east-1"},
        {"type": "iam_role", "id": "role-1", "region": "us-east-1"},
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
    claim_mock = MagicMock(return_value=True)
    monkeypatch.setattr(dh, "_claim_delete_status", claim_mock)

    lambda_client = MagicMock()
    monkeypatch.setattr(dh.boto3, "client", lambda *a, **k: lambda_client)

    client = TestClient(dh.deployment_app)
    client._state = state  # type: ignore[attr-defined]
    client._cleanup = cleanup_mock  # type: ignore[attr-defined]
    client._status = status_mock  # type: ignore[attr-defined]
    client._claim = claim_mock  # type: ignore[attr-defined]
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
    delete_client._cleanup.assert_called_once_with("rt_fast_1", "tester", allow_imported_destroy=False)
    delete_client._lambda.invoke.assert_not_called()
    delete_client._claim.assert_called_once_with("dep-fast-1")
    assert delete_client._status.call_args_list == [
        call("dep-fast-1", "deleted", "inline cleanup done"),
    ]


def test_fast_delete_records_partial_cleanup_as_delete_failed(delete_client):
    delete_client._state["record"] = FAST_RECORD
    delete_client._cleanup.return_value = DeleteResponse(
        success=False,
        message="Cleanup failures in: gateway",
    )

    resp = delete_client.delete("/api/runtime/rt_fast_1")

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is False
    delete_client._claim.assert_called_once_with("dep-fast-1")
    assert delete_client._status.call_args_list == [
        call(
            "dep-fast-1",
            "delete_failed",
            "Cleanup failures in: gateway",
        ),
    ]


def test_fast_delete_records_a_safety_retention_distinctly(delete_client):
    delete_client._state["record"] = FAST_RECORD
    delete_client._cleanup.return_value = DeleteResponse(
        success=False,
        message="Resources retained by deletion-authority policy: gateway",
        retained=True,
    )

    resp = delete_client.delete("/api/runtime/rt_fast_1")

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is False
    assert resp.json()["retained"] is True
    delete_client._claim.assert_called_once_with("dep-fast-1")
    assert delete_client._status.call_args_list == [
        call(
            "dep-fast-1",
            "delete_retained",
            "Resources retained by deletion-authority policy: gateway",
        ),
    ]


def test_missing_record_delete_is_refused(delete_client):
    """No deployment record at all → 404, and NOTHING is torn down.

    This test used to assert the opposite ("external / already-purged → fast class",
    cleanup called). That was peer finding F-9's worst instance: with no record there is
    no owner to compare the caller against, and the cleanup body would go on to call
    destroy_runtime() on the id straight from the URL — so any authenticated caller could
    delete any runtime in the account by naming it, including runtimes this platform
    never created. "Already purged" costs a caller one 404; the alternative cost someone
    else their runtime.
    """
    delete_client._state["record"] = None
    resp = delete_client.delete("/api/runtime/rt_unknown")
    assert resp.status_code == 404, resp.text
    delete_client._cleanup.assert_not_called()
    delete_client._lambda.invoke.assert_not_called()


def test_cross_tenant_delete_404s_before_any_cleanup(delete_client):
    delete_client._state["record"] = {**SLOW_RECORD_KB_RESULT, "user_id": "someone-else"}
    resp = delete_client.delete("/api/runtime/rt_slow_1")
    assert resp.status_code == 404
    delete_client._cleanup.assert_not_called()
    delete_client._lambda.invoke.assert_not_called()


def test_a_second_concurrent_delete_does_not_run_cleanup(delete_client):
    delete_client._state["record"] = FAST_RECORD
    delete_client._claim.return_value = False

    resp = delete_client.delete("/api/runtime/rt_fast_1")

    assert resp.status_code == 200
    assert "already" in resp.json()["message"].lower()
    delete_client._cleanup.assert_not_called()
    delete_client._lambda.invoke.assert_not_called()
    delete_client._status.assert_not_called()


def test_an_observed_terminal_delete_is_idempotent(delete_client):
    delete_client._state["record"] = {
        **FAST_RECORD,
        "delete_status": "deleted",
    }

    resp = delete_client.delete("/api/runtime/rt_fast_1")

    assert resp.status_code == 200
    delete_client._claim.assert_not_called()
    delete_client._cleanup.assert_not_called()
    delete_client._status.assert_not_called()


class _Reopen:
    """The strict reopen of a "deleted" row: records the write, or fails it."""

    def __init__(self, monkeypatch, outcome, *, fail=None):
        self.writes = []
        self.table = object()

        def _reclaim(*, deployment_id, claims=None):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def _update(table, **kw):
            assert table is self.table
            if fail is not None:
                raise fail
            self.writes.append(kw)
            return {}

        monkeypatch.setattr(dh, "reclaim_recovery_pointer", _reclaim)
        monkeypatch.setattr(dh, "_get_state_store", lambda: MagicMock(_table=self.table))
        monkeypatch.setattr(state_store_module, "_update_item", _update)


def _conditional_failure():
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


@pytest.mark.parametrize("outcome", [dh.POINTER_ABSENT, dh.POINTER_MARKED])
def test_a_deleted_row_with_no_live_recovery_is_final(delete_client, monkeypatch, outcome):
    reopen = _Reopen(monkeypatch, outcome)
    delete_client._state["record"] = {**FAST_RECORD, "delete_status": "deleted"}

    resp = delete_client.delete("/api/runtime/rt_fast_1")

    assert resp.json() == {**resp.json(), "success": True, "message": "This deployment has already been deleted."}
    assert reopen.writes == []
    delete_client._claim.assert_not_called()
    delete_client._cleanup.assert_not_called()


@pytest.mark.parametrize("outcome", [dh.POINTER_ACTIVE, dh.POINTER_RACED])
def test_a_deleted_row_with_live_recovery_is_reopened_and_torn_down_again(delete_client, monkeypatch, outcome):
    """A promote that landed after the teardown read left a gateway only its name claim
    records: "already deleted" would be a success over a live gateway, forever."""
    reopen = _Reopen(monkeypatch, outcome)
    delete_client._state["record"] = {**FAST_RECORD, "delete_status": "deleted"}

    resp = delete_client.delete("/api/runtime/rt_fast_1")

    assert resp.json()["message"] == "inline cleanup done"
    [write] = reopen.writes
    assert write["key"] == {"deployment_id": "dep-fast-1"}
    assert write["condition_expr"] == "#ds = :deleted"
    assert write["expr_values"][":failed"] == "delete_failed"
    assert write["expr_values"][":deleted"] == "deleted"
    assert "REMOVE #t" in write["update_expr"] and write["expr_names"]["#t"] == "ttl"
    delete_client._claim.assert_called_once_with("dep-fast-1")
    delete_client._cleanup.assert_called_once()


@pytest.mark.parametrize(
    ("outcome", "fail"),
    [
        (gnc.POINTER_MALFORMED, None),
        (RuntimeError("ThrottlingException"), None),
        (dh.POINTER_ACTIVE, _conditional_failure()),
        (dh.POINTER_RACED, RuntimeError("ProvisionedThroughputExceededException")),
    ],
    ids=["malformed", "unreadable", "reopen-lost", "reopen-failed"],
)
def test_a_deleted_row_whose_recovery_is_unverified_is_never_success(delete_client, monkeypatch, outcome, fail):
    _Reopen(monkeypatch, outcome, fail=fail)
    delete_client._state["record"] = {**FAST_RECORD, "delete_status": "deleted"}

    resp = delete_client.delete("/api/runtime/rt_fast_1")

    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert resp.json()["message"] == dh._DELETED_UNVERIFIED_MESSAGE
    delete_client._claim.assert_not_called()
    delete_client._cleanup.assert_not_called()
    delete_client._status.assert_not_called()


def test_an_active_delete_claim_is_idempotent(delete_client):
    delete_client._state["record"] = {
        **FAST_RECORD,
        "delete_status": "deleting",
    }
    # The real conditional claim rejects an unexpired lease.
    delete_client._claim.return_value = False

    resp = delete_client.delete("/api/runtime/rt_fast_1")

    assert resp.status_code == 200
    delete_client._claim.assert_called_once_with("dep-fast-1")
    delete_client._cleanup.assert_not_called()
    delete_client._status.assert_not_called()


def test_a_delete_claim_storage_failure_prevents_destructive_work(delete_client):
    delete_client._state["record"] = FAST_RECORD
    delete_client._claim.side_effect = RuntimeError("DynamoDB unavailable")

    resp = delete_client.delete("/api/runtime/rt_fast_1")

    assert resp.status_code == 503
    delete_client._cleanup.assert_not_called()
    delete_client._lambda.invoke.assert_not_called()
    delete_client._status.assert_not_called()


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

    # delete_status="deleting" atomically claimed before dispatch
    delete_client._claim.assert_called_once_with(record["deployment_id"])
    delete_client._status.assert_not_called()

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
    delete_client._cleanup.assert_called_once_with("rt_slow_1", "tester", allow_imported_destroy=False)
    delete_client._claim.assert_called_once_with("dep-slow-1")
    assert delete_client._status.call_args_list == [
        call("dep-slow-1", "deleted", "inline cleanup done"),
    ]


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


def test_async_delete_writes_delete_retained_for_a_safety_refusal(monkeypatch):
    monkeypatch.setattr(dh, "_lookup_deployment_record", lambda rid: {"deployment_id": "dep-slow-1"})
    monkeypatch.setattr(
        dh,
        "_run_delete_cleanup",
        MagicMock(
            return_value=DeleteResponse(
                success=False,
                message="Resources retained by deletion-authority policy: gateway",
                retained=True,
            )
        ),
    )
    status_mock = MagicMock()
    monkeypatch.setattr(dh, "_set_delete_status", status_mock)

    out = dh._handle_async_delete(
        {
            "_async_delete": True,
            "runtime_id": "rt_slow_1",
            "caller_sub": "tester",
        }
    )

    assert out == {"success": False}
    status_mock.assert_called_once_with(
        "dep-slow-1",
        "delete_retained",
        "Resources retained by deletion-authority policy: gateway",
    )


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

    def _fake_async_delete(event, context=None):
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


def test_memory_teardown_is_slow_because_its_deletion_is_confirmed():
    """F-56: a Memory delete is polled to absence (up to ~2 min) before its role goes.

    Inline, that outlives API Gateway's 29-second cap: the caller gets a 503 while the
    claim stays held. Both record shapes name a Memory, and both teardowns confirm it.
    """
    manifest = {"created_resources": [{"type": "memory", "id": "mem-1", "region": "us-east-1"}]}
    legacy = {"memory_result": {"memory_id": "mem-1", "memory_role_name": "AgentCoreMemory-m"}}

    assert dh._is_slow_delete(manifest) is True
    assert dh._is_slow_delete(legacy) is True
    assert dh._is_slow_delete({"memory_result": {}}) is False
    assert dh._is_slow_delete({"memory_result": {"memory_id": ""}}) is False


# ---------------------------------------------------------------------------
# Delete-state durability — only successful tombstones expire
# ---------------------------------------------------------------------------


def test_deleted_status_sets_ttl_30_days_from_success(monkeypatch):
    state_store = MagicMock()
    state_store._table = object()
    monkeypatch.setattr(dh, "_get_state_store", lambda: state_store)
    update = MagicMock()
    monkeypatch.setattr(state_store_module, "_update_item", update)

    thirty_days = 30 * 24 * 60 * 60
    before = int(datetime.now(timezone.utc).timestamp()) + thirty_days
    dh._set_delete_status("dep-1", "deleted", "all torn down")
    after = int(datetime.now(timezone.utc).timestamp()) + thirty_days

    kwargs = update.call_args.kwargs
    assert kwargs["update_expr"] == ("SET delete_status = :ds, delete_message = :dm, #t = :ttl REMOVE #claim")
    assert before <= kwargs["expr_values"][":ttl"] <= after
    assert kwargs["expr_names"] == {
        "#t": "ttl",
        "#claim": "delete_claim_expires_at",
    }
    assert kwargs["condition_expr"] == "attribute_exists(deployment_id)"


@pytest.mark.parametrize("status", ["deleting", "delete_failed", "delete_retained"])
def test_non_deleted_status_removes_legacy_ttl(monkeypatch, status):
    state_store = MagicMock()
    state_store._table = object()
    monkeypatch.setattr(dh, "_get_state_store", lambda: state_store)
    update = MagicMock()
    monkeypatch.setattr(state_store_module, "_update_item", update)

    dh._set_delete_status("dep-1", status)

    kwargs = update.call_args.kwargs
    assert kwargs["update_expr"] == "SET delete_status = :ds REMOVE #t, #claim"
    assert ":ttl" not in kwargs["expr_values"]
    assert kwargs["expr_names"] == {
        "#t": "ttl",
        "#claim": "delete_claim_expires_at",
    }
    assert kwargs["condition_expr"] == "attribute_exists(deployment_id)"


def test_delete_claim_is_conditional_and_retryable(monkeypatch):
    state_store = MagicMock()
    state_store._table = object()
    monkeypatch.setattr(dh, "_get_state_store", lambda: state_store)
    update = MagicMock()
    monkeypatch.setattr(state_store_module, "_update_item", update)

    assert dh._claim_delete_status("dep-1") is True

    kwargs = update.call_args.kwargs
    removed = {part.strip() for part in kwargs["update_expr"].split(" REMOVE ", 1)[1].split(",")}
    assert removed == {"#t", "#fin", "#ftok"}
    assert kwargs["expr_values"][":failed"] == "delete_failed"
    assert kwargs["expr_values"][":retained"] == "delete_retained"
    assert kwargs["expr_values"][":lease"] - kwargs["expr_values"][":now"] == 15 * 60
    assert kwargs["expr_names"]["#claim"] == "delete_claim_expires_at"
    assert kwargs["expr_names"]["#fin"] == "finalizer_lease_expires_at"
    assert kwargs["expr_names"]["#ftok"] == "finalizer_lease_token"
    assert "attribute_not_exists(#ds)" in kwargs["condition_expr"]
    assert "#ds IN (:failed, :retained)" in kwargs["condition_expr"]
    assert "#claim < :now" in kwargs["condition_expr"]


def test_delete_claim_loses_a_concurrent_race_without_raising(monkeypatch):
    class ConditionalRace(Exception):
        response = {
            "Error": {
                "Code": "ConditionalCheckFailedException",
            }
        }

    state_store = MagicMock()

    # The refusal has to be attributable. `_claim_delete_status` now distinguishes
    # a rival teardown (False) from a deploy finalizer still writing
    # (ActiveFinalizerConflict), and it decides by reading the row's finalizer
    # lease. A fake with no readable table cannot answer that, and the classifier
    # deliberately fails toward "active finalizer" so nothing destructive runs --
    # so this fake must serve a real row that simply holds no lease, which is what
    # a rival teardown's row actually looks like.
    class _Table:
        def get_item(self, **_kwargs):
            return {"Item": {"deployment_id": "dep-1", "delete_status": "deleting"}}

    state_store._table = _Table()
    monkeypatch.setattr(dh, "_get_state_store", lambda: state_store)
    monkeypatch.setattr(
        state_store_module,
        "_update_item",
        MagicMock(side_effect=ConditionalRace()),
    )

    assert dh._claim_delete_status("dep-1") is False


def test_an_unreadable_row_is_classified_as_an_active_finalizer(monkeypatch):
    """A refusal we cannot attribute must be reported as the non-destructive one.

    The classifier read is the only thing that can tell a rival teardown from a
    live deploy finalizer. If it fails, guessing "rival teardown" would let the
    caller proceed on the assumption that another worker is already tearing down,
    while a finalizer could still be appending manifest rows -- the leak this
    barrier exists to prevent. Guessing "active finalizer" costs a retry.
    """

    class ConditionalRace(Exception):
        response = {"Error": {"Code": "ConditionalCheckFailedException"}}

    class _Unreadable:
        def get_item(self, **_kwargs):
            raise RuntimeError("the row cannot be read")

    state_store = MagicMock()
    state_store._table = _Unreadable()
    monkeypatch.setattr(dh, "_get_state_store", lambda: state_store)
    monkeypatch.setattr(
        state_store_module,
        "_update_item",
        MagicMock(side_effect=ConditionalRace()),
    )

    with pytest.raises(dh.ActiveFinalizerConflict):
        dh._claim_delete_status("dep-1")
