"""F-81e — the name-claim release is one transaction, or it is nothing.

The release deletes a row in AgentVersions and settles a row in RuntimeSlots. Done as two
sequential writes, a failure between them leaves a slot pointing at a version row that no longer
exists, and ``resolve_owned_runtime_target`` requires slot + version + deployment to agree before
it will build a client -- so a cleanup step turns a live agent into a 404 for its own tenant.

Every test here drives the real ``release_name_claim_atomically`` against moto-backed tables and
asserts on the ROWS in both tables afterwards, including in the failure cases: "neither row moved"
is the actual claim of a transaction and it cannot be checked by looking at a return value.

The value contract is also pinned here, because a peer session reproduced it failing: the
transaction goes through ``table.meta.client``, which is the DynamoDB RESOURCE's client and
therefore already applies the document serializer. Typed ``{"S": ...}`` values get wrapped a
second time and every condition fails with a TypeError cancellation reason -- a bug that looks
exactly like a lost race, and would have read as "the CAS is working". A mocked
``transact_write_items`` cannot see it, which is why these tests use moto.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import datetime, timezone

import boto3
import pytest

sys.path.insert(0, "src")

moto = pytest.importorskip("moto")
from app.services import agent_versions_store as avs  # noqa: E402
from app.services.agent_versions_store import (  # noqa: E402
    AgentVersion,
    AgentVersionsStore,
    NameClaimConflict,
    RuntimeSlots,
    RuntimeSlotsStore,
    release_name_claim_atomically,
)
from moto import mock_aws  # noqa: E402

NAME = "demo_bot"
OWNER = "sub-alice"
NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[AgentVersionsStore, RuntimeSlotsStore]]:
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
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
        vstore = AgentVersionsStore(table_name="AgentVersions", region="us-east-1")
        sstore = RuntimeSlotsStore(table_name="RuntimeSlots", region="us-east-1")
        monkeypatch.setattr(avs, "_versions_store", vstore, raising=False)
        monkeypatch.setattr(avs, "_slots_store", sstore, raising=False)
        yield vstore, sstore


def seed(vstore, sstore, *, status: str = "succeeded", **slot_kwargs) -> RuntimeSlots:
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v1",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d1",
            agentcore_runtime_name=f"{NAME}_v1",
            status=status,
        )
    )
    slot = RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id="v1", **slot_kwargs)
    sstore.upsert(slot)
    return slot


def test_both_rows_go_in_one_call(stores):
    """The happy path, and the proof that the value contract is right.

    A suite of race tests all passing is compatible with every condition failing for a reason
    that has nothing to do with races -- which is exactly what typed AttributeValues through the
    resource client produce. This is the control that says the transaction can succeed at all.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore)

    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", NOW),
        slot_action="delete",
        slot_expected=slot,
    )

    assert vstore.get(NAME, "v1") is None
    assert sstore.get(NAME, consistent=True) is None


def test_clearing_pointers_removes_only_those_fields(stores):
    vstore, sstore = stores
    slot = seed(vstore, sstore, staging_version_id="v2")

    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", NOW),
        slot_action="clear",
        slot_expected=slot,
        slot_clear_fields=("production_version_id",),
    )

    after = sstore.get(NAME, consistent=True)
    assert after is not None
    assert after.production_version_id is None
    assert after.staging_version_id == "v2", "an untouched slot field must survive the REMOVE"
    assert after.owner_sub == OWNER
    assert vstore.get(NAME, "v1") is None


def test_an_observed_absent_slot_is_pinned_during_complete_release(stores):
    """No slot row is still a slot observation, not permission to omit the table.

    A complete no-slot release must prove the row stayed absent in the same transaction that
    removes the target and its name sentinel. This control proves the absent-row condition can
    succeed against the real moto-backed DynamoDB API rather than making every release conflict.
    """
    vstore, sstore = stores
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v1",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d1",
            agentcore_runtime_name=f"{NAME}_v1",
            status="succeeded",
        )
    )
    snapshot = vstore.snapshot_for_name_release(NAME)
    assert sstore.get(NAME, consistent=True) is None

    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", NOW),
        claim_snapshot=snapshot,
        release_name=True,
        slot_action="assert_absent",
    )

    assert vstore.get(NAME, "v1", consistent=True) is None
    assert sstore.get(NAME, consistent=True) is None
    assert (
        vstore._table.get_item(  # noqa: SLF001 - a complete release removes the sentinel
            Key={"runtime_name": NAME, "version_id": avs.NAME_CLAIM_VERSION_ID},
            ConsistentRead=True,
        ).get("Item")
        is None
    )


def test_a_slot_created_after_an_absent_read_cancels_the_whole_release(stores):
    """The finalizer may publish its slot after teardown's read but before teardown's write.

    The version generation cannot detect that independent table write. The explicit
    ``assert_absent`` condition is what keeps the target and name sentinel intact instead of
    deleting the row underneath a slot that has just become the invocation authority.
    """
    vstore, sstore = stores
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v1",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d1",
            agentcore_runtime_name=f"{NAME}_v1",
            status="succeeded",
        )
    )
    snapshot = vstore.snapshot_for_name_release(NAME)
    assert sstore.get(NAME, consistent=True) is None

    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id="v1"))

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v1", OWNER, "d1", "succeeded", NOW),
            claim_snapshot=snapshot,
            release_name=True,
            slot_action="assert_absent",
        )

    assert vstore.get(NAME, "v1", consistent=True) is not None
    assert sstore.get(NAME, consistent=True) is not None
    assert _claim_generation(vstore) == snapshot.generation


