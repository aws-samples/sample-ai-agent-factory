"""F-81f — a trigger create is a transaction fenced to the claim that authorized it.

THE DEFECT, measured against the real stores by the Codex audit session on the tree before this
change: the owner resolves ``POST /api/runtimes/{name}/triggers`` through the production slot; a
teardown deletes the version row and the slot; the previously-authorized, UNCONDITIONAL
``TriggerStore.create_trigger`` then succeeds; a durable trigger row remains under a name whose slot
is gone; and the owner's own ``_resolve_owned_runtime`` answers 404 for it forever, because it
resolves through the slot that was just removed. Output of that probe::

    {trigger_put_after_claim_deleted: true, owner_can_resolve_afterwards: false, owner_retry_status: 404}

Two writers, neither reading the other's table, have to exclude each other. The join is the slot
row: the create moves its ``trigger_fence`` inside the same transaction that puts the trigger row
and condition-checks the version row, and the teardown release pins the fence it read. Whichever
commits second is cancelled -- in BOTH orderings, and both are tested here, because a fence that
only catches one of them is the original bug with a longer explanation.

Every test drives the real ``TriggerStore``, ``release_name_claim_atomically`` and
``_release_runtime_name_claim`` against moto-backed tables and asserts on the ROWS afterwards.
"Nothing was written" is a claim about tables, and a mocked ``transact_write_items`` cannot make
it -- it also cannot see the double-serialization failure that makes every condition fail for a
reason that has nothing to do with races, which is why the control test comes first.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from unittest.mock import MagicMock

import boto3
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

moto = pytest.importorskip("moto")
from app.models.deployment_models import (  # noqa: E402
    DeploymentState,
    DeploymentStatusEnum,
)
from app.services import agent_versions_store as avs  # noqa: E402
from app.services import runtime_target_context as rtc  # noqa: E402
from app.services import trigger_store as ts  # noqa: E402
from app.services.agent_versions_store import (  # noqa: E402
    AgentVersion,
    AgentVersionsStore,
    NameClaimConflict,
    RuntimeSlots,
    RuntimeSlotsStore,
    release_name_claim_atomically,
)
from app.services.auth import get_caller_sub  # noqa: E402
from app.services.trigger_store import (  # noqa: E402
    TYPE_CRON,
    RuntimeClaim,
    TriggerClaimConflict,
    TriggerStore,
)
from moto import mock_aws  # noqa: E402

REGION = "us-east-1"
NAME = "orders_bot"
OWNER = "sub-alice"
STRANGER = "sub-mallory"
DEPLOYMENT = "d1"
VERSION = "v1"
RUNTIME_ID = "orders_bot_1a2b3c4d-AbCdEfGhIj"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{RUNTIME_ID}"
OTHER_RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/other_bot_9f8e7d6c-ZyXwVuTsRq"
NOW = datetime.now(timezone.utc).isoformat()
CRON = "cron(0 12 * * ? *)"


# ---------------------------------------------------------------------------
# Real tables
# ---------------------------------------------------------------------------


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[AgentVersionsStore, RuntimeSlotsStore, TriggerStore]]:
    """All three tables over moto, wired into the singletons every code path resolves through."""
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
        ddb.create_table(
            TableName="Triggers",
            KeySchema=[
                {"AttributeName": "runtime_name", "KeyType": "HASH"},
                {"AttributeName": "trigger_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "runtime_name", "AttributeType": "S"},
                {"AttributeName": "trigger_id", "AttributeType": "S"},
                {"AttributeName": "owner_sub", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": TriggerStore.GSI_NAME,
                    "KeySchema": [
                        {"AttributeName": "owner_sub", "KeyType": "HASH"},
                        {"AttributeName": "trigger_id", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        vstore = AgentVersionsStore(table_name="AgentVersions", region=REGION)
        sstore = RuntimeSlotsStore(table_name="RuntimeSlots", region=REGION)
        tstore = TriggerStore(table_name="Triggers", region=REGION)
        monkeypatch.setattr(avs, "_versions_store", vstore, raising=False)
        monkeypatch.setattr(avs, "_slots_store", sstore, raising=False)
        monkeypatch.setattr(ts, "_trigger_store", tstore, raising=False)
        # The router reads the runtime's protocol off the owner-checked deployment
        # row before any side effect and fails CLOSED (503) if it cannot. The
        # deploy that wrote seed()'s rows left a succeeded HTTP deployment record
        # for DEPLOYMENT; the teardown these tests race deletes the version and
        # slot rows, NOT that record. Return it so the protocol read passes and the
        # create reaches -- and is cancelled by -- the fence, which is what these
        # tests measure. Clear the account identifiers so the ARN's account is not
        # rejected as foreign (home-account path, no STS).
        deployment = DeploymentState(
            deployment_id=DEPLOYMENT,
            user_id=OWNER,
            status=DeploymentStatusEnum.SUCCEEDED,
            started_at=datetime.now(timezone.utc),
            runtime_id=RUNTIME_ID,
            runtime_arn=RUNTIME_ARN,
            version_id=VERSION,
        )
        dstore = MagicMock()
        dstore.get.return_value = deployment
        monkeypatch.setattr(rtc, "_deployment_store", dstore, raising=False)
        monkeypatch.delenv("STATE_MACHINE_ARN", raising=False)
        monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
        yield vstore, sstore, tstore


def seed(vstore: AgentVersionsStore, sstore: RuntimeSlotsStore) -> None:
    """One succeeded production version under NAME, owned by OWNER -- the shape a deploy leaves."""
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id=VERSION,
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id=DEPLOYMENT,
            agentcore_runtime_name="orders_bot_1a2b3c4d",
            runtime_id=RUNTIME_ID,
            runtime_arn=RUNTIME_ARN,
            status="succeeded",
        )
    )
    sstore.upsert(RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=VERSION, last_promoted_at=NOW))


def resolve(caller: str = OWNER) -> RuntimeClaim:
    """What the trigger API does before any write, through the real resolver."""
    from app.routers.triggers import _resolve_owned_runtime_claim  # noqa: PLC0415

    return _resolve_owned_runtime_claim(NAME, caller)


def resolve_status(caller: str = OWNER) -> int:
    try:
        resolve(caller)
    except HTTPException as exc:
        return exc.status_code
    return 200


def teardown_record() -> dict:
    """The deployment record the teardown holds for the deploy that wrote ``seed``'s rows."""
    return {
        "deployment_id": DEPLOYMENT,
        "user_id": OWNER,
        "friendly_runtime_name": NAME,
        "agentcore_runtime_name": "orders_bot_1a2b3c4d",
        "version_id": VERSION,
        "runtime_id": RUNTIME_ID,
        "node_id": "orders-bot",
    }


