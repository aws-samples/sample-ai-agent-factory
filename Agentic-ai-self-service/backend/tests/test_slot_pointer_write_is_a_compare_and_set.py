"""F-83 — a slot pointer move is a compare-and-set, not a full-row PUT.

THE DEFECT. Every writer of ``RuntimeSlots`` -- the deploy finalizer, ``POST .../promote`` and
``POST .../rollback`` -- did the same three things: an eventually consistent ``get``, an in-place
mutation of the dataclass, and ``slots_store.upsert``, which is an unconditional ``put_item`` of the
whole row. That is wrong in three separate ways, and the third is the one that destroys data:

1. It RESURRECTS. A teardown that deleted the slot between the read and the write gets the row back,
   pointing at a version row that no longer exists -- a friendly name locked by a row nothing can
   release, because the release needs the version row it would delete.
2. It CLOBBERS. Two promotes, or a promote and a finalizer, read the same row and the second write
   silently discards the first. The lost value decides where every invoke and every trigger resolves.
3. It DROPS ATTRIBUTES IT NEVER READ. ``RuntimeSlots.to_item`` omits a None, so a writer holding a
   read from before ``trigger_fence`` existed re-puts the row WITHOUT the fence. That erasure
   re-satisfies the teardown release's fence condition (F-81f), so the release then deletes the slot
   and the version out from under a live trigger -- leaving a schedule firing at a destroyed runtime
   and no handle to delete it by. Stated as an exact interleaving by a peer session:

       staging finalizer reads a slot whose staging pointer already equals its version and whose
       fence is absent; teardown reads the same slot and enumerates no triggers; a trigger create
       writes the row + the fence; the stale idempotent finalizer re-puts the same pointers and
       omits the fence; the teardown's conditions now match, and it deletes slot + version.

``test_the_exact_measured_interleaving_is_refused`` is that interleaving, run step by step against
real tables, and it is the reason this file exists.

WHY REAL TABLES. "Nothing was written" is a claim about rows. A mocked ``transact_write_items``
cannot make it, and it cannot see the double-serialization bug that would make every condition fail
for a reason that has nothing to do with races -- which is why the control tests come first and
assert a SUCCESSFUL write.

Also pinned here: the four looseness defects three peer audit sessions found in the primitive by
probing it, each with the input that exploited it --
``test_a_create_may_not_write_a_different_rows_key`` (8b.1), ``test_the_fence_must_be_the_pointer_
being_written`` (8b.2), ``test_a_non_succeeded_version_is_refused`` and
``test_a_version_with_no_deployment_id_is_refused`` (2b.a/b).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timezone

import boto3
import pytest

sys.path.insert(0, "src")

pytest.importorskip("moto")
from app.services import agent_versions_store as avs  # noqa: E402
from app.services.agent_versions_store import (  # noqa: E402
    AgentVersion,
    AgentVersionsStore,
    NameClaimConflict,
    RuntimeSlots,
    RuntimeSlotsStore,
    SlotWriteConflict,
    VersionFence,
    release_name_claim_atomically,
    set_slot_pointers_atomically,
)
from moto import mock_aws  # noqa: E402

REGION = "us-east-1"
NAME = "orders_bot"
OWNER = "sub-alice"
STRANGER = "sub-mallory"
DEPLOYMENT = "d1"
V1 = "v1"
V2 = "v2"
RUNTIME_ID = "orders_bot_1a2b3c4d-AbCdEfGhIj"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{RUNTIME_ID}"
NOW = datetime.now(timezone.utc).isoformat()
LATER = datetime.now(timezone.utc).isoformat() + "-later"


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[AgentVersionsStore, RuntimeSlotsStore]]:
    """Both tables over moto, wired into the singletons the primitive resolves through."""
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName="AgentVersions",
            KeySchema=[
                {"AttributeName": "runtime_name", "KeyType": "HASH"},
                {"AttributeName": "version_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "runtime_name", "AttributeType": "S"},
                {"AttributeName": "version_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="RuntimeSlots",
            KeySchema=[{"AttributeName": "runtime_name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "runtime_name", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        vstore = AgentVersionsStore(table_name="AgentVersions", region=REGION)
        sstore = RuntimeSlotsStore(table_name="RuntimeSlots", region=REGION)
        monkeypatch.setattr(avs, "_versions_store", vstore, raising=False)
        monkeypatch.setattr(avs, "_slots_store", sstore, raising=False)
        yield vstore, sstore


def a_version(version_id: str = V1, **over) -> AgentVersion:
    """The shape a succeeded deploy leaves behind."""
    fields = {
        "runtime_name": NAME,
        "version_id": version_id,
        "owner_sub": OWNER,
        "created_at": NOW,
        "deployment_id": DEPLOYMENT,
        "agentcore_runtime_name": f"{NAME}_{version_id}",
        "runtime_id": RUNTIME_ID,
        "runtime_arn": RUNTIME_ARN,
        "status": "succeeded",
    }
    fields.update(over)
    return AgentVersion(**fields)


def a_fence(version: AgentVersion, slot: str = "production") -> VersionFence:
    """The fence a caller builds from a version row it just read."""
    return VersionFence(
        version_id=version.version_id,
        owner_sub=version.owner_sub,
        status=version.status,
        created_at=version.created_at or None,
        slot=slot,
        deployment_id=version.deployment_id,
        runtime_id=version.runtime_id,
        runtime_arn=version.runtime_arn,
    )


def raw_slot(name: str = NAME) -> dict | None:
    """The slot row as DynamoDB holds it -- the only honest answer to 'what was written'."""
    resp = boto3.client("dynamodb", region_name=REGION).get_item(
        TableName="RuntimeSlots",
        Key={"runtime_name": {"S": name}},
        ConsistentRead=True,
    )
    return resp.get("Item")


# ---------------------------------------------------------------------------
# Controls. A suite of refusals is compatible with refusing everything.
# ---------------------------------------------------------------------------


def test_a_first_deploy_creates_the_slot_row(stores):
    vstore, sstore = stores
    vstore.put(a_version())

    set_slot_pointers_atomically(
        NAME,
        expected=None,
        new=RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=V1, last_promoted_at=NOW),
        require_version=a_fence(a_version()),
    )

    slot = sstore.get(NAME, consistent=True)
    assert slot is not None
    assert slot.production_version_id == V1
    assert slot.owner_sub == OWNER
    assert slot.last_promoted_at == NOW


def test_a_promote_moves_the_pointer_and_keeps_the_previous(stores):
    vstore, sstore = stores
    vstore.put(a_version(V1))
    vstore.put(a_version(V2, created_at=LATER))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=V1, last_promoted_at=NOW))

    expected = sstore.get(NAME, consistent=True)
    set_slot_pointers_atomically(
        NAME,
        expected=expected,
        new=replace(
            expected,
            previous_production_version_id=V1,
            production_version_id=V2,
            last_promoted_at=LATER,
        ),
        require_version=a_fence(a_version(V2, created_at=LATER)),
    )

    slot = sstore.get(NAME, consistent=True)
    assert (slot.production_version_id, slot.previous_production_version_id) == (V2, V1)


def test_both_slots_are_writable(stores):
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER))

    expected = sstore.get(NAME, consistent=True)
    set_slot_pointers_atomically(
        NAME,
        expected=expected,
        new=replace(expected, staging_version_id=V1),
        require_version=a_fence(a_version(V1), slot="staging"),
    )
    assert sstore.get(NAME, consistent=True).staging_version_id == V1


def test_a_harness_version_with_no_runtime_arn_can_still_be_promoted(stores):
    """HARNESS mode writes ``harness_id``/``harness_arn`` and leaves runtime_id/runtime_arn unset.

    Those two are pinned when present, so they must be OPTIONAL -- requiring them would silently stop
    every harness deploy's slot from ever moving, which is a feature quietly removed rather than a
    race prevented.
    """
    vstore, sstore = stores
    vstore.put(a_version(V1, runtime_id=None, runtime_arn=None))

    set_slot_pointers_atomically(
        NAME,
        expected=None,
        new=RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=V1),
        require_version=a_fence(a_version(V1, runtime_id=None, runtime_arn=None)),
    )
    assert sstore.get(NAME, consistent=True).production_version_id == V1


def test_clearing_a_pointer_removes_the_attribute_and_needs_no_fence(stores):
    """A None pointer must REMOVE, not write an empty string: every condition elsewhere is written as
    ``attribute_not_exists``, and an empty string would satisfy none of them.

    A clear takes no fence, and that is deliberate rather than an omission: the fence exists so a
    pointer cannot end up naming a version row that no longer exists, and a REMOVE cannot create a
    dangling pointer. Requiring one would mean a caller clearing ``staging`` had to prove the liveness
    of a version it is in the act of forgetting.
    """
    _, sstore = stores
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=V1, staging_version_id=V2))

    expected = sstore.get(NAME, consistent=True)
    set_slot_pointers_atomically(NAME, expected=expected, new=replace(expected, staging_version_id=None))
    row = raw_slot()
    assert "staging_version_id" not in row
    assert row["production_version_id"]["S"] == V1


# ---------------------------------------------------------------------------
# The measured F-83 interleaving, step by step.
# ---------------------------------------------------------------------------


def test_the_update_expression_names_only_the_mutable_pointers(stores):
    """The narrow, positive version: a pointer move must not carry ``trigger_fence`` at all.

    NOT a stale-reader test -- it re-reads after the fence lands, so the fence is present in the
    object too. What it pins is the UpdateExpression's SHAPE: the write names exactly the four mutable
    pointer attributes, so an attribute the primitive has no opinion about survives whatever the
    caller's dataclass says about it. That is what makes every FUTURE fence-like attribute safe by
    default instead of safe by somebody remembering to copy it forward.
    """
    vstore, sstore = stores
    vstore.put(a_version(V1))
    # The row as the finalizer read it: no fence yet.
    stale_read = RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, staging_version_id=V1)
    sstore.upsert(stale_read)
    # A trigger create then moves the fence, using the same store the real one does.
    sstore._table.update_item(  # noqa: SLF001 - standing in for TriggerStore's transaction
        Key={"runtime_name": NAME},
        UpdateExpression="SET trigger_fence = :f",
        ExpressionAttributeValues={":f": "fence-1"},
    )

    # The finalizer writes with its stale object, which carries trigger_fence=None.
    fresh = sstore.get(NAME, consistent=True)
    set_slot_pointers_atomically(
        NAME,
        expected=fresh,
        new=replace(fresh, staging_version_id=V1, trigger_fence=None),
        require_version=a_fence(a_version(V1), slot="staging"),
    )

    assert raw_slot()["trigger_fence"]["S"] == "fence-1", (
        "the pointer write erased the trigger fence; the teardown release's fence condition is now "
        "satisfiable again and it will delete the slot out from under the live trigger"
    )


def test_the_exact_measured_interleaving_no_longer_destroys_the_trigger(stores):
    """The full five-step race a peer stated, end to end, through the REAL release.

    Steps: (1) the staging finalizer reads a fenceless slot whose staging pointer already equals its
    version -- so its write is idempotent and looks harmless; (2) a trigger create writes the fence;
    (3) the stale finalizer writes. The old code's PUT succeeded here and erased the fence, and the
    teardown release (4) then found its conditions satisfied and deleted the slot and the version (5).

    THE FINALIZER WRITE STILL SUCCEEDS, and that is the correct outcome, not a hole. A pointer move and
    a trigger create are compatible operations; ``trigger_fence`` is deliberately in neither the
    UpdateExpression nor the condition, because pinning it would fail a legitimate promote whenever a
    trigger happened to be registered concurrently. What changed is that the write can no longer
    ERASE the fence -- which is the step the destruction actually depended on. The stale teardown then
    loses on its own condition, which is where a lost race belongs.

    (I first wrote this test asserting the finalizer write was refused. It failed, and a peer audit
    session had already flagged the oracle as contradicting the design before the run finished.)

    Three assertions, because any one alone is satisfiable by an unrelated bug: the fence is INTACT
    after the pointer move, the release holding the pre-fence read is CANCELLED, and both rows are
    still there for the owner to resolve and delete the trigger through.
    """
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, staging_version_id=V1))

    # (1) both the finalizer and the teardown read the fenceless row.
    finalizer_read = sstore.get(NAME, consistent=True)
    teardown_read = sstore.get(NAME, consistent=True)
    assert finalizer_read.trigger_fence is None

    # (2) the trigger create moves the fence.
    sstore._table.update_item(  # noqa: SLF001 - standing in for TriggerStore's transaction
        Key={"runtime_name": NAME},
        UpdateExpression="SET trigger_fence = :f",
        ExpressionAttributeValues={":f": "fence-1"},
    )

    # (3) the stale, idempotent finalizer write: allowed, and it carries trigger_fence=None.
    set_slot_pointers_atomically(
        NAME,
        expected=finalizer_read,
        new=replace(finalizer_read, staging_version_id=V1),
        require_version=a_fence(a_version(V1), slot="staging"),
    )
    assert raw_slot()["trigger_fence"]["S"] == "fence-1", (
        "the pointer write erased the trigger fence; the teardown release's fence condition is now "
        "satisfiable again and it will delete the slot out from under the live trigger"
    )

    # (4) the teardown release, holding the pre-fence read.
    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=(V1, OWNER, DEPLOYMENT, "succeeded", NOW),
            slot_expected=teardown_read,
            slot_action="delete",
        )

    # (5) both rows survive, so the owner can still resolve and delete the trigger.
    assert raw_slot() is not None, "the release deleted the slot the live trigger is reachable through"
    assert vstore.get(NAME, V1, consistent=True) is not None


# ---------------------------------------------------------------------------
# Resurrection and lost updates.
# ---------------------------------------------------------------------------


def test_a_deleted_slot_is_not_resurrected(stores):
    """The fence must name V2 here, because V2 is what the write points at. Fencing V1 while writing
    V2 is rejected by the input invariants BEFORE any call to DynamoDB, so it would never reach the
    deleted-row condition this test exists for -- a vacuous pass that still shows green."""
    vstore, sstore = stores
    vstore.put(a_version(V1))
    vstore.put(a_version(V2, created_at=LATER))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=V1))
    stale = sstore.get(NAME, consistent=True)

    sstore.delete(NAME)  # the teardown wins

    with pytest.raises(SlotWriteConflict):
        set_slot_pointers_atomically(
            NAME,
            expected=stale,
            new=replace(stale, production_version_id=V2, previous_production_version_id=V1),
            require_version=a_fence(a_version(V2, created_at=LATER)),
        )
    assert raw_slot() is None, "the slot row came back; the friendly name is locked by a phantom"


def test_a_concurrent_pointer_move_is_not_clobbered(stores):
    vstore, sstore = stores
    vstore.put(a_version(V1))
    vstore.put(a_version(V2, created_at=LATER))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=V1))

    reader_a = sstore.get(NAME, consistent=True)
    reader_b = sstore.get(NAME, consistent=True)

    set_slot_pointers_atomically(
        NAME,
        expected=reader_a,
        new=replace(reader_a, production_version_id=V2, previous_production_version_id=V1, last_promoted_at=LATER),
        require_version=a_fence(a_version(V2, created_at=LATER)),
    )
    with pytest.raises(SlotWriteConflict):
        set_slot_pointers_atomically(
            NAME,
            expected=reader_b,
            new=replace(reader_b, production_version_id=V1, last_promoted_at=NOW),
            require_version=a_fence(a_version(V1)),
        )
    assert sstore.get(NAME, consistent=True).production_version_id == V2


def test_an_ab_a_promote_is_still_detected(stores):
    """The pointer values can return to what a reader saw while ``last_promoted_at`` moved: promote
    v2, roll back to v1. The pointer terms alone cannot see that, which is why the timestamp is in
    the condition too."""
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=V1, last_promoted_at=NOW))
    stale = sstore.get(NAME, consistent=True)

    sstore.upsert(replace(stale, last_promoted_at=LATER))  # someone else touched it

    with pytest.raises(SlotWriteConflict):
        set_slot_pointers_atomically(
            NAME,
            expected=stale,
            new=replace(stale, staging_version_id=V1),
            require_version=a_fence(a_version(V1), slot="staging"),
        )


def test_a_create_refuses_a_row_that_appeared_in_between(stores):
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=V2))

    with pytest.raises(SlotWriteConflict):
        set_slot_pointers_atomically(
            NAME,
            expected=None,
            new=RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=V1),
            require_version=a_fence(a_version(V1)),
        )
    assert sstore.get(NAME, consistent=True).production_version_id == V2


def test_a_foreign_owner_cannot_move_the_pointer(stores):
    """The stranger's whole read is internally consistent -- their own version row, their own claimed
    slot row, a fence over the version they are writing -- so every input invariant passes and the
    TRANSACTION is what refuses, on ``owner_sub = :owner`` against the row that really exists.

    Constructed this way on a peer's correction: my first attempt wrote V2 while fencing V1, which the
    input invariants reject before any network call, so it proved nothing about the ownership
    condition it was named after.
    """
    vstore, sstore = stores
    vstore.put(a_version(V1))
    # The public version store now atomically claims the runtime name for its first owner, so it
    # correctly refuses this cross-tenant state before the pointer layer sees it. Seed the foreign
    # row raw to model legacy/corrupt data and keep this test focused on the independent slot CAS
    # defence it is named for.
    vstore._table.put_item(  # noqa: SLF001 - deliberate impossible-state fixture
        Item=a_version(V2, owner_sub=STRANGER, created_at=LATER).to_item()
    )
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=V1))
    real = sstore.get(NAME, consistent=True)

    with pytest.raises(SlotWriteConflict):
        set_slot_pointers_atomically(
            NAME,
            expected=replace(real, owner_sub=STRANGER),
            new=replace(real, owner_sub=STRANGER, production_version_id=V2, previous_production_version_id=V1),
            require_version=a_fence(a_version(V2, owner_sub=STRANGER, created_at=LATER)),
        )
    surviving = sstore.get(NAME, consistent=True)
    assert surviving.owner_sub == OWNER
    assert surviving.production_version_id == V1


# ---------------------------------------------------------------------------
# The version fence.
# ---------------------------------------------------------------------------


def test_a_complete_absent_slot_release_beats_a_late_first_slot_write(stores):
    """The opposite ordering of the absent-slot race is safe too.

    If the release commits before the finalizer, its slot-absence condition succeeds and removes
    the version in that same transaction. The finalizer's later first-slot transaction must then
    lose on its version fence, so it cannot resurrect the namespace with a dangling pointer.
    """
    vstore, _ = stores
    version = a_version(V1)
    vstore.put(version)
    snapshot = vstore.snapshot_for_name_release(NAME)

    release_name_claim_atomically(
        NAME,
        delete_version=(V1, OWNER, DEPLOYMENT, "succeeded", NOW),
        claim_snapshot=snapshot,
        release_name=True,
        slot_action="assert_absent",
    )

    with pytest.raises(SlotWriteConflict):
        set_slot_pointers_atomically(
            NAME,
            expected=None,
            new=RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=V1),
            require_version=a_fence(version),
        )

    assert vstore.get(NAME, V1, consistent=True) is None
    assert raw_slot() is None


def test_a_vanished_version_cancels_the_pointer_move(stores):
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER))
    expected = sstore.get(NAME, consistent=True)
    vstore.delete(NAME, V1)

    with pytest.raises(SlotWriteConflict):
        set_slot_pointers_atomically(
            NAME,
            expected=expected,
            new=replace(expected, production_version_id=V1),
            require_version=a_fence(a_version(V1)),
        )
    assert "production_version_id" not in (raw_slot() or {})


def test_a_reput_version_row_is_a_different_claim(stores):
    """Same version id, same succeeded status, new ``created_at``: a retry re-created the row. The
    pointer must not end up naming a version whose content was replaced underneath."""
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER))
    expected = sstore.get(NAME, consistent=True)
    read_version = a_version(V1)

    vstore.put(a_version(V1, created_at=LATER))

    with pytest.raises(SlotWriteConflict):
        set_slot_pointers_atomically(
            NAME,
            expected=expected,
            new=replace(expected, production_version_id=V1),
            require_version=a_fence(read_version),
        )


def test_a_version_repointed_at_another_runtime_cancels_the_move(stores):
    """``runtime_arn`` is pinned when present: a re-put row can carry the same status, the same
    ``deployment_id`` and the same ``created_at`` and still name a different runtime."""
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER))
    expected = sstore.get(NAME, consistent=True)
    read_version = a_version(V1)

    vstore.put(a_version(V1, runtime_arn=RUNTIME_ARN.replace("orders_bot", "other_bot")))

    with pytest.raises(SlotWriteConflict):
        set_slot_pointers_atomically(
            NAME,
            expected=expected,
            new=replace(expected, production_version_id=V1),
            require_version=a_fence(read_version),
        )


# ---------------------------------------------------------------------------
# The four looseness defects peer audits found by probing the primitive.
# ---------------------------------------------------------------------------


def test_a_create_may_not_write_a_different_rows_key(stores):
    """Peer 8b, finding 1. The probe passed ``runtime_name="expected-name"`` with
    ``new.runtime_name="different-name"`` and a version fence under expected-name, and the create
    SUCCEEDED -- authorizing against one runtime and writing another runtime's pointer row. The
    check existed but sat inside the ``expected is not None`` branch, so only the update path had it.
    """
    vstore, _ = stores
    vstore.put(a_version(V1))

    with pytest.raises(ValueError, match="the row being written"):
        set_slot_pointers_atomically(
            NAME,
            expected=None,
            new=RuntimeSlots(runtime_name="a_different_runtime", owner_sub=OWNER, production_version_id=V1),
            require_version=a_fence(a_version(V1)),
        )
    assert raw_slot(NAME) is None
    assert raw_slot("a_different_runtime") is None


def test_the_fence_must_be_the_pointer_being_written(stores):
    """Peer 8b, finding 2. The probe fenced a live ``v1`` while writing
    ``production_version_id="v-does-not-exist"`` and the write SUCCEEDED, so the ConditionCheck was
    proving the liveness of a row unrelated to the pointer it authorized."""
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER))
    expected = sstore.get(NAME, consistent=True)

    with pytest.raises(ValueError, match="must be the pointer it authorizes"):
        set_slot_pointers_atomically(
            NAME,
            expected=expected,
            new=replace(expected, production_version_id="v-does-not-exist"),
            require_version=a_fence(a_version(V1)),
        )
    assert "production_version_id" not in raw_slot()


def test_the_fence_slot_must_be_the_slot_that_changed(stores):
    """The other half of 8b.2, and the one the first cut of the primitive still allowed.

    Production ALREADY equals v1, so a fence naming production/v1 satisfies "the fenced pointer equals
    the fenced version" -- while the write's only real effect is to point staging somewhere new. The
    fence would be proving the liveness of a pointer the write does not touch. Each slot is resolved by
    a different caller, so that proof authorizes nothing about the change being made.
    """
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=V1))
    expected = sstore.get(NAME, consistent=True)

    with pytest.raises(ValueError, match="also points staging at a new version"):
        set_slot_pointers_atomically(
            NAME,
            expected=expected,
            new=replace(expected, staging_version_id=V2),
            require_version=a_fence(a_version(V1), slot="production"),
        )
    assert "staging_version_id" not in raw_slot()


def test_an_unfenced_acquisition_is_refused(stores):
    """``require_version`` defaults to None so a clear needs no fence, which means a caller can omit it
    entirely. A write that POINTS a slot at a version and omits it is the original unguarded write
    wearing the new primitive's name, so it has to be rejected rather than merely discouraged."""
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER))
    expected = sstore.get(NAME, consistent=True)

    with pytest.raises(ValueError, match="carries no fence"):
        set_slot_pointers_atomically(NAME, expected=expected, new=replace(expected, production_version_id=V1))
    assert "production_version_id" not in raw_slot()