def test_an_unchanged_slot_is_pinned_while_only_the_target_is_deleted(stores):
    """A slot not pointing at the target needs a check even though it needs no mutation.

    The positive control proves ``check`` can commit the target delete and generation advance
    while preserving every attribute of the observed slot.
    """
    vstore, sstore = stores
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v1",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d1",
            agentcore_runtime_name=f"{NAME}_v1",
            status="failed",
        )
    )
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v2",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d2",
            agentcore_runtime_name=f"{NAME}_v2",
            status="succeeded",
        )
    )
    sstore.upsert(
        RuntimeSlots(
            runtime_name=NAME,
            owner_sub=OWNER,
            production_version_id="v2",
            last_promoted_at=NOW,
            trigger_fence="trigger-generation-7",
        )
    )
    snapshot = vstore.snapshot_for_name_release(NAME)
    slot = sstore.get(NAME, consistent=True)
    assert slot is not None

    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "failed", NOW),
        claim_snapshot=snapshot,
        slot_action="check",
        slot_expected=slot,
    )

    assert vstore.get(NAME, "v1", consistent=True) is None
    assert vstore.get(NAME, "v2", consistent=True) is not None
    assert sstore.get(NAME, consistent=True) == slot
    assert _claim_generation(vstore) == snapshot.generation + 1


def test_a_slot_repointed_to_the_target_after_read_cancels_target_deletion(stores):
    """The race that a no-op slot branch previously left completely unfenced.

    Teardown reads a slot pointing at v2 and plans to delete only failed v1. A concurrent promote
    then makes v1 authoritative. The later release must lose, otherwise it leaves the surviving
    slot pointing at a version row it just deleted.
    """
    vstore, sstore = stores
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v1",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d1",
            agentcore_runtime_name=f"{NAME}_v1",
            status="failed",
        )
    )
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v2",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d2",
            agentcore_runtime_name=f"{NAME}_v2",
            status="succeeded",
        )
    )
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id="v2"))
    snapshot = vstore.snapshot_for_name_release(NAME)
    stale_slot = sstore.get(NAME, consistent=True)
    assert stale_slot is not None

    sstore.upsert(
        RuntimeSlots(
            runtime_name=NAME,
            owner_sub=OWNER,
            production_version_id="v1",
            previous_production_version_id="v2",
            last_promoted_at=NOW,
        )
    )

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v1", OWNER, "d1", "failed", NOW),
            claim_snapshot=snapshot,
            slot_action="check",
            slot_expected=stale_slot,
        )

    assert vstore.get(NAME, "v1", consistent=True) is not None
    assert vstore.get(NAME, "v2", consistent=True) is not None
    after = sstore.get(NAME, consistent=True)
    assert after is not None and after.production_version_id == "v1"
    assert _claim_generation(vstore) == snapshot.generation


def test_a_status_that_moved_cancels_the_whole_release(stores):
    """The ABA race the status term exists for.

    The caller decided this row was releasable BECAUSE it was a stale ``pending``. A slow deploy
    completing between that read and this write makes it a live ``succeeded`` row, and deleting it
    then erases a version that just came up. The important half of the assertion is the SLOT: with
    two sequential writes the version delete would have been refused and the slot deleted anyway.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore, status="pending")
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v1",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d1",
            agentcore_runtime_name=f"{NAME}_v1",
            status="succeeded",  # the deploy landed between the read and the write
        )
    )

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v1", OWNER, "d1", "pending", NOW),
            slot_action="delete",
            slot_expected=slot,
        )

    assert vstore.get(NAME, "v1") is not None
    assert sstore.get(NAME, consistent=True) is not None


def test_a_slot_pointer_that_moved_cancels_the_whole_release(stores):
    """A deploy or a promote that repointed the slot after the read wins the race, not us."""
    vstore, sstore = stores
    slot = seed(vstore, sstore)
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id="v2"))

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v1", OWNER, "d1", "succeeded", NOW),
            slot_action="delete",
            slot_expected=slot,
        )

    after = sstore.get(NAME, consistent=True)
    assert after is not None and after.production_version_id == "v2"
    assert vstore.get(NAME, "v1") is not None, "the version row must survive a lost slot race"


def test_a_promote_with_the_same_pointers_still_cancels(stores):
    """``last_promoted_at`` is in the condition for the pure-ABA case.

    Promote v2 then roll back to v1 and the pointer values can come back to what was read, so
    pointers alone cannot detect that the tenant touched this slot. The timestamp moves either way.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore)
    sstore.upsert(
        RuntimeSlots(
            runtime_name=NAME,
            owner_sub=OWNER,
            production_version_id="v1",
            last_promoted_at=NOW,
        )
    )

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v1", OWNER, "d1", "succeeded", NOW),
            slot_action="delete",
            slot_expected=slot,  # read before the promote: no last_promoted_at
        )

    assert sstore.get(NAME, consistent=True) is not None
    assert vstore.get(NAME, "v1") is not None


def test_a_sibling_row_completing_cancels_the_release(stores):
    """The same race one row over.

    Releasing the name required every OTHER row under it to be non-live. A sibling stale-pending
    row that completes between the read and the write now holds the name, so the release must not
    land -- and it is the sibling's status, not ours, that changed.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore)
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-sibling",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d2",
            agentcore_runtime_name=f"{NAME}_v2",
            status="succeeded",  # was read as a stale "pending"
        )
    )

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v1", OWNER, "d1", "succeeded", NOW),
            require_unchanged_rows=(("v-sibling", "pending", NOW),),
            slot_action="delete",
            slot_expected=slot,
        )

    assert sstore.get(NAME, consistent=True) is not None
    assert vstore.get(NAME, "v1") is not None
    assert vstore.get(NAME, "v-sibling") is not None


def test_a_stale_pending_row_that_was_re_put_cancels_the_release(stores):
    """``created_at`` is the OTHER half of liveness, and status cannot stand in for it.

    ``_version_claim_is_live`` calls a ``pending`` row live only while it is younger than
    DEPLOY_PENDING_CLAIM_TTL_SECONDS, so the caller released this name BECAUSE the row's timestamp
    was old. A retry that re-puts the same version_id refreshes that timestamp and the row is live
    again -- with its status still reading exactly the ``"pending"`` a status-only condition pinned.
    That race is invisible to every other condition in the transaction.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore, status="pending")
    fresh = datetime.now(timezone.utc).isoformat()
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v1",
            owner_sub=OWNER,
            created_at=fresh,  # the retry landed between the read and the write
            deployment_id="d1",
            agentcore_runtime_name=f"{NAME}_v1",
            status="pending",
        )
    )

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v1", OWNER, "d1", "pending", "2020-01-01T00:00:00+00:00"),
            slot_action="delete",
            slot_expected=slot,
        )

    assert vstore.get(NAME, "v1") is not None
    assert sstore.get(NAME, consistent=True) is not None


