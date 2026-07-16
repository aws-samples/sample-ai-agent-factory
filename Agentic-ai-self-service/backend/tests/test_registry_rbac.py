"""Two-persona approval-workflow RBAC tests for the agent registry.

moto-backed DDB; FastAPI TestClient with get_caller_sub overridden to inject the
caller, and is_registry_admin overridden to simulate admin vs developer WITHOUT
requiring real Cognito groups.

Covers:
  - status defaults to 'approved' on legacy rows (no status attribute)
  - publish sets status='pending'
  - developer cannot approve/reject (403)
  - admin can approve/reject
  - pending entries hidden from non-owner developer (search + get_entry 404)
  - clone gated on approved / owner / admin
  - delete allowed for admin (any entry); non-owner non-admin -> 404
  - PUT by non-admin resets status to 'pending'; admin edit preserves status
"""

from __future__ import annotations

import sys
from typing import Iterator

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from app.routers import registry as registry_router_mod  # noqa: E402
from app.routers.registry import caller_is_admin  # noqa: E402
from app.services import registry_store as rs_mod  # noqa: E402
from app.services.auth import get_caller_sub  # noqa: E402
from app.services.registry_store import (  # noqa: E402
    DEFAULT_ORG_ID,
    RegistryEntry,
    RegistryStore,
)