def test_an_unfenced_create_is_refused(stores):
    """Same rule on the create path, where EVERY pointer is an acquisition by definition."""
    vstore, _ = stores
    vstore.put(a_version(V1))

    with pytest.raises(ValueError, match="carries no fence"):
        set_slot_pointers_atomically(
            NAME,
            expected=None,
            new=RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=V1),
        )
    assert raw_slot() is None


def test_a_harness_fence_pins_the_absence_of_a_runtime_identity(stores):
    """The harness exemption must be the weaker CLAIM, not the weaker CONDITION.

    A both-absent fence that adds no runtime_id/runtime_arn term to the ConditionCheck is satisfied by
    a row that GAINED an identity after the read, as long as every other fenced term is unchanged --
    and "gains runtime_id and runtime_arn, keeps status/deployment_id/created_at" is precisely what a
    deploy finalizer does. The harness caller would then move a pointer on the strength of a read that
    no longer describes the row. Raised by a peer audit; this is the race, run against real tables.
    """
    vstore, sstore = stores
    vstore.put(a_version(V1, runtime_id=None, runtime_arn=None))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER))
    expected = sstore.get(NAME, consistent=True)
    harness_fence = a_fence(a_version(V1, runtime_id=None, runtime_arn=None))

    # The row becomes runtime-backed. created_at, status, deployment_id and owner_sub are all
    # preserved, so every OTHER fenced term still matches.
    vstore.put(a_version(V1))

    with pytest.raises(SlotWriteConflict):
        set_slot_pointers_atomically(
            NAME,
            expected=expected,
            new=replace(expected, production_version_id=V1),
            require_version=harness_fence,
        )
    assert "production_version_id" not in raw_slot()