def test_a_sibling_whose_timestamp_moved_cancels_the_release(stores):
    """The same refresh, one row over, through the ConditionCheck."""
    vstore, sstore = stores
    slot = seed(vstore, sstore)
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-sibling",
            owner_sub=OWNER,
            created_at=datetime.now(timezone.utc).isoformat(),
            deployment_id="d2",
            agentcore_runtime_name=f"{NAME}_v2",
            status="pending",
        )
    )

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v1", OWNER, "d1", "succeeded", NOW),
            require_unchanged_rows=(("v-sibling", "pending", "2020-01-01T00:00:00+00:00"),),
            slot_action="delete",
            slot_expected=slot,
        )

    assert vstore.get(NAME, "v-sibling") is not None
    assert vstore.get(NAME, "v1") is not None
    assert sstore.get(NAME, consistent=True) is not None


def test_a_row_with_no_timestamp_is_still_releasable(stores):
    """A legacy row read with no ``created_at`` must not be unreleasable forever.

    ``to_item`` writes nothing for a None, so the condition has to be ``attribute_not_exists``.
    An equality against "" would compare against a missing attribute, never match, and turn every
    release of such a row into a permanent false conflict -- a name locked by the fix.
    """
    vstore, sstore = stores
    vstore._table.put_item(  # noqa: SLF001 - the legacy shape, written raw
        Item={
            "runtime_name": NAME,
            "version_id": "v1",
            "owner_sub": OWNER,
            "deployment_id": "d1",
            "status": "succeeded",
        }
    )
    slot = RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id="v1")
    sstore.upsert(slot)

    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", None),
        slot_action="delete",
        slot_expected=slot,
    )

    assert vstore.get(NAME, "v1") is None
    assert sstore.get(NAME, consistent=True) is None


def test_a_cancellation_with_no_reasons_is_still_a_conflict(stores, monkeypatch):
    """A transaction is all-or-nothing, so EVERY cancellation means nothing was written.

    The API does not always populate ``CancellationReasons`` (throttling, capacity, an internal
    serialization error). Letting that escape as a raw ClientError hands the caller a botocore
    message that echoes the entire request -- every row value and the tenant's sub -- into whatever
    log line catches it, and the caller's ``except NameClaimConflict`` would miss the one outcome it
    is there to report.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore)

    from botocore.exceptions import ClientError

    def _boom(**_kwargs):
        raise ClientError(
            {
                "Error": {
                    "Code": "TransactionCanceledException",
                    "Message": "Transaction cancelled [sub-alice, d1, succeeded]",
                }
            },
            "TransactWriteItems",
        )

    monkeypatch.setattr(vstore._table.meta.client, "transact_write_items", _boom)  # noqa: SLF001

    with pytest.raises(NameClaimConflict) as excinfo:
        release_name_claim_atomically(
            NAME,
            delete_version=("v1", OWNER, "d1", "succeeded", NOW),
            slot_action="delete",
            slot_expected=slot,
        )

    assert "sub-alice" not in str(excinfo.value), "the raise must not carry the botocore message"
    assert vstore.get(NAME, "v1") is not None
    assert sstore.get(NAME, consistent=True) is not None


def test_an_empty_identity_term_is_refused_before_any_write(stores):
    """An empty expected value would compare against an ABSENT attribute and could match.

    That is the ownerless/legacy row shape this whole path refuses to touch, so it must not be
    expressible: the raise happens before the transaction is built, not inside DynamoDB.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore)

    for bad in (
        ("v1", "", "d1", "succeeded", NOW),
        ("v1", OWNER, "", "succeeded", NOW),
        ("v1", OWNER, "d1", "", NOW),
    ):
        with pytest.raises(ValueError):
            release_name_claim_atomically(NAME, delete_version=bad, slot_action="delete", slot_expected=slot)

    with pytest.raises(ValueError):
        release_name_claim_atomically(
            NAME,
            slot_action="delete",
            slot_expected=RuntimeSlots(runtime_name=NAME, owner_sub=""),
        )
    with pytest.raises(ValueError):
        release_name_claim_atomically(NAME, slot_action="clear", slot_expected=slot)
    with pytest.raises(ValueError):
        release_name_claim_atomically(NAME, slot_action="clear", slot_expected=slot, slot_clear_fields=("owner_sub",))

    assert vstore.get(NAME, "v1") is not None
    assert sstore.get(NAME, consistent=True) is not None


