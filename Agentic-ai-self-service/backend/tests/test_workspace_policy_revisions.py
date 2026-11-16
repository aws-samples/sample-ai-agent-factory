"""Two admins cannot overwrite each other's workspace policy (Codex round-3 finding 11, round-4 finding 12).

``PUT /api/workspaces/{id}/policy`` used to be an unconditional replacement of the whole document, so an
admin saving a form opened before a colleague tightened the policy silently undid the tightening (a
``g-security`` added to the high routing vanished, and an owner holding older approvals could then
attach public ingress without it). Every stored policy now carries a ``PolicyVersion``: an incarnation
minted when the policy is created and a revision counting the writes within it. A write names the
version it was based on (or "none yet") and the store enforces BOTH attributes as a DynamoDB condition in
the same ``PutItem``; a delete followed by a create mints a new incarnation, so a token read before the
deletion cannot name the recreated policy either (round 4: a plain counter restarted at 1 and could).
Driven against REAL (moto) tables through the real store and the real router, asserting on the ROWS and
on the exact HTTP contract (``ETag``, ``If-Match``, ``If-None-Match``, ``412``, ``428``), and on the
trigger gate reading the surviving tightened policy.
"""

from __future__ import annotations

import re
from decimal import Decimal

import boto3
import pytest
from app.routers.workspace_policies import POLICY_REVISION_CONFLICT, PRECONDITION_REQUIRED
from app.services import trigger_admission as ta
from app.services import workspace_policy_service as wps
from app.services.trigger_store import TYPE_WEBHOOK
from app.services.workspace_admission import admit
from app.services.workspace_approval import approval_record
from app.services.workspace_policy_service import (
    LEGACY_VERSION,
    PolicyVersion,
    StoredPolicy,
    WorkspacePolicyError,
    WorkspacePolicyRevisionConflict,
)

from tests.test_trigger_admission import NOW, OWNER, WRITER
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
OPENED = {"medium": ["g-approvers"], "high": ["g-approvers", "g-privacy"]}
TIGHTENED_ROUTING = {"medium": ["g-approvers"], "high": ["g-approvers", "g-privacy", "g-security"]}
DOC = {"workspaceId": WS, "name": "Finance", "approval": OPENED}
TIGHTENED = {**DOC, "approval": TIGHTENED_ROUTING}
#: What a served ETag looks like: a quoted ``<16 hex>.<revision>`` token.
ETAG = re.compile(r'^"[0-9a-f]{16}\.[1-9][0-9]*"$')
INC = "0123456789abcdef"


def _table():
    return boto3.resource("dynamodb", region_name=REGION).Table("OrgConfig")


def _item(workspace_id: str = WS) -> dict | None:
    return (
        _table().get_item(Key={"org_id": ORG, "sk": wps.policy_sort_key(workspace_id)}, ConsistentRead=True).get("Item")
    )


def _version(etag: str) -> PolicyVersion:
    return PolicyVersion.parse(etag.strip('"'))


# --------------------------------------------------------------------------- the version token


def test_a_version_token_round_trips_and_the_legacy_token_is_zero():
    version = PolicyVersion(incarnation=INC, revision=3)
    assert version.token == f"{INC}.3"
    assert PolicyVersion.parse(version.token) == version
    assert PolicyVersion.parse("0") == LEGACY_VERSION and LEGACY_VERSION.token == "0"


@pytest.mark.parametrize(
    "token", ["", "1", "abc.1", f"{INC}.0", f"{INC}.01", f"{INC}", f"{INC}.1.2", "*", f"{INC.upper()}.1"]
)
def test_a_token_this_store_never_issued_does_not_parse(token):
    with pytest.raises(ValueError):
        PolicyVersion.parse(token)


@pytest.mark.parametrize(
    ("incarnation", "revision"), [(None, 1), (INC, 0), ("xyz", 1), (INC[:-1], 1), (INC, -1)], ids=str
)
def test_an_inconsistent_version_cannot_be_built(incarnation, revision):
    with pytest.raises(ValueError):
        PolicyVersion(incarnation=incarnation, revision=revision)