@pytest.mark.parametrize(
    ("runtime_id", "runtime_arn"),
    [(RUNTIME_ID, None), (None, RUNTIME_ARN)],
    ids=["id-without-arn", "arn-without-id"],
)
def test_exactly_one_of_runtime_id_and_arn_is_refused(stores, runtime_id, runtime_arn):
    """A peer probed the harness exemption and found both half-identities accepted and WRITTEN.

    Neither half is a harness deploy (that case is both absent, with ``harness_id``/``harness_arn``
    carrying the identity) and neither is resolvable: ``runtime_target_context`` requires a non-empty
    ``runtime_id`` AND a well-formed ``runtime_arn`` whose tail matches it. So the exemption that exists
    for harness rows was also admitting RUNTIME rows that had merely lost one attribute -- a pointer
    move nothing downstream can act on. Both-absent stays legal; that is
    ``test_a_harness_version_with_no_runtime_arn_can_still_be_promoted``.
    """
    vstore, sstore = stores
    half = a_version(V1, runtime_id=runtime_id, runtime_arn=runtime_arn)
    vstore.put(half)
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER))
    expected = sstore.get(NAME, consistent=True)

    with pytest.raises(ValueError, match="both runtime_id and runtime_arn or neither"):
        set_slot_pointers_atomically(
            NAME,
            expected=expected,
            new=replace(expected, production_version_id=V1),
            require_version=a_fence(half),
        )
    assert "production_version_id" not in raw_slot()


