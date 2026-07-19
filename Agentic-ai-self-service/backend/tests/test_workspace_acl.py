"""Phase 2 Gap 2E — workspaces + ACL unit tests.

Standalone: no AWS, no boto3, no applied shared edits. Part A exercises the
pure ACL logic (``services/workspace_acl``); Part B exercises the share / list
endpoints (``routers/workspaces``) via FastAPI's TestClient against an
in-memory fake storage injected with ``set_workflow_storage`` and
``app.dependency_overrides[get_caller_sub]``.

The fake workflow object below intentionally does NOT depend on the
``WorkflowDefinition.acl`` shared edit (which the main loop applies serially).
It mimics the relevant surface: ``id``, ``name``, ``owner_sub``, ``acl``, and a
``model_copy(update=...)`` that carries the acl field. This is exactly the
contract the router relies on, so the tests pass before the shared edit lands.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from app.services.auth import _LOCAL_DEV_SUB, get_caller_sub  # noqa: E402
from app.services.workspace_acl import (  # noqa: E402
    Acl,
    add_member,
    can_edit,
    can_manage,
    can_view,
    remove_member,
)

OWNER = "alice"
BOB = "bob"
CAROL = "carol"


# ===========================================================================
# Part A — pure ACL logic (no FastAPI, no storage)
# ===========================================================================


def test_owner_has_full_access_with_empty_acl():
    acl = Acl.normalize(None, owner_sub=OWNER)
    assert can_view(acl, OWNER) is True
    assert can_edit(acl, OWNER) is True
    assert can_manage(acl, OWNER) is True


def test_owner_has_full_access_even_when_acl_dict_is_empty():
    acl = Acl.normalize({}, owner_sub=OWNER)
    assert can_view(acl, OWNER)
    assert can_edit(acl, OWNER)
    assert can_manage(acl, OWNER)


def test_legacy_none_owner_is_invisible_to_everyone():
    acl = Acl.normalize(None, owner_sub=None)
    assert acl.owner_sub is None
    for sub in (OWNER, BOB, CAROL, _LOCAL_DEV_SUB):
        assert can_view(acl, sub) is False
        assert can_edit(acl, sub) is False
        assert can_manage(acl, sub) is False


def test_editor_can_view_and_edit_but_not_manage():
    acl = Acl.normalize({"editors": [BOB]}, owner_sub=OWNER)
    assert can_view(acl, BOB) is True
    assert can_edit(acl, BOB) is True
    assert can_manage(acl, BOB) is False


def test_viewer_can_view_only():
    acl = Acl.normalize({"viewers": [BOB]}, owner_sub=OWNER)
    assert can_view(acl, BOB) is True
    assert can_edit(acl, BOB) is False
    assert can_manage(acl, BOB) is False


def test_stranger_has_no_access():
    acl = Acl.normalize({"viewers": [BOB]}, owner_sub=OWNER)
    assert can_view(acl, CAROL) is False
    assert can_edit(acl, CAROL) is False
    assert can_manage(acl, CAROL) is False


def test_add_member_viewer_then_editor_promotes_no_dup():
    acl = add_member(None, BOB, "viewer", owner_sub=OWNER)
    assert BOB in acl["viewers"]
    assert BOB not in acl["editors"]
    promoted = add_member(acl, BOB, "editor", owner_sub=OWNER)
    assert BOB in promoted["editors"]
    assert BOB not in promoted["viewers"]
    # No duplicates anywhere.
    assert promoted["editors"].count(BOB) == 1


def test_add_member_editor_then_viewer_demotes():
    acl = add_member(None, BOB, "editor", owner_sub=OWNER)
    demoted = add_member(acl, BOB, "viewer", owner_sub=OWNER)
    assert BOB in demoted["viewers"]
    assert BOB not in demoted["editors"]


def test_add_member_unknown_role_raises():
    with pytest.raises(ValueError):
        add_member(None, BOB, "admin", owner_sub=OWNER)


def test_add_member_owner_is_noop():
    acl = add_member(None, OWNER, "editor", owner_sub=OWNER)
    assert OWNER not in acl["editors"]
    assert OWNER not in acl["viewers"]
    # Owner still has full access (implicitly).
    assert can_edit(acl, OWNER, owner_sub=OWNER)


def test_remove_member_is_idempotent():
    acl = add_member(None, BOB, "viewer", owner_sub=OWNER)
    removed = remove_member(acl, BOB, owner_sub=OWNER)
    assert BOB not in removed["viewers"]
    # Removing again is a no-op (no error).
    removed_again = remove_member(removed, BOB, owner_sub=OWNER)
    assert BOB not in removed_again["viewers"]


def test_remove_member_never_removes_owner():
    acl = Acl.normalize({"editors": [BOB]}, owner_sub=OWNER)
    removed = remove_member(acl, OWNER, owner_sub=OWNER)
    # Owner is implicit (not in any list), so this is structurally safe and the
    # owner keeps full access.
    assert can_manage(removed, OWNER, owner_sub=OWNER)
    assert can_edit(removed, OWNER, owner_sub=OWNER)


def test_normalize_scrubs_owner_from_lists():
    acl = Acl.normalize({"editors": [OWNER, BOB], "viewers": [OWNER, CAROL]}, owner_sub=OWNER)
    assert OWNER not in acl.editors
    assert OWNER not in acl.viewers
    assert BOB in acl.editors
    assert CAROL in acl.viewers


def test_normalize_editor_precedence_over_viewer():
    acl = Acl.normalize({"editors": [BOB], "viewers": [BOB]}, owner_sub=OWNER)
    assert BOB in acl.editors
    assert BOB not in acl.viewers


def test_normalize_row_owner_overrides_dict_owner():
    # A malicious/legacy dict claiming a different owner must not escalate.
    acl = Acl.normalize({"owner_sub": "attacker", "editors": []}, owner_sub=OWNER)
    assert acl.owner_sub == OWNER
    assert can_manage(acl, "attacker") is False
    assert can_manage(acl, OWNER) is True


def test_mutators_do_not_mutate_input():
    original = {"owner_sub": OWNER, "editors": [], "viewers": []}
    snapshot = {k: list(v) if isinstance(v, list) else v for k, v in original.items()}
    add_member(original, BOB, "editor", owner_sub=OWNER)
    remove_member(original, BOB, owner_sub=OWNER)
    assert original == snapshot  # unchanged


# ===========================================================================
# Part B — share / list endpoints via TestClient + fake storage
# ===========================================================================


@dataclass
class FakeWorkflow:
    """Minimal stand-in for WorkflowDefinition (no acl shared edit required)."""

    id: str
    name: str
    owner_sub: str | None = None
    acl: dict | None = None

    def model_copy(self, update: dict | None = None) -> FakeWorkflow:
        update = update or {}
        # Deep-ish copy of acl so callers can't alias our stored dict.
        new = replace(self, **update)
        if "acl" in update and isinstance(new.acl, dict):
            new.acl = dict(new.acl)
        return new


class FakeStorage:
    """In-memory storage matching the get/update/list_all contract."""

    def __init__(self) -> None:
        self._rows: dict[str, FakeWorkflow] = {}

    def put(self, wf: FakeWorkflow) -> None:
        self._rows[wf.id] = wf

    def get(self, workflow_id: str) -> FakeWorkflow | None:
        return self._rows.get(workflow_id)

    def update(self, workflow_id: str, wf: FakeWorkflow) -> FakeWorkflow | None:
        if workflow_id not in self._rows:
            return None
        stored = wf.model_copy(update={"id": workflow_id})
        self._rows[workflow_id] = stored
        return stored

    def list_all(self) -> list[FakeWorkflow]:
        return list(self._rows.values())


@pytest.fixture
def storage() -> FakeStorage:
    from app.services.storage import get_workflow_storage, set_workflow_storage

    original = get_workflow_storage()
    fake = FakeStorage()
    set_workflow_storage(fake)
    yield fake
    set_workflow_storage(original)


def _make_app(caller_sub: str) -> FastAPI:
    from app.routers.workspaces import router as workspaces_router

    app = FastAPI()
    app.include_router(workspaces_router)
    app.dependency_overrides[get_caller_sub] = lambda: caller_sub
    return app


def _client(caller_sub: str) -> TestClient:
    return TestClient(_make_app(caller_sub))


def test_owner_shares_viewer_then_appears_in_viewers(storage: FakeStorage):
    storage.put(FakeWorkflow(id="wf1", name="Alice WF", owner_sub=OWNER))
    client = _client(OWNER)
    resp = client.post("/api/workflows/wf1/share", json={"sub": BOB, "role": "viewer"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert BOB in body["viewers"]
    assert BOB not in body["editors"]
    # Persisted on the row.
    assert BOB in storage.get("wf1").acl["viewers"]


def test_shared_viewer_sees_workflow_in_workspaces(storage: FakeStorage):
    storage.put(
        FakeWorkflow(
            id="wf1", name="Alice WF", owner_sub=OWNER, acl={"owner_sub": OWNER, "editors": [], "viewers": [BOB]}
        )
    )
    bob_client = _client(BOB)
    resp = bob_client.get("/api/workspaces")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["workflow_id"] == "wf1"
    assert rows[0]["role"] == "viewer"


def test_viewer_promoted_to_editor_via_second_post(storage: FakeStorage):
    storage.put(FakeWorkflow(id="wf1", name="Alice WF", owner_sub=OWNER))
    owner_client = _client(OWNER)
    owner_client.post("/api/workflows/wf1/share", json={"sub": BOB, "role": "viewer"})
    resp = owner_client.post("/api/workflows/wf1/share", json={"sub": BOB, "role": "editor"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert BOB in body["editors"]
    assert BOB not in body["viewers"]
    # Bob now reports editor role in his workspace list.
    bob_rows = _client(BOB).get("/api/workspaces").json()
    assert bob_rows[0]["role"] == "editor"


def test_unshare_removes_access(storage: FakeStorage):
    storage.put(
        FakeWorkflow(
            id="wf1", name="Alice WF", owner_sub=OWNER, acl={"owner_sub": OWNER, "editors": [], "viewers": [BOB]}
        )
    )
    owner_client = _client(OWNER)
    resp = owner_client.delete(f"/api/workflows/wf1/share/{BOB}")
    assert resp.status_code == 200, resp.text
    assert BOB not in resp.json()["viewers"]
    # Bob no longer sees it.
    bob_rows = _client(BOB).get("/api/workspaces").json()
    assert bob_rows == []


def test_unknown_role_returns_400(storage: FakeStorage):
    storage.put(FakeWorkflow(id="wf1", name="Alice WF", owner_sub=OWNER))
    resp = _client(OWNER).post("/api/workflows/wf1/share", json={"sub": BOB, "role": "admin"})
    # Pydantic Literal rejects "admin" at request validation → 422.
    assert resp.status_code == 422


def test_share_nonexistent_workflow_returns_404(storage: FakeStorage):
    resp = _client(OWNER).post("/api/workflows/missing/share", json={"sub": BOB, "role": "viewer"})
    assert resp.status_code == 404


def test_owner_cannot_share_with_self(storage: FakeStorage):
    storage.put(FakeWorkflow(id="wf1", name="Alice WF", owner_sub=OWNER))
    resp = _client(OWNER).post("/api/workflows/wf1/share", json={"sub": OWNER, "role": "editor"})
    assert resp.status_code == 400


def test_owner_lists_owned_workflow_with_owner_role(storage: FakeStorage):
    storage.put(FakeWorkflow(id="wf1", name="Alice WF", owner_sub=OWNER))
    rows = _client(OWNER).get("/api/workspaces").json()
    assert len(rows) == 1
    assert rows[0]["role"] == "owner"


def test_workspaces_excludes_unrelated_workflows(storage: FakeStorage):
    storage.put(FakeWorkflow(id="wf1", name="Alice WF", owner_sub=OWNER))
    storage.put(FakeWorkflow(id="wf2", name="Carol WF", owner_sub=CAROL))
    bob_rows = _client(BOB).get("/api/workspaces").json()
    assert bob_rows == []  # Bob has no ACL entry on either


# ---------------------------------------------------------------------------
# Cross-tenant isolation (404, not 403)
# ---------------------------------------------------------------------------


def test_non_owner_cannot_share_returns_404(storage: FakeStorage):
    storage.put(FakeWorkflow(id="wf1", name="Alice WF", owner_sub=OWNER))
    resp = _client(CAROL).post("/api/workflows/wf1/share", json={"sub": BOB, "role": "viewer"})
    assert resp.status_code == 404


def test_non_owner_cannot_unshare_returns_404(storage: FakeStorage):
    storage.put(
        FakeWorkflow(
            id="wf1", name="Alice WF", owner_sub=OWNER, acl={"owner_sub": OWNER, "editors": [], "viewers": [BOB]}
        )
    )
    # Carol (a stranger) tries to revoke Bob.
    resp = _client(CAROL).delete(f"/api/workflows/wf1/share/{BOB}")
    assert resp.status_code == 404


def test_shared_editor_cannot_reshare_returns_404(storage: FakeStorage):
    """A shared editor is NOT the owner and must not be able to manage the ACL
    (escalation guard, Bug 122 class)."""
    storage.put(
        FakeWorkflow(
            id="wf1", name="Alice WF", owner_sub=OWNER, acl={"owner_sub": OWNER, "editors": [BOB], "viewers": []}
        )
    )
    # Bob (editor) tries to grant Carol access — assert_owner rejects with 404.
    resp = _client(BOB).post("/api/workflows/wf1/share", json={"sub": CAROL, "role": "editor"})
    assert resp.status_code == 404
    # ACL unchanged.
    assert CAROL not in storage.get("wf1").acl.get("editors", [])
    assert CAROL not in storage.get("wf1").acl.get("viewers", [])


def test_legacy_owner_none_invisible_in_workspaces(storage: FakeStorage):
    storage.put(FakeWorkflow(id="wf1", name="Legacy WF", owner_sub=None, acl=None))
    for caller in (OWNER, BOB, CAROL):
        rows = _client(caller).get("/api/workspaces").json()
        assert rows == [], f"legacy row visible to {caller}"


# ---------------------------------------------------------------------------
# List-filter behaviour for GET /api/workflows (the workflows.py shared edit),
# asserted via can_view directly so it's testable without the applied edit.
# ---------------------------------------------------------------------------


def test_list_filter_includes_owned_and_shared_excludes_others():
    owned = Acl.normalize(None, owner_sub=OWNER)
    shared_editor = Acl.normalize({"editors": [BOB]}, owner_sub=OWNER)
    shared_viewer = Acl.normalize({"viewers": [BOB]}, owner_sub=OWNER)
    unrelated = Acl.normalize(None, owner_sub=CAROL)

    # Bob's GET /api/workflows would include shared_editor + shared_viewer.
    assert can_view(shared_editor, BOB)
    assert can_view(shared_viewer, BOB)
    # Bob does NOT see Alice's purely-owned WF or Carol's WF.
    assert can_view(owned, BOB) is False
    assert can_view(unrelated, BOB) is False
    # Owner always sees own workflow.
    assert can_view(owned, OWNER)


# ---------------------------------------------------------------------------
# Round-trip with a REAL WorkflowDefinition.model_copy to prove the router's
# model_copy(update={"acl": ...}) carries the field even before the shared
# edit (pydantic v2 ignores unknown update keys gracefully under model_config;
# this guards the persist path against a regression).
# ---------------------------------------------------------------------------


def test_real_workflow_model_copy_carries_acl_when_field_exists():
    from app.models import WorkflowDefinition

    fields = WorkflowDefinition.model_fields
    if "acl" not in fields:
        pytest.skip("acl shared edit not applied yet — covered by FakeWorkflow path")
    from datetime import datetime, timezone

    from app.models import Viewport, WorkflowMetadata

    now = datetime.now(timezone.utc)
    wf = WorkflowDefinition(
        id="wf1",
        name="Real WF",
        version="1.0.0",
        metadata=WorkflowMetadata(author="alice", aws_region="us-east-1"),
        viewport=Viewport(x=0, y=0, zoom=1.0),
        created_at=now,
        updated_at=now,
        owner_sub=OWNER,
    )
    acl = add_member(getattr(wf, "acl", None), BOB, "editor", owner_sub=OWNER)
    updated = wf.model_copy(update={"acl": acl})
    assert updated.acl["editors"] == [BOB]


# ---------------------------------------------------------------------------
# Part C — M-1 regression: GET/PUT /api/workflows/{id} must honor the ACL
# (security review 2026-05-29). A shared editor/viewer who appears in the LIST
# endpoint must also be able to GET the workflow by id; an editor must be able
# to PUT it. Owner-only used to 404 them. Denial stays 404 (not 403).
# ---------------------------------------------------------------------------


def _workflows_app(caller_sub: str):
    from app.routers import workflows_router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(workflows_router)
    app.dependency_overrides[get_caller_sub] = lambda: caller_sub
    return TestClient(app)


def _real_wf(wid: str, owner: str, acl=None):
    from datetime import datetime, timezone

    from app.models import Viewport, WorkflowDefinition, WorkflowMetadata

    now = datetime.now(timezone.utc)
    return WorkflowDefinition(
        id=wid,
        name="ACL WF",
        version="1.0.0",
        nodes=[],
        edges=[],
        viewport=Viewport(x=0, y=0, zoom=1.0),
        metadata=WorkflowMetadata(author="alice", aws_region="us-east-1"),
        created_at=now,
        updated_at=now,
        owner_sub=owner,
        acl=acl,
    )


@pytest.fixture
def real_storage():
    """Fake storage that stores real WorkflowDefinition instances."""
    from app.services.storage import get_workflow_storage, set_workflow_storage

    class _Store:
        def __init__(self):
            self._rows = {}

        def put(self, wf):
            self._rows[wf.id] = wf

        def get(self, wid):
            return self._rows.get(wid)

        def update(self, wid, wf):
            if wid not in self._rows:
                return None
            self._rows[wid] = wf
            return wf

        def list_all(self):
            return list(self._rows.values())

        def list_by_owner(self, sub):
            return [w for w in self._rows.values() if getattr(w, "owner_sub", None) == sub]

    original = get_workflow_storage()
    s = _Store()
    set_workflow_storage(s)
    yield s
    set_workflow_storage(original)


def test_m1_shared_editor_can_get_workflow_by_id(real_storage):
    acl = {"owner_sub": OWNER, "editors": [BOB], "viewers": []}
    real_storage.put(_real_wf("wf-m1", OWNER, acl))
    # Bob (editor) GETs by id → 200 (was 404 before the M-1 fix).
    resp = _workflows_app(BOB).get("/api/workflows/wf-m1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == "wf-m1"


def test_m1_shared_viewer_can_get_but_not_put(real_storage):
    acl = {"owner_sub": OWNER, "editors": [], "viewers": [BOB]}
    real_storage.put(_real_wf("wf-m1v", OWNER, acl))
    bob = _workflows_app(BOB)
    # Viewer can GET.
    assert bob.get("/api/workflows/wf-m1v").status_code == 200
    # Viewer cannot PUT → 404 (existence-non-disclosure, not 403).
    put = bob.put("/api/workflows/wf-m1v", json={"name": "hacked"})
    assert put.status_code == 404


def test_m1_editor_can_put(real_storage):
    acl = {"owner_sub": OWNER, "editors": [BOB], "viewers": []}
    real_storage.put(_real_wf("wf-m1e", OWNER, acl))
    put = _workflows_app(BOB).put("/api/workflows/wf-m1e", json={"name": "edited by bob"})
    assert put.status_code == 200, put.text
    assert put.json()["workflow"]["name"] == "edited by bob"


def test_m1_unrelated_user_still_404(real_storage):
    acl = {"owner_sub": OWNER, "editors": [BOB], "viewers": []}
    real_storage.put(_real_wf("wf-m1x", OWNER, acl))
    # Carol is neither owner nor shared → 404 on GET and PUT.
    carol = _workflows_app("carol-sub")
    assert carol.get("/api/workflows/wf-m1x").status_code == 404
    assert carol.put("/api/workflows/wf-m1x", json={"name": "x"}).status_code == 404


def test_m1_editor_cannot_escalate_via_put(real_storage):
    """An editor PUT must not be able to change owner_sub or acl (no field for
    it on WorkflowUpdateRequest) — confirms no self-escalation path."""
    acl = {"owner_sub": OWNER, "editors": [BOB], "viewers": []}
    real_storage.put(_real_wf("wf-m1esc", OWNER, acl))
    _workflows_app(BOB).put(
        "/api/workflows/wf-m1esc",
        json={"name": "edited", "owner_sub": BOB, "acl": {"owner_sub": BOB}},
    )
    # Stored row's owner + acl unchanged (extra fields ignored by the model).
    stored = real_storage.get("wf-m1esc")
    assert stored.owner_sub == OWNER
    assert stored.acl == acl