def test_slot_action_shapes_are_refused_before_any_write(stores):
    """The action names encode mutually exclusive observations and mutations.

    Accepting an expected row with ``assert_absent`` would turn a caller bug into the opposite
    condition, while accepting clear fields on a non-clear action silently discards mutation
    intent. Both must fail before DynamoDB receives a transaction.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore)

    for action in ("check", "delete", "clear"):
        with pytest.raises(ValueError):
            release_name_claim_atomically(NAME, slot_action=action)

    with pytest.raises(ValueError):
        release_name_claim_atomically(NAME, slot_action="assert_absent", slot_expected=slot)
    with pytest.raises(ValueError):
        release_name_claim_atomically(NAME, slot_action="not-an-action")

    for action, expected in (
        ("none", None),
        ("assert_absent", None),
        ("check", slot),
        ("delete", slot),
    ):
        with pytest.raises(ValueError):
            release_name_claim_atomically(
                NAME,
                slot_action=action,
                slot_expected=expected,
                slot_clear_fields=("production_version_id",),
            )

    assert vstore.get(NAME, "v1", consistent=True) is not None
    assert sstore.get(NAME, consistent=True) == slot


def _seed_siblings(vstore, count: int) -> tuple[tuple[str, str, str | None], ...]:
    """``count`` terminal sibling rows under NAME, and the require_unchanged_rows tuple for them."""
    rows = []
    for i in range(count):
        vid = f"v-old-{i:03d}"
        vstore.put(
            AgentVersion(
                runtime_name=NAME,
                version_id=vid,
                owner_sub=OWNER,
                created_at=NOW,
                deployment_id=f"d-old-{i}",
                agentcore_runtime_name=f"{NAME}_{i}",
                status="failed",
            )
        )
        rows.append((vid, "failed", NOW))
    return tuple(rows)


def _claim_generation(vstore, name: str = NAME) -> int:
    item = vstore._table.get_item(  # noqa: SLF001 - inspect the persisted concurrency primitive
        Key={"runtime_name": name, "version_id": avs.NAME_CLAIM_VERSION_ID},
        ConsistentRead=True,
    )["Item"]
    return int(item["claim_generation"])


def _claim_owner(vstore, name: str = NAME) -> str | None:
    item = vstore._table.get_item(  # noqa: SLF001 - inspect the persisted ownership primitive
        Key={"runtime_name": name, "version_id": avs.NAME_CLAIM_VERSION_ID},
        ConsistentRead=True,
    )["Item"]
    return item.get("claim_owner_sub")


def test_the_first_version_writer_atomically_owns_the_runtime_name(stores):
    """Two empty pre-flight reads cannot become two cross-tenant claims.

    The API's read-before-write ownership check cannot serialize two requests: Alice and Bob can
    both observe an unused name before either writes. DynamoDB serializes the transactions on the
    sentinel instead. The losing version row must not land, and its cancelled transaction must not
    advance the generation by itself.
    """
    vstore, _ = stores
    alice = AgentVersion(
        runtime_name=NAME,
        version_id="v-alice",
        owner_sub=OWNER,
        created_at=NOW,
        deployment_id="d-alice",
        agentcore_runtime_name=f"{NAME}_alice",
        status="pending",
    )
    bob = AgentVersion(
        runtime_name=NAME,
        version_id="v-bob",
        owner_sub="sub-bob",
        created_at=NOW,
        deployment_id="d-bob",
        agentcore_runtime_name=f"{NAME}_bob",
        status="pending",
    )

    vstore.put(alice)
    with pytest.raises(NameClaimConflict) as excinfo:
        vstore.put(bob)

    assert "sub-bob" not in str(excinfo.value)
    assert vstore.get(NAME, "v-alice", consistent=True) is not None
    assert vstore.get(NAME, "v-bob", consistent=True) is None
    assert _claim_owner(vstore) == OWNER
    assert _claim_generation(vstore) == 1

    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-alice-2",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d-alice-2",
            agentcore_runtime_name=f"{NAME}_alice2",
            status="pending",
        )
    )
    assert _claim_owner(vstore) == OWNER
    assert _claim_generation(vstore) == 2


def test_a_partial_release_keeps_the_owner_and_advances_the_fence(stores):
    """Deleting one version is not permission to hand a still-live name to another tenant."""
    vstore, sstore = stores
    slot = seed(vstore, sstore)
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v2",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d2",
            agentcore_runtime_name=f"{NAME}_v2",
            status="succeeded",
        )
    )
    snapshot = vstore.snapshot_for_name_release(NAME)

    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", NOW),
        claim_snapshot=snapshot,
        slot_action="clear",
        slot_expected=slot,
        slot_clear_fields=("production_version_id",),
    )

    assert vstore.get(NAME, "v1", consistent=True) is None
    assert vstore.get(NAME, "v2", consistent=True) is not None
    assert _claim_owner(vstore) == OWNER
    assert _claim_generation(vstore) == snapshot.generation + 1
    with pytest.raises(NameClaimConflict):
        vstore.put(
            AgentVersion(
                runtime_name=NAME,
                version_id="v-bob",
                owner_sub="sub-bob",
                created_at=NOW,
                deployment_id="d-bob",
                agentcore_runtime_name=f"{NAME}_bob",
                status="pending",
            )
        )


def test_a_complete_release_allows_the_next_tenant_to_claim_the_name(stores):
    """A durable owner claim is removed only with the exact final version and slot release."""
    vstore, sstore = stores
    slot = seed(vstore, sstore)
    snapshot = vstore.snapshot_for_name_release(NAME)

    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", NOW),
        claim_snapshot=snapshot,
        release_name=True,
        slot_action="delete",
        slot_expected=slot,
    )

    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-bob",
            owner_sub="sub-bob",
            created_at=NOW,
            deployment_id="d-bob",
            agentcore_runtime_name=f"{NAME}_bob",
            status="pending",
        )
    )
    assert _claim_owner(vstore) == "sub-bob"
    assert _claim_generation(vstore) == 1


def test_a_delayed_status_writer_cannot_cross_a_reacquired_name_claim(stores):
    """Historical rows from the previous owner must not bypass the new owner's sentinel.

    A complete release deliberately retains failed history. After Bob acquires the now-free name,
    an old Alice finalizer can still address one of those rows by runtime/version id. Callers that
    omit ``expected_owner_sub`` used to condition only on row existence and advance Bob's sentinel
    without checking its owner, turning Alice's failed history live underneath Bob's namespace.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore)
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-alice-history",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d-alice-history",
            agentcore_runtime_name=f"{NAME}_alice_history",
            status="failed",
        )
    )
    snapshot = vstore.snapshot_for_name_release(NAME)
    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", NOW),
        claim_snapshot=snapshot,
        release_name=True,
        slot_action="delete",
        slot_expected=slot,
    )

    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-bob",
            owner_sub="sub-bob",
            created_at=NOW,
            deployment_id="d-bob",
            agentcore_runtime_name=f"{NAME}_bob",
            status="pending",
        )
    )
    bob_generation = _claim_generation(vstore)

    with pytest.raises(NameClaimConflict):
        vstore.update_status(
            NAME,
            "v-alice-history",
            status="succeeded",
            runtime_id="alice-runtime",
            runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/alice-runtime",
        )

    history = vstore.get(NAME, "v-alice-history", consistent=True)
    assert history is not None and history.status == "failed"
    assert history.runtime_id is None and history.runtime_arn is None
    assert _claim_owner(vstore) == "sub-bob"
    assert _claim_generation(vstore) == bob_generation


