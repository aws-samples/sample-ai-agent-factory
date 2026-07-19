"""Phase 2 Gap 2D — human-in-the-loop store + router unit tests.

moto-backed DDB; FastAPI TestClient with get_caller_sub overridden. No live
AWS and no applied shared edits required. These verify the store round-trip,
the owner_sub-GSI pending queue isolation (alice vs bob), the PENDING ->
APPROVED/REJECTED transition, double-decide 409, and cross-tenant 404 (the
row stays PENDING).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

# moto is a transitive test dependency; skip if unavailable rather than
# breaking the rest of the test suite.
moto = pytest.importorskip("moto")
from app.routers import hitl as hitl_router_mod  # noqa: E402
from app.services import hitl_store as hs_mod  # noqa: E402
from app.services.auth import get_caller_sub  # noqa: E402
from app.services.hitl_store import (  # noqa: E402
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    HitlNotPending,
    HitlRequestsStore,
    new_request_id,
)
from moto import mock_aws  # noqa: E402  — import after the importorskip

TABLE_NAME = "HitlRequests"


def _create_table() -> None:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "runtime_id", "KeyType": "HASH"},
            {"AttributeName": "request_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "runtime_id", "AttributeType": "S"},
            {"AttributeName": "request_id", "AttributeType": "S"},
            {"AttributeName": "owner_sub", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "owner_sub-request_id-index",
                "KeySchema": [
                    {"AttributeName": "owner_sub", "KeyType": "HASH"},
                    {"AttributeName": "request_id", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        _create_table()
        yield


@pytest.fixture
def store(aws: None) -> HitlRequestsStore:
    s = HitlRequestsStore(table_name=TABLE_NAME, region="us-east-1")
    # Point the module singleton at the moto-backed store so the router uses it.
    hs_mod._hitl_store = s
    return s


def _client(caller: str) -> TestClient:
    app = FastAPI()
    app.include_router(hitl_router_mod.router)
    app.dependency_overrides[get_caller_sub] = lambda: caller
    return TestClient(app)


# ---------------------------------------------------------------------------
# new_request_id invariants
# ---------------------------------------------------------------------------


def test_new_request_id_is_32_lowercase_hex():
    rid = new_request_id()
    assert len(rid) == 32
    assert all(c in "0123456789abcdef" for c in rid)


def test_request_ids_sort_by_creation_time_across_ms_boundary():
    early = new_request_id()
    time.sleep(0.005)
    late = new_request_id()
    assert early < late, f"{early} should sort before {late}"


# ---------------------------------------------------------------------------
# store round-trip
# ---------------------------------------------------------------------------


def test_create_request_get_round_trip(store: HitlRequestsStore):
    req = store.create_request(
        runtime_id="rt-1",
        owner_sub="alice",
        action="delete the production database",
        reason="user asked",
    )
    loaded = store.get("rt-1", req.request_id)
    assert loaded is not None
    assert loaded.owner_sub == "alice"
    assert loaded.status == STATUS_PENDING
    assert loaded.action == "delete the production database"
    assert loaded.reason == "user asked"
    assert loaded.created_at > 0
    # TTL is ~24h in the future.
    assert loaded.ttl >= int(time.time()) + 23 * 3600


def test_list_pending_for_owner_gsi_isolation(store: HitlRequestsStore):
    a1 = store.create_request(runtime_id="rt-a", owner_sub="alice", action="a1")
    time.sleep(0.003)
    a2 = store.create_request(runtime_id="rt-a", owner_sub="alice", action="a2")
    store.create_request(runtime_id="rt-b", owner_sub="bob", action="b1")

    alice_pending = store.list_pending_for_owner("alice")
    assert {r.request_id for r in alice_pending} == {a1.request_id, a2.request_id}
    # Newest-first ordering (GSI SK is the sortable request_id).
    assert alice_pending[0].request_id == a2.request_id

    bob_pending = store.list_pending_for_owner("bob")
    assert {r.runtime_id for r in bob_pending} == {"rt-b"}


def test_decided_rows_drop_out_of_pending_queue(store: HitlRequestsStore):
    a1 = store.create_request(runtime_id="rt-a", owner_sub="alice", action="a1")
    a2 = store.create_request(runtime_id="rt-a", owner_sub="alice", action="a2")
    store.decide(
        runtime_id="rt-a",
        request_id=a1.request_id,
        decision=STATUS_APPROVED,
        decided_by="alice",
    )
    pending = store.list_pending_for_owner("alice")
    assert {r.request_id for r in pending} == {a2.request_id}


def test_decide_approved_stamps_comment_and_decided_at(store: HitlRequestsStore):
    req = store.create_request(runtime_id="rt-1", owner_sub="alice", action="x")
    updated = store.decide(
        runtime_id="rt-1",
        request_id=req.request_id,
        decision=STATUS_APPROVED,
        decided_by="alice",
        comment="looks good",
    )
    assert updated.status == STATUS_APPROVED
    assert updated.comment == "looks good"
    assert updated.decided_at is not None
    assert updated.decided_by == "alice"
    # Persisted.
    loaded = store.get("rt-1", req.request_id)
    assert loaded.status == STATUS_APPROVED
    assert loaded.comment == "looks good"


def test_decide_rejected_transition(store: HitlRequestsStore):
    req = store.create_request(runtime_id="rt-1", owner_sub="alice", action="x")
    updated = store.decide(
        runtime_id="rt-1",
        request_id=req.request_id,
        decision=STATUS_REJECTED,
        decided_by="alice",
    )
    assert updated.status == STATUS_REJECTED


def test_double_decide_raises_not_pending(store: HitlRequestsStore):
    req = store.create_request(runtime_id="rt-1", owner_sub="alice", action="x")
    store.decide(
        runtime_id="rt-1",
        request_id=req.request_id,
        decision=STATUS_APPROVED,
        decided_by="alice",
    )
    with pytest.raises(HitlNotPending):
        store.decide(
            runtime_id="rt-1",
            request_id=req.request_id,
            decision=STATUS_REJECTED,
            decided_by="alice",
        )


def test_decide_invalid_decision_raises(store: HitlRequestsStore):
    req = store.create_request(runtime_id="rt-1", owner_sub="alice", action="x")
    with pytest.raises(ValueError):
        store.decide(
            runtime_id="rt-1",
            request_id=req.request_id,
            decision="MAYBE",
            decided_by="alice",
        )


# ---------------------------------------------------------------------------
# router: pending queue
# ---------------------------------------------------------------------------


def test_router_pending_owner_scoped(store: HitlRequestsStore):
    store.create_request(runtime_id="rt-a", owner_sub="alice", action="a1")
    store.create_request(runtime_id="rt-a", owner_sub="alice", action="a2")
    store.create_request(runtime_id="rt-b", owner_sub="bob", action="b1")

    alice = _client("alice")
    resp = alice.get("/api/hitl/pending")
    assert resp.status_code == 200, resp.text
    actions = {r["action"] for r in resp.json()}
    assert actions == {"a1", "a2"}

    bob = _client("bob")
    bob_resp = bob.get("/api/hitl/pending")
    assert {r["action"] for r in bob_resp.json()} == {"b1"}


def test_router_approve_flow(store: HitlRequestsStore):
    req = store.create_request(runtime_id="rt-a", owner_sub="alice", action="ship it")
    alice = _client("alice")
    resp = alice.post(
        f"/api/hitl/{req.request_id}/decision",
        json={"decision": "approve", "comment": "ok", "runtime_id": "rt-a"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == STATUS_APPROVED
    # No longer in the pending queue.
    assert alice.get("/api/hitl/pending").json() == []


def test_router_cross_tenant_decision_404_and_row_untouched(store: HitlRequestsStore):
    req = store.create_request(runtime_id="rt-a", owner_sub="alice", action="secret")
    bob = _client("bob")
    resp = bob.post(
        f"/api/hitl/{req.request_id}/decision",
        json={"decision": "approve", "runtime_id": "rt-a"},
    )
    assert resp.status_code == 404
    # Row stays PENDING — bob's attempt did not mutate alice's request.
    loaded = store.get("rt-a", req.request_id)
    assert loaded.status == STATUS_PENDING


def test_router_decide_missing_row_404(store: HitlRequestsStore):
    alice = _client("alice")
    resp = alice.post(
        f"/api/hitl/{new_request_id()}/decision",
        json={"decision": "approve", "runtime_id": "rt-a"},
    )
    assert resp.status_code == 404


def test_router_double_decide_409(store: HitlRequestsStore):
    req = store.create_request(runtime_id="rt-a", owner_sub="alice", action="x")
    alice = _client("alice")
    first = alice.post(
        f"/api/hitl/{req.request_id}/decision",
        json={"decision": "approve", "runtime_id": "rt-a"},
    )
    assert first.status_code == 200
    second = alice.post(
        f"/api/hitl/{req.request_id}/decision",
        json={"decision": "reject", "runtime_id": "rt-a"},
    )
    assert second.status_code == 409


def test_router_malformed_request_id_400(store: HitlRequestsStore):
    alice = _client("alice")
    # An explicitly invalid charset ('$') trips the _validate_request_id guard.
    resp = alice.post(
        "/api/hitl/bad$id/decision",
        json={"decision": "approve", "runtime_id": "rt-a"},
    )
    assert resp.status_code == 400


def test_router_invalid_decision_value_422(store: HitlRequestsStore):
    req = store.create_request(runtime_id="rt-a", owner_sub="alice", action="x")
    alice = _client("alice")
    resp = alice.post(
        f"/api/hitl/{req.request_id}/decision",
        json={"decision": "maybe", "runtime_id": "rt-a"},
    )
    assert resp.status_code == 422