# --------------------------------------------------------------------------- the store


def test_create_then_replace_keeps_the_incarnation_and_counts_revisions(policy_store):
    first = policy_store.put(ORG, _policy(name="v1"), expected=None)
    assert first.policy == _policy(name="v1") and first.version.revision == 1
    assert policy_store.read(ORG, WS) == first
    item = _item()
    assert (item["revision"], item["incarnation"]) == (1, first.version.incarnation)
    second = policy_store.put(ORG, _policy(name="v2"), expected=first.version)
    assert second.version == PolicyVersion(incarnation=first.version.incarnation, revision=2)
    assert policy_store.read(ORG, WS) == StoredPolicy(policy=_policy(name="v2"), version=second.version)
    assert policy_store.get(ORG, WS) == _policy(name="v2")


def test_every_create_mints_a_fresh_unpredictable_incarnation(policy_store):
    seen = set()
    for index in range(5):
        stored = policy_store.put(ORG, _policy(name=f"v{index}"), expected=None)
        seen.add(stored.version.incarnation)
        policy_store.delete(ORG, WS, expected=stored.version)
    assert len(seen) == 5


def test_a_create_over_an_existing_policy_is_a_conflict_that_writes_nothing(policy_store):
    first = policy_store.put(ORG, _policy(name="first"), expected=None)
    with pytest.raises(WorkspacePolicyRevisionConflict) as raised:
        policy_store.put(ORG, _policy(name="second"), expected=None)
    assert (raised.value.expected, raised.value.current) == (None, first.version)
    assert not raised.value.recreated
    assert policy_store.read(ORG, WS) == first


def test_two_admins_the_stale_save_loses_and_the_tightening_survives(policy_store):
    """Admin A opens the policy (revision 1). Admin B adds g-security to the high routing (revision 2).
    A saves a form that only renamed the policy: it must not remove B's security requirement."""
    a_read = policy_store.put(ORG, _policy(name="Finance", approval=OPENED), expected=None)
    b_saved = policy_store.put(ORG, _policy(name="Finance", approval=TIGHTENED_ROUTING), expected=a_read.version)
    assert b_saved.version == PolicyVersion(incarnation=a_read.version.incarnation, revision=2)
    with pytest.raises(WorkspacePolicyRevisionConflict) as raised:
        policy_store.put(ORG, _policy(name="Finance (EU)", approval=OPENED), expected=a_read.version)
    assert (raised.value.expected, raised.value.current) == (a_read.version, b_saved.version)
    assert not raised.value.recreated
    now = policy_store.read(ORG, WS)
    assert now is not None and now.version == b_saved.version
    assert now.policy.approval.high == ["g-approvers", "g-privacy", "g-security"]
    assert now.policy.name == "Finance"


def test_a_policy_deleted_and_recreated_tighter_cannot_be_overwritten_by_a_form_from_before(policy_store):
    """Codex round-4 finding 12: with a plain counter the recreated policy was revision 1 again, so the
    stale form's "revision 1" authorised replacing a different policy than the one it had read. The
    incarnation makes the two tokens differ, and the trigger gate keeps reading the tightened policy."""
    a_read = policy_store.put(ORG, _policy(name="Finance", approval=OPENED), expected=None)
    policy_store.delete(ORG, WS, expected=a_read.version)
    recreated = policy_store.put(ORG, _policy(name="Finance", approval=TIGHTENED_ROUTING), expected=None)
    assert recreated.version.revision == 1 and recreated.version.incarnation != a_read.version.incarnation
    with pytest.raises(WorkspacePolicyRevisionConflict) as raised:
        policy_store.put(ORG, _policy(name="Finance (EU)", approval=OPENED), expected=a_read.version)
    assert (raised.value.expected, raised.value.current) == (a_read.version, recreated.version)
    assert raised.value.recreated
    now = policy_store.read(ORG, WS)
    assert now == recreated
    assert now.policy.approval.high == ["g-approvers", "g-privacy", "g-security"]

    # The owner's approvals date from the policy A read: under the surviving policy a webhook still needs g-security.
    admission = admit(WRITER, a_read.policy).model_dump(by_alias=True)
    approvals = [approval_record("g-approvers", "sub-approver", NOW), approval_record("g-privacy", "sub-privacy", NOW)]
    judged = ta.judge_trigger_attachment(
        TYPE_WEBHOOK,
        admission,
        approvals,
        owner_sub=OWNER,
        policy_lookup=lambda workspace_id: policy_store.get_snapshot(ORG, workspace_id),
    )
    assert judged.outcome == "denied"
    assert [violation.code for violation in judged.violations] == [ta.TRIGGER_RAISES_RISK_TIER]
    assert "g-security" in judged.violations[0].message


