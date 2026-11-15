"""Two admins cannot overwrite each other's workspace policy (Codex round-3 finding 11).

``PUT /api/workspaces/{id}/policy`` used to be an unconditional replacement of the whole document, so an
admin saving a form opened before a colleague tightened the policy silently undid the tightening (a
``g-security`` added to the high routing vanished, and an owner holding older approvals could then
attach public ingress without it). Every stored policy now carries a revision; a write names the
revision it was based on (or "none yet") and the store enforces it as a DynamoDB condition in the same
``PutItem``. Driven against REAL (moto) tables through the real store and the real router, asserting on
the ROWS and on the exact HTTP contract (``ETag``, ``If-Match``, ``If-None-Match``, ``412``, ``428``).
"""

from __future__ import annotations

from decimal import Decimal

import boto3
import pytest
from app.routers.workspace_policies import POLICY_REVISION_CONFLICT, PRECONDITION_REQUIRED
from app.services import workspace_policy_service as wps
from app.services.workspace_policy_service import (
    StoredPolicy,
    WorkspacePolicyError,
    WorkspacePolicyRevisionConflict,
)

from tests.test_trigger_create_is_fenced_to_the_claim import REGION
from tests.test_trigger_create_is_fenced_to_the_claim import stores as _stores_fixture
from tests.test_trigger_policy_fence import policy_store as _policy_store_fixture
from tests.test_workspace_admission import _admin_client, _policy

#: The moto context and the org-config table, from the fence suites (binding them collects them here).
stores = _stores_fixture
policy_store = _policy_store_fixture

ORG = wps.DEFAULT_ORG_ID  # the org the router writes to
WS = "finance"  # ``_policy()``'s workspace
PATH = f"/api/workspaces/{WS}/policy"
CREATE = {"If-None-Match": "*"}
DOC = {
    "workspaceId": WS,
    "name": "Finance",
    "approval": {"medium": ["g-approvers"], "high": ["g-approvers", "g-privacy"]},
}
TIGHTENED = {**DOC, "approval": {"medium": ["g-approvers"], "high": ["g-approvers", "g-privacy", "g-security"]}}


def _table():
    return boto3.resource("dynamodb", region_name=REGION).Table("OrgConfig")


def _item(workspace_id: str = WS) -> dict | None:
    return (
        _table().get_item(Key={"org_id": ORG, "sk": wps.policy_sort_key(workspace_id)}, ConsistentRead=True).get("Item")
    )


# --------------------------------------------------------------------------- the store


def test_create_then_replace_counts_revisions_and_reads_them_back(policy_store):
    first = policy_store.put(ORG, _policy(name="v1"), expected_revision=None)
    assert first == StoredPolicy(policy=_policy(name="v1"), revision=1)
    assert policy_store.read(ORG, WS) == first
    assert _item()["revision"] == 1
    second = policy_store.put(ORG, _policy(name="v2"), expected_revision=1)
    assert second.revision == 2
    assert policy_store.read(ORG, WS) == StoredPolicy(policy=_policy(name="v2"), revision=2)
    assert policy_store.get(ORG, WS) == _policy(name="v2")
    assert _item()["revision"] == 2


def test_a_create_over_an_existing_policy_is_a_conflict_that_writes_nothing(policy_store):
    policy_store.put(ORG, _policy(name="first"), expected_revision=None)
    with pytest.raises(WorkspacePolicyRevisionConflict) as raised:
        policy_store.put(ORG, _policy(name="second"), expected_revision=None)
    assert (raised.value.expected_revision, raised.value.current_revision) == (None, 1)
    assert policy_store.read(ORG, WS) == StoredPolicy(policy=_policy(name="first"), revision=1)


def test_two_admins_the_stale_save_loses_and_the_tightening_survives(policy_store):
    """Admin A opens the policy (revision 1). Admin B adds g-security to the high routing (revision 2).
    A saves a form that only renamed the policy: it must not remove B's security requirement."""
    opened = {"medium": ["g-approvers"], "high": ["g-approvers", "g-privacy"]}
    a_read = policy_store.put(ORG, _policy(name="Finance", approval=opened), expected_revision=None)
    tightened = {"medium": ["g-approvers"], "high": ["g-approvers", "g-privacy", "g-security"]}
    assert (
        policy_store.put(ORG, _policy(name="Finance", approval=tightened), expected_revision=a_read.revision).revision
        == 2
    )
    with pytest.raises(WorkspacePolicyRevisionConflict) as raised:
        policy_store.put(ORG, _policy(name="Finance (EU)", approval=opened), expected_revision=a_read.revision)
    assert (raised.value.expected_revision, raised.value.current_revision) == (1, 2)
    now = policy_store.read(ORG, WS)
    assert now is not None and now.revision == 2
    assert now.policy.approval.high == ["g-approvers", "g-privacy", "g-security"]
    assert now.policy.name == "Finance"


