"""F-81b/c/d — what the teardown name release is allowed to touch, against the real stores.

The release block was dead code for its whole life: it queried a key that never matched
(F-81), so nothing it did had any effect. Fixing the key turns every latent decision in it into a
live one, and the first read of it afterwards found three ways it could destroy state that was
never this deployment's to destroy:

* it iterated EVERY row for the friendly name and deleted them all, then deleted the slot -- but
  several live versions under one friendly name is the designed model, and
  ``resolve_owned_runtime_target`` needs slot + version + deployment to agree before it will build
  a client, so deleting v1 turned a running v2 into a 404 for its own tenant;
* it treated a missing ``owner_sub`` as "ours", although both halves of the H-1 deploy guard skip
  an ownerless row -- so deleting one frees nothing and destroys a row nobody can attribute;
* it ran even when ``destroy_runtime`` had failed or the manifest had retained the runtime, which
  hands the name to another tenant while our runtime is still serving.

Every test drives the real ``_release_runtime_name_claim`` against moto-backed stores and asserts
on the ROWS, not on the returned messages. The messages are the thing that lied for months: this
block appended "Released runtime name ..." while releasing nothing at all.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

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
    new_version_id,
    short_version_suffix,
)
from moto import mock_aws  # noqa: E402

CALLER = "sub-ours"
OTHER = "sub-theirs"


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[AgentVersionsStore, RuntimeSlotsStore]]:
    """Real stores over moto, wired into the module singletons the release resolves through.

    The singletons are cached at first use, so without resetting them the function under test
    would talk to whatever a previous import bound -- and a release that silently touches nothing
    is exactly the failure mode being tested, so it must not be reachable from the harness.
    """
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
                {"AttributeName": "owner_sub", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "owner_sub-version_id-index",
                    "KeySchema": [
                        {"AttributeName": "owner_sub", "KeyType": "HASH"},
                        {"AttributeName": "version_id", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="RuntimeSlots",
            KeySchema=[{"AttributeName": "runtime_name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "runtime_name", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # F-81f: the release re-reads the triggers partition before it settles the slot, and a
        # table it cannot read keeps the name locked -- so an EMPTY real table is what "no trigger
        # residue" looks like to it. Without this every release below would refuse.
        ddb.create_table(
            TableName="Triggers",
            KeySchema=[
                {"AttributeName": "runtime_name", "KeyType": "HASH"},
                {"AttributeName": "trigger_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "runtime_name", "AttributeType": "S"},
                {"AttributeName": "trigger_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        vstore = AgentVersionsStore(table_name="AgentVersions", region="us-east-1")
        sstore = RuntimeSlotsStore(table_name="RuntimeSlots", region="us-east-1")
        monkeypatch.setattr(avs, "_versions_store", vstore, raising=False)
        monkeypatch.setattr(avs, "_slots_store", sstore, raising=False)
        from app.services import trigger_store as ts  # noqa: PLC0415

        monkeypatch.setattr(
            ts, "_trigger_store", ts.TriggerStore(table_name="Triggers", region="us-east-1"), raising=False
        )
        yield vstore, sstore


def release(record: dict | None, *, caller: str | None = CALLER, runtime_may_still_live: bool = False) -> list[str]:
    from app.deployment_handler import _release_runtime_name_claim  # noqa: PLC0415

    return _release_runtime_name_claim(record, caller, runtime_may_still_live=runtime_may_still_live)


def put_version(
    vstore: AgentVersionsStore,
    name: str,
    *,
    deployment_id: str,
    owner: str = CALLER,
    status: str = "succeeded",
    created_at: str | None = None,
    version_id: str | None = None,
) -> str:
    vid = version_id or new_version_id()
    vstore.put(
        AgentVersion(
            runtime_name=name,
            version_id=vid,
            owner_sub=owner,
            created_at=created_at or datetime.now(timezone.utc).isoformat(),
            deployment_id=deployment_id,
            agentcore_runtime_name=f"{name}_{short_version_suffix(vid)}",
            runtime_id=f"rt-{deployment_id}",
            status=status,
        )
    )
    return vid


def record_for(name: str, deployment_id: str, version_id: str, **extra) -> dict:
    """A deployment record as the store would return it for a normal platform deploy."""
    base = {
        "deployment_id": deployment_id,
        "user_id": CALLER,
        "friendly_runtime_name": name,
        "agentcore_runtime_name": f"{name}_{short_version_suffix(version_id)}",
        "version_id": version_id,
        "runtime_id": f"rt-{deployment_id}",
        "node_id": name.replace("_", "-"),
    }
    base.update(extra)
    return base


def names_in(vstore: AgentVersionsStore, name: str) -> set[str]:
    return {v.version_id for v in vstore.list_for_runtime(name)}


# ---------------------------------------------------------------------------
# The cascade: one deployment's teardown may not touch another version
# ---------------------------------------------------------------------------


def test_deleting_v1_leaves_live_v2_and_the_slot_pointing_at_it(stores):
    vstore, sstore = stores
    v1 = put_version(vstore, "prod_bot", deployment_id="dep-1")
    v2 = put_version(vstore, "prod_bot", deployment_id="dep-2")
    sstore.upsert(
        RuntimeSlots(
            runtime_name="prod_bot",
            owner_sub=CALLER,
            production_version_id=v2,
            previous_production_version_id=v1,
        )
    )

    release(record_for("prod_bot", "dep-1", v1))

    assert names_in(vstore, "prod_bot") == {v2}, "v1 and only v1 should be gone"
    slot = sstore.get("prod_bot")
    assert slot is not None, "the slot still serves v2; deleting it 404s a running agent"
    assert slot.production_version_id == v2
    # v1's row is gone, so the pointer that named it must not be left dangling either.
    assert slot.previous_production_version_id is None


def test_deleting_the_production_version_clears_its_pointer_and_promotes_nothing(stores):
    """A cleanup path may not decide where traffic goes.

    The slot is what ``resolve_owned_runtime_target`` and every trigger resolve through, so
    repointing production at a surviving version would be a traffic change disguised as tidying
    up. Cleared reads as "no production version", which is true, and promote/rollback can set it.
    """
    vstore, sstore = stores
    v1 = put_version(vstore, "prod_bot", deployment_id="dep-1")
    v2 = put_version(vstore, "prod_bot", deployment_id="dep-2")
    sstore.upsert(RuntimeSlots(runtime_name="prod_bot", owner_sub=CALLER, production_version_id=v1))

    release(record_for("prod_bot", "dep-1", v1))

    slot = sstore.get("prod_bot")
    assert slot is not None
    assert slot.production_version_id is None
    assert slot.production_version_id != v2
    assert names_in(vstore, "prod_bot") == {v2}


def test_the_last_live_version_releases_the_name(stores):
    """Bug 192's actual purpose, which has to keep working after all the narrowing."""
    vstore, sstore = stores
    v1 = put_version(vstore, "solo_bot", deployment_id="dep-1")
    sstore.upsert(RuntimeSlots(runtime_name="solo_bot", owner_sub=CALLER, production_version_id=v1))

    messages = release(record_for("solo_bot", "dep-1", v1))

    assert names_in(vstore, "solo_bot") == set()
    assert sstore.get("solo_bot") is None
    assert any("Released runtime name 'solo_bot'" in m for m in messages)