def test_replacing_a_policy_that_vanished_is_a_conflict_naming_no_current_version(policy_store):
    first = policy_store.put(ORG, _policy(), expected=None)
    policy_store.delete(ORG, WS, expected=first.version)
    with pytest.raises(WorkspacePolicyRevisionConflict) as raised:
        policy_store.put(ORG, _policy(name="again"), expected=first.version)
    assert (raised.value.expected, raised.value.current) == (first.version, None)
    assert not raised.value.recreated
    assert policy_store.read(ORG, WS) is None


def test_an_item_written_before_versions_reads_as_legacy_and_is_claimed_exactly_once(policy_store):
    legacy = wps.policy_to_item(ORG, _policy(name="legacy"), PolicyVersion(incarnation=INC, revision=1))
    del legacy["revision"]
    del legacy["incarnation"]
    _table().put_item(Item=legacy)
    assert policy_store.read(ORG, WS) == StoredPolicy(policy=_policy(name="legacy"), version=LEGACY_VERSION)
    claimed = policy_store.put(ORG, _policy(name="claimed"), expected=LEGACY_VERSION)
    assert claimed.version.revision == 1 and claimed.version.incarnation is not None
    with pytest.raises(WorkspacePolicyRevisionConflict) as raised:
        policy_store.put(ORG, _policy(name="stale"), expected=LEGACY_VERSION)
    assert (raised.value.expected, raised.value.current) == (LEGACY_VERSION, claimed.version)
    assert policy_store.get(ORG, WS) == _policy(name="claimed")
    # A legacy token after a delete and recreate is just as stale.
    policy_store.delete(ORG, WS, expected=claimed.version)
    recreated = policy_store.put(ORG, _policy(name="recreated"), expected=None)
    with pytest.raises(WorkspacePolicyRevisionConflict):
        policy_store.put(ORG, _policy(name="stale again"), expected=LEGACY_VERSION)
    assert policy_store.read(ORG, WS) == recreated


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda item: item.__setitem__("revision", "7"),
        lambda item: item.__setitem__("revision", -1),
        lambda item: item.__setitem__("revision", Decimal("1.5")),
        lambda item: item.__setitem__("revision", True),
        lambda item: item.__setitem__("revision", 0),
        lambda item: item.pop("incarnation"),
        lambda item: item.pop("revision"),
        lambda item: item.__setitem__("incarnation", "not-hex"),
        lambda item: item.__setitem__("incarnation", 42),
    ],
    ids=[
        "string",
        "negative",
        "fraction",
        "bool",
        "zero",
        "no incarnation",
        "no revision",
        "bad incarnation",
        "numeric incarnation",
    ],
)
def test_a_corrupt_version_attribute_is_refused_not_guessed(policy_store, corrupt):
    item = wps.policy_to_item(ORG, _policy(), PolicyVersion(incarnation=INC, revision=1))
    corrupt(item)
    _table().put_item(Item=item)
    with pytest.raises(WorkspacePolicyError):
        policy_store.read(ORG, WS)