def test_replacing_a_policy_that_vanished_is_a_conflict_naming_no_current_revision(policy_store):
    policy_store.put(ORG, _policy(), expected_revision=None)
    policy_store.delete(ORG, WS)
    with pytest.raises(WorkspacePolicyRevisionConflict) as raised:
        policy_store.put(ORG, _policy(name="again"), expected_revision=1)
    assert (raised.value.expected_revision, raised.value.current_revision) == (1, None)
    assert policy_store.read(ORG, WS) is None


def test_an_item_written_before_revisions_reads_as_zero_and_is_claimed_exactly_once(policy_store):
    legacy = wps.policy_to_item(ORG, _policy(name="legacy"), 1)
    del legacy["revision"]
    _table().put_item(Item=legacy)
    assert policy_store.read(ORG, WS) == StoredPolicy(policy=_policy(name="legacy"), revision=0)
    assert policy_store.put(ORG, _policy(name="claimed"), expected_revision=0).revision == 1
    with pytest.raises(WorkspacePolicyRevisionConflict) as raised:
        policy_store.put(ORG, _policy(name="stale"), expected_revision=0)
    assert (raised.value.expected_revision, raised.value.current_revision) == (0, 1)
    assert policy_store.get(ORG, WS) == _policy(name="claimed")


@pytest.mark.parametrize("bad", ["7", -1, Decimal("1.5"), True], ids=["string", "negative", "fraction", "bool"])
def test_a_corrupt_revision_attribute_is_refused_not_guessed(policy_store, bad):
    item = wps.policy_to_item(ORG, _policy(), 1)
    item["revision"] = bad
    _table().put_item(Item=item)
    with pytest.raises(WorkspacePolicyError):
        policy_store.read(ORG, WS)


@pytest.mark.parametrize("bad", [-1, True, 2.0, "1"], ids=["negative", "bool", "float", "string"])
def test_put_refuses_an_expected_revision_that_is_not_a_non_negative_integer(policy_store, bad):
    with pytest.raises(WorkspacePolicyError):
        policy_store.put(ORG, _policy(), expected_revision=bad)
    assert policy_store.read(ORG, WS) is None


def test_the_snapshot_fence_still_binds_to_the_exact_document_whatever_the_revision(policy_store):
    policy_store.put(ORG, _policy(name="v1"), expected_revision=None)
    snapshot = policy_store.get_snapshot(ORG, WS)
    assert snapshot is not None
    policy, fence = snapshot
    assert policy == _policy(name="v1")
    assert fence.document == _item()["policy"]


# --------------------------------------------------------------------------- the admin API


@pytest.fixture
def admin(monkeypatch, policy_store):
    monkeypatch.setattr(wps, "_store", policy_store)
    monkeypatch.setattr(wps, "get_workspace_policy_store", lambda: policy_store)
    return _admin_client(monkeypatch, flags={"workspacePolicy": True})


@pytest.mark.parametrize("existing", [False, True], ids=["absent", "present"])
def test_a_put_without_a_precondition_is_428_and_writes_nothing(admin, policy_store, existing):
    if existing:
        policy_store.put(ORG, _policy(name="before"), expected_revision=None)
    response = admin.put(PATH, json=DOC)
    assert response.status_code == 428, response.text
    detail = response.json()["detail"]
    assert detail["code"] == PRECONDITION_REQUIRED
    assert "If-Match" in detail["message"] and "If-None-Match" in detail["message"]
    assert detail["message"].endswith("Nothing was changed.")
    after = policy_store.read(ORG, WS)
    assert (after.policy.name if after else None) == ("before" if existing else None)


def test_create_serves_the_revision_as_etag_and_a_second_create_is_412(admin, policy_store):
    created = admin.put(PATH, json=DOC, headers=CREATE)
    assert created.status_code == 200, created.text
    assert created.headers["ETag"] == '"1"'
    got = admin.get(PATH)
    assert got.status_code == 200 and got.headers["ETag"] == '"1"' and got.json() == created.json()
    again = admin.put(PATH, json={**DOC, "name": "Again"}, headers=CREATE)
    assert again.status_code == 412, again.text
    detail = again.json()["detail"]
    assert (detail["code"], detail["workspaceId"], detail["expectedRevision"], detail["currentRevision"]) == (
        POLICY_REVISION_CONFLICT,
        WS,
        None,
        1,
    )
    assert "already exists" in detail["message"] and detail["message"].endswith("Nothing was changed.")
    now = policy_store.read(ORG, WS)
    assert now is not None and (now.policy.name, now.revision) == ("Finance", 1)