@pytest.mark.parametrize(
    "bounded_snapshot",
    [False, True],
    ids=["legacy-caller", "bounded-caller"],
)
def test_a_delayed_release_cannot_advance_a_reacquired_name_claim(stores, bounded_snapshot):
    """Deleting Alice's retained history must not mutate Bob's current name claim.

    Complete release intentionally keeps terminal history.  After Bob acquires the free name,
    Alice's old teardown can still identify one of her historical rows.  The version-row delete
    and Bob's slot check used to succeed together because the sentinel transaction pinned only
    its epoch/generation, not its owner.  That let a previous tenant advance the current tenant's
    fence and manufacture conflicts in Bob's deploy/finalizer/release operations.
    """
    vstore, sstore = stores
    alice_slot = seed(vstore, sstore)
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-alice-history",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d-alice-history",
            agentcore_runtime_name=f"{NAME}_alice_history",
            status="failed",
        )
    )
    alice_snapshot = vstore.snapshot_for_name_release(NAME)
    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", NOW),
        claim_snapshot=alice_snapshot,
        release_name=True,
        slot_action="delete",
        slot_expected=alice_slot,
    )

    bob = "sub-bob"
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-bob",
            owner_sub=bob,
            created_at=NOW,
            deployment_id="d-bob",
            agentcore_runtime_name=f"{NAME}_bob",
            status="succeeded",
        )
    )
    bob_slot = RuntimeSlots(runtime_name=NAME, owner_sub=bob, production_version_id="v-bob")
    sstore.upsert(bob_slot)
    bob_generation = _claim_generation(vstore)
    delayed_alice_snapshot = vstore.snapshot_for_name_release(NAME) if bounded_snapshot else None

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v-alice-history", OWNER, "d-alice-history", "failed", NOW),
            claim_snapshot=delayed_alice_snapshot,
            slot_action="check" if bounded_snapshot else "none",
            slot_expected=bob_slot if bounded_snapshot else None,
        )

    assert vstore.get(NAME, "v-alice-history", consistent=True) is not None
    assert vstore.get(NAME, "v-bob", consistent=True) is not None
    assert sstore.get(NAME, consistent=True) == bob_slot
    assert _claim_owner(vstore) == bob
    assert _claim_generation(vstore) == bob_generation


def test_same_owner_delayed_release_can_clean_history_after_reacquisition(stores):
    """The owner fence distinguishes tenants; it does not reject every delayed cleanup.

    Alice may release a failed historical row after Alice herself reacquires the name.  The current
    slot stays byte-for-byte intact while the same-owner claim generation advances with the delete.
    This is the positive control for the two cross-tenant refusals above.
    """
    vstore, sstore = stores
    first_slot = seed(vstore, sstore)
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-alice-history",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d-alice-history",
            agentcore_runtime_name=f"{NAME}_alice_history",
            status="failed",
        )
    )
    first_snapshot = vstore.snapshot_for_name_release(NAME)
    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", NOW),
        claim_snapshot=first_snapshot,
        release_name=True,
        slot_action="delete",
        slot_expected=first_slot,
    )

    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-alice-current",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d-alice-current",
            agentcore_runtime_name=f"{NAME}_alice_current",
            status="succeeded",
        )
    )
    current_slot = RuntimeSlots(
        runtime_name=NAME,
        owner_sub=OWNER,
        production_version_id="v-alice-current",
        last_promoted_at=NOW,
        trigger_fence="trigger-generation-current",
    )
    sstore.upsert(current_slot)
    generation_before = _claim_generation(vstore)
    current_snapshot = vstore.snapshot_for_name_release(NAME)
    assert current_snapshot.claim_owner_sub == OWNER

    release_name_claim_atomically(
        NAME,
        delete_version=("v-alice-history", OWNER, "d-alice-history", "failed", NOW),
        claim_snapshot=current_snapshot,
        slot_action="check",
        slot_expected=current_slot,
    )

    assert vstore.get(NAME, "v-alice-history", consistent=True) is None
    assert vstore.get(NAME, "v-alice-current", consistent=True) is not None
    assert sstore.get(NAME, consistent=True) == current_slot
    assert _claim_owner(vstore) == OWNER
    assert _claim_generation(vstore) == generation_before + 1