@pytest.mark.parametrize("bad", [-1, 0, 1, True, 2.0, "1", f"{INC}.1"], ids=str)
def test_put_refuses_an_expected_version_that_is_not_a_policy_version(policy_store, bad):
    with pytest.raises(WorkspacePolicyError):
        policy_store.put(ORG, _policy(), expected=bad)
    assert policy_store.read(ORG, WS) is None


def test_the_snapshot_fence_still_binds_to_the_exact_document_whatever_the_version(policy_store):
    policy_store.put(ORG, _policy(name="v1"), expected=None)
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
        policy_store.put(ORG, _policy(name="before"), expected=None)
    response = admin.put(PATH, json=DOC)
    assert response.status_code == 428, response.text
    detail = response.json()["detail"]
    assert detail["code"] == PRECONDITION_REQUIRED
    assert "If-Match" in detail["message"] and "If-None-Match" in detail["message"]
    assert detail["message"].endswith("Nothing was changed.")
    after = policy_store.read(ORG, WS)
    assert (after.policy.name if after else None) == ("before" if existing else None)


def test_create_serves_the_version_as_etag_and_a_second_create_is_412(admin, policy_store):
    created = admin.put(PATH, json=DOC, headers=CREATE)
    assert created.status_code == 200, created.text
    assert ETAG.match(created.headers["ETag"]), created.headers["ETag"]
    assert _version(created.headers["ETag"]) == policy_store.read(ORG, WS).version
    got = admin.get(PATH)
    assert got.status_code == 200 and got.headers["ETag"] == created.headers["ETag"] and got.json() == created.json()
    again = admin.put(PATH, json={**DOC, "name": "Again"}, headers=CREATE)
    assert again.status_code == 412, again.text
    detail = again.json()["detail"]
    assert (detail["code"], detail["workspaceId"], detail["expectedVersion"], detail["currentVersion"]) == (
        POLICY_REVISION_CONFLICT,
        WS,
        None,
        created.headers["ETag"].strip('"'),
    )
    assert "already exists" in detail["message"] and detail["message"].endswith("Nothing was changed.")
    now = policy_store.read(ORG, WS)
    assert now is not None and (now.policy.name, now.version.revision) == ("Finance", 1)


def test_two_admins_through_the_api_the_stale_form_cannot_undo_the_tightening(admin):
    """The round-3 scenario end to end: A reads revision 1; B tightens (revision 2); A's stale save is 412
    and the stored policy still requires g-security for high risk. Re-read, reapply, save succeeds."""
    a_opened = admin.put(PATH, json=DOC, headers=CREATE)
    etag_a = a_opened.headers["ETag"]
    b_saved = admin.put(PATH, json=TIGHTENED, headers={"If-Match": etag_a})
    assert b_saved.status_code == 200, b_saved.text
    etag_b = b_saved.headers["ETag"]
    assert ETAG.match(etag_b) and etag_b != etag_a
    assert _version(etag_b) == PolicyVersion(incarnation=_version(etag_a).incarnation, revision=2)
    a_saved = admin.put(PATH, json={**DOC, "name": "Finance (renamed)"}, headers={"If-Match": etag_a})
    assert a_saved.status_code == 412, a_saved.text
    detail = a_saved.json()["detail"]
    assert (detail["code"], detail["expectedVersion"], detail["currentVersion"]) == (
        POLICY_REVISION_CONFLICT,
        etag_a.strip('"'),
        etag_b.strip('"'),
    )
    assert "changed since it was read (revision 1, now 2)" in detail["message"]
    now = admin.get(PATH)
    assert now.headers["ETag"] == etag_b
    assert now.json()["approval"]["high"] == ["g-approvers", "g-privacy", "g-security"]
    assert now.json()["name"] == "Finance"
    retry = admin.put(PATH, json={**TIGHTENED, "name": "Finance (renamed)"}, headers={"If-Match": now.headers["ETag"]})
    assert retry.status_code == 200, retry.text
    assert _version(retry.headers["ETag"]).revision == 3
    assert admin.get(PATH).json()["approval"]["high"] == ["g-approvers", "g-privacy", "g-security"]