def test_the_handler_releases_a_name_with_more_than_100_historical_versions(stores):
    """The real teardown caller must use the bounded claim snapshot, not per-row checks.

    A long-lived name can accumulate more rows than DynamoDB permits in one transaction. Failed
    history is retained for audit, but it must not make the current deployment's last live claim
    impossible to release.
    """
    vstore, sstore = stores
    target = put_version(vstore, "crowded_bot", deployment_id="dep-current")
    for index in range(125):
        put_version(
            vstore,
            "crowded_bot",
            deployment_id=f"dep-history-{index}",
            status="failed",
        )
    sstore.upsert(RuntimeSlots(runtime_name="crowded_bot", owner_sub=CALLER, production_version_id=target))

    messages = release(record_for("crowded_bot", "dep-current", target))

    remaining = names_in(vstore, "crowded_bot")
    assert target not in remaining
    assert len(remaining) == 125, "failed audit history is retained without holding the name"
    assert sstore.get("crowded_bot", consistent=True) is None
    assert any("Released runtime name 'crowded_bot'" in message for message in messages)


def test_a_stale_pending_sibling_does_not_keep_the_name_locked(stores):
    """F-82 again, from the release side.

    A pending row past the state machine's ceiling cannot belong to a running execution, and the
    deploy guard ignores it -- so if the release treated it as live it would keep a slot row that
    the guard says is free, and the slot alone 409s another tenant.
    """
    vstore, sstore = stores
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    v1 = put_version(vstore, "abort_bot", deployment_id="dep-1")
    put_version(vstore, "abort_bot", deployment_id="dep-0", status="pending", created_at=old)
    sstore.upsert(RuntimeSlots(runtime_name="abort_bot", owner_sub=CALLER, production_version_id=v1))

    release(record_for("abort_bot", "dep-1", v1))

    assert sstore.get("abort_bot") is None, "a dead pending row must not hold the slot"