def _create_table() -> None:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="AgentRegistry",
        KeySchema=[
            {"AttributeName": "org_id", "KeyType": "HASH"},
            {"AttributeName": "agent_slug", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "agent_slug", "AttributeType": "S"},
            {"AttributeName": "owner_sub", "AttributeType": "S"},
            {"AttributeName": "visibility", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "owner_sub-agent_slug-index",
                "KeySchema": [
                    {"AttributeName": "owner_sub", "KeyType": "HASH"},
                    {"AttributeName": "agent_slug", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "visibility-agent_slug-index",
                "KeySchema": [
                    {"AttributeName": "visibility", "KeyType": "HASH"},
                    {"AttributeName": "agent_slug", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        _create_table()
        yield


@pytest.fixture
def store(aws: None) -> RegistryStore:
    s = RegistryStore(table_name="AgentRegistry", region="us-east-1")
    rs_mod._registry_store = s
    return s


def _client(caller: str, admin: bool = False) -> TestClient:
    """TestClient with caller_sub and admin status injected via FastAPI
    dependency overrides — no real Cognito groups / aws.event needed.

    The router decides admin status through the ``caller_is_admin`` dependency,
    so overriding it cleanly simulates admin vs developer per client.
    """
    app = FastAPI()
    app.include_router(registry_router_mod.router)
    app.dependency_overrides[get_caller_sub] = lambda: caller
    app.dependency_overrides[caller_is_admin] = lambda: admin
    return TestClient(app)


def _publish(client: TestClient, name: str, visibility: str = "org") -> dict:
    resp = client.post(
        "/api/registry",
        json={
            "display_name": name,
            "visibility": visibility,
            "canvas_snapshot": {"nodes": [{"type": "runtime"}], "edges": []},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Defaults / legacy rows
# ---------------------------------------------------------------------------


def test_legacy_row_without_status_defaults_to_approved(store: RegistryStore):
    """A row written WITHOUT a status attribute deserializes as 'approved'."""
    # Simulate a pre-existing row: write the raw item with no status attribute.
    store._table.put_item(
        Item={
            "org_id": DEFAULT_ORG_ID,
            "agent_slug": "legacy",
            "owner_sub": "alice",
            "display_name": "Legacy Bot",
            "visibility": "org",
            "usage_count": 0,
            "created_at": "2020-01-01T00:00:00+00:00",
            "updated_at": "2020-01-01T00:00:00+00:00",
        }
    )
    loaded = store.get(DEFAULT_ORG_ID, "legacy")
    assert loaded is not None
    assert loaded.status == "approved"

    # And it stays visible to a non-owner developer (does NOT disappear).
    bob = _client("bob", admin=False)
    assert bob.get("/api/registry/legacy").status_code == 200
    listing = bob.get("/api/registry?scope=all").json()
    assert any(e["agent_slug"] == "legacy" for e in listing)


def test_publish_sets_status_pending(store: RegistryStore):
    alice = _client("alice")
    body = _publish(alice, "Fresh Bot")
    assert body["status"] == "pending"
    assert store.get(DEFAULT_ORG_ID, "fresh-bot").status == "pending"


def test_republish_unchanged_preserves_approved_status(store: RegistryStore):
    """PR #3 review (mNemlaghi): re-publishing an APPROVED entry with the SAME
    canvas must NOT silently reset it to pending (which would un-publish it and
    404 clones until re-approval). Status + review provenance are preserved."""
    alice = _client("alice")
    _publish(alice, "Stable Bot")
    # Admin approves it.
    admin = _client("carol", admin=True)
    approved = admin.post("/api/registry/stable-bot/approve").json()
    assert approved["status"] == "approved"
    assert approved["reviewed_by"]

    # Owner re-publishes the EXACT same canvas (e.g. a metadata refresh / no-op).
    body = alice.post(
        "/api/registry",
        json={
            "display_name": "Stable Bot",
            "visibility": "org",
            "canvas_snapshot": {"nodes": [{"type": "runtime"}], "edges": []},
        },
    ).json()
    assert body["status"] == "approved", "unchanged re-publish must stay approved"
    # Still visible + clonable by a different developer (not un-published).
    bob = _client("bob")
    assert bob.get("/api/registry/stable-bot").status_code == 200
    assert bob.post("/api/registry/stable-bot/clone").status_code == 200


def test_republish_changed_canvas_resets_to_pending(store: RegistryStore):
    """If the canvas snapshot actually changes, the approved blueprint no longer
    matches what was reviewed → reset to pending for re-review."""
    alice = _client("alice")
    _publish(alice, "Evolving Bot")
    _client("carol", admin=True).post("/api/registry/evolving-bot/approve")

    body = alice.post(
        "/api/registry",
        json={
            "display_name": "Evolving Bot",
            "visibility": "org",
            "canvas_snapshot": {"nodes": [{"type": "runtime"}, {"type": "memory"}], "edges": []},
        },
    ).json()
    assert body["status"] == "pending", "a changed canvas must require re-review"


# ---------------------------------------------------------------------------
# Approve / reject RBAC
# ---------------------------------------------------------------------------


def test_developer_cannot_approve(store: RegistryStore):
    _publish(_client("alice"), "Pending Bot")
    # A different developer cannot approve.
    bob = _client("bob", admin=False)
    resp = bob.post("/api/registry/pending-bot/approve")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Requires registry-admin role"
    # Even the OWNER (developer) cannot self-approve.
    alice = _client("alice", admin=False)
    assert alice.post("/api/registry/pending-bot/approve").status_code == 403
    assert store.get(DEFAULT_ORG_ID, "pending-bot").status == "pending"


def test_developer_cannot_reject(store: RegistryStore):
    _publish(_client("alice"), "Pending Bot")
    bob = _client("bob", admin=False)
    resp = bob.post("/api/registry/pending-bot/reject", json={"reason": "no"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Requires registry-admin role"


def test_admin_can_approve(store: RegistryStore):
    _publish(_client("alice"), "Pending Bot")
    admin = _client("admin-user", admin=True)
    resp = admin.post("/api/registry/pending-bot/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["reviewed_by"] == "admin-user"
    assert body["reviewed_at"]
    assert store.get(DEFAULT_ORG_ID, "pending-bot").status == "approved"


def test_admin_can_reject_with_reason(store: RegistryStore):
    _publish(_client("alice"), "Pending Bot")
    admin = _client("admin-user", admin=True)
    resp = admin.post("/api/registry/pending-bot/reject", json={"reason": "off-policy"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["reviewed_by"] == "admin-user"
    assert body["rejection_reason"] == "off-policy"


def test_admin_reject_without_body(store: RegistryStore):
    _publish(_client("alice"), "Pending Bot")
    admin = _client("admin-user", admin=True)
    resp = admin.post("/api/registry/pending-bot/reject")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    assert resp.json()["rejection_reason"] is None


def test_approve_missing_entry_404(store: RegistryStore):
    admin = _client("admin-user", admin=True)
    assert admin.post("/api/registry/does-not-exist/approve").status_code == 404
    assert admin.post("/api/registry/does-not-exist/reject").status_code == 404


# ---------------------------------------------------------------------------
# Visibility of pending entries
# ---------------------------------------------------------------------------


def test_pending_hidden_from_non_owner_developer_search(store: RegistryStore):
    _publish(_client("alice"), "Secret Pending")
    bob = _client("bob", admin=False)
    listing = bob.get("/api/registry?scope=all").json()
    assert all(e["agent_slug"] != "secret-pending" for e in listing)


def test_pending_hidden_from_non_owner_developer_get(store: RegistryStore):
    _publish(_client("alice"), "Secret Pending")
    bob = _client("bob", admin=False)
    assert bob.get("/api/registry/secret-pending").status_code == 404


def test_owner_sees_own_pending(store: RegistryStore):
    alice = _client("alice", admin=False)
    _publish(alice, "Mine Pending")
    # Owner can GET and search their own pending entry.
    assert alice.get("/api/registry/mine-pending").status_code == 200
    listing = alice.get("/api/registry?scope=all").json()
    assert any(e["agent_slug"] == "mine-pending" for e in listing)


def test_admin_sees_pending_in_all_scope(store: RegistryStore):
    _publish(_client("alice"), "Pending Bot")
    admin = _client("admin-user", admin=True)
    listing = admin.get("/api/registry?scope=all").json()
    assert any(e["agent_slug"] == "pending-bot" for e in listing)


def test_admin_pending_queue(store: RegistryStore):
    _publish(_client("alice"), "P1")
    _publish(_client("bob"), "P2")
    admin = _client("admin-user", admin=True)
    listing = admin.get("/api/registry?scope=pending").json()
    slugs = {e["agent_slug"] for e in listing}
    assert {"p1", "p2"} <= slugs


def test_developer_pending_scope_only_own(store: RegistryStore):
    _publish(_client("alice"), "Alice Pending")
    _publish(_client("bob"), "Bob Pending")
    bob = _client("bob", admin=False)
    listing = bob.get("/api/registry?scope=pending").json()
    slugs = {e["agent_slug"] for e in listing}
    assert "bob-pending" in slugs
    assert "alice-pending" not in slugs


def test_approved_entry_visible_to_non_owner(store: RegistryStore):
    _publish(_client("alice"), "Will Approve")
    _client("admin-user", admin=True).post("/api/registry/will-approve/approve")
    bob = _client("bob", admin=False)
    assert bob.get("/api/registry/will-approve").status_code == 200
    listing = bob.get("/api/registry?scope=all").json()
    assert any(e["agent_slug"] == "will-approve" for e in listing)


# ---------------------------------------------------------------------------
# Clone gating
# ---------------------------------------------------------------------------


def test_clone_blocked_on_pending_for_non_owner(store: RegistryStore):
    _publish(_client("alice"), "Pending Clone")
    bob = _client("bob", admin=False)
    assert bob.post("/api/registry/pending-clone/clone").status_code == 404
    # usage_count NOT bumped.
    assert store.get(DEFAULT_ORG_ID, "pending-clone").usage_count == 0


def test_clone_allowed_for_owner_even_pending(store: RegistryStore):
    alice = _client("alice", admin=False)
    _publish(alice, "Owner Clone")
    resp = alice.post("/api/registry/owner-clone/clone")
    assert resp.status_code == 200
    assert store.get(DEFAULT_ORG_ID, "owner-clone").usage_count == 1


def test_clone_allowed_for_admin_even_pending(store: RegistryStore):
    _publish(_client("alice"), "Admin Clone")
    admin = _client("admin-user", admin=True)
    assert admin.post("/api/registry/admin-clone/clone").status_code == 200


def test_clone_allowed_on_approved_for_non_owner(store: RegistryStore):
    _publish(_client("alice"), "Approved Clone")
    _client("admin-user", admin=True).post("/api/registry/approved-clone/approve")
    bob = _client("bob", admin=False)
    resp = bob.post("/api/registry/approved-clone/clone")
    assert resp.status_code == 200
    assert store.get(DEFAULT_ORG_ID, "approved-clone").usage_count == 1


# ---------------------------------------------------------------------------
# Delete RBAC
# ---------------------------------------------------------------------------


def test_admin_can_delete_any_entry(store: RegistryStore):
    _publish(_client("alice"), "Delete Me")
    admin = _client("admin-user", admin=True)
    resp = admin.delete("/api/registry/delete-me")
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert store.get(DEFAULT_ORG_ID, "delete-me") is None


def test_non_owner_non_admin_delete_404(store: RegistryStore):
    _publish(_client("alice"), "Keep Me")
    bob = _client("bob", admin=False)
    assert bob.delete("/api/registry/keep-me").status_code == 404
    assert store.get(DEFAULT_ORG_ID, "keep-me") is not None


def test_owner_can_delete_own(store: RegistryStore):
    alice = _client("alice", admin=False)
    _publish(alice, "Own Delete")
    assert alice.delete("/api/registry/own-delete").status_code == 200
    assert store.get(DEFAULT_ORG_ID, "own-delete") is None


# ---------------------------------------------------------------------------
# PUT re-review reset
# ---------------------------------------------------------------------------


def test_put_by_non_admin_resets_to_pending(store: RegistryStore):
    _publish(_client("alice"), "Edit Me")
    # Approve it first so we can observe the reset.
    _client("admin-user", admin=True).post("/api/registry/edit-me/approve")
    assert store.get(DEFAULT_ORG_ID, "edit-me").status == "approved"

    alice = _client("alice", admin=False)
    resp = alice.put("/api/registry/edit-me", json={"description": "changed"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert store.get(DEFAULT_ORG_ID, "edit-me").status == "pending"
    assert store.get(DEFAULT_ORG_ID, "edit-me").description == "changed"


def test_put_by_admin_preserves_status(store: RegistryStore):
    _publish(_client("alice"), "Admin Edit")
    _client("admin-user", admin=True).post("/api/registry/admin-edit/approve")
    assert store.get(DEFAULT_ORG_ID, "admin-edit").status == "approved"

    # Admin happens to also own a different entry; here admin edits alice's via
    # admin rights. assert_owner is bypassed only when admin, but PUT still
    # requires ownership (assert_owner runs for everyone). So use the owner who
    # is also flagged admin to verify status preservation on admin edits.
    owner_admin = _client("alice", admin=True)
    resp = owner_admin.put("/api/registry/admin-edit", json={"description": "tweak"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert store.get(DEFAULT_ORG_ID, "admin-edit").status == "approved"


# ---------------------------------------------------------------------------
# Detail response carries the canvas snapshot; list does NOT (UI Components tab)
# ---------------------------------------------------------------------------


def test_detail_get_includes_canvas_snapshot_but_list_omits_it(store: RegistryStore):
    """GET /{slug} returns canvas_snapshot (for the detail Components tab); the
    list response omits it (null) to keep the browse-grid payload lean."""
    alice = _client("alice", admin=True)
    _publish(alice, "snap-bot")
    alice.post("/api/registry/snap-bot/approve")

    detail = alice.get("/api/registry/snap-bot")
    assert detail.status_code == 200, detail.text
    snap = detail.json()["canvas_snapshot"]
    assert snap is not None
    assert snap["nodes"] == [{"type": "runtime"}]

    listing = alice.get("/api/registry?scope=all").json()
    row = next(e for e in listing if e["agent_slug"] == "snap-bot")
    # List rows must NOT carry the (potentially large) snapshot.
    assert row["canvas_snapshot"] is None
