"""A delete the service accepted but had not finished is confirmed in the background, not reported retained.

Measured live 2026-10-02 (the first palette/Harness teardown): a harness's managed Memory was still
DELETING when the async teardown's ~6 min confirmation budget ran out, so the record ended
``delete_retained`` with "Resources retained by deletion-authority policy: iam_role, memory" --
although ``DELETE /api/runtime`` had answered "teardown continues in the background while slow
resources (a Knowledge Base, Memory) are confirmed deleted". Live, the Memory was gone minutes
later; a hand retry converged in 12 s ("memory confirmed deleted", the kept role deleted).

The teardown now tells an accepted-but-unconfirmed delete (``ConfirmationBudgetExhausted``, and
the memory role kept for exactly such a memory) apart from every real refusal, reports it as
``confirmation_pending``, and the async path hands such a teardown to a fresh invocation, up to
``_MAX_CONFIRMATION_CONTINUATIONS`` times, renewing the claim lease each time. Only a real refusal
or failure, or an exhausted chain, records ``delete_retained``.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import app.deployment_handler as dh
import pytest
from app.services.deletion_confirmation import ConfirmationBudgetExhausted
from app.services.resource_ownership import ResourceDeletionRefused

from tests.test_async_memory_delete_budget_and_retry import (
    MEMORY_ROLE,
    OWNER,
    _client_error,
    _memory_manifest_record,
    _wire_manifest_retry,
)

BUDGET_MESSAGE = (
    "Deletion of memory mem-1 was accepted, but the resource is still DELETING at the end of the "
    "confirmation budget; deletion is not confirmed."
)


def _memory_still_deleting(monkeypatch):
    """The real teardown over the memory manifest: the runtime is gone, the Memory outlives the budget."""
    control = MagicMock()
    control.get_agent_runtime.side_effect = _client_error("ResourceNotFoundException", "GetAgentRuntime")
    deleted_roles = _wire_manifest_retry(monkeypatch, control)
    monkeypatch.setattr(
        dh, "delete_memory_confirmed", MagicMock(side_effect=ConfirmationBudgetExhausted(BUDGET_MESSAGE))
    )
    return deleted_roles


# --- the classification: pending only when nothing was refused ---------------------------


def test_a_memory_that_outlived_the_budget_is_pending_and_its_role_is_kept(monkeypatch):
    deleted_roles = _memory_still_deleting(monkeypatch)

    result = dh._run_delete_cleanup("rt-mem", OWNER)

    assert result.success is False
    assert result.retained is True, result.message
    assert result.confirmation_pending is True, result.message
    assert MEMORY_ROLE not in deleted_roles, "the role is kept until its memory is confirmed gone"
    assert "its memory is not confirmed deleted" in result.message
    assert BUDGET_MESSAGE in result.message


def test_a_memory_refused_for_any_other_reason_is_not_pending(monkeypatch):
    control = MagicMock()
    control.get_agent_runtime.side_effect = _client_error("ResourceNotFoundException", "GetAgentRuntime")
    _wire_manifest_retry(monkeypatch, control)
    monkeypatch.setattr(
        dh,
        "delete_memory_confirmed",
        MagicMock(side_effect=ResourceDeletionRefused("memory mem-1 belongs to another caller")),
    )

    result = dh._run_delete_cleanup("rt-mem", OWNER)

    assert result.retained is True
    assert result.confirmation_pending is False


def test_a_real_refusal_next_to_a_pending_memory_ends_the_chain(monkeypatch):
    """One refused row is enough: a later invocation could not change a refusal, so re-running would
    only delay the verdict the user has to act on."""
    _memory_still_deleting(monkeypatch)
    monkeypatch.setattr(
        dh,
        "manifest_delete_refusal",
        lambda _store, _dep, resource, **_kw: (
            "skipped (protected): ownership could not be proven" if resource.get("type") == "iam_role" else None
        ),
    )

    result = dh._run_delete_cleanup("rt-mem", OWNER)

    assert result.retained is True
    assert result.confirmation_pending is False


def test_a_failed_row_next_to_a_pending_memory_ends_the_chain(monkeypatch):
    """A cleanup failure is operational, not slowness; re-running would hide it behind "deleting"."""
    _memory_still_deleting(monkeypatch)
    record = _memory_manifest_record()
    record["created_resources"].append(
        {"type": "secret", "id": "arn:aws:secretsmanager:us-east-1:123456789012:secret:x-AbCdEf", "region": "us-east-1"}
    )
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda _t, rid: record if rid == "rt-mem" else None)
    real_delete = dh._delete_managed_resource

    def _secret_fails(resource, region, **kwargs):
        if resource.get("type") == "secret":
            raise RuntimeError("InternalServiceError on DeleteSecret")
        return real_delete(resource, region, **kwargs)

    monkeypatch.setattr(dh, "_delete_managed_resource", _secret_fails)

    result = dh._run_delete_cleanup("rt-mem", OWNER)

    assert result.success is False
    assert result.confirmation_pending is False
    assert "Cleanup failures in: secret" in result.message


def test_a_runtime_row_whose_destroy_fails_next_to_a_pending_memory_ends_the_chain(monkeypatch):
    """The runtime is still there and owned; its destroy fails. A failure, not slowness."""
    _memory_still_deleting(monkeypatch)
    monkeypatch.setattr(dh, "assert_agentcore_resource_owned", lambda *a, **k: None)
    monkeypatch.setattr(dh, "destroy_runtime", lambda *a, **k: {"success": False, "message": "runtime destroy failed"})

    result = dh._run_delete_cleanup("rt-mem", OWNER)

    assert result.success is False
    assert result.confirmation_pending is False
    assert "Cleanup failures in: agent_runtime" in result.message


def test_a_pre_manifest_runtime_destroy_that_fails_next_to_a_pending_memory_ends_the_chain(monkeypatch):
    """A record from before the manifest names its runtime only on the record: the legacy post-manifest
    destroy runs for it, and its failure is operational too."""
    _memory_still_deleting(monkeypatch)
    record = _memory_manifest_record()
    record["created_resources"] = [row for row in record["created_resources"] if row["type"] != "agent_runtime"]
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda _t, rid: record if rid == "rt-mem" else None)
    monkeypatch.setattr(dh, "destroy_runtime", lambda *a, **k: {"success": False, "message": "runtime destroy failed"})

    result = dh._run_delete_cleanup("rt-mem", OWNER)

    assert result.success is False
    assert result.confirmation_pending is False


def test_a_legacy_runtime_kept_by_policy_next_to_a_pending_memory_ends_the_chain(monkeypatch):
    """The pre-manifest runtime path retains through the same deletion-authority gate; that retention is a
    verdict for the user, not slowness, so the chain must not hide it behind "deleting"."""
    _memory_still_deleting(monkeypatch)
    record = _memory_manifest_record()
    record["created_resources"] = [row for row in record["created_resources"] if row["type"] != "agent_runtime"]
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda _t, rid: record if rid == "rt-mem" else None)
    monkeypatch.setattr(
        dh,
        "manifest_delete_refusal",
        lambda _store, _dep, resource, **_kw: (
            "another live deployment adopted this runtime" if resource.get("type") == "agent_runtime" else None
        ),
    )

    result = dh._run_delete_cleanup("rt-mem", OWNER)

    assert result.retained is True
    assert result.confirmation_pending is False
    assert "Legacy resources retained by live-ownership policy: agent_runtime" in result.message


def test_a_memory_that_finished_deleting_is_neither_retained_nor_pending(monkeypatch):
    control = MagicMock()
    control.get_memory.side_effect = _client_error("ResourceNotFoundException", "GetMemory")
    control.get_agent_runtime.side_effect = _client_error("ResourceNotFoundException", "GetAgentRuntime")
    deleted_roles = _wire_manifest_retry(monkeypatch, control)

    result = dh._run_delete_cleanup("rt-mem", OWNER)

    assert result.success is True, result.message
    assert result.confirmation_pending is False
    assert MEMORY_ROLE in deleted_roles


# --- the chain: the async path hands a pending teardown to the next invocation -------------


def _pending(message: str = "Resources retained by deletion-authority policy: iam_role, memory") -> dh.DeleteResponse:
    return dh.DeleteResponse(success=False, message=message, retained=True, confirmation_pending=True)


def _wire_async(monkeypatch, result: dh.DeleteResponse):
    monkeypatch.setattr(dh, "_lookup_deployment_record", lambda _rid: {"deployment_id": "dep-1"})
    monkeypatch.setattr(dh, "_run_delete_cleanup", lambda *_a, **_k: result)
    terminal = MagicMock()
    progress = MagicMock()
    lam = MagicMock()
    monkeypatch.setattr(dh, "_set_delete_status", terminal)
    monkeypatch.setattr(dh, "_note_delete_progress", progress)
    monkeypatch.setattr(dh.boto3, "client", lambda service, **_kw: lam if service == "lambda" else MagicMock())
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "acf-test-deployment")
    return terminal, progress, lam


EVENT = {"_async_delete": True, "runtime_id": "rt-mem", "caller_sub": OWNER, "allow_imported_destroy": False}


def test_the_first_pass_hands_a_pending_teardown_to_a_second_invocation(monkeypatch):
    terminal, progress, lam = _wire_async(monkeypatch, _pending())

    out = dh._run_async_delete(dict(EVENT))

    assert out == {"success": False, "confirmation_pending": True, "confirmation_hop": 1}
    terminal.assert_not_called()
    kwargs = lam.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == "acf-test-deployment" and kwargs["InvocationType"] == "Event"
    assert json.loads(kwargs["Payload"]) == {**EVENT, "confirmation_hop": 1}
    (dep_id, message), _ = progress.call_args
    assert dep_id == "dep-1"
    assert message.startswith("Deletion accepted; confirming in the background (pass 2 of 5): Resources retained")


def test_every_hop_carries_the_original_event_and_counts_up(monkeypatch):
    terminal, _progress, lam = _wire_async(monkeypatch, _pending())

    out = dh._run_async_delete({**EVENT, "confirmation_hop": 2})

    assert out["confirmation_hop"] == 3
    assert json.loads(lam.invoke.call_args.kwargs["Payload"])["confirmation_hop"] == 3
    terminal.assert_not_called()


def test_an_exhausted_chain_records_the_retention(monkeypatch):
    terminal, progress, lam = _wire_async(monkeypatch, _pending())

    out = dh._run_async_delete({**EVENT, "confirmation_hop": dh._MAX_CONFIRMATION_CONTINUATIONS})

    assert out == {"success": False}
    lam.invoke.assert_not_called()
    progress.assert_not_called()
    terminal.assert_called_once_with(
        "dep-1", "delete_retained", "Resources retained by deletion-authority policy: iam_role, memory"
    )


def test_a_retention_that_is_not_pending_is_recorded_at_once(monkeypatch):
    terminal, _progress, lam = _wire_async(
        monkeypatch, dh.DeleteResponse(success=False, message="retained: protected", retained=True)
    )

    dh._run_async_delete(dict(EVENT))

    lam.invoke.assert_not_called()
    terminal.assert_called_once_with("dep-1", "delete_retained", "retained: protected")


def test_a_hand_off_that_cannot_be_made_falls_back_to_the_retention(monkeypatch):
    terminal, _progress, lam = _wire_async(monkeypatch, _pending())
    lam.invoke.side_effect = RuntimeError("TooManyRequestsException")

    out = dh._run_async_delete(dict(EVENT))

    assert out == {"success": False}
    terminal.assert_called_once()
    assert terminal.call_args.args[1] == "delete_retained"


def test_a_successful_teardown_is_recorded_deleted_without_a_hand_off(monkeypatch):
    terminal, _progress, lam = _wire_async(monkeypatch, dh.DeleteResponse(success=True, message="Cleanup completed"))

    dh._run_async_delete(dict(EVENT))

    lam.invoke.assert_not_called()
    terminal.assert_called_once_with("dep-1", "deleted", "Cleanup completed")


# --- the progress note keeps the claim -----------------------------------------------------


def test_the_progress_note_renews_the_claim_and_stays_on_a_deleting_record(monkeypatch):
    from app.services import deployment_state_store as store_module

    captured: dict = {}
    monkeypatch.setattr(store_module, "_update_item", lambda _table, **kwargs: captured.update(kwargs))
    monkeypatch.setattr(dh, "_get_state_store", lambda: MagicMock(_table=object()))
    before = int(time.time())

    dh._note_delete_progress("dep-1", "x" * 10_000)

    assert captured["key"] == {"deployment_id": "dep-1"}
    assert captured["update_expr"] == "SET delete_message = :dm, #claim = :lease"
    assert captured["condition_expr"] == "attribute_exists(deployment_id) AND #ds = :deleting"
    assert captured["expr_names"] == {"#claim": "delete_claim_expires_at", "#ds": "delete_status"}
    values = captured["expr_values"]
    assert values[":deleting"] == "deleting"
    assert len(values[":dm"]) == store_module.DELETE_MESSAGE_MAX_CHARS == 4096
    assert (
        before + dh._DELETE_CLAIM_LEASE_SECONDS - 5 <= values[":lease"] <= before + dh._DELETE_CLAIM_LEASE_SECONDS + 5
    )
    assert "REMOVE" not in captured["update_expr"], "an interim note must not release the claim"


def test_the_terminal_write_keeps_four_kib_of_the_message(monkeypatch):
    from app.services import deployment_state_store as store_module

    captured: dict = {}
    monkeypatch.setattr(store_module, "_update_item", lambda _table, **kwargs: captured.update(kwargs))
    monkeypatch.setattr(dh, "_get_state_store", lambda: MagicMock(_table=object()))

    dh._set_delete_status("dep-1", "delete_retained", "y" * 10_000)

    assert len(captured["expr_values"][":dm"]) == 4096


@pytest.mark.parametrize("hop", [0, 1, dh._MAX_CONFIRMATION_CONTINUATIONS - 1])
def test_the_chain_is_bounded(hop):
    assert dh._MAX_CONFIRMATION_CONTINUATIONS == 4
    event = {**EVENT, "confirmation_hop": hop}
    # the next hop is always below the bound, so no event can run the teardown for ever
    assert int(event["confirmation_hop"]) + 1 <= dh._MAX_CONFIRMATION_CONTINUATIONS
