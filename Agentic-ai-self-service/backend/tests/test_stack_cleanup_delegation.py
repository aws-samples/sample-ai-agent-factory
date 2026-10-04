"""The standalone cleanup script delegates to the guarded Python teardown.

This direct Lambda event is an operator path, not another deletion
implementation.  It must be stack-bound, accept only real deployment rows,
atomically claim each cleanup, derive tenant identity from storage, and report
every retained/failed result before CDK can destroy the evidence needed to
retry.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "src")

import app.deployment_handler as dh  # noqa: E402
import app.services.deployment_state_store as state_store_module  # noqa: E402
import app.services.gateway_name_claim as gnc  # noqa: E402
from app.models.deployment_models import DeleteResponse  # noqa: E402

DEPLOYMENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
STACK_OWNER = "unit-tests-local-us-east-1"


def _state(record: dict) -> MagicMock:
    state = MagicMock()
    state.model_dump.return_value = record
    state.delete_status = record.get("delete_status")
    return state


def _event(**updates) -> dict:
    event = {
        "_stack_cleanup_delete": True,
        "deployment_id": DEPLOYMENT_ID,
        "expected_stack_owner": STACK_OWNER,
    }
    event.update(updates)
    return event


def test_wrong_stack_identity_cannot_read_or_delete(monkeypatch):
    store = MagicMock()
    monkeypatch.setattr(dh, "stack_id", lambda region: STACK_OWNER)
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    cleanup = MagicMock()
    monkeypatch.setattr(dh, "_run_delete_cleanup", cleanup)

    result = dh._handle_stack_cleanup_delete(_event(expected_stack_owner="some-other-stack-us-east-1"))

    assert result["success"] is False
    assert "different stack identity" in result["message"]
    store.get.assert_not_called()
    cleanup.assert_not_called()


def test_scratch_rows_and_noncanonical_ids_never_enter_teardown(monkeypatch):
    store = MagicMock()
    monkeypatch.setattr(dh, "stack_id", lambda region: STACK_OWNER)
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)

    for invalid in ("test-c640691d725d", "gen-abcdef", DEPLOYMENT_ID.upper(), ""):
        result = dh._handle_stack_cleanup_delete(_event(deployment_id=invalid))
        assert result["success"] is False
        assert "canonical deployment UUIDs" in result["message"]

    store.get.assert_not_called()


def test_absent_row_is_idempotent_and_touches_nothing(monkeypatch):
    store = MagicMock()
    store.get.return_value = None
    monkeypatch.setattr(dh, "stack_id", lambda region: STACK_OWNER)
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    claim = MagicMock()
    cleanup = MagicMock()
    monkeypatch.setattr(dh, "_claim_delete_status", claim)
    monkeypatch.setattr(dh, "_run_delete_cleanup", cleanup)

    result = dh._handle_stack_cleanup_delete(_event())

    assert result["success"] is True
    assert "no resources were touched" in result["message"]
    claim.assert_not_called()
    cleanup.assert_not_called()


def test_operator_delete_claims_and_uses_the_stored_owner(monkeypatch):
    record = {
        "deployment_id": DEPLOYMENT_ID,
        "runtime_id": "runtime-1",
        "user_id": "stored-tenant",
    }
    store = MagicMock()
    store.get.return_value = _state(record)
    monkeypatch.setattr(dh, "stack_id", lambda region: STACK_OWNER)
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    claim = MagicMock(return_value=True)
    cleanup = MagicMock(return_value=DeleteResponse(success=True, message="all resources confirmed absent"))
    status = MagicMock()
    monkeypatch.setattr(dh, "_claim_delete_status", claim)
    monkeypatch.setattr(dh, "_run_delete_cleanup", cleanup)
    monkeypatch.setattr(dh, "_set_delete_status", status)

    # A spoofed event owner is deliberately ignored.
    result = dh._handle_stack_cleanup_delete(_event(caller_sub="attacker"))

    assert result == {
        "success": True,
        "retained": False,
        "message": "all resources confirmed absent",
    }
    claim.assert_called_once_with(DEPLOYMENT_ID)
    cleanup.assert_called_once_with(
        "runtime-1",
        "stored-tenant",
        allow_imported_destroy=False,
    )
    status.assert_called_once_with(
        DEPLOYMENT_ID,
        "deleted",
        "all resources confirmed absent",
    )


def test_partial_deployment_uses_its_uuid_as_the_cleanup_identifier(monkeypatch):
    record = {
        "deployment_id": DEPLOYMENT_ID,
        "user_id": "stored-tenant",
    }
    store = MagicMock()
    store.get.return_value = _state(record)
    monkeypatch.setattr(dh, "stack_id", lambda region: STACK_OWNER)
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    monkeypatch.setattr(dh, "_claim_delete_status", lambda dep_id: True)
    cleanup = MagicMock(return_value=DeleteResponse(success=True, message="partial deployment removed"))
    monkeypatch.setattr(dh, "_run_delete_cleanup", cleanup)
    monkeypatch.setattr(dh, "_set_delete_status", MagicMock())

    result = dh._handle_stack_cleanup_delete(_event())

    assert result["success"] is True
    cleanup.assert_called_once_with(
        DEPLOYMENT_ID,
        "stored-tenant",
        allow_imported_destroy=False,
    )


def test_inflight_delete_stops_stack_destruction(monkeypatch):
    record = {
        "deployment_id": DEPLOYMENT_ID,
        "runtime_id": "runtime-1",
        "user_id": "stored-tenant",
        "delete_status": "deleting",
    }
    store = MagicMock()
    store.get.return_value = _state(record)
    monkeypatch.setattr(dh, "stack_id", lambda region: STACK_OWNER)
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    claim = MagicMock(return_value=False)
    cleanup = MagicMock()
    monkeypatch.setattr(dh, "_claim_delete_status", claim)
    monkeypatch.setattr(dh, "_run_delete_cleanup", cleanup)

    result = dh._handle_stack_cleanup_delete(_event())

    assert result["success"] is False
    assert result["retryable"] is True
    claim.assert_called_once_with(DEPLOYMENT_ID)
    cleanup.assert_not_called()


def test_safety_retention_is_preserved_and_reported(monkeypatch):
    record = {
        "deployment_id": DEPLOYMENT_ID,
        "runtime_id": "runtime-1",
        "user_id": "stored-tenant",
    }
    store = MagicMock()
    store.get.return_value = _state(record)
    monkeypatch.setattr(dh, "stack_id", lambda region: STACK_OWNER)
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    monkeypatch.setattr(dh, "_claim_delete_status", lambda dep_id: True)
    monkeypatch.setattr(
        dh,
        "_run_delete_cleanup",
        lambda *args, **kwargs: DeleteResponse(
            success=False,
            retained=True,
            message="ownership could not be proven",
        ),
    )
    status = MagicMock()
    monkeypatch.setattr(dh, "_set_delete_status", status)

    result = dh._handle_stack_cleanup_delete(_event())

    assert result == {
        "success": False,
        "retained": True,
        "message": "ownership could not be proven",
    }
    status.assert_called_once_with(
        DEPLOYMENT_ID,
        "delete_retained",
        "ownership could not be proven",
    )


def test_handler_routes_the_stack_cleanup_sentinel(monkeypatch):
    delegated = MagicMock(return_value={"success": True})
    monkeypatch.setattr(dh, "_handle_stack_cleanup_delete", delegated)
    event = _event()

    assert dh.handler(event, None) == {"success": True}
    delegated.assert_called_once_with(event)


_DELETED = {
    "deployment_id": DEPLOYMENT_ID,
    "runtime_id": "runtime-1",
    "user_id": "stored-tenant",
    "delete_status": "deleted",
}


def _recovery(monkeypatch, outcome, *, fail=None, claimed=True, current="deleted"):
    """A "deleted" row, a recovery outcome, and the strict reopen write."""
    writes, store = [], MagicMock()
    store.get.side_effect = [_state(_DELETED), _state({**_DELETED, "delete_status": current})]

    def _reclaim(*, deployment_id, claims=None):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _update(table, **kw):
        if fail is not None:
            raise fail
        writes.append(kw)

    monkeypatch.setattr(dh, "stack_id", lambda region: STACK_OWNER)
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    monkeypatch.setattr(dh, "reclaim_recovery_pointer", _reclaim)
    monkeypatch.setattr(state_store_module, "_update_item", _update)
    claim = MagicMock(return_value=claimed)
    cleanup = MagicMock(return_value=DeleteResponse(success=True, message="all resources confirmed absent"))
    monkeypatch.setattr(dh, "_claim_delete_status", claim)
    monkeypatch.setattr(dh, "_run_delete_cleanup", cleanup)
    monkeypatch.setattr(dh, "_set_delete_status", MagicMock())
    return writes, claim, cleanup


@pytest.mark.parametrize("outcome", [dh.POINTER_ABSENT, dh.POINTER_MARKED])
def test_an_observed_deleted_row_with_no_live_recovery_is_final(monkeypatch, outcome):
    writes, claim, cleanup = _recovery(monkeypatch, outcome)

    assert dh._handle_stack_cleanup_delete(_event()) == {"success": True, "message": "Deployment was already deleted."}
    assert writes == []
    claim.assert_not_called()
    cleanup.assert_not_called()


@pytest.mark.parametrize("outcome", [dh.POINTER_ACTIVE, dh.POINTER_RACED])
def test_an_observed_deleted_row_with_live_recovery_is_torn_down_again(monkeypatch, outcome):
    writes, claim, cleanup = _recovery(monkeypatch, outcome)

    result = dh._handle_stack_cleanup_delete(_event())

    assert result["success"] is True and result["message"] == "all resources confirmed absent"
    [write] = writes
    assert write["condition_expr"] == "#ds = :deleted" and write["expr_values"][":failed"] == "delete_failed"
    claim.assert_called_once_with(DEPLOYMENT_ID)
    cleanup.assert_called_once()


@pytest.mark.parametrize(
    ("outcome", "fail"),
    [
        (gnc.POINTER_MALFORMED, None),
        (RuntimeError("ThrottlingException"), None),
        (dh.POINTER_ACTIVE, RuntimeError("ConditionalCheckFailedException")),
    ],
    ids=["malformed", "unreadable", "reopen-failed"],
)
def test_an_observed_deleted_row_whose_recovery_is_unverified_stops_the_destroy(monkeypatch, outcome, fail):
    writes, claim, cleanup = _recovery(monkeypatch, outcome, fail=fail)

    result = dh._handle_stack_cleanup_delete(_event())

    assert result == {"success": False, "retryable": True, "message": dh._DELETED_UNVERIFIED_MESSAGE}
    claim.assert_not_called()
    cleanup.assert_not_called()


@pytest.mark.parametrize(
    ("outcome", "success"),
    [
        (dh.POINTER_ABSENT, True),
        (dh.POINTER_MARKED, True),
        (dh.POINTER_ACTIVE, False),
        (dh.POINTER_RACED, False),
        (gnc.POINTER_MALFORMED, False),
        (RuntimeError("ThrottlingException"), False),
    ],
)
def test_a_concurrent_deletion_is_success_only_once_its_recovery_is_final(monkeypatch, outcome, success):
    """The claim was lost to a worker that wrote "deleted": the same reconciliation
    decides, or a promote after that worker's read would let CDK destroy the evidence."""
    store = MagicMock()
    record = {**_DELETED, "delete_status": "deleting"}
    store.get.side_effect = [_state(record), _state(_DELETED)]
    _, claim, cleanup = _recovery(monkeypatch, outcome, claimed=False)
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)

    result = dh._handle_stack_cleanup_delete(_event())

    assert result["success"] is success
    if not success:
        assert result["retryable"] is True
    claim.assert_called_once_with(DEPLOYMENT_ID)
    cleanup.assert_not_called()