@pytest.mark.parametrize("bad_slot", ["previous_production", "owner_sub", "trigger_fence", "", "PRODUCTION"])
def test_an_unmodelled_fence_slot_is_an_error(stores, bad_slot):
    """A slot name outside the modelled two is refused by the WHITELIST, not incidentally.

    The match string is ``must name one of`` and not the looser ``version fence`` it started as.
    That mattered: under a ``getattr(new, f"{slot}_version_id", None)`` dispatch every name here also
    raises, just from the *pointer mismatch* check one line later, whose message also contains
    "version fence". A mutation run that reinstated the computed read SURVIVED this test until the
    regex was narrowed -- the assertion was true of both the fix and the defect.
    """
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER))
    expected = sstore.get(NAME, consistent=True)

    with pytest.raises(ValueError, match="must name one of"):
        set_slot_pointers_atomically(
            NAME,
            expected=expected,
            new=replace(expected, production_version_id=V1),
            require_version=a_fence(a_version(V1), slot=bad_slot),
        )


def test_a_fence_on_previous_production_cannot_be_satisfied_by_its_value(stores):
    """The exploit a computed attribute read actually enables, rather than the tidy version of it.

    ``previous_production_version_id`` is a REAL attribute. Point it at v1, fence
    ``slot="previous_production"`` with ``version_id=v1``, and a ``getattr`` dispatch reads it, finds
    v1, and the "the fence must be the pointer it authorizes" check PASSES -- so the write proceeds
    on a fence naming a slot that is never a promotion target and that nothing resolves through. The
    whitelist has to refuse the name before any attribute is read.
    """
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, previous_production_version_id=V1))
    expected = sstore.get(NAME, consistent=True)

    with pytest.raises(ValueError, match="must name one of"):
        set_slot_pointers_atomically(
            NAME,
            expected=expected,
            new=replace(expected, staging_version_id=V1),
            require_version=a_fence(a_version(V1), slot="previous_production"),
        )
    assert "staging_version_id" not in raw_slot()