def test_direct_version_delete_cannot_cross_a_reacquired_name_claim(stores):
    """The store's direct delete path must carry the same owner fence as release.

    It is not currently called by the product teardown, but it is a supported liveness writer and
    therefore participates in the generation protocol.  Leaving it owner-blind would let a future
    caller—or a maintenance path—delete retained Alice history while advancing Bob's live claim.
    """
    vstore, sstore = stores
    alice_slot = seed(vstore, sstore)
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-alice-history",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d-alice-history",
            agentcore_runtime_name=f"{NAME}_alice_history",
            status="failed",
        )
    )
    alice_snapshot = vstore.snapshot_for_name_release(NAME)
    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", NOW),
        claim_snapshot=alice_snapshot,
        release_name=True,
        slot_action="delete",
        slot_expected=alice_slot,
    )

    bob = "sub-bob"
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-bob",
            owner_sub=bob,
            created_at=NOW,
            deployment_id="d-bob",
            agentcore_runtime_name=f"{NAME}_bob",
            status="succeeded",
        )
    )
    bob_generation = _claim_generation(vstore)

    with pytest.raises(NameClaimConflict):
        vstore.delete(NAME, "v-alice-history")

    assert vstore.get(NAME, "v-alice-history", consistent=True) is not None
    assert vstore.get(NAME, "v-bob", consistent=True) is not None
    assert _claim_owner(vstore) == bob
    assert _claim_generation(vstore) == bob_generation


def test_a_delayed_release_cannot_delete_a_reacquired_name_claim(stores):
    """Only the tenant named by the sentinel may perform a complete release.

    A terminal Bob row and an absent slot mean the namespace has no live runtime, but they do not
    authorize Alice's delayed teardown to delete Bob's claim.  Without an owner term on the
    sentinel delete, Alice could remove it and let a third tenant acquire the name before Bob's
    own cleanup or retry decided what to do with his deployment history.
    """
    vstore, sstore = stores
    alice_slot = seed(vstore, sstore)
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-alice-history",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d-alice-history",
            agentcore_runtime_name=f"{NAME}_alice_history",
            status="failed",
        )
    )
    alice_snapshot = vstore.snapshot_for_name_release(NAME)
    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", NOW),
        claim_snapshot=alice_snapshot,
        release_name=True,
        slot_action="delete",
        slot_expected=alice_slot,
    )

    bob = "sub-bob"
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-bob-history",
            owner_sub=bob,
            created_at=NOW,
            deployment_id="d-bob-history",
            agentcore_runtime_name=f"{NAME}_bob_history",
            status="failed",
        )
    )
    bob_generation = _claim_generation(vstore)
    delayed_alice_snapshot = vstore.snapshot_for_name_release(NAME)
    assert sstore.get(NAME, consistent=True) is None

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v-alice-history", OWNER, "d-alice-history", "failed", NOW),
            claim_snapshot=delayed_alice_snapshot,
            release_name=True,
            slot_action="assert_absent",
        )

    assert vstore.get(NAME, "v-alice-history", consistent=True) is not None
    assert vstore.get(NAME, "v-bob-history", consistent=True) is not None
    assert _claim_owner(vstore) == bob
    assert _claim_generation(vstore) == bob_generation


def test_an_ownerless_legacy_row_cannot_acquire_a_name_via_status_update(stores):
    """Inferring the owner must fail closed when the persisted row has none.

    Treating an empty legacy owner as an omitted condition would let the status writer create an
    ownerless sentinel and a live row that no tenant can invoke or safely release.
    """
    vstore, _ = stores
    vstore._table.put_item(  # noqa: SLF001 - deliberate legacy/corrupt shape
        Item={
            "runtime_name": NAME,
            "version_id": "v-ownerless",
            "created_at": NOW,
            "deployment_id": "d-ownerless",
            "agentcore_runtime_name": f"{NAME}_ownerless",
            "status": "pending",
        }
    )

    with pytest.raises(NameClaimConflict):
        vstore.update_status(NAME, "v-ownerless", status="succeeded")

    row = vstore._table.get_item(  # noqa: SLF001 - inspect the unchanged legacy row
        Key={"runtime_name": NAME, "version_id": "v-ownerless"},
        ConsistentRead=True,
    )["Item"]
    assert row["status"] == "pending"
    assert (
        vstore._table.get_item(  # noqa: SLF001 - refusal must not manufacture a sentinel
            Key={"runtime_name": NAME, "version_id": avs.NAME_CLAIM_VERSION_ID},
            ConsistentRead=True,
        ).get("Item")
        is None
    )


def test_every_version_liveness_writer_advances_the_same_generation(stores):
    """Put, status transition and delete cannot drift into separate fencing schemes.

    The bounded release is sound only when every supported way to change version liveness moves
    this one row atomically. The failed update assertion is equally important: advancing without
    changing the target would manufacture a conflict from an operation that never landed.
    """
    vstore, _ = stores
    version = AgentVersion(
        runtime_name=NAME,
        version_id="v1",
        owner_sub=OWNER,
        created_at=NOW,
        deployment_id="d1",
        agentcore_runtime_name=f"{NAME}_v1",
        status="pending",
    )

    vstore.put(version)
    assert _claim_generation(vstore) == 1

    vstore.update_status(NAME, "v1", status="succeeded", expected_owner_sub=OWNER)
    assert _claim_generation(vstore) == 2

    with pytest.raises(NameClaimConflict):
        vstore.update_status(NAME, "missing", status="failed", expected_owner_sub=OWNER)
    assert _claim_generation(vstore) == 2, "a cancelled row write advanced the generation by itself"

    vstore.delete(NAME, "v1")
    assert _claim_generation(vstore) == 3
    assert vstore.list_for_runtime(NAME, consistent=True) == [], "the sentinel leaked into version history"