def release_through_the_teardown(*, trigger_cleanup_unconfirmed: bool = False) -> list[str]:
    from app.deployment_handler import _release_runtime_name_claim  # noqa: PLC0415

    return _release_runtime_name_claim(
        teardown_record(),
        OWNER,
        runtime_may_still_live=False,
        trigger_cleanup_unconfirmed=trigger_cleanup_unconfirmed,
    )


def create(tstore: TriggerStore, claim: RuntimeClaim, owner: str = OWNER):
    return tstore.create_trigger(claim=claim, owner_sub=owner, type=TYPE_CRON, schedule=CRON)


# ---------------------------------------------------------------------------
# The control: a current claim creates the trigger
# ---------------------------------------------------------------------------


def test_a_current_claim_creates_the_trigger_and_moves_the_fence(stores):
    """The happy path, and the proof the transaction can succeed at all.

    Every refusal test below is compatible with every condition failing for a reason that has
    nothing to do with races -- typed AttributeValues through the resource client do exactly that.
    This is the test that says the value contract is right. It also pins what the create records:
    the runtime name and target come from the claim, and the slot's fence has moved.
    """
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    before = sstore.get(NAME, consistent=True)
    assert before is not None and before.trigger_fence is None, "a fresh slot carries no fence"

    claim = resolve()
    trig = create(tstore, claim)

    rows = tstore.list_for_runtime(NAME, consistent=True)
    assert [t.trigger_id for t in rows] == [trig.trigger_id]
    assert rows[0].target_runtime_arn == RUNTIME_ARN, "the target is the version's, not an argument"
    assert rows[0].owner_sub == OWNER
    after = sstore.get(NAME, consistent=True)
    assert after is not None
    assert after.trigger_fence, "the create must move the slot fence in the same transaction"
    assert after.production_version_id == VERSION and after.owner_sub == OWNER, "and change nothing else"
    assert vstore.get(NAME, VERSION, consistent=True) is not None, "a ConditionCheck writes nothing"

    # A second create is fenced to the NEW slot state, so a stale claim from before it must fail
    # and a fresh one must pass: the fence is a version counter, not a one-shot flag.
    with pytest.raises(TriggerClaimConflict):
        create(tstore, claim)
    create(tstore, resolve())
    assert len(tstore.list_for_runtime(NAME, consistent=True)) == 2


