"""Phase 2 Gap 2A — Agent Registry store + router unit tests.

moto-backed DDB; FastAPI TestClient with get_caller_sub overridden. No live
AWS. These verify CRUD, slug disambiguation (Bug 122 class), and the
visibility model (private / org / public) including cross-tenant isolation.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

moto = pytest.importorskip("moto")
from app.routers import registry as registry_router_mod  # noqa: E402
from app.services import registry_store as rs_mod  # noqa: E402
from app.services.auth import get_caller_sub  # noqa: E402
from app.services.registry_store import (  # noqa: E402
    DEFAULT_ORG_ID,
    RegistryEntry,
    RegistryStore,
    slugify,
)
from moto import mock_aws  # noqa: E402


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
    # Point the module singleton at the moto-backed store so the router uses it.
    rs_mod._registry_store = s
    return s


def _client(caller: str) -> TestClient:
    app = FastAPI()
    app.include_router(registry_router_mod.router)
    app.dependency_overrides[get_caller_sub] = lambda: caller
    return TestClient(app)


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


def test_slugify_basic():
    assert slugify("Stock Research Agent") == "stock-research-agent"
    assert slugify("My!!!Agent  v2") == "my-agent-v2"
    assert slugify("") == "agent"


# ---------------------------------------------------------------------------
# store round-trip
# ---------------------------------------------------------------------------


def test_store_put_get(store: RegistryStore):
    entry = RegistryEntry(
        agent_slug="my-agent",
        owner_sub="alice",
        display_name="My Agent",
        visibility="org",
        canvas_snapshot={"nodes": [], "edges": []},
    )
    store.put(entry)
    loaded = store.get(DEFAULT_ORG_ID, "my-agent")
    assert loaded is not None
    assert loaded.owner_sub == "alice"
    assert loaded.usage_count == 0


def test_increment_usage(store: RegistryStore):
    store.put(RegistryEntry(agent_slug="a", owner_sub="alice", display_name="A"))
    store.increment_usage(DEFAULT_ORG_ID, "a")
    store.increment_usage(DEFAULT_ORG_ID, "a")
    assert store.get(DEFAULT_ORG_ID, "a").usage_count == 2


def test_list_for_owner_gsi(store: RegistryStore):
    store.put(RegistryEntry(agent_slug="a", owner_sub="alice", display_name="A"))
    store.put(RegistryEntry(agent_slug="b", owner_sub="alice", display_name="B"))
    store.put(RegistryEntry(agent_slug="c", owner_sub="bob", display_name="C"))
    assert {e.agent_slug for e in store.list_for_owner("alice")} == {"a", "b"}
    assert {e.agent_slug for e in store.list_for_owner("bob")} == {"c"}


def test_list_public_gsi(store: RegistryStore):
    store.put(RegistryEntry(agent_slug="pub", owner_sub="alice", display_name="P", visibility="public"))
    store.put(RegistryEntry(agent_slug="org", owner_sub="alice", display_name="O", visibility="org"))
    pubs = store.list_public()
    assert {e.agent_slug for e in pubs} == {"pub"}


# ---------------------------------------------------------------------------
# router: publish + visibility
# ---------------------------------------------------------------------------


def test_publish_and_get(store: RegistryStore):
    c = _client("alice")
    resp = c.post(
        "/api/registry",
        json={
            "display_name": "Stock Bot",
            "description": "researches stocks",
            "tags": ["finance"],
            "visibility": "org",
            "canvas_snapshot": {"nodes": [{"type": "runtime"}], "edges": []},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_slug"] == "stock-bot"
    assert body["is_owner"] is True

    got = c.get("/api/registry/stock-bot")
    assert got.status_code == 200
    assert got.json()["display_name"] == "Stock Bot"


def test_private_entry_invisible_to_others(store: RegistryStore):
    alice = _client("alice")
    alice.post(
        "/api/registry",
        json={
            "display_name": "Secret Bot",
            "visibility": "private",
            "canvas_snapshot": {"nodes": [], "edges": []},
        },
    )
    bob = _client("bob")
    # Bob can't GET Alice's private entry.
    assert bob.get("/api/registry/secret-bot").status_code == 404
    # Bob's search doesn't surface it.
    listing = bob.get("/api/registry?scope=all").json()
    assert all(e["agent_slug"] != "secret-bot" for e in listing)


def test_org_entry_visible_to_same_org(store: RegistryStore):
    alice = _client("alice")
    alice.post(
        "/api/registry",
        json={
            "display_name": "Shared Bot",
            "visibility": "org",
            "canvas_snapshot": {"nodes": [], "edges": []},
        },
    )
    # Publishing now creates a 'pending' entry (approval workflow). An admin
    # must approve it before non-owners in the org can see it.
    store.update(DEFAULT_ORG_ID, "shared-bot", {"status": "approved"})
    bob = _client("bob")
    # Same default org → Bob sees it.
    assert bob.get("/api/registry/shared-bot").status_code == 200
    listing = bob.get("/api/registry?scope=all").json()
    assert any(e["agent_slug"] == "shared-bot" for e in listing)
    # But Bob is not the owner.
    assert bob.get("/api/registry/shared-bot").json()["is_owner"] is False


def test_clone_increments_usage_and_returns_snapshot(store: RegistryStore):
    alice = _client("alice")
    alice.post(
        "/api/registry",
        json={
            "display_name": "Clonable",
            "visibility": "org",
            "canvas_snapshot": {"nodes": [{"type": "runtime"}], "edges": []},
        },
    )
    # Publishing now creates a 'pending' entry; approve it so a non-owner can
    # clone it (approval gates reuse).
    store.update(DEFAULT_ORG_ID, "clonable", {"status": "approved"})
    bob = _client("bob")
    resp = bob.post("/api/registry/clonable/clone")
    assert resp.status_code == 200
    assert resp.json()["canvas_snapshot"]["nodes"][0]["type"] == "runtime"
    # usage_count bumped.
    assert store.get(DEFAULT_ORG_ID, "clonable").usage_count == 1


def test_non_owner_cannot_update_or_delete(store: RegistryStore):
    alice = _client("alice")
    alice.post(
        "/api/registry",
        json={
            "display_name": "Alice Only",
            "visibility": "org",
            "canvas_snapshot": {"nodes": [], "edges": []},
        },
    )
    bob = _client("bob")
    # Update → 404 (assert_owner hides existence).
    assert bob.put("/api/registry/alice-only", json={"description": "hax"}).status_code == 404
    # Delete → 404.
    assert bob.delete("/api/registry/alice-only").status_code == 404
    # Entry intact + unchanged.
    assert store.get(DEFAULT_ORG_ID, "alice-only").description == ""


def test_publish_slug_collision_disambiguates(store: RegistryStore):
    # Alice publishes "dup".
    _client("alice").post(
        "/api/registry",
        json={"display_name": "Dup", "visibility": "org", "canvas_snapshot": {}},
    )
    # Bob publishes the same display name → must NOT overwrite Alice's entry.
    bob_resp = _client("bob").post(
        "/api/registry",
        json={"display_name": "Dup", "visibility": "org", "canvas_snapshot": {}},
    )
    assert bob_resp.status_code == 200
    assert bob_resp.json()["agent_slug"] != "dup"  # disambiguated
    # Alice's original entry is untouched + still owned by Alice.
    alice_entry = store.get(DEFAULT_ORG_ID, "dup")
    assert alice_entry is not None
    assert alice_entry.owner_sub == "alice"


def test_owner_can_republish_same_slug(store: RegistryStore):
    alice = _client("alice")
    alice.post(
        "/api/registry",
        json={"display_name": "Mine", "visibility": "org", "canvas_snapshot": {"v": 1}},
    )
    # Re-publish same name as same owner → overwrites in place, same slug.
    resp2 = alice.post(
        "/api/registry",
        json={"display_name": "Mine", "visibility": "public", "canvas_snapshot": {"v": 2}},
    )
    assert resp2.json()["agent_slug"] == "mine"
    assert resp2.json()["visibility"] == "public"