def test_a_bounded_claim_releases_a_name_with_more_than_100_historical_rows(stores):
    """The release stays three actions no matter how much history the name accumulated.

    This is the production case that the old per-sibling transaction could never handle:
    target delete + 125 sibling checks + slot delete exceeded DynamoDB's 100-action limit.
    The snapshot's one generation fence now represents every sibling. The historical rows
    deliberately remain -- release means the target and slot no longer hold the name, not that
    audit history is erased.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore)
    siblings = _seed_siblings(vstore, 125)
    snapshot = vstore.snapshot_for_name_release(NAME)

    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", NOW),
        require_unchanged_rows=siblings,
        claim_snapshot=snapshot,
        release_name=True,
        slot_action="delete",
        slot_expected=slot,
    )

    assert vstore.get(NAME, "v1") is None
    assert sstore.get(NAME, consistent=True) is None
    assert len(vstore.list_for_runtime(NAME, consistent=True)) == 125
    assert (
        vstore._table.get_item(  # noqa: SLF001 - a complete release leaves no metadata tombstone
            Key={"runtime_name": NAME, "version_id": avs.NAME_CLAIM_VERSION_ID},
            ConsistentRead=True,
        ).get("Item")
        is None
    )


def test_the_legacy_per_row_fallback_still_refuses_instead_of_truncating(stores):
    """A caller that has not adopted the generation snapshot remains fail-closed.

    Backward compatibility is not permission to silently drop sibling checks. The bounded path
    above is the fix; this fallback remains a safe refusal until every caller supplies a snapshot.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore)
    siblings = _seed_siblings(vstore, 99)

    with pytest.raises(avs.NameClaimReleaseTooLarge) as excinfo:
        release_name_claim_atomically(
            NAME,
            delete_version=("v1", OWNER, "d1", "succeeded", NOW),
            require_unchanged_rows=siblings,
            slot_action="delete",
            slot_expected=slot,
        )

    assert isinstance(excinfo.value, NameClaimConflict), (
        "callers catch NameClaimConflict; a new sibling type would escape every one of them"
    )
    assert OWNER not in str(excinfo.value)
    assert vstore.get(NAME, "v1") is not None
    assert sstore.get(NAME, consistent=True) is not None
    assert len(vstore.list_for_runtime(NAME, consistent=True)) == 100


def test_a_new_sibling_after_the_snapshot_cancels_the_bounded_release(stores):
    """A writer absent from the caller's row list is still represented by the generation.

    This is the race truncating sibling checks would miss: the release read no ``v-new`` row,
    then a new deployment created one before the transaction. The target and slot must both
    survive so the new deployment does not lose the only name/slot metadata it can resolve.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore)
    snapshot = vstore.snapshot_for_name_release(NAME)

    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-new",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d-new",
            agentcore_runtime_name=f"{NAME}_new",
            status="pending",
        )
    )

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v1", OWNER, "d1", "succeeded", NOW),
            claim_snapshot=snapshot,
            release_name=True,
            slot_action="delete",
            slot_expected=slot,
        )

    assert vstore.get(NAME, "v1") is not None
    assert vstore.get(NAME, "v-new") is not None
    assert sstore.get(NAME, consistent=True) is not None


def test_a_stale_snapshot_cannot_delete_a_reacquired_name_when_the_number_repeats(stores):
    """The epoch closes delete/recreate ABA on the sentinel itself.

    A complete release removes the fence row. The next deployment creates a new row whose numeric
    generation starts at one again. This deliberately makes every target and slot condition match
    the newly acquired name and reuses the old numeric generation; only the epoch can distinguish
    the two ownership eras.
    """
    vstore, sstore = stores
    first_slot = seed(vstore, sstore)
    stale = vstore.snapshot_for_name_release(NAME)
    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", NOW),
        claim_snapshot=stale,
        release_name=True,
        slot_action="delete",
        slot_expected=first_slot,
    )

    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v2",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d2",
            agentcore_runtime_name=f"{NAME}_v2",
            status="succeeded",
        )
    )
    second_slot = RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id="v2")
    sstore.upsert(second_slot)
    assert _claim_generation(vstore) == stale.generation

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v2", OWNER, "d2", "succeeded", NOW),
            claim_snapshot=stale,
            release_name=True,
            slot_action="delete",
            slot_expected=second_slot,
        )

    assert vstore.get(NAME, "v2") is not None
    assert sstore.get(NAME, consistent=True) is not None
    assert _claim_generation(vstore) == stale.generation


def test_a_sibling_reput_after_the_snapshot_cancels_the_bounded_release(stores):
    """Generation is an ABA fence, not a row count.

    Re-putting the same sibling id can keep the row count and status unchanged while refreshing
    ``created_at`` and making a stale pending deployment live again. Every put must advance the
    generation or the bounded release would miss this exact liveness transition.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore)
    old_created = "2020-01-01T00:00:00+00:00"
    sibling = AgentVersion(
        runtime_name=NAME,
        version_id="v-sibling",
        owner_sub=OWNER,
        created_at=old_created,
        deployment_id="d-sibling",
        agentcore_runtime_name=f"{NAME}_sibling",
        status="pending",
    )
    vstore.put(sibling)
    snapshot = vstore.snapshot_for_name_release(NAME)

    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id=sibling.version_id,
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id=sibling.deployment_id,
            agentcore_runtime_name=sibling.agentcore_runtime_name,
            status=sibling.status,
        )
    )

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v1", OWNER, "d1", "succeeded", NOW),
            require_unchanged_rows=((sibling.version_id, sibling.status, old_created),),
            claim_snapshot=snapshot,
            release_name=True,
            slot_action="delete",
            slot_expected=slot,
        )

    assert vstore.get(NAME, "v1") is not None
    assert sstore.get(NAME, consistent=True) is not None


def test_a_sibling_status_change_after_the_snapshot_cancels_the_bounded_release(stores):
    """The finalizer's pending-to-succeeded write is fenced too.

    A status transition is the most important liveness mutation: the caller chose to release
    because this sibling was stale pending, then its delayed finalizer completed. Forgetting the
    generation update in ``update_status`` would delete the target and slot out from under that
    newly live deployment.
    """
    vstore, sstore = stores
    slot = seed(vstore, sstore)
    old_created = "2020-01-01T00:00:00+00:00"
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v-sibling",
            owner_sub=OWNER,
            created_at=old_created,
            deployment_id="d-sibling",
            agentcore_runtime_name=f"{NAME}_sibling",
            status="pending",
        )
    )
    snapshot = vstore.snapshot_for_name_release(NAME)

    vstore.update_status(
        NAME,
        "v-sibling",
        status="succeeded",
        expected_owner_sub=OWNER,
    )

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=("v1", OWNER, "d1", "succeeded", NOW),
            require_unchanged_rows=(("v-sibling", "pending", old_created),),
            claim_snapshot=snapshot,
            release_name=True,
            slot_action="delete",
            slot_expected=slot,
        )

    assert vstore.get(NAME, "v1") is not None
    assert vstore.get(NAME, "v-sibling").status == "succeeded"
    assert sstore.get(NAME, consistent=True) is not None