# ---------------------------------------------------------------------------
# Ordering 1: the teardown commits first. The Codex sequence, on the fixed tree.
# ---------------------------------------------------------------------------


def test_a_create_authorized_before_the_teardown_writes_nothing_after_it(stores):
    """The exact probe sequence, expected to come out the other way.

    Owner resolves the production target; the teardown's release deletes the version row and the
    slot in its transaction; the create that was authorized against the old rows must now be
    cancelled, leave no row, and the owner's next resolve is 404 for a runtime that is gone --
    which is honest, because there is no trigger under it either.
    """
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    claim = resolve()  # authorized against live rows

    release_name_claim_atomically(
        NAME,
        delete_version=(VERSION, OWNER, DEPLOYMENT, "succeeded", NOW),
        slot_action="delete",
        slot_expected=claim.slot,
    )
    assert vstore.get(NAME, VERSION, consistent=True) is None and sstore.get(NAME, consistent=True) is None

    with pytest.raises(TriggerClaimConflict):
        create(tstore, claim)

    assert tstore.list_for_runtime(NAME, consistent=True) == [], "trigger_put_after_claim_deleted must be false"
    assert sstore.get(NAME, consistent=True) is None, "a cancelled Update must not have created a slot row"
    assert resolve_status() == 404


def test_a_create_authorized_before_the_real_teardown_release_writes_nothing_after_it(stores):
    """The same ordering through ``_release_runtime_name_claim`` -- the code the Lambda runs."""
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    claim = resolve()

    messages = release_through_the_teardown()
    assert sstore.get(NAME, consistent=True) is None, messages

    with pytest.raises(TriggerClaimConflict):
        create(tstore, claim)
    assert tstore.list_for_runtime(NAME, consistent=True) == []


# ---------------------------------------------------------------------------
# Ordering 2: the create commits first. The release must lose.
# ---------------------------------------------------------------------------