@pytest.mark.parametrize("status", ["pending", "failed", "superseded", "succeeded "])
def test_a_non_succeeded_version_is_refused(stores, status):
    """Peer 2b, finding a. The probe fenced ``status="pending"`` and the pointer moved.
    ``runtime_target_context`` then refuses that version, so the API reported a promotion it could
    not invoke -- a successful-looking write with no usable outcome."""
    vstore, sstore = stores
    vstore.put(a_version(V1, status=status))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER))
    expected = sstore.get(NAME, consistent=True)

    with pytest.raises(ValueError, match="only point at a succeeded version"):
        set_slot_pointers_atomically(
            NAME,
            expected=expected,
            new=replace(expected, production_version_id=V1),
            require_version=a_fence(a_version(V1, status=status)),
        )
    assert "production_version_id" not in raw_slot()


@pytest.mark.parametrize("deployment_id", ["", None])
def test_a_version_with_no_deployment_id_is_refused(stores, deployment_id):
    """Peer 2b, finding b. ``runtime_target_context`` requires the immutable ``deployment_id`` to
    lead back to the deployment, and refuses a version without one. Promoting such a row moves the
    pointer and then answers 404 on every invoke, so refusing the write loses nothing and says so
    where the caller can act on it."""
    vstore, sstore = stores
    vstore.put(a_version(V1, deployment_id=deployment_id or ""))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER))
    expected = sstore.get(NAME, consistent=True)

    with pytest.raises(ValueError, match="deployment_id"):
        set_slot_pointers_atomically(
            NAME,
            expected=expected,
            new=replace(expected, production_version_id=V1),
            require_version=VersionFence(
                version_id=V1,
                owner_sub=OWNER,
                status="succeeded",
                created_at=NOW,
                slot="production",
                deployment_id=deployment_id,
            ),
        )
    assert "production_version_id" not in raw_slot()