def test_two_admins_through_the_api_the_stale_form_cannot_undo_the_tightening(admin):
    """The round-3 scenario end to end: A reads revision 1; B tightens (revision 2); A's stale save is 412
    and the stored policy still requires g-security for high risk. Re-read, reapply, save succeeds."""
    a_opened = admin.put(PATH, json=DOC, headers=CREATE)
    etag_a = a_opened.headers["ETag"]
    b_saved = admin.put(PATH, json=TIGHTENED, headers={"If-Match": etag_a})
    assert b_saved.status_code == 200, b_saved.text
    assert b_saved.headers["ETag"] == '"2"'
    a_saved = admin.put(PATH, json={**DOC, "name": "Finance (renamed)"}, headers={"If-Match": etag_a})
    assert a_saved.status_code == 412, a_saved.text
    detail = a_saved.json()["detail"]
    assert (detail["code"], detail["expectedRevision"], detail["currentRevision"]) == (POLICY_REVISION_CONFLICT, 1, 2)
    assert "changed since it was read" in detail["message"]
    now = admin.get(PATH)
    assert now.headers["ETag"] == '"2"'
    assert now.json()["approval"]["high"] == ["g-approvers", "g-privacy", "g-security"]
    assert now.json()["name"] == "Finance"
    retry = admin.put(PATH, json={**TIGHTENED, "name": "Finance (renamed)"}, headers={"If-Match": now.headers["ETag"]})
    assert retry.status_code == 200, retry.text
    assert retry.headers["ETag"] == '"3"'
    assert admin.get(PATH).json()["approval"]["high"] == ["g-approvers", "g-privacy", "g-security"]


@pytest.mark.parametrize("value", ['"1"', "1"], ids=["quoted", "bare"])
def test_if_match_accepts_the_served_etag_quoted_or_bare(admin, value):
    admin.put(PATH, json=DOC, headers=CREATE)
    response = admin.put(PATH, json={**DOC, "name": "Renamed"}, headers={"If-Match": value})
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == '"2"'


@pytest.mark.parametrize(
    "headers",
    [
        {"If-Match": "*"},
        {"If-Match": 'W/"1"'},
        {"If-Match": "latest"},
        {"If-Match": '"1"', "If-None-Match": "*"},
        {"If-None-Match": '"1"'},
    ],
    ids=["if-match star", "weak validator", "non-numeric", "both headers", "if-none-match not star"],
)
def test_an_ambiguous_precondition_is_400_and_writes_nothing(admin, policy_store, headers):
    policy_store.put(ORG, _policy(name="before"), expected_revision=None)
    response = admin.put(PATH, json=DOC, headers=headers)
    assert response.status_code == 400, response.text
    assert "If-Match" in response.json()["detail"]
    assert policy_store.read(ORG, WS) == StoredPolicy(policy=_policy(name="before"), revision=1)


def test_a_replace_of_a_vanished_policy_is_412_with_no_current_revision(admin, policy_store):
    created = admin.put(PATH, json=DOC, headers=CREATE)
    policy_store.delete(ORG, WS)
    response = admin.put(PATH, json=DOC, headers={"If-Match": created.headers["ETag"]})
    assert response.status_code == 412, response.text
    detail = response.json()["detail"]
    assert (detail["expectedRevision"], detail["currentRevision"]) == (1, None)
    assert "no longer exists" in detail["message"]
    assert policy_store.read(ORG, WS) is None


def test_the_body_and_path_mismatch_is_judged_before_the_precondition(admin, policy_store):
    response = admin.put("/api/workspaces/marketing/policy", json=DOC)  # no precondition either
    assert response.status_code == 400
    assert "must equal" in response.json()["detail"]
    assert policy_store.read(ORG, WS) is None and policy_store.read(ORG, "marketing") is None


def test_a_conflict_response_echoes_no_store_detail(admin):
    admin.put(PATH, json=DOC, headers=CREATE)
    response = admin.put(PATH, json=DOC, headers=CREATE)
    assert response.status_code == 412
    assert "ConditionalCheck" not in response.text and "botocore" not in response.text