def test_through_the_api_a_form_from_before_a_delete_and_recreate_is_412_and_the_recreated_policy_survives(
    admin, policy_store
):
    """Codex round-4 finding 12 end to end: read, delete, recreate tighter, stale PUT."""
    a_opened = admin.put(PATH, json=DOC, headers=CREATE)
    etag_a = a_opened.headers["ETag"]
    assert admin.delete(PATH, headers={"If-Match": etag_a}).status_code == 204
    recreated = admin.put(PATH, json=TIGHTENED, headers=CREATE)
    assert recreated.status_code == 200, recreated.text
    etag_new = recreated.headers["ETag"]
    assert ETAG.match(etag_new) and etag_new != etag_a, "a recreated policy never reuses a token"
    stale = admin.put(PATH, json={**DOC, "name": "Finance (renamed)"}, headers={"If-Match": etag_a})
    assert stale.status_code == 412, stale.text
    detail = stale.json()["detail"]
    assert (detail["code"], detail["expectedVersion"], detail["currentVersion"]) == (
        POLICY_REVISION_CONFLICT,
        etag_a.strip('"'),
        etag_new.strip('"'),
    )
    assert "deleted and recreated" in detail["message"]
    now = admin.get(PATH)
    assert now.headers["ETag"] == etag_new
    assert now.json()["approval"]["high"] == ["g-approvers", "g-privacy", "g-security"]
    assert now.json()["name"] == "Finance"
    assert policy_store.read(ORG, WS).version == _version(etag_new)


def test_if_match_accepts_the_served_etag_quoted_or_bare(admin):
    created = admin.put(PATH, json=DOC, headers=CREATE)
    token = created.headers["ETag"].strip('"')
    quoted = admin.put(PATH, json={**DOC, "name": "Renamed"}, headers={"If-Match": f'"{token}"'})
    assert quoted.status_code == 200, quoted.text
    bare = admin.put(
        PATH, json={**DOC, "name": "Renamed twice"}, headers={"If-Match": quoted.headers["ETag"].strip('"')}
    )
    assert bare.status_code == 200, bare.text
    assert _version(bare.headers["ETag"]).revision == 3


@pytest.mark.parametrize(
    "headers",
    [
        {"If-Match": "*"},
        {"If-Match": f'W/"{INC}.1"'},
        {"If-Match": "latest"},
        {"If-Match": '"1"'},
        {"If-Match": f'"{INC}.1"', "If-None-Match": "*"},
        {"If-None-Match": f'"{INC}.1"'},
    ],
    ids=["if-match star", "weak validator", "non-token", "bare counter", "both headers", "if-none-match not star"],
)
def test_an_ambiguous_precondition_is_400_and_writes_nothing(admin, policy_store, headers):
    before = policy_store.put(ORG, _policy(name="before"), expected=None)
    response = admin.put(PATH, json=DOC, headers=headers)
    assert response.status_code == 400, response.text
    assert "If-Match" in response.json()["detail"]
    assert policy_store.read(ORG, WS) == before


def test_a_guessed_token_of_the_right_shape_is_412_not_400_and_writes_nothing(admin, policy_store):
    """A forged token is a precondition that does not hold, so the write is refused like any stale one; the
    incarnation is 64 random bits, so guessing a current one is not a strategy."""
    before = policy_store.put(ORG, _policy(name="before"), expected=None)
    response = admin.put(PATH, json=DOC, headers={"If-Match": f'"{INC}.1"'})
    assert response.status_code == 412, response.text
    assert response.json()["detail"]["currentVersion"] == before.version.token
    assert policy_store.read(ORG, WS) == before


def test_a_replace_of_a_vanished_policy_is_412_with_no_current_version(admin, policy_store):
    created = admin.put(PATH, json=DOC, headers=CREATE)
    policy_store.delete(ORG, WS, expected=_version(created.headers["ETag"]))
    response = admin.put(PATH, json=DOC, headers={"If-Match": created.headers["ETag"]})
    assert response.status_code == 412, response.text
    detail = response.json()["detail"]
    assert (detail["expectedVersion"], detail["currentVersion"]) == (created.headers["ETag"].strip('"'), None)
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


