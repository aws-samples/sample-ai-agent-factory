"""The deploy-vs-delete barrier: a teardown may not begin while a finalizer writes.

THE MEASURED DEFECT. ``status_update_step.handler`` commits the terminal SUCCEEDED
status and only THEN does the version row, the slot pointer, the registry record
and its last manifest appends. Three peer audit sessions measured the same
five-step interleaving through that window:

  1. a step succeeds and its resource is recorded
  2. a DELETE arrives and claims the row: ``delete_status=deleting``
  3. teardown snapshots ``created_resources``
  4. the still-running finalizer appends one more row (a Lambda)
  5. teardown writes ``delete_status=deleted`` -- FINAL -- over a manifest row no
     deleter had ever seen

The Lambda leaks permanently, ``_deleted_is_final`` afterwards answers "already
deleted", and DELETE reported success the whole way. One session measured the
surrounding states directly: ``{status_before_claim: succeeded,
claim_won_before_handler_return: True}`` on the success path and
``{status_before_claim: failed, claim_won_while_handler_active: True}`` at entry
to ``_auto_cleanup_on_failure``.

WHY A LEASE AND NOT A STATUS CHECK. ``status`` cannot see this window: it is
already ``succeeded`` for the whole of it. And refusing to claim ``in_progress``
would strand rows forever, because an aborted execution bypasses the Catch and
sits at ``in_progress`` permanently. So the barrier is a bounded lease the
finalizer holds, evaluated INSIDE the claim's own ConditionExpression.

ARCC cnt_vBC0kXE8PNHqrW names this class exactly -- an authorization check made
at a time when it is no longer true, i.e. TOCTOU in an asynchronous workflow --
and cnt_4mD5f0eLH0RCDK prescribes the remedy: make the critical section atomic
and surface the conflict at use time rather than pre-reading state that moves.

WHY A REAL TABLE. "Nothing was written" and "the claim was refused" are claims
about rows and about DynamoDB's own condition evaluation. A mocked
``update_item`` cannot make either one: it would assert that this file's
understanding of the expression matches itself. Every test here drives a real
moto-backed table through the real store.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import boto3
import pytest

sys.path.insert(0, "src")

pytest.importorskip("moto")

import app.deployment_handler as dh  # noqa: E402
import app.services.deployment_state_store as state_store_module  # noqa: E402
from app.models.deployment_models import (  # noqa: E402
    DeleteResponse,
    DeploymentState,
    DeploymentStatusEnum,
    DeploymentStepName,
)
from app.services.deployment_state_store import (  # noqa: E402
    FINALIZER_LEASE_ATTR,
    FINALIZER_TOKEN_ATTR,
    DeploymentLifecycleConflict,
    DeploymentStateStore,
    FinalizerLeaseBusy,
)
from app.step_handlers import status_update_step as sus  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

DEPLOYMENT = "dep-barrier-1"
RUNTIME = "rt_barrier_1"
OWNER = "sub-owner"
TABLE = "Deployments"
REGION = "us-east-1"


@pytest.fixture
def store(monkeypatch):
    """A real store over a real moto table, wired into the delete handler."""
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "deployment_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "deployment_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        live = DeploymentStateStore(TABLE, REGION)
        live.create(
            DeploymentState(
                deployment_id=DEPLOYMENT,
                workflow_id="wf-barrier",
                user_id=OWNER,
                status=DeploymentStatusEnum.IN_PROGRESS,
                started_at=datetime.now(timezone.utc),
            )
        )
        monkeypatch.setattr(dh, "_get_state_store", lambda: live)
        yield live


def raw(store, deployment_id: str = DEPLOYMENT) -> dict:
    """The persisted row, strongly consistent: the only honest answer to "what was written"."""
    return (
        store._table.get_item(  # noqa: SLF001 - exact persisted-state assertion
            Key={"deployment_id": deployment_id},
            ConsistentRead=True,
        ).get("Item")
        or {}
    )


def record_a_resource(
    store,
    resource: dict,
    *,
    finalizer_token: str | None = None,
) -> None:
    store.record_resource_strict(
        DEPLOYMENT,
        {**resource, "created_by_deployment": True},
        finalizer_token=finalizer_token,
    )


# ---------------------------------------------------------------------------
# Controls. A suite that only proves refusals is compatible with refusing
# everything, and a barrier that blocks every teardown is not a fix.
# ---------------------------------------------------------------------------


def test_a_claim_with_no_finalizer_running_succeeds(store):
    assert dh._claim_delete_status(DEPLOYMENT) is True
    assert raw(store)["delete_status"] == "deleting"


def test_the_lease_round_trips_and_leaves_no_residue(store):
    """Also the only coverage of a REMOVE-only update against a real table.

    ``_update_item`` used to pass ExpressionAttributeValues unconditionally, and
    DynamoDB rejects an empty map -- so the release could not have worked at all.
    A mock would have accepted it silently.
    """
    token = store.acquire_finalizer_lease(DEPLOYMENT)
    row = raw(store)
    assert row[FINALIZER_TOKEN_ATTR] == token
    assert int(row[FINALIZER_LEASE_ATTR]) > int(datetime.now(timezone.utc).timestamp())

    store.release_finalizer_lease(DEPLOYMENT, token)
    row = raw(store)
    assert FINALIZER_LEASE_ATTR not in row
    assert FINALIZER_TOKEN_ATTR not in row


def test_a_released_lease_no_longer_blocks_a_claim(store):
    token = store.acquire_finalizer_lease(DEPLOYMENT)
    store.release_finalizer_lease(DEPLOYMENT, token)
    assert dh._claim_delete_status(DEPLOYMENT) is True


def test_a_rival_teardown_is_still_reported_as_a_lost_race_not_a_conflict(store):
    """The pre-existing contract: two teardown workers, second gets False.

    This must NOT become ``ActiveFinalizerConflict``. The callers act on the
    difference -- a rival teardown is progress, an active deploy means come back.
    """
    assert dh._claim_delete_status(DEPLOYMENT) is True
    assert dh._claim_delete_status(DEPLOYMENT) is False


# ---------------------------------------------------------------------------
# The barrier itself.
# ---------------------------------------------------------------------------


def test_a_live_finalizer_lease_refuses_the_delete_claim(store):
    store.acquire_finalizer_lease(DEPLOYMENT)

    with pytest.raises(dh.ActiveFinalizerConflict):
        dh._claim_delete_status(DEPLOYMENT)

    # Refused means nothing was written. A claim that sets `deleting` and then
    # reports a conflict would have already broken the finalizer's next write.
    row = raw(store)
    assert "delete_status" not in row
    assert "delete_claim_expires_at" not in row


def test_the_exact_measured_five_step_leak_cannot_start(store):
    """Replay the measured interleaving; step 2 is where it now stops.

    The original run reached step 5 and produced
    ``final_delete_status=deleted late_manifest_rows=[late-fn]`` with no deleter
    ever called for that Lambda.
    """
    # (1) a step succeeded and recorded its resource.
    record_a_resource(store, {"type": "agent_runtime", "id": RUNTIME, "region": REGION})
    # the finalizer is running: terminal status committed, post-work outstanding.
    token = store.acquire_finalizer_lease(DEPLOYMENT)

    # (2) the DELETE arrives and tries to claim. THIS is the new stopping point.
    with pytest.raises(dh.ActiveFinalizerConflict):
        dh._claim_delete_status(DEPLOYMENT)

    # (3) there is therefore no teardown snapshot, and (4) the finalizer's late
    # append lands on a row no teardown owns.
    record_a_resource(
        store,
        {"type": "lambda", "id": "late-fn", "region": REGION},
        finalizer_token=token,
    )

    # (5) no `deleted` tombstone exists, so the late row is still deletable.
    row = raw(store)
    assert "delete_status" not in row
    ids = [r.get("id") for r in row["created_resources"]]
    assert ids == [RUNTIME, "late-fn"]


def test_the_measured_success_path_interleaving_is_refused(store):
    """``{status_before_claim: succeeded, claim_won_before_handler_return: True}``.

    The status is already terminal here, which is precisely why a status-based
    gate cannot see this window and the lease can.
    """
    token = store.acquire_finalizer_lease(DEPLOYMENT)
    store.update_status(
        DEPLOYMENT,
        DeploymentStatusEnum.SUCCEEDED,
        finalizer_token=token,
    )
    assert raw(store)["status"] == DeploymentStatusEnum.SUCCEEDED.value

    with pytest.raises(dh.ActiveFinalizerConflict):
        dh._claim_delete_status(DEPLOYMENT)


def test_the_measured_failure_path_interleaving_is_refused(store):
    """``{status_before_claim: failed, claim_won_while_handler_active: True}``.

    The finalizer's own auto-cleanup has announced ``deleting`` and
    ``update_delete_status`` REMOVEd ``delete_claim_expires_at`` on the way -- so
    the row reads as an unclaimed teardown and the pre-existing
    ``#claim`` term alone would hand it straight to a second worker. Only the
    separate finalizer lease still holds the door.
    """
    token = store.acquire_finalizer_lease(DEPLOYMENT)
    store.update_status(
        DEPLOYMENT,
        DeploymentStatusEnum.FAILED,
        finalizer_token=token,
    )
    store.update_delete_status(
        DEPLOYMENT,
        "deleting",
        "Automatic cleanup started.",
        finalizer_token=token,
    )

    row = raw(store)
    assert row["delete_status"] == "deleting"
    assert "delete_claim_expires_at" not in row, (
        "precondition of this test: the claim lease is gone, so it cannot be what refuses"
    )

    with pytest.raises(dh.ActiveFinalizerConflict):
        dh._claim_delete_status(DEPLOYMENT)


# ---------------------------------------------------------------------------
# Nothing may be stranded. A barrier that cannot expire is an outage.
# ---------------------------------------------------------------------------


def test_an_expired_lease_does_not_strand_the_deployment(store):
    store.acquire_finalizer_lease(DEPLOYMENT, seconds=-60)
    assert dh._claim_delete_status(DEPLOYMENT) is True


def test_a_lease_expiring_exactly_now_is_claimable(store):
    """The equality boundary, pinned because the two sides disagreed once.

    The condition says expired at ``<= :now`` and the classifier says live at
    ``> now``. If those drift apart, a refusal at the boundary gets classified as
    a rival teardown when in fact nothing owns the row.
    """
    store.acquire_finalizer_lease(DEPLOYMENT, seconds=0)
    assert dh._claim_delete_status(DEPLOYMENT) is True


def test_the_claim_retries_once_when_the_lease_expires_mid_classification(store):
    """Refused on a live lease, then the lease expires before classification.

    Without the retry the row is reported as owned by a rival teardown: the
    public route would say "still deploying" forever and stack cleanup could
    never take an already-expired barrier. The retry is itself a conditional
    write, so settling it this way adds no check-then-act window.
    """
    real_update = state_store_module._update_item
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # The lease was live at this instant, and expires immediately after.
            raise state_store_module.ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        return real_update(*args, **kwargs)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(state_store_module, "_update_item", flaky)
    try:
        assert dh._claim_delete_status(DEPLOYMENT) is True
    finally:
        monkey.undo()
    assert calls["n"] == 2, "the second attempt is the whole point of this test"
    assert raw(store)["delete_status"] == "deleting"


# ---------------------------------------------------------------------------
# Ownership. A retry must wait, and a stale owner must never drop the barrier.
# ---------------------------------------------------------------------------


def test_a_live_finalizer_refuses_a_concurrent_retry_without_changing_owner(store):
    """A retry waits; it never overlaps the external side effects of its predecessor."""
    first = store.acquire_finalizer_lease(DEPLOYMENT)
    before = raw(store)

    with pytest.raises(FinalizerLeaseBusy):
        store.acquire_finalizer_lease(DEPLOYMENT)

    assert raw(store) == before
    assert raw(store)[FINALIZER_TOKEN_ATTR] == first
    with pytest.raises(dh.ActiveFinalizerConflict):
        dh._claim_delete_status(DEPLOYMENT)


def test_a_stale_owner_cannot_release_a_newer_owners_lease(store):
    """The reason the release token is mandatory.

    A stale invocation finishing late must not tear down the barrier a retry is
    relying on -- that would re-open the exact window the lease exists to close.
    """
    first = store.acquire_finalizer_lease(DEPLOYMENT, seconds=-1)
    second = store.acquire_finalizer_lease(DEPLOYMENT)

    store.release_finalizer_lease(DEPLOYMENT, first)  # best-effort, must be refused

    assert raw(store)[FINALIZER_TOKEN_ATTR] == second
    with pytest.raises(dh.ActiveFinalizerConflict):
        dh._claim_delete_status(DEPLOYMENT)


def test_the_status_handler_propagates_a_busy_lease_for_step_functions_retry(
    store,
    monkeypatch,
):
    incumbent = store.acquire_finalizer_lease(DEPLOYMENT)
    before = raw(store)
    monkeypatch.setattr(sus, "_get_deployment_store", lambda: store)

    with pytest.raises(FinalizerLeaseBusy):
        sus.handler({"deployment_id": DEPLOYMENT}, None)

    assert raw(store) == before
    assert raw(store)[FINALIZER_TOKEN_ATTR] == incumbent


def _write_step(store, token: str | None) -> None:
    store.update_step(
        DEPLOYMENT,
        DeploymentStepName.STATUS_UPDATE,
        finalizer_token=token,
    )


def _write_resource(store, token: str | None) -> None:
    record_a_resource(
        store,
        {"type": "lambda", "id": "fenced-function", "region": REGION},
        finalizer_token=token,
    )


def _write_manifest_error(store, token: str | None) -> None:
    store.mark_resource_manifest_error(DEPLOYMENT, finalizer_token=token)


def _write_gateway_handle(store, token: str | None) -> None:
    store.record_gateway_handle(
        DEPLOYMENT,
        {"gateway_id": "gateway-fenced", "gateway_name": "gateway-fenced"},
        finalizer_token=token,
    )


def _write_status(store, token: str | None) -> None:
    store.update_status(
        DEPLOYMENT,
        DeploymentStatusEnum.SUCCEEDED,
        finalizer_token=token,
    )


def _write_failed_status(store, token: str | None) -> None:
    store.update_status(
        DEPLOYMENT,
        DeploymentStatusEnum.FAILED,
        resource_manifest_complete=False,
        finalizer_token=token,
    )


def _write_registry_pointer(store, token: str | None) -> None:
    store.set_registry_record(
        DEPLOYMENT,
        "record-fenced",
        "DRAFT",
        finalizer_token=token,
    )


def _write_deleted_tombstone(store, token: str | None) -> None:
    store.update_delete_status(
        DEPLOYMENT,
        "deleted",
        "stale finalizer must not publish this",
        finalizer_token=token,
    )


LIFECYCLE_WRITERS = [
    pytest.param(_write_step, id="step"),
    pytest.param(_write_resource, id="resource"),
    pytest.param(_write_manifest_error, id="manifest-error"),
    pytest.param(_write_gateway_handle, id="gateway-handle"),
    pytest.param(_write_status, id="status"),
    pytest.param(_write_registry_pointer, id="registry-pointer"),
]


@pytest.mark.parametrize("writer", LIFECYCLE_WRITERS)
def test_an_ordinary_writer_cannot_enter_a_live_finalizer(store, writer):
    store.acquire_finalizer_lease(DEPLOYMENT)
    before = raw(store)

    with pytest.raises(DeploymentLifecycleConflict):
        writer(store, None)

    assert raw(store) == before


@pytest.mark.parametrize("writer", LIFECYCLE_WRITERS)
def test_an_ordinary_writer_cannot_enter_a_teardown_owned_row(store, writer):
    assert dh._claim_delete_status(DEPLOYMENT) is True
    before = raw(store)

    with pytest.raises(DeploymentLifecycleConflict):
        writer(store, None)

    assert raw(store) == before


@pytest.mark.parametrize(
    "writer",
    [
        pytest.param(_write_resource, id="resource"),
        pytest.param(_write_manifest_error, id="manifest-error"),
        pytest.param(_write_gateway_handle, id="gateway-handle"),
        pytest.param(_write_failed_status, id="failed-status"),
    ],
)
def test_the_token_owner_can_write_failure_recovery_after_announcing_deleting(
    store,
    writer,
):
    token = store.acquire_finalizer_lease(DEPLOYMENT)
    store.update_delete_status(
        DEPLOYMENT,
        "deleting",
        "Automatic cleanup started.",
        finalizer_token=token,
    )

    writer(store, token)

    assert raw(store)[FINALIZER_TOKEN_ATTR] == token


@pytest.mark.parametrize(
    "writer",
    [*LIFECYCLE_WRITERS, pytest.param(_write_deleted_tombstone, id="deleted-tombstone")],
)
def test_every_stale_finalizer_write_is_refused_after_takeover(store, writer):
    stale = store.acquire_finalizer_lease(DEPLOYMENT, seconds=-1)
    current = store.acquire_finalizer_lease(DEPLOYMENT)
    before = raw(store)

    with pytest.raises(DeploymentLifecycleConflict):
        writer(store, stale)

    assert raw(store) == before
    assert raw(store)[FINALIZER_TOKEN_ATTR] == current


def test_record_resource_marks_a_transport_failure_with_the_same_token(
    store,
    monkeypatch,
):
    token = store.acquire_finalizer_lease(DEPLOYMENT)
    strict = MagicMock(side_effect=RuntimeError("transport failed"))
    marker = MagicMock()
    monkeypatch.setattr(store, "record_resource_strict", strict)
    monkeypatch.setattr(store, "mark_resource_manifest_error", marker)

    store.record_resource(
        DEPLOYMENT,
        {
            "type": "lambda",
            "id": "transport-failure",
            "region": REGION,
            "created_by_deployment": True,
        },
        finalizer_token=token,
    )

    marker.assert_called_once_with(
        DEPLOYMENT,
        finalizer_token=token,
    )


def test_gateway_fallback_does_not_swallow_lost_finalizer_ownership():
    store = MagicMock()
    store.record_gateway_handle.side_effect = DeploymentLifecycleConflict("lost")

    with pytest.raises(DeploymentLifecycleConflict):
        sus._record_gateway_handle(
            store,
            DEPLOYMENT,
            {"id": "gateway-fenced", "name": "gateway-fenced"},
            finalizer_token="stale-token",
        )


@pytest.mark.parametrize("delete_status", ["deleting", "deleted", "delete_failed", "delete_retained"])
def test_a_finalizer_cannot_take_a_lease_on_a_row_a_teardown_owns(store, delete_status):
    """The other direction of the barrier, and it must refuse BEFORE any write.

    ``deleted`` is the important one: it carries a TTL that a later write would
    strip, resurrecting a tombstone.
    """
    store.update_delete_status(DEPLOYMENT, delete_status, "teardown owns this row")

    with pytest.raises(DeploymentLifecycleConflict):
        store.acquire_finalizer_lease(DEPLOYMENT)

    assert FINALIZER_LEASE_ATTR not in raw(store)


def test_a_lease_cannot_conjure_a_row_that_does_not_exist(store):
    """UpdateItem creates an absent item. A row holding only a lease is
    unparseable by every status and delete path."""
    with pytest.raises(DeploymentLifecycleConflict):
        store.acquire_finalizer_lease("dep-does-not-exist")
    assert raw(store, "dep-does-not-exist") == {}


# ---------------------------------------------------------------------------
# Both entrypoints. The claim is non-destructive; so must the report be.
# ---------------------------------------------------------------------------


def test_the_public_delete_route_reports_409_and_deletes_nothing(store, monkeypatch):
    """409, not 503: the request is well-formed and the state legitimate, it is
    simply not this caller's turn."""
    store.acquire_finalizer_lease(DEPLOYMENT)

    monkeypatch.setattr(
        dh,
        "_lookup_deployment_record",
        lambda rid: {"deployment_id": DEPLOYMENT, "runtime_id": RUNTIME, "user_id": OWNER},
    )
    monkeypatch.setattr(dh, "_get_user_id", lambda req: OWNER)
    cleanup = MagicMock(return_value=DeleteResponse(success=True, message="should not run"))
    monkeypatch.setattr(dh, "_run_delete_cleanup", cleanup)
    monkeypatch.setattr(dh.boto3, "client", lambda *a, **k: MagicMock())

    resp = TestClient(dh.deployment_app).delete(f"/api/runtime/{RUNTIME}")

    assert resp.status_code == 409, resp.text
    cleanup.assert_not_called()
    assert "delete_status" not in raw(store)


