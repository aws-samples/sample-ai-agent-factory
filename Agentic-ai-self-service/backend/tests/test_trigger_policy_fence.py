"""The trigger row's transaction is bound to the workspace policy it was judged against (plan §3.B).

``trigger_admission`` judges a webhook against a strongly consistent read of the workspace policy;
the read is a snapshot. ``TriggerStore.create_trigger`` therefore takes the ``PolicyFence`` of that
read and adds a ``ConditionCheck`` on the policy item to the same ``TransactWriteItems`` that puts the
trigger row and pins the slot and version rows. Driven against REAL (moto) tables through the real
store, asserting on the ROWS afterwards:

* a current fence lets the create commit;
* a policy changed after the judgement cancels the write (``TriggerPolicyConflict``): no row, fence
  unmoved;
* a policy deleted after the judgement cancels it the same way;
* a stale CLAIM (the runtime moved) still reads as the parent ``TriggerClaimConflict``, so the two
  causes are told apart.
"""

from __future__ import annotations

import boto3
import pytest
from app.services import workspace_policy_service as wps
from app.services.trigger_store import TYPE_CRON, TriggerClaimConflict, TriggerPolicyConflict
from app.services.workspace_policy_service import WorkspacePolicyStore

from tests.test_trigger_create_is_fenced_to_the_claim import CRON, NAME, OWNER, REGION, resolve, seed
from tests.test_trigger_create_is_fenced_to_the_claim import stores as _stores_fixture
from tests.test_workspace_admission import _policy

ORG = "org-1"

#: The claim-fence suite's fixture (it opens the moto context and creates the trigger, version and slot
#: tables); binding it here makes pytest collect it for this module too.
stores = _stores_fixture


@pytest.fixture
def policy_store(stores):  # noqa: ARG001 - depends on the moto context the stores fixture opened
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName="OrgConfig",
        KeySchema=[{"AttributeName": "org_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return WorkspacePolicyStore(table_name="OrgConfig", region=REGION)


def _rows(table_name: str) -> list[dict]:
    return boto3.resource("dynamodb", region_name=REGION).Table(table_name).scan()["Items"]


def _snapshot(pstore: WorkspacePolicyStore):
    pstore.put(ORG, _policy(approval={"medium": [], "high": []}, budgets={"maxAgentMonthlyUsd": 100.5}))
    snapshot = pstore.get_snapshot(ORG, "finance")
    assert snapshot is not None
    return snapshot


def test_the_snapshot_carries_the_exact_stored_item(policy_store):
    policy, fence = _snapshot(policy_store)
    assert policy.workspace_id == "finance"
    assert fence.table_name == "OrgConfig"
    assert fence.key == {"org_id": ORG, "sk": wps.policy_sort_key("finance")}
    assert fence.document == _rows("OrgConfig")[0]["policy"], "the fence is the item as stored, Decimals included"
    assert policy_store.get_snapshot(ORG, "nobody") is None


def test_a_current_policy_fence_lets_the_create_commit(stores, policy_store):
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    _, fence = _snapshot(policy_store)
    trig = tstore.create_trigger(claim=resolve(), owner_sub=OWNER, type=TYPE_CRON, schedule=CRON, policy_fence=fence)
    rows = _rows("Triggers")
    assert [row["trigger_id"] for row in rows] == [trig.trigger_id]
    slot = sstore.get(NAME, consistent=True)
    assert slot is not None and slot.trigger_fence, "the slot fence moved: the whole transaction committed"


def test_a_policy_changed_after_the_judgement_cancels_the_write(stores, policy_store):
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    _, fence = _snapshot(policy_store)
    # The admin tightens the policy between the judgement and the write.
    policy_store.put(
        ORG, _policy(approval={"medium": [], "high": ["g-security"]}, budgets={"maxAgentMonthlyUsd": 100.5})
    )
    with pytest.raises(TriggerPolicyConflict):
        tstore.create_trigger(claim=resolve(), owner_sub=OWNER, type=TYPE_CRON, schedule=CRON, policy_fence=fence)
    assert _rows("Triggers") == []
    slot = sstore.get(NAME, consistent=True)
    assert slot is not None and not slot.trigger_fence, "nothing moved"


def test_a_policy_deleted_after_the_judgement_cancels_the_write(stores, policy_store):
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    _, fence = _snapshot(policy_store)
    policy_store.delete(ORG, "finance")
    with pytest.raises(TriggerPolicyConflict):
        tstore.create_trigger(claim=resolve(), owner_sub=OWNER, type=TYPE_CRON, schedule=CRON, policy_fence=fence)
    assert _rows("Triggers") == []


def test_a_stale_claim_is_the_parent_conflict_not_a_policy_conflict(stores, policy_store):
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    _, fence = _snapshot(policy_store)
    claim = resolve()
    # The runtime moves under the request: a first create moves the slot fence, so the pinned claim is stale.
    tstore.create_trigger(claim=claim, owner_sub=OWNER, type=TYPE_CRON, schedule=CRON)
    with pytest.raises(TriggerClaimConflict) as raised:
        tstore.create_trigger(claim=claim, owner_sub=OWNER, type=TYPE_CRON, schedule=CRON, policy_fence=fence)
    assert not isinstance(raised.value, TriggerPolicyConflict)
    assert len(_rows("Triggers")) == 1


def test_without_a_fence_the_transaction_is_the_three_action_one_it_always_was(stores, policy_store):
    vstore, sstore, tstore = stores
    seed(vstore, sstore)
    policy_store.put(ORG, _policy())
    policy_store.delete(ORG, "finance")  # no policy at all: a flag-off or non-ingress create does not care
    trig = tstore.create_trigger(claim=resolve(), owner_sub=OWNER, type=TYPE_CRON, schedule=CRON)
    assert [row["trigger_id"] for row in _rows("Triggers")] == [trig.trigger_id]