# --------------------------------------------------------------------------- deletion (Codex round-5 finding 13)


def test_a_stale_delete_after_a_tightening_is_a_conflict_that_deletes_nothing(policy_store):
    """Admin A opens the delete confirmation at revision 1; B tightens the high routing (revision 2); A
    confirms. The deletion must fail: it would send the workspace's builders to the looser default."""
    a_read = policy_store.put(ORG, _policy(name="Finance", approval=OPENED), expected=None)
    b_saved = policy_store.put(ORG, _policy(name="Finance", approval=TIGHTENED_ROUTING), expected=a_read.version)
    with pytest.raises(WorkspacePolicyRevisionConflict) as raised:
        policy_store.delete(ORG, WS, expected=a_read.version)
    assert (raised.value.expected, raised.value.current) == (a_read.version, b_saved.version)
    assert policy_store.read(ORG, WS) == b_saved
    assert policy_store.read(ORG, WS).policy.approval.high == ["g-approvers", "g-privacy", "g-security"]


def test_a_stale_delete_after_a_recreate_is_a_conflict_that_deletes_nothing(policy_store):
    a_read = policy_store.put(ORG, _policy(name="Finance", approval=OPENED), expected=None)
    policy_store.delete(ORG, WS, expected=a_read.version)
    recreated = policy_store.put(ORG, _policy(name="Finance", approval=TIGHTENED_ROUTING), expected=None)
    with pytest.raises(WorkspacePolicyRevisionConflict) as raised:
        policy_store.delete(ORG, WS, expected=a_read.version)
    assert raised.value.recreated and raised.value.current == recreated.version
    assert policy_store.read(ORG, WS) == recreated


def test_deleting_the_version_that_is_there_removes_it_and_a_second_delete_is_a_conflict(policy_store):
    first = policy_store.put(ORG, _policy(), expected=None)
    policy_store.delete(ORG, WS, expected=first.version)
    assert policy_store.read(ORG, WS) is None
    with pytest.raises(WorkspacePolicyRevisionConflict) as raised:
        policy_store.delete(ORG, WS, expected=first.version)
    assert (raised.value.expected, raised.value.current) == (first.version, None)


def test_a_legacy_item_is_deleted_with_the_legacy_version_exactly_once(policy_store):
    legacy = wps.policy_to_item(ORG, _policy(name="legacy"), PolicyVersion(incarnation=INC, revision=1))
    del legacy["revision"]
    del legacy["incarnation"]
    _table().put_item(Item=legacy)
    policy_store.delete(ORG, WS, expected=LEGACY_VERSION)
    assert policy_store.read(ORG, WS) is None
    recreated = policy_store.put(ORG, _policy(name="recreated"), expected=None)
    with pytest.raises(WorkspacePolicyRevisionConflict):
        policy_store.delete(ORG, WS, expected=LEGACY_VERSION)
    assert policy_store.read(ORG, WS) == recreated


@pytest.mark.parametrize("bad", [None, 1, f"{INC}.1"], ids=str)
def test_delete_refuses_an_expected_version_that_is_not_a_policy_version(policy_store, bad):
    first = policy_store.put(ORG, _policy(), expected=None)
    with pytest.raises(WorkspacePolicyError):
        policy_store.delete(ORG, WS, expected=bad)
    assert policy_store.read(ORG, WS) == first


def test_a_delete_without_a_precondition_is_428_and_deletes_nothing(admin, policy_store):
    before = policy_store.put(ORG, _policy(name="before"), expected=None)
    response = admin.delete(PATH)
    assert response.status_code == 428, response.text
    detail = response.json()["detail"]
    assert detail["code"] == PRECONDITION_REQUIRED and "If-Match" in detail["message"]
    assert detail["message"].endswith("Nothing was deleted.")
    assert policy_store.read(ORG, WS) == before