def test_a_create_that_lands_after_the_release_read_cancels_the_release(stores):
    """The release read the slot, then a trigger was registered, then the release wrote.

    Its own trigger enumeration may have run before the create too (it runs after the slot read,
    but the create can land after both). The fence is the only thing left that can see it: the
    create moved ``trigger_fence``, the release pinned the value it read, so the transaction is
    cancelled and BOTH rows survive -- the owner can still resolve, and can still delete the trigger.
    """
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    slot_as_the_release_read_it = sstore.get(NAME, consistent=True)
    assert slot_as_the_release_read_it is not None

    trig = create(tstore, resolve())

    with pytest.raises(NameClaimConflict):
        release_name_claim_atomically(
            NAME,
            delete_version=(VERSION, OWNER, DEPLOYMENT, "succeeded", NOW),
            slot_action="delete",
            slot_expected=slot_as_the_release_read_it,
        )

    assert vstore.get(NAME, VERSION, consistent=True) is not None, "the version row must survive a cancelled release"
    assert sstore.get(NAME, consistent=True) is not None, "the slot -- the owner's handle -- must survive"
    assert resolve_status() == 200, "owner_can_resolve_afterwards must be true"
    # And the handle works: the owner deletes the trigger through the real router.
    client = make_client(OWNER)
    resp = client.delete(f"/api/runtimes/{NAME}/triggers/{trig.trigger_id}")
    assert resp.status_code == 200, resp.text
    assert tstore.get(NAME, trig.trigger_id) is None


def test_the_release_sees_a_trigger_registered_after_the_destroy_enumerated(stores):
    """The window between ``destroy_runtime``'s trigger enumeration and the release.

    The destroy reported "confirmed" (nothing under the name) and then spent time deleting AWS
    resources; the owner registered a trigger meanwhile. The release re-reads the partition after
    its slot read and must keep the name locked, with a message that says why, so the row stays
    deletable through the slot.
    """
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    trig = create(tstore, resolve())

    messages = release_through_the_teardown(trigger_cleanup_unconfirmed=False)

    assert any("trigger row(s) still registered" in m for m in messages), messages
    assert sstore.get(NAME, consistent=True) is not None
    assert vstore.get(NAME, VERSION, consistent=True) is not None
    assert tstore.get(NAME, trig.trigger_id) is not None
    assert resolve_status() == 200


def test_a_row_that_is_positively_a_strangers_does_not_keep_the_name_locked(stores):
    """The cost bound: a colliding friendly name must still be releasable.

    A row with another owner AND a target naming a different runtime is somebody else's residue by
    the same three-way classification the destroy uses; counting it would make a shared name
    unreleasable forever. An unattributable row (no target) is NOT treated that way -- see below.
    """
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    tstore.put_trigger_unfenced(
        runtime_name=NAME, owner_sub=STRANGER, type=TYPE_CRON, target_runtime_arn=OTHER_RUNTIME_ARN, schedule=CRON
    )

    messages = release_through_the_teardown()

    assert sstore.get(NAME, consistent=True) is None, messages
    assert vstore.get(NAME, VERSION, consistent=True) is None


@pytest.mark.parametrize(
    ("owner", "target", "why"),
    [
        (STRANGER, "", "no target: cannot be attributed, may be a legacy row of ours"),
        (OWNER, OTHER_RUNTIME_ARN, "our own row, whatever it targets"),
        (STRANGER, RUNTIME_ARN, "targets OUR runtime: it fires at the ARN being deleted"),
        ("", OTHER_RUNTIME_ARN, "no owner: legacy row, absence is not attribution"),
    ],
)
def test_a_row_that_is_not_positively_foreign_keeps_the_name_locked(stores, owner, target, why):
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    if owner:
        tstore.put_trigger_unfenced(runtime_name=NAME, owner_sub=owner, type=TYPE_CRON, target_runtime_arn=target)
    else:
        # A legacy row has the attribute ABSENT (it is a GSI key, so it cannot be written empty).
        tstore._table.put_item(  # noqa: SLF001
            Item={"runtime_name": NAME, "trigger_id": "legacy-1", "type": TYPE_CRON, "target_runtime_arn": target}
        )

    messages = release_through_the_teardown()

    assert sstore.get(NAME, consistent=True) is not None, f"{why}: {messages}"
    assert any("trigger row(s) still registered" in m for m in messages), messages