def test_the_snapshot_retries_when_a_writer_lands_during_its_query(stores, monkeypatch):
    """Strongly consistent Query pages are not one cross-page transaction snapshot.

    The sentinel is read before and after the query. A version put between those reads forces a
    retry, and the returned snapshot includes the new row rather than pairing an old row list with
    a new generation.
    """
    vstore, _ = stores
    seed_version = AgentVersion(
        runtime_name=NAME,
        version_id="v1",
        owner_sub=OWNER,
        created_at=NOW,
        deployment_id="d1",
        agentcore_runtime_name=f"{NAME}_v1",
        status="succeeded",
    )
    vstore.put(seed_version)
    original = vstore.list_for_runtime
    calls = 0

    def interleaved(runtime_name: str, *, consistent: bool = False):
        nonlocal calls
        calls += 1
        rows = original(runtime_name, consistent=consistent)
        if calls == 1:
            vstore.put(
                AgentVersion(
                    runtime_name=NAME,
                    version_id="v-concurrent",
                    owner_sub=OWNER,
                    created_at=NOW,
                    deployment_id="d-concurrent",
                    agentcore_runtime_name=f"{NAME}_concurrent",
                    status="pending",
                )
            )
        return rows

    monkeypatch.setattr(vstore, "list_for_runtime", interleaved)
    snapshot = vstore.snapshot_for_name_release(NAME)

    assert calls == 2
    assert {row.version_id for row in snapshot.versions} == {"v1", "v-concurrent"}


def test_a_legacy_name_with_no_sentinel_is_bootstrapped_at_unbounded_scale(stores):
    """Rows written before the fence rollout are not permanently locked.

    The sentinel is created before the stable double-read. Existing rows need no retroactive
    transaction: they are in that stable snapshot, while every writer after sentinel creation
    advances its generation. This seeds 126 rows without the new store API to model a real
    pre-rollout partition and proves the first bounded release succeeds.
    """
    vstore, sstore = stores
    with vstore._table.batch_writer() as batch:  # noqa: SLF001 - deliberate pre-rollout rows
        for i in range(126):
            version_id = "v1" if i == 0 else f"v-legacy-{i:03d}"
            batch.put_item(
                Item=AgentVersion(
                    runtime_name=NAME,
                    version_id=version_id,
                    owner_sub=OWNER,
                    created_at=NOW,
                    deployment_id="d1" if i == 0 else f"d-legacy-{i}",
                    agentcore_runtime_name=f"{NAME}_{i}",
                    status="succeeded" if i == 0 else "failed",
                ).to_item()
            )
    slot = RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id="v1")
    sstore.upsert(slot)

    assert (
        vstore._table.get_item(  # noqa: SLF001 - prove the migration precondition
            Key={"runtime_name": NAME, "version_id": avs.NAME_CLAIM_VERSION_ID}
        ).get("Item")
        is None
    )
    snapshot = vstore.snapshot_for_name_release(NAME)
    assert snapshot.generation == 0
    assert len(snapshot.versions) == 126

    release_name_claim_atomically(
        NAME,
        delete_version=("v1", OWNER, "d1", "succeeded", NOW),
        require_unchanged_rows=tuple(
            (row.version_id, row.status, row.created_at or None) for row in snapshot.versions if row.version_id != "v1"
        ),
        claim_snapshot=snapshot,
        release_name=True,
        slot_action="delete",
        slot_expected=slot,
    )

    assert vstore.get(NAME, "v1") is None
    assert sstore.get(NAME, consistent=True) is None
    assert len(vstore.list_for_runtime(NAME, consistent=True)) == 125


def test_the_pointer_name_tuple_and_the_pointer_reader_cannot_drift():
    """``SLOT_POINTER_FIELDS`` and ``slot_pointer_pairs`` must name the same three fields.

    The pairs function replaced a ``getattr(slot, field) for field in SLOT_POINTER_FIELDS`` loop, so
    that the set of attributes this module reads is fixed at import time and the export bundle's
    dynamic-dispatch scan has nothing to review here. The cost of spelling them out is that a field
    added to one and not the other is now possible, and the failure is silent in the direction that
    matters: a pointer left out of ``slot_pointer_pairs`` is a pointer left out of the release's
    compare-and-set, so a concurrent promote that moves only that pointer would no longer cancel the
    release. Names AND order, because the conditions are built by enumerate index.
    """
    slot = RuntimeSlots(
        runtime_name=NAME,
        owner_sub=OWNER,
        production_version_id="v1",
        staging_version_id="v2",
        previous_production_version_id="v0",
    )
    pairs = avs.slot_pointer_pairs(slot)
    assert tuple(name for name, _ in pairs) == avs.SLOT_POINTER_FIELDS
    # ... and each name must actually read its own field, not another one's value.
    assert dict(pairs) == {
        "production_version_id": "v1",
        "staging_version_id": "v2",
        "previous_production_version_id": "v0",
    }


def test_nothing_to_do_is_not_an_error(stores):
    vstore, sstore = stores
    seed(vstore, sstore)
    release_name_claim_atomically(NAME)
    assert vstore.get(NAME, "v1") is not None
    assert sstore.get(NAME, consistent=True) is not None