def test_through_the_api_a_stale_delete_confirmation_is_412_and_the_tightened_policy_keeps_refusing_the_webhook(
    admin, policy_store
):
    """Codex round-5 finding 13 end to end: A reads revision 1 and opens the delete confirmation; B tightens
    to revision 2; A confirms with the old token. Nothing is deleted, the tightened routing stands, and the
    trigger gate under it still refuses a webhook whose approvals predate g-security."""
    a_opened = admin.put(PATH, json=DOC, headers=CREATE)
    etag_a = a_opened.headers["ETag"]
    b_saved = admin.put(PATH, json=TIGHTENED, headers={"If-Match": etag_a})
    stale = admin.delete(PATH, headers={"If-Match": etag_a})
    assert stale.status_code == 412, stale.text
    detail = stale.json()["detail"]
    assert (detail["code"], detail["expectedVersion"], detail["currentVersion"]) == (
        POLICY_REVISION_CONFLICT,
        etag_a.strip('"'),
        b_saved.headers["ETag"].strip('"'),
    )
    assert "changed since it was read" in detail["message"] and detail["message"].endswith("Nothing was deleted.")
    now = admin.get(PATH)
    assert now.status_code == 200 and now.headers["ETag"] == b_saved.headers["ETag"]
    assert now.json()["approval"]["high"] == ["g-approvers", "g-privacy", "g-security"]

    admission = admit(WRITER, _policy(name="Finance", approval=OPENED)).model_dump(by_alias=True)
    approvals = [approval_record("g-approvers", "sub-approver", NOW), approval_record("g-privacy", "sub-privacy", NOW)]
    judged = ta.judge_trigger_attachment(
        TYPE_WEBHOOK,
        admission,
        approvals,
        owner_sub=OWNER,
        policy_lookup=lambda workspace_id: policy_store.get_snapshot(ORG, workspace_id),
    )
    assert judged.outcome == "denied" and "g-security" in judged.violations[0].message

    # Re-read, then delete exactly what is there: gone; the old confirmation can never fire afterwards either.
    assert admin.delete(PATH, headers={"If-Match": now.headers["ETag"]}).status_code == 204
    assert admin.get(PATH).status_code == 404
    again = admin.delete(PATH, headers={"If-Match": now.headers["ETag"]})
    assert again.status_code == 412, again.text
    assert again.json()["detail"]["currentVersion"] is None and "no longer exists" in again.json()["detail"]["message"]


def test_through_the_api_a_delete_confirmation_from_before_a_recreate_is_412(admin, policy_store):
    a_opened = admin.put(PATH, json=DOC, headers=CREATE)
    etag_a = a_opened.headers["ETag"]
    assert admin.delete(PATH, headers={"If-Match": etag_a}).status_code == 204
    recreated = admin.put(PATH, json=TIGHTENED, headers=CREATE)
    stale = admin.delete(PATH, headers={"If-Match": etag_a})
    assert stale.status_code == 412, stale.text
    assert "deleted and recreated" in stale.json()["detail"]["message"]
    assert stale.json()["detail"]["currentVersion"] == recreated.headers["ETag"].strip('"')
    assert admin.get(PATH).headers["ETag"] == recreated.headers["ETag"]


@pytest.mark.parametrize(
    "headers",
    [{"If-Match": "*"}, {"If-Match": 'W/"' + INC + '.1"'}, {"If-Match": "latest"}, {"If-None-Match": "*"}],
    ids=["if-match star", "weak validator", "non-token", "if-none-match"],
)
def test_an_ambiguous_delete_precondition_is_400_or_428_and_deletes_nothing(admin, policy_store, headers):
    before = policy_store.put(ORG, _policy(name="before"), expected=None)
    response = admin.delete(PATH, headers=headers)
    assert response.status_code in (400, 428), response.text
    assert policy_store.read(ORG, WS) == before