def test_stack_cleanup_defers_as_retryable_and_performs_no_destructive_work(store, monkeypatch):
    """Retryable, because the finalizer releases the lease within seconds and the
    stack destroy must not proceed against a snapshot taken now.

    ``retryable`` is the load-bearing field: ``scripts/cleanup.sh`` stops before
    CDK removes the Lambda and the deployment table, so reporting a bare failure
    here would destroy the evidence needed to retry.
    """
    # This entrypoint accepts only canonical deployment UUIDs, so it needs its
    # own row rather than the fixture's readable id.
    uuid_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    store.create(
        DeploymentState(
            deployment_id=uuid_id,
            workflow_id="wf-barrier-uuid",
            user_id=OWNER,
            status=DeploymentStatusEnum.IN_PROGRESS,
            started_at=datetime.now(timezone.utc),
        )
    )
    store.acquire_finalizer_lease(uuid_id)

    stack_owner = "unit-tests-local-us-east-1"
    monkeypatch.setattr(dh, "stack_id", lambda region: stack_owner)
    cleanup = MagicMock(return_value=DeleteResponse(success=True, message="should not run"))
    monkeypatch.setattr(dh, "_run_delete_cleanup", cleanup)

    result = dh._handle_stack_cleanup_delete(
        {
            "_stack_cleanup_delete": True,
            "deployment_id": uuid_id,
            "expected_stack_owner": stack_owner,
        }
    )

    assert result["success"] is False
    assert result["retryable"] is True
    cleanup.assert_not_called()
    assert "delete_status" not in raw(store, uuid_id)