def test_an_ownerless_slot_row_is_never_conditioned_on(stores):
    """An empty ``owner_sub`` would be compared against an attribute that may be ABSENT, which is the
    ownerless legacy shape every other guard in this module refuses to touch."""
    vstore, sstore = stores
    vstore.put(a_version(V1))
    with pytest.raises(ValueError, match="owner_sub"):
        set_slot_pointers_atomically(
            NAME,
            expected=RuntimeSlots(runtime_name=NAME, owner_sub=""),
            new=RuntimeSlots(runtime_name=NAME, owner_sub="", production_version_id=V1),
            require_version=a_fence(a_version(V1)),
        )


def test_a_pointer_write_may_not_change_the_owner(stores):
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER))
    expected = sstore.get(NAME, consistent=True)

    with pytest.raises(ValueError, match="may not change owner_sub"):
        set_slot_pointers_atomically(
            NAME,
            expected=expected,
            new=replace(expected, owner_sub=STRANGER, production_version_id=V1),
            require_version=a_fence(a_version(V1)),
        )


def test_the_fence_and_the_slot_must_agree_on_the_owner(stores):
    vstore, sstore = stores
    vstore.put(a_version(V1))
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER))
    expected = sstore.get(NAME, consistent=True)

    with pytest.raises(ValueError, match="same owner_sub"):
        set_slot_pointers_atomically(
            NAME,
            expected=expected,
            new=replace(expected, production_version_id=V1),
            require_version=a_fence(a_version(V1, owner_sub=STRANGER)),
        )


# ---------------------------------------------------------------------------
# No caller may reach the unconditional writer.
# ---------------------------------------------------------------------------


def test_no_production_code_path_calls_upsert():
    """``upsert`` survives for first-row creation in tests and seeding only. A source-text assertion,
    because the defect is the CALL SITE: any writer of an existing row that uses it re-puts the whole
    row and drops whatever it did not read, which is defect 3 above. Scoped to the three writers this
    change migrated, so it names the file that regressed rather than failing on an unrelated one."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "app"
    offenders = []
    for rel in ("routers/versions.py", "step_handlers/status_update_step.py", "deployment_handler.py"):
        text = (root / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if ".upsert(" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "these writers re-put the whole slot row; use set_slot_pointers_atomically so the write is a "
        "compare-and-set that cannot erase an attribute it never read:\n" + "\n".join(offenders)
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
