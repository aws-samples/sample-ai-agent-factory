"""F-12: an agent or tag cost budget belongs to the caller who wrote it.

``POST /api/cost/budgets`` pinned only ``scope=owner`` to the caller; ``agent`` and ``tag`` keys
were caller-supplied and the row carried no owner, so any ``cost:write`` caller could overwrite or
delete another tenant's budget (``{"scope":"agent","key":"<their runtime>","limit_usd":0.01}``)
and ``GET`` listed every tenant's agent-scope keys -- their runtime names. ``cost_reconcile_step``
consumes these budgets, so the overwrite had teeth.

Fix: the row records ``owner_sub``; a write or delete on someone else's row is a conditional
DynamoDB write that fails and surfaces as 404 (``assert_owner``'s existence-non-disclosure
convention: a 403 would confirm the row exists); the list is filtered to the caller unless they
hold the org-wide ``admin`` scope that ``services/rbac.py`` already defines. Legacy rows with no
owner are invisible to everyone but an admin, exactly as ``assert_owner`` treats a ``None`` owner.
ARCC cnt_dwzZ05hLnqhYXQ names this shape -- authorizing on caller identity alone, with no check of
the caller's relationship to the object -- as the anti-pattern.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from app.routers import cost as cost_router
from app.services import budget_store as bs_mod
from app.services.auth import get_caller_sub
from app.services.budget_store import Budget, BudgetOwnedByAnother, BudgetStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

TABLE = "Budget"
ORG = "default"
ALICE = "alice-sub-1111"
BOB = "bob-sub-2222"


def _create_table() -> None:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "org_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def store() -> Iterator[BudgetStore]:
    with mock_aws():
        _create_table()
        s = BudgetStore(table_name=TABLE, region="us-east-1")
        bs_mod._store = s
        yield s
        bs_mod._store = None


class _Client:
    """A TestClient bound to one caller, admin or not."""

    def __init__(self, sub: str, *, admin: bool = False) -> None:
        app = FastAPI()
        app.include_router(cost_router.budgets_router)
        app.dependency_overrides[get_caller_sub] = lambda: sub
        app.dependency_overrides[cost_router._caller_is_budget_admin] = lambda: admin
        self.http = TestClient(app)

    def post(self, scope: str, key: str, limit: float):
        return self.http.post("/api/cost/budgets", json={"scope": scope, "key": key, "limit_usd": limit})

    def delete(self, scope: str, key: str):
        return self.http.delete(f"/api/cost/budgets/{scope}/{key}")

    def keys(self) -> set[tuple[str, str]]:
        r = self.http.get("/api/cost/budgets")
        assert r.status_code == 200, r.text
        return {(b["scope"], b["key"]) for b in r.json()}


@pytest.fixture
def alice(store):
    return _Client(ALICE)


@pytest.fixture
def bob(store):
    return _Client(BOB)


@pytest.fixture
def admin(store):
    return _Client("admin-sub-9999", admin=True)


# --------------------------------------------------------------------------- the row carries its owner


def test_a_written_agent_budget_records_the_caller_as_owner(store, alice):
    assert alice.post("agent", "alice_bot", 20).status_code == 200
    row = store.get(ORG, "agent", "alice_bot")
    assert row is not None and row.owner_sub == ALICE


def test_a_written_owner_budget_is_keyed_and_owned_by_the_caller_whatever_key_was_sent(store, bob):
    """Pre-existing behaviour, kept: the owner scope was never cross-tenant."""
    assert bob.post("owner", ALICE, 5).status_code == 200
    assert store.get(ORG, "owner", ALICE) is None
    mine = store.get(ORG, "owner", BOB)
    assert mine is not None and mine.owner_sub == BOB


# --------------------------------------------------------------------------- cross-tenant write / delete


@pytest.mark.parametrize("scope", ["agent", "tag"])
def test_another_tenant_cannot_overwrite_the_budget(store, alice, bob, scope):
    key = "alice_bot" if scope == "agent" else "team=alice"
    assert alice.post(scope, key, 20).status_code == 200

    r = bob.post(scope, key, 0.01)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Not found"
    row = store.get(ORG, scope, key)
    assert row is not None and row.limit_usd == 20.0 and row.owner_sub == ALICE


@pytest.mark.parametrize("scope", ["agent", "tag"])
def test_another_tenant_cannot_delete_the_budget(store, alice, bob, scope):
    key = "alice_bot" if scope == "agent" else "team=alice"
    assert alice.post(scope, key, 20).status_code == 200

    r = bob.delete(scope, key)
    assert r.status_code == 404, r.text
    assert store.get(ORG, scope, key) is not None


def test_the_owner_can_still_update_and_delete_their_own_budget(store, alice):
    assert alice.post("agent", "alice_bot", 20).status_code == 200
    assert alice.post("agent", "alice_bot", 30).status_code == 200
    assert store.get(ORG, "agent", "alice_bot").limit_usd == 30.0
    assert alice.delete("agent", "alice_bot").status_code == 200
    assert store.get(ORG, "agent", "alice_bot") is None


def test_deleting_a_budget_that_does_not_exist_is_still_an_idempotent_200(store, bob):
    r = bob.delete("agent", "never_existed")
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- the list


def test_the_list_shows_only_the_callers_budgets(store, alice, bob):
    assert alice.post("agent", "alice_bot", 20).status_code == 200
    assert alice.post("tag", "team=alice", 200).status_code == 200
    assert alice.post("owner", ALICE, 50).status_code == 200
    assert bob.post("agent", "bob_bot", 10).status_code == 200

    assert alice.keys() == {("agent", "alice_bot"), ("tag", "team=alice"), ("owner", ALICE)}
    assert bob.keys() == {("agent", "bob_bot")}, "bob must not learn alice's runtime name"


def test_a_legacy_row_with_no_owner_is_invisible_and_unwritable_to_a_tenant(store, alice, bob, admin):
    """Same rule as ``assert_owner``: an un-owned row is not found to every tenant."""
    store.put(Budget(org_id=ORG, scope="agent", key="legacy_bot", limit_usd=7))
    assert store.get(ORG, "agent", "legacy_bot").owner_sub is None

    assert ("agent", "legacy_bot") not in alice.keys()
    assert alice.post("agent", "legacy_bot", 1).status_code == 404
    assert bob.delete("agent", "legacy_bot").status_code == 404
    assert store.get(ORG, "agent", "legacy_bot").limit_usd == 7.0
    assert ("agent", "legacy_bot") in admin.keys()


# --------------------------------------------------------------------------- the admin scope


def test_an_admin_sees_and_may_change_every_budget(store, alice, bob, admin):
    assert alice.post("agent", "alice_bot", 20).status_code == 200
    assert bob.post("agent", "bob_bot", 10).status_code == 200

    assert admin.keys() >= {("agent", "alice_bot"), ("agent", "bob_bot")}
    assert admin.post("agent", "alice_bot", 99).status_code == 200
    row = store.get(ORG, "agent", "alice_bot")
    assert row.limit_usd == 99.0
    assert row.owner_sub == ALICE, "an admin edit does not steal the row"
    assert admin.delete("agent", "bob_bot").status_code == 200
    assert store.get(ORG, "agent", "bob_bot") is None


def test_the_admin_check_is_the_org_wide_admin_scope(monkeypatch):
    """Not a new island: the dependency asks rbac for ``admin``, which only ``g-admins-super``
    and legacy ``org-admin`` hold. ``g-admins-cost`` (cost:read/write) is a tenant here."""
    from starlette.requests import Request

    def _req(groups):
        scope = {
            "type": "http",
            "path": "/api/cost/budgets",
            "headers": [],
            "aws.event": {"requestContext": {"authorizer": {"jwt": {"claims": {"cognito:groups": groups}}}}},
        }
        return Request(scope)

    assert cost_router._caller_is_budget_admin(_req(["g-admins-super"])) is True
    assert cost_router._caller_is_budget_admin(_req(["org-admin"])) is True
    assert cost_router._caller_is_budget_admin(_req(["g-admins-cost"])) is False
    assert cost_router._caller_is_budget_admin(_req(["g-users-default"])) is False
    assert cost_router._caller_is_budget_admin(_req([])) is False


# --------------------------------------------------------------------------- the store's own contract


def test_the_store_write_is_conditional_not_read_then_write(store):
    """The guard is a DynamoDB condition, so two tenants racing on one key cannot both win."""
    store.put_owned(Budget(org_id=ORG, scope="agent", key="shared_name", limit_usd=1), caller_sub=ALICE)
    with pytest.raises(BudgetOwnedByAnother):
        store.put_owned(Budget(org_id=ORG, scope="agent", key="shared_name", limit_usd=2), caller_sub=BOB)
    with pytest.raises(BudgetOwnedByAnother):
        store.delete_owned(ORG, "agent", "shared_name", caller_sub=BOB)
    assert store.get(ORG, "agent", "shared_name").limit_usd == 1.0
    assert store.get(ORG, "agent", "shared_name").owner_sub == ALICE


def test_list_all_is_unchanged_for_the_reconcile_step(store):
    """``cost_reconcile_step`` walks every budget in the org; it is the platform, not a tenant."""
    store.put_owned(Budget(org_id=ORG, scope="agent", key="a", limit_usd=1), caller_sub=ALICE)
    store.put_owned(Budget(org_id=ORG, scope="agent", key="b", limit_usd=1), caller_sub=BOB)
    assert {b.key for b in store.list_all(ORG)} == {"a", "b"}