# ---------------------------------------------------------------------------
# The registry seam. ``register`` is an upsert, so a compensation-delete has two
# ways to be wrong: deleting a record another deployment refreshed, and deleting
# a record the finalizer that now owns this row is about to point at.
# ---------------------------------------------------------------------------


class _RegistryStub:
    """A registry whose ``register`` creates a brand-new record (no ``updated``)."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def register(self, **_kwargs) -> dict:
        return {"record_id": "rec-new", "status": "DRAFT"}

    def delete(self, record_id: str) -> None:
        self.deleted.append(record_id)


def _register(store, registry, token, monkeypatch) -> None:
    monkeypatch.setattr("app.services.aws_agent_registry.get_registry", lambda: registry)
    with pytest.raises(DeploymentLifecycleConflict):
        sus._auto_register_in_aws_registry(
            store=store,
            deployment_id=DEPLOYMENT,
            runtime_arn="arn:rt",
            runtime_endpoint="https://e",
            friendly_runtime_name="agent-barrier",
            is_a2a=False,
            finalizer_token=token,
        )


def test_ownership_snapshot_reports_the_row_as_it_is(store):
    assert store.ownership_snapshot("dep-does-not-exist") is None
    token = store.acquire_finalizer_lease(DEPLOYMENT)
    snap = store.ownership_snapshot(DEPLOYMENT)
    assert snap == {
        "delete_status": None,
        "finalizer_token": token,
        "finalizer_lease_live": True,
        "aws_registry_record_id": None,
    }
    store.release_finalizer_lease(DEPLOYMENT, token)
    assert dh._claim_delete_status(DEPLOYMENT) is True
    snap = store.ownership_snapshot(DEPLOYMENT)
    assert snap["delete_status"] == "deleting"
    assert snap["finalizer_lease_live"] is False


def test_a_new_record_is_compensated_only_when_a_teardown_owns_the_row(store, monkeypatch):
    """The stale finalizer's pointer write lost to a teardown claim. Nothing will
    ever name the record it just created, so deleting it is the only way it does
    not leak."""
    stale = store.acquire_finalizer_lease(DEPLOYMENT, seconds=-1)
    assert dh._claim_delete_status(DEPLOYMENT) is True
    registry = _RegistryStub()

    _register(store, registry, stale, monkeypatch)

    assert registry.deleted == ["rec-new"]
    assert "aws_registry_record_id" not in raw(store)


def test_a_new_record_is_left_for_the_finalizer_that_took_the_row(store, monkeypatch):
    """The stale finalizer's pointer write lost to a NEWER finalizer. That one
    upserts the same name and gets this very record back, so deleting it here
    would dangle the pointer it is about to write."""
    stale = store.acquire_finalizer_lease(DEPLOYMENT, seconds=-1)
    current = store.acquire_finalizer_lease(DEPLOYMENT)
    registry = _RegistryStub()

    _register(store, registry, stale, monkeypatch)

    assert registry.deleted == []
    assert raw(store)[FINALIZER_TOKEN_ATTR] == current
    # ...and the current owner's own pointer write still lands.
    store.set_registry_record(DEPLOYMENT, "rec-new", "DRAFT", finalizer_token=current)
    assert raw(store)["aws_registry_record_id"] == "rec-new"


# ---------------------------------------------------------------------------
# Release. Mutant m13 ("the handler never releases the lease") survived the first
# scoring run: every test above proved the barrier stands, none proved it comes
# down. A lease that is never released is a five-minute outage per deploy for
# every teardown, so the handler's ``finally`` is load-bearing and gets pinned.
# ---------------------------------------------------------------------------


def _finalize(store, monkeypatch, event: dict) -> dict:
    monkeypatch.setattr(sus, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(sus, "get_versions_store", lambda: MagicMock())
    monkeypatch.setattr(sus, "get_slots_store", lambda: MagicMock())
    monkeypatch.setattr(sus, "_auto_register_in_aws_registry", lambda **_kw: None)
    # No AWS teardown from a unit test; the barrier is about the row, not the cleanup.
    monkeypatch.setattr(sus, "_auto_cleanup_on_failure", lambda *_a, **_kw: None)
    return sus.handler({"deployment_id": DEPLOYMENT, **event}, None)


@pytest.mark.parametrize(
    "event",
    [
        pytest.param({"runtime_id": RUNTIME, "runtime_endpoint": "https://rt.example"}, id="success-path"),
        pytest.param({"error": "step blew up"}, id="failure-path"),
    ],
)
def test_the_handler_releases_its_lease_on_the_way_out(store, monkeypatch, event):
    result = _finalize(store, monkeypatch, event)

    assert result["status"] != "cancelled", result
    row = raw(store)
    assert row["status"] == result["status"], "the finalizer wrote its terminal status"
    assert FINALIZER_LEASE_ATTR not in row and FINALIZER_TOKEN_ATTR not in row, (
        "the lease outlived the handler; every teardown now waits out the full lease"
    )
    # ...and a teardown can therefore start at once.
    assert dh._claim_delete_status(DEPLOYMENT) is True


def test_the_handler_holds_the_lease_for_the_whole_of_its_work(store, monkeypatch):
    """The complement: released on exit, but not a moment before. A claim attempted
    from inside the finalizer's own post-work must still be refused."""
    seen: dict = {}

    def register_probe(**kw):
        # Runs where the real registry write does: after the terminal status commit.
        seen["lease_live_during_post_work"] = FINALIZER_LEASE_ATTR in raw(store)
        with pytest.raises(dh.ActiveFinalizerConflict):
            dh._claim_delete_status(DEPLOYMENT)

    monkeypatch.setattr(sus, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(sus, "get_versions_store", lambda: MagicMock())
    monkeypatch.setattr(sus, "get_slots_store", lambda: MagicMock())
    monkeypatch.setattr(sus, "_auto_register_in_aws_registry", register_probe)
    result = sus.handler(
        {"deployment_id": DEPLOYMENT, "runtime_id": RUNTIME, "runtime_endpoint": "https://rt.example"},
        None,
    )

    assert result["status"] == DeploymentStatusEnum.SUCCEEDED.value, result
    assert seen == {"lease_live_during_post_work": True}
    assert FINALIZER_LEASE_ATTR not in raw(store)