def test_a_fresh_pending_sibling_keeps_the_slot(stores):
    """The other direction: a deploy that is genuinely in flight still owns the name."""
    vstore, sstore = stores
    v1 = put_version(vstore, "busy_bot", deployment_id="dep-1")
    put_version(
        vstore,
        "busy_bot",
        deployment_id="dep-2",
        status="pending",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    sstore.upsert(RuntimeSlots(runtime_name="busy_bot", owner_sub=CALLER, production_version_id=v1))

    release(record_for("busy_bot", "dep-1", v1))

    assert sstore.get("busy_bot") is not None


def test_a_failed_sibling_does_not_keep_the_name_locked(stores):
    """Bug 192b: a failed deploy never produced a runtime, so it holds nothing."""
    vstore, sstore = stores
    v1 = put_version(vstore, "flaky_bot", deployment_id="dep-1")
    put_version(vstore, "flaky_bot", deployment_id="dep-0", status="failed")
    sstore.upsert(RuntimeSlots(runtime_name="flaky_bot", owner_sub=CALLER, production_version_id=v1))

    release(record_for("flaky_bot", "dep-1", v1))

    assert sstore.get("flaky_bot") is None


# ---------------------------------------------------------------------------
# Identity and ownership: anything unprovable deletes nothing
# ---------------------------------------------------------------------------


def test_a_foreign_live_version_is_untouched_and_keeps_its_slot(stores):
    vstore, sstore = stores
    theirs = put_version(vstore, "shared_name", deployment_id="dep-theirs", owner=OTHER)
    sstore.upsert(RuntimeSlots(runtime_name="shared_name", owner_sub=OTHER, production_version_id=theirs))

    release(record_for("shared_name", "dep-ours", new_version_id()))

    assert names_in(vstore, "shared_name") == {theirs}
    slot = sstore.get("shared_name")
    assert slot is not None and slot.owner_sub == OTHER


def test_a_version_row_that_OMITS_owner_sub_is_never_deleted(stores):
    """The ownerless row is real, and it is the sparse shape -- not the empty-string one.

    An empty ``owner_sub`` is genuinely impossible: it is the hash key of the
    owner_sub-version_id GSI and DynamoDB rejects an empty string for an index key (asserted
    below, because that is why the sparse shape is the ONLY ownerless shape). A GSI is sparse,
    though, so an item that omits the attribute entirely is accepted and simply does not appear in
    the index -- which is exactly the legacy/corrupt row the old release block claimed was "safe
    to clean up". Seeded raw, because the dataclass cannot express it.

    Both halves of the H-1 deploy guard skip an ownerless row, so never deleting one costs no
    functionality: it locks nobody out. Deleting one destroys a row nobody can attribute.
    """
    vstore, _ = stores
    vid = new_version_id()
    boto3.client("dynamodb", region_name="us-east-1").put_item(
        TableName="AgentVersions",
        Item={
            "runtime_name": {"S": "legacy_bot"},
            "version_id": {"S": vid},
            "deployment_id": {"S": "dep-1"},
            "created_at": {"S": datetime.now(timezone.utc).isoformat()},
            "agentcore_runtime_name": {"S": "legacy_bot_x"},
            "status": {"S": "succeeded"},
        },
    )
    assert vstore.get("legacy_bot", vid) is not None, "the sparse row has to exist for this to test anything"

    release(record_for("legacy_bot", "dep-1", vid))

    assert names_in(vstore, "legacy_bot") == {vid}

    with pytest.raises(Exception, match="key attribute cannot contain an empty string"):
        put_version(vstore, "legacy_bot", deployment_id="dep-1", owner="")


def test_a_record_with_no_version_id_deletes_nothing(stores):
    """Identity is TWO terms, both present. This used to read ``not record_version_id or ...``,
    so a record missing its version id matched on the deployment id alone and the absent term
    counted as agreement. Every deployment that has a version row persisted its version_id
    alongside it, so requiring it costs nothing real."""
    vstore, sstore = stores
    v1 = put_version(vstore, "prod_bot", deployment_id="dep-1")
    sstore.upsert(RuntimeSlots(runtime_name="prod_bot", owner_sub=CALLER, production_version_id=v1))

    messages = release(
        {
            "deployment_id": "dep-1",
            "user_id": CALLER,
            "friendly_runtime_name": "prod_bot",
            "runtime_id": "rt-dep-1",
        }
    )

    assert names_in(vstore, "prod_bot") == {v1}
    assert sstore.get("prod_bot") is not None
    assert not any("Released" in m for m in messages)


def test_a_proven_name_alone_may_not_delete_an_orphan_slot(stores):
    """The name is not the row. A proven name says "this key is the one I recorded"; it does not
    say "this slot row is the one I wrote". A same-tenant deploy that has upserted its slot and
    not yet landed its version row looks exactly like an orphan slot, and deleting it leaves that
    deploy with no slot at all -- which is what every invocation and trigger resolves through.

    The deliberate cost: a genuinely orphaned slot stays locked until an operator clears it. The
    message has to say so, because "released" when nothing was released is the original F-81 bug.
    """
    _, sstore = stores
    sstore.upsert(RuntimeSlots(runtime_name="orphan_bot", owner_sub=CALLER))

    messages = release(
        {
            "deployment_id": "dep-1",
            "user_id": CALLER,
            "friendly_runtime_name": "orphan_bot",
            "version_id": new_version_id(),
            "runtime_id": "rt-1",
        }
    )

    assert sstore.get("orphan_bot") is not None
    assert any("kept locked" in m for m in messages)
    assert not any("Released" in m for m in messages)


def test_a_slot_that_moves_to_v2_between_read_and_write_is_not_released(stores, monkeypatch):
    """The race the transaction's compare-and-set exists for.

    A deploy lands v2 and repoints the slot after the release has read both. The stale read says
    "nothing live remains, release the name"; the conditional write disagrees. Simulated by
    handing the release a stale slot value while the table holds the new one -- the only way to
    make the interleaving deterministic, and it exercises the real conditions.
    """
    vstore, sstore = stores
    v1 = put_version(vstore, "racy_bot", deployment_id="dep-1")
    stale = RuntimeSlots(runtime_name="racy_bot", owner_sub=CALLER, production_version_id=v1)
    sstore.upsert(stale)

    v2 = put_version(vstore, "racy_bot", deployment_id="dep-2")
    sstore.upsert(RuntimeSlots(runtime_name="racy_bot", owner_sub=CALLER, production_version_id=v2))

    def stale_get(runtime_name: str, *, consistent: bool = False):
        return stale if runtime_name == "racy_bot" else None

    monkeypatch.setattr(sstore, "get", stale_get)

    messages = release(record_for("racy_bot", "dep-1", v1))

    monkeypatch.undo()
    slot = sstore.get("racy_bot")
    assert slot is not None and slot.production_version_id == v2, "v2's slot must survive the race"
    assert names_in(vstore, "racy_bot") == {v1, v2}, "a cancelled transaction deletes nothing"
    assert any("not released" in m for m in messages)
    assert not any("Released" in m for m in messages)


def test_a_first_slot_created_after_an_absent_read_cancels_the_release(stores, monkeypatch):
    """Absence is a compare-and-set state, not permission to skip the slot condition.

    A deployment finalizer creates the first slot after teardown read no row. The target version
    and the newly created slot must both survive; otherwise the slot is born pointing at a version
    the same transaction just deleted.
    """
    vstore, sstore = stores
    target = put_version(vstore, "late_slot_bot", deployment_id="dep-1")
    real_get = sstore.get
    injected = False

    def absent_then_create(runtime_name: str, *, consistent: bool = False):
        nonlocal injected
        if runtime_name == "late_slot_bot" and not injected:
            injected = True
            sstore.upsert(
                RuntimeSlots(
                    runtime_name="late_slot_bot",
                    owner_sub=CALLER,
                    production_version_id=target,
                )
            )
            return None
        return real_get(runtime_name, consistent=consistent)

    monkeypatch.setattr(sstore, "get", absent_then_create)

    messages = release(record_for("late_slot_bot", "dep-1", target))

    assert injected, "the test did not create the slot in the read/write window"
    assert vstore.get("late_slot_bot", target, consistent=True) is not None
    slot = real_get("late_slot_bot", consistent=True)
    assert slot is not None and slot.production_version_id == target
    assert any("not released" in message for message in messages)
    assert not any("Released" in message for message in messages)


def test_a_slot_repointed_to_the_target_after_read_cancels_target_deletion(stores, monkeypatch):
    """A no-op-looking slot path still needs an exact row check.

    Teardown reads production=v2, so v1 is not a pointer it plans to clear. A concurrent promote
    then points production at v1 before the transaction. Deleting v1 against only the version
    fence would leave production dangling; the slot ``check`` must cancel the whole operation.
    """
    vstore, sstore = stores
    target = put_version(vstore, "repoint_bot", deployment_id="dep-1")
    survivor = put_version(vstore, "repoint_bot", deployment_id="dep-2")
    stale = RuntimeSlots(
        runtime_name="repoint_bot",
        owner_sub=CALLER,
        production_version_id=survivor,
        last_promoted_at="before-repoint",
    )
    sstore.upsert(stale)
    real_get = sstore.get
    injected = False

    def stale_then_repoint(runtime_name: str, *, consistent: bool = False):
        nonlocal injected
        if runtime_name == "repoint_bot" and not injected:
            injected = True
            sstore.upsert(
                RuntimeSlots(
                    runtime_name="repoint_bot",
                    owner_sub=CALLER,
                    production_version_id=target,
                    previous_production_version_id=survivor,
                    last_promoted_at="after-repoint",
                )
            )
            return stale
        return real_get(runtime_name, consistent=consistent)

    monkeypatch.setattr(sstore, "get", stale_then_repoint)

    messages = release(record_for("repoint_bot", "dep-1", target))

    assert injected, "the test did not repoint the slot in the read/write window"
    assert names_in(vstore, "repoint_bot") == {target, survivor}
    slot = real_get("repoint_bot", consistent=True)
    assert slot is not None and slot.production_version_id == target
    assert slot.previous_production_version_id == survivor
    assert any("not released" in message for message in messages)
    assert not any("Released" in message for message in messages)


def test_a_conflicted_name_snapshot_keeps_every_row_locked(stores, monkeypatch):
    """Churn while the paginated snapshot is read is a refusal, never a partial release."""
    vstore, sstore = stores
    target = put_version(vstore, "snapshot_race_bot", deployment_id="dep-1")
    slot = RuntimeSlots(
        runtime_name="snapshot_race_bot",
        owner_sub=CALLER,
        production_version_id=target,
    )
    sstore.upsert(slot)

    def conflict(_runtime_name: str):
        raise NameClaimConflict("the bounded snapshot changed")

    monkeypatch.setattr(vstore, "snapshot_for_name_release", conflict)

    messages = release(record_for("snapshot_race_bot", "dep-1", target))

    assert vstore.get("snapshot_race_bot", target, consistent=True) is not None
    assert sstore.get("snapshot_race_bot", consistent=True) == slot
    assert any("changed during teardown" in message for message in messages)
    assert not any("Released" in message for message in messages)


def test_a_slot_pointing_at_an_unknown_version_keeps_the_name_locked(stores):
    """A pointer naming a row we did not read is evidence of a row we cannot see -- a deploy that
    wrote its slot before its version row, or a pointer left dangling by an earlier partial
    delete. Neither is something a teardown may resolve by guessing."""
    vstore, sstore = stores
    v1 = put_version(vstore, "ahead_bot", deployment_id="dep-1")
    sstore.upsert(
        RuntimeSlots(
            runtime_name="ahead_bot",
            owner_sub=CALLER,
            production_version_id=v1,
            staging_version_id="v-not-landed-yet",
        )
    )

    messages = release(record_for("ahead_bot", "dep-1", v1))

    assert sstore.get("ahead_bot") is not None
    assert any("cannot account for" in m for m in messages)


@pytest.mark.parametrize("consumer_field", ["runtime_id", "harness_id", "mcp_server_runtime_id"])
def test_any_live_consumer_keeps_the_name_locked(stores, consumer_field):
    """A harness deploy and an MCP-server deploy write the SAME version and slot rows an
    agent-runtime deploy does, so they claim the name the same way. The liveness gate used to be
    keyed off ``runtime_id`` alone, which released the name of a live harness or MCP server."""
    vstore, sstore = stores
    v1 = put_version(vstore, "kept_bot", deployment_id="dep-1")
    sstore.upsert(RuntimeSlots(runtime_name="kept_bot", owner_sub=CALLER, production_version_id=v1))

    record = {
        "deployment_id": "dep-1",
        "user_id": CALLER,
        "friendly_runtime_name": "kept_bot",
        "version_id": v1,
        consumer_field: f"{consumer_field}-value",
    }
    messages = release(record, runtime_may_still_live=True)

    assert names_in(vstore, "kept_bot") == {v1}
    assert sstore.get("kept_bot") is not None
    assert any("kept locked" in m for m in messages)


def test_an_ownerless_slot_is_never_deleted(stores):
    vstore, sstore = stores
    v1 = put_version(vstore, "legacy_bot", deployment_id="dep-1")
    legacy_slot = RuntimeSlots(
        runtime_name="legacy_bot",
        owner_sub="",
        production_version_id=v1,
        last_promoted_at="legacy-promotion",
        trigger_fence="legacy-trigger-fence",
    )
    sstore.upsert(legacy_slot)

    release(record_for("legacy_bot", "dep-1", v1))

    assert names_in(vstore, "legacy_bot") == set(), "our own row is still ours to delete"
    assert sstore.get("legacy_bot", consistent=True) == legacy_slot, "ownerless slot must remain byte-for-byte logical"


def test_a_version_id_match_with_a_different_deployment_deletes_nothing(stores):
    """Identity is the deployment id. A version id that agrees while the deployment id does not is
    a corrupt or foreign row, not a legacy shape -- ``AgentVersion.deployment_id`` has no default.
    """
    vstore, _ = stores
    vid = new_version_id()
    put_version(vstore, "prod_bot", deployment_id="dep-somebody-else", version_id=vid)

    release(record_for("prod_bot", "dep-ours", vid))

    assert names_in(vstore, "prod_bot") == {vid}


def test_a_row_with_no_deployment_id_deletes_nothing(stores):
    vstore, _ = stores
    vid = new_version_id()
    put_version(vstore, "prod_bot", deployment_id="", version_id=vid)

    release(record_for("prod_bot", "dep-1", vid))

    assert names_in(vstore, "prod_bot") == {vid}


def test_an_unproven_name_may_not_delete_a_slot(stores):
    """With only a canvas id to go on, the name is an inference.

    A record that predates ``friendly_runtime_name`` and whose runtime name cannot be correlated
    with its version id gets a sanitized canvas id -- good enough to look something up, not good
    enough to delete under. Deleting our own row under the key is the independent proof; with no
    such row, the slot stays.
    """
    _, sstore = stores
    sstore.upsert(RuntimeSlots(runtime_name="guess_bot", owner_sub=CALLER, production_version_id="v-unknown"))

    release({"deployment_id": "dep-1", "node_id": "guess-bot", "runtime_id": "rt-1"})

    assert sstore.get("guess_bot") is not None


# ---------------------------------------------------------------------------
# The name must come from something this deployment recorded
# ---------------------------------------------------------------------------


def test_an_imported_runtime_releases_nothing(stores):
    """Import persists the AWS ``agentRuntimeName`` verbatim and writes no versions or slots row.

    Those names routinely contain an underscore -- adopting ``my_agent`` used to derive the key
    ``my``, which is some other agent entirely. There is nothing to release for an imported
    runtime, so the honest answer is to touch nothing.
    """
    vstore, sstore = stores
    mine = put_version(vstore, "my", deployment_id="dep-my")
    sstore.upsert(RuntimeSlots(runtime_name="my", owner_sub=CALLER, production_version_id=mine))

    release(
        {
            "deployment_id": "dep-import",
            "user_id": CALLER,
            "imported": True,
            "agentcore_runtime_name": "my_agent",
            "runtime_id": "rt-imported",
            "workflow_id": "imported-abc123",
        }
    )

    assert names_in(vstore, "my") == {mine}
    assert sstore.get("my") is not None


@pytest.mark.parametrize("suffix", ["zzzzzzzz", "1234567", "agent", "deadbeef"])
def test_a_suffix_that_is_not_this_records_version_is_not_stripped(stores, suffix):
    """The strip is only a derivation when the suffix is the one THIS record's version produced.

    An 8-hex-looking tail proves nothing by itself: real adopted runtimes in this account are
    named like ``Omar1_8fb9892d``. ``deadbeef`` is in this list for exactly that reason -- it
    satisfies the shape and still must not be believed.
    """
    vstore, sstore = stores
    mine = put_version(vstore, "my", deployment_id="dep-my")
    sstore.upsert(RuntimeSlots(runtime_name="my", owner_sub=CALLER, production_version_id=mine))
    other = put_version(vstore, "my_agent", deployment_id="dep-other", owner=OTHER)

    release(
        {
            "deployment_id": "dep-1",
            "user_id": CALLER,
            "agentcore_runtime_name": f"my_agent_{suffix}",
            "version_id": new_version_id(),
            "runtime_id": "rt-1",
        }
    )

    assert names_in(vstore, "my") == {mine}
    assert names_in(vstore, "my_agent") == {other}
    assert sstore.get("my") is not None


def test_a_correlated_suffix_is_stripped(stores):
    """The control for the test above: with no persisted name, a suffix that DOES match this
    record's version id is the real key, and the release has to work from it -- that is the whole
    legacy population of records written before the field existed."""
    vstore, sstore = stores
    v1 = put_version(vstore, "old_bot", deployment_id="dep-1")
    sstore.upsert(RuntimeSlots(runtime_name="old_bot", owner_sub=CALLER, production_version_id=v1))

    release(
        {
            "deployment_id": "dep-1",
            "user_id": CALLER,
            "agentcore_runtime_name": f"old_bot_{short_version_suffix(v1)}",
            "version_id": v1,
            "runtime_id": "rt-dep-1",
        }
    )

    assert names_in(vstore, "old_bot") == set()
    assert sstore.get("old_bot") is None


# ---------------------------------------------------------------------------
# A claim may not be released while the thing it protects is alive
# ---------------------------------------------------------------------------


def test_a_retained_runtime_keeps_its_name_locked(stores):
    """If the AWS runtime survived teardown, releasing the name hands it to another tenant while
    ours is still serving -- and strips the rows its own owner needs to manage it. A locked name
    is recoverable; that is not."""
    vstore, sstore = stores
    v1 = put_version(vstore, "kept_bot", deployment_id="dep-1")
    sstore.upsert(RuntimeSlots(runtime_name="kept_bot", owner_sub=CALLER, production_version_id=v1))

    messages = release(record_for("kept_bot", "dep-1", v1), runtime_may_still_live=True)

    assert names_in(vstore, "kept_bot") == {v1}
    assert sstore.get("kept_bot") is not None
    assert any("kept locked" in m for m in messages)


def test_a_deployment_that_never_had_a_runtime_still_releases_its_own_claim(stores):
    """The exception, and it is the F-82 case: a deploy that failed before creating a runtime has
    nothing alive to protect, and its pending row is the permanent lock. A retention flag raised
    by some unrelated resource type must not keep that name locked for good."""
    vstore, sstore = stores
    v1 = put_version(vstore, "early_fail", deployment_id="dep-1", status="pending")
    sstore.upsert(RuntimeSlots(runtime_name="early_fail", owner_sub=CALLER))

    release(
        {
            "deployment_id": "dep-1",
            "user_id": CALLER,
            "friendly_runtime_name": "early_fail",
            "version_id": v1,
        },
        runtime_may_still_live=True,
    )

    assert names_in(vstore, "early_fail") == set()
    assert sstore.get("early_fail") is None


def test_a_trigger_row_keeps_a_no_slot_name_claim_recoverable(stores):
    """The no-slot complete-release path must re-read triggers before deleting the claim.

    A legacy or repair row can exist even when the slot is absent. Releasing the version and name
    would strand that trigger permanently because the owner's trigger API resolves through the
    claim being removed.
    """
    vstore, sstore = stores
    target = put_version(vstore, "trigger_residue_bot", deployment_id="dep-1")
    from app.services.trigger_store import TYPE_CRON, get_trigger_store  # noqa: PLC0415

    tstore = get_trigger_store()
    trigger = tstore.put_trigger_unfenced(
        runtime_name="trigger_residue_bot",
        owner_sub=CALLER,
        type=TYPE_CRON,
        target_runtime_arn=(
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/trigger_residue_bot_1a2b3c4d-AbCdEfGhIj"
        ),
        version_id=target,
        deployment_id="dep-1",
    )

    messages = release(record_for("trigger_residue_bot", "dep-1", target))

    assert vstore.get("trigger_residue_bot", target, consistent=True) is not None
    assert sstore.get("trigger_residue_bot", consistent=True) is None
    assert tstore.get("trigger_residue_bot", trigger.trigger_id, consistent=True) is not None
    assert any("trigger row(s) still registered" in message for message in messages)
    assert not any("Released" in message for message in messages)


def test_no_record_touches_nothing(stores):
    vstore, sstore = stores
    v1 = put_version(vstore, "prod_bot", deployment_id="dep-1")
    sstore.upsert(RuntimeSlots(runtime_name="prod_bot", owner_sub=CALLER, production_version_id=v1))

    assert release(None) == []
    assert names_in(vstore, "prod_bot") == {v1}
    assert sstore.get("prod_bot") is not None


# ---------------------------------------------------------------------------
# A retry after this deployment's own row was already released
# ---------------------------------------------------------------------------


def release_with_outcome(record: dict) -> tuple[list[str], dict]:
    from app.deployment_handler import _release_runtime_name_claim  # noqa: PLC0415

    outcome: dict = {}
    messages = _release_runtime_name_claim(record, CALLER, runtime_may_still_live=False, outcome=outcome)
    return messages, outcome


def test_a_retry_whose_row_is_already_released_retains_nothing(stores):
    """Measured live 2026-10-01: nine older mcp-server-gateway-target versions stayed delete_retained
    on every retry with "kept locked (no version row proves this deployment owns it)". Their first
    attempt had released their own rows; a live version holds the name. No retry can bring the row
    back, so that verdict was a retention that could never clear -- and a delete_retained tombstone
    is a live reference that pins every shared row it lists.
    """
    vstore, sstore = stores
    released = new_version_id()  # this deployment's row, released by its first attempt
    live = put_version(vstore, "gw_bot", deployment_id="dep-live")
    sstore.upsert(RuntimeSlots(runtime_name="gw_bot", owner_sub=CALLER, production_version_id=live))

    messages, outcome = release_with_outcome(record_for("gw_bot", "dep-old", released))

    assert outcome == {"kept_locked": False, "released": True}, messages
    assert names_in(vstore, "gw_bot") == {live}
    slot = sstore.get("gw_bot")
    assert slot is not None and slot.production_version_id == live, "the live version's slot is untouched"
    assert not any("Released" in m for m in messages), "nothing was released, and the message may not claim it"


def test_a_slot_pointer_still_naming_the_released_version_stays_a_retention(stores):
    """A pointer at our version with no row behind it is a dangling pointer of ours."""
    vstore, sstore = stores
    released = new_version_id()
    live = put_version(vstore, "gw_bot", deployment_id="dep-live")
    sstore.upsert(
        RuntimeSlots(
            runtime_name="gw_bot", owner_sub=CALLER, production_version_id=live, previous_production_version_id=released
        )
    )

    messages, outcome = release_with_outcome(record_for("gw_bot", "dep-old", released))

    assert outcome["kept_locked"] is True, messages
    assert sstore.get("gw_bot").previous_production_version_id == released


def test_a_row_carrying_this_deployments_id_stays_a_retention_even_if_unprovable(stores):
    """Our deployment id on a row whose version id disagrees: ours, unprovable, never "nothing left"."""
    vstore, sstore = stores
    live = put_version(vstore, "gw_bot", deployment_id="dep-live")
    put_version(vstore, "gw_bot", deployment_id="dep-old")  # a row of ours under a different version id
    sstore.upsert(RuntimeSlots(runtime_name="gw_bot", owner_sub=CALLER, production_version_id=live))

    messages, outcome = release_with_outcome(record_for("gw_bot", "dep-old", new_version_id()))

    assert outcome["kept_locked"] is True, messages
    assert len(names_in(vstore, "gw_bot")) == 2


def test_with_no_live_version_an_orphan_slot_stays_a_retention(stores):
    """Nobody holds the name: the slot is an orphan only an operator can clear (see the proven-name test)."""
    _, sstore = stores
    sstore.upsert(RuntimeSlots(runtime_name="gw_bot", owner_sub=CALLER, production_version_id="v-gone"))

    messages, outcome = release_with_outcome(record_for("gw_bot", "dep-old", new_version_id()))

    assert outcome["kept_locked"] is True, messages
    assert sstore.get("gw_bot") is not None