def test_an_unreadable_triggers_table_keeps_the_name_locked(stores, monkeypatch):
    """Fail closed: no evidence of "nothing left" is not evidence of nothing left."""
    vstore, sstore, tstore = stores
    seed(vstore, sstore)

    def boom(*_a, **_k):
        raise RuntimeError("ProvisionedThroughputExceededException")

    monkeypatch.setattr(tstore, "list_for_runtime", boom)
    messages = release_through_the_teardown()

    assert sstore.get(NAME, consistent=True) is not None
    assert any("trigger rows could not be read" in m for m in messages), messages


# ---------------------------------------------------------------------------
# The claim pins the whole identity, not just existence
# ---------------------------------------------------------------------------


def test_a_promote_between_the_read_and_the_write_cancels_the_create(stores):
    """The slot still exists and the version row still exists; only the pointer moved.

    The trigger would have recorded v1's target while production is v2 -- a trigger firing at a
    version the tenant just moved traffic off. Pointer and ``last_promoted_at`` are both pinned.
    """
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    claim = resolve()

    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id="v2",
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id="d2",
            agentcore_runtime_name="orders_bot_5e6f7a8b",
            runtime_id="orders_bot_5e6f7a8b-KlMnOpQrSt",
            status="succeeded",
        )
    )
    slot = sstore.get(NAME, consistent=True)
    assert slot is not None
    slot.previous_production_version_id = slot.production_version_id
    slot.production_version_id = "v2"
    slot.last_promoted_at = datetime.now(timezone.utc).isoformat()
    sstore.upsert(slot)

    with pytest.raises(TriggerClaimConflict):
        create(tstore, claim)
    assert tstore.list_for_runtime(NAME, consistent=True) == []
    assert sstore.get(NAME, consistent=True).trigger_fence is None, "a cancelled create moves no fence"


def test_a_version_row_re_put_by_a_retry_cancels_the_create(stores):
    """Same key, same status, fresh ``created_at``: a different claim, by the release's own rule."""
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    claim = resolve()

    fresh = vstore.get(NAME, VERSION, consistent=True)
    assert fresh is not None
    fresh.created_at = datetime.now(timezone.utc).isoformat()
    vstore.put(fresh)

    with pytest.raises(TriggerClaimConflict):
        create(tstore, claim)
    assert tstore.list_for_runtime(NAME, consistent=True) == []


def test_a_claim_is_refused_at_construction_when_its_rows_disagree():
    """The claim type refuses shapes that would make the conditions vacuous or wrong."""
    version = AgentVersion(
        runtime_name=NAME,
        version_id=VERSION,
        owner_sub=OWNER,
        created_at=NOW,
        deployment_id=DEPLOYMENT,
        agentcore_runtime_name="x",
        runtime_id=RUNTIME_ID,
        runtime_arn=RUNTIME_ARN,
        status="succeeded",
    )
    good_slot = RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=VERSION)
    RuntimeClaim(slot=good_slot, version=version, target_runtime_arn=RUNTIME_ARN)  # the control

    # The target is the version's, not an argument: a caller-supplied ARN that the row does not
    # record is refused, and so is a version with nothing to derive a target from.
    with pytest.raises(ValueError):
        RuntimeClaim(slot=good_slot, version=version, target_runtime_arn=OTHER_RUNTIME_ARN)
    with pytest.raises(ValueError):
        RuntimeClaim(slot=good_slot, version=version, target_runtime_arn=RUNTIME_ID)  # the ARN wins when recorded
    id_only = AgentVersion(
        runtime_name=NAME,
        version_id=VERSION,
        owner_sub=OWNER,
        created_at=NOW,
        deployment_id=DEPLOYMENT,
        agentcore_runtime_name="x",
        runtime_id=RUNTIME_ID,
        status="succeeded",
    )
    RuntimeClaim(slot=good_slot, version=id_only, target_runtime_arn=RUNTIME_ID)  # no ARN yet: the id is the target
    targetless = AgentVersion(
        runtime_name=NAME,
        version_id=VERSION,
        owner_sub=OWNER,
        created_at=NOW,
        deployment_id=DEPLOYMENT,
        agentcore_runtime_name="x",
        status="succeeded",
    )
    with pytest.raises(ValueError):
        RuntimeClaim(slot=good_slot, version=targetless, target_runtime_arn=RUNTIME_ARN)

    with pytest.raises(ValueError):
        RuntimeClaim(
            slot=RuntimeSlots(runtime_name=NAME, owner_sub=STRANGER, production_version_id=VERSION),
            version=version,
            target_runtime_arn=RUNTIME_ARN,
        )
    with pytest.raises(ValueError):
        RuntimeClaim(
            slot=RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id="v9"),
            version=version,
            target_runtime_arn=RUNTIME_ARN,
        )
    with pytest.raises(ValueError):
        RuntimeClaim(
            slot=RuntimeSlots(runtime_name=NAME, owner_sub=OWNER, production_version_id=None),
            version=version,
            target_runtime_arn=RUNTIME_ARN,
        )
    with pytest.raises(ValueError):
        RuntimeClaim(slot=good_slot, version=version, target_runtime_arn="")


def test_the_store_refuses_an_owner_that_is_not_the_claims(stores):
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    with pytest.raises(ValueError):
        create(tstore, resolve(), owner=STRANGER)
    assert tstore.list_for_runtime(NAME, consistent=True) == []


# ---------------------------------------------------------------------------
# Through the router
# ---------------------------------------------------------------------------


def make_client(caller_sub: str) -> TestClient:
    from app.routers.triggers import router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_caller_sub] = lambda: caller_sub
    return TestClient(app)


def test_the_router_answers_409_and_writes_nothing_when_the_teardown_wins(stores, monkeypatch):
    """The HTTP shape of ordering 1: the teardown lands between the router's read and its write."""
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    import app.routers.triggers as router_mod  # noqa: PLC0415

    real_resolve = router_mod._resolve_owned_runtime_claim  # noqa: SLF001

    def resolve_then_lose_the_race(runtime_name: str, caller_sub: str) -> RuntimeClaim:
        claim = real_resolve(runtime_name, caller_sub)
        release_through_the_teardown()  # the teardown commits in the window
        return claim

    monkeypatch.setattr(router_mod, "_resolve_owned_runtime_claim", resolve_then_lose_the_race)
    resp = make_client(OWNER).post(f"/api/runtimes/{NAME}/triggers", json={"type": "cron", "schedule": CRON})

    assert resp.status_code == 409, resp.text
    assert tstore.list_for_runtime(NAME, consistent=True) == []
    assert sstore.get(NAME, consistent=True) is None


def test_a_webhook_secret_minted_for_a_lost_race_is_deleted(stores, monkeypatch):
    """The compensation path must run for a conflict too, or every lost race leaks a credential."""
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    import app.routers.triggers as router_mod  # noqa: PLC0415

    real_resolve = router_mod._resolve_owned_runtime_claim  # noqa: SLF001

    def resolve_then_lose_the_race(runtime_name: str, caller_sub: str) -> RuntimeClaim:
        claim = real_resolve(runtime_name, caller_sub)
        release_through_the_teardown()
        return claim

    monkeypatch.setattr(router_mod, "_resolve_owned_runtime_claim", resolve_then_lose_the_race)
    monkeypatch.setenv("APP_AWS_REGION", REGION)
    resp = make_client(OWNER).post(f"/api/runtimes/{NAME}/triggers", json={"type": "webhook"})

    assert resp.status_code == 409, resp.text
    sm = boto3.client("secretsmanager", region_name=REGION)
    remaining = [s["Name"] for s in sm.list_secrets().get("SecretList", [])]
    assert remaining == [], f"a secret survived a cancelled create: {remaining}"
    assert tstore.list_for_runtime(NAME, consistent=True) == []


def test_the_router_never_writes_an_unfenced_trigger():
    """``put_trigger_unfenced`` exists for seeding and repair. No request path may reach it."""
    source = pathlib.Path("src/app/routers/triggers.py").read_text()
    assert "put_trigger_unfenced" not in source
    assert "claim=claim" in source, "the router's create must pass the claim it resolved"
    for stale in ("runtime_name=runtime_name,\n            owner_sub", "target_runtime_arn=target_runtime_arn"):
        assert stale not in source, "the row's name and target come from the claim, never from the router"


# ---------------------------------------------------------------------------
# An older version's release while a live version keeps the name
# ---------------------------------------------------------------------------

OLD_DEPLOYMENT = "d0"
OLD_VERSION = "v0"
OLD_RUNTIME_ID = "orders_bot_0a0b0c0d-OldOldOldO"
OLD_RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{OLD_RUNTIME_ID}"


def seed_an_older_version(vstore: AgentVersionsStore) -> None:
    """An older succeeded version of NAME, by the same owner, beside ``seed``'s live production one."""
    vstore.put(
        AgentVersion(
            runtime_name=NAME,
            version_id=OLD_VERSION,
            owner_sub=OWNER,
            created_at=NOW,
            deployment_id=OLD_DEPLOYMENT,
            agentcore_runtime_name="orders_bot_0a0b0c0d",
            runtime_id=OLD_RUNTIME_ID,
            runtime_arn=OLD_RUNTIME_ARN,
            status="succeeded",
        )
    )


def release_the_older_version() -> tuple[list[str], dict]:
    from app.deployment_handler import _release_runtime_name_claim  # noqa: PLC0415

    outcome: dict = {}
    messages = _release_runtime_name_claim(
        {**teardown_record(), "deployment_id": OLD_DEPLOYMENT, "version_id": OLD_VERSION, "runtime_id": OLD_RUNTIME_ID},
        OWNER,
        runtime_may_still_live=False,
        outcome=outcome,
    )
    return messages, outcome


def test_a_live_versions_trigger_does_not_keep_an_older_versions_release_locked(stores):
    """Measured live 2026-10-01: two older strands_gateway_agent versions stayed delete_retained with
    "kept locked (4 trigger row(s) still registered under it)" -- the four triggers belonged to the
    LIVE version of the same name. While that version holds the name, the slot stays and only the
    older version's own row goes, so the live version's trigger stays deletable through the slot.
    """
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    seed_an_older_version(vstore)
    trigger = tstore.put_trigger_unfenced(
        runtime_name=NAME, owner_sub=OWNER, type=TYPE_CRON, target_runtime_arn=RUNTIME_ARN, schedule=CRON
    )

    messages, outcome = release_the_older_version()

    assert outcome == {"kept_locked": False, "released": True}, messages
    assert vstore.get(NAME, OLD_VERSION, consistent=True) is None, "the older version's own row is released"
    assert vstore.get(NAME, VERSION, consistent=True) is not None, "the live version's row is untouched"
    slot = sstore.get(NAME, consistent=True)
    assert slot is not None and slot.production_version_id == VERSION, "the slot still serves the live version"
    assert tstore.get(NAME, trigger.trigger_id) is not None, "the live version's trigger is untouched"
    assert resolve_status() == 200, "and still deletable through the slot"


@pytest.mark.parametrize(
    ("target", "why"),
    [
        (OLD_RUNTIME_ARN, "aimed at the runtime being deleted: residue of this teardown"),
        ("", "no target: cannot be attributed"),
    ],
)
def test_an_older_versions_own_or_unattributable_trigger_still_keeps_the_name_locked(stores, target, why):
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    seed_an_older_version(vstore)
    tstore.put_trigger_unfenced(runtime_name=NAME, owner_sub=OWNER, type=TYPE_CRON, target_runtime_arn=target)

    messages, outcome = release_the_older_version()

    assert outcome["kept_locked"] is True, f"{why}: {messages}"
    assert any("trigger row(s) still registered" in m for m in messages), messages
    assert vstore.get(NAME, OLD_VERSION, consistent=True) is not None, why
