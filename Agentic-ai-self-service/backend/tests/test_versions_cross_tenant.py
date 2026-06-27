"""H-1 regression — cross-tenant slot hijack via runtime_name collision.

The AgentVersionsTable + RuntimeSlotsTable use ``runtime_name`` as the
PK, which is a tenant-supplied friendly name. A bug in deployment_handler
that didn't gate writes on ``owner_sub`` would let Tenant B clobber
Tenant A's slot row by deploying with the same friendly name.

Caught by the security review (security-standard-agent, 2026-05-28).
Fix landed in the same commit as this test. See lessons.md Bug 122.

The test exercises the verification helpers directly against an in-memory
moto DDB. We don't go through the full /api/deploy path because that
would need the entire SFN stack mocked; instead we replicate the
ownership check that deployment_handler now performs.
"""

from __future__ import annotations

import sys
from typing import Iterator

import boto3
import pytest

sys.path.insert(0, "src")

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from app.services.agent_versions_store import (  # noqa: E402
    AgentVersion,
    AgentVersionsStore,
    RuntimeSlots,
    RuntimeSlotsStore,
    new_version_id,
)


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        yield


@pytest.fixture
def stores(aws: None) -> tuple[AgentVersionsStore, RuntimeSlotsStore]:
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
    return (
        AgentVersionsStore(table_name="AgentVersions", region="us-east-1"),
        RuntimeSlotsStore(table_name="RuntimeSlots", region="us-east-1"),
    )


def _seed_alice(versions_store, slots_store, runtime_name="alice_bot"):
    """Pretend Alice has already deployed and populated her slot."""
    alice_v1 = new_version_id()
    versions_store.put(
        AgentVersion(
            runtime_name=runtime_name,
            version_id=alice_v1,
            owner_sub="alice",
            created_at="2026-05-28T10:00:00+00:00",
            deployment_id="dep-alice-1",
            agentcore_runtime_name=f"{runtime_name}_aliceaaa",
            runtime_id="alice-runtime",
            status="succeeded",
        )
    )
    slots_store.upsert(
        RuntimeSlots(
            runtime_name=runtime_name,
            owner_sub="alice",
            production_version_id=alice_v1,
        )
    )
    return alice_v1


# ---------------------------------------------------------------------------
# Replicates the deployment_handler ownership check
# ---------------------------------------------------------------------------


def _deploy_handler_ownership_check(
    versions_store: AgentVersionsStore,
    slots_store: RuntimeSlotsStore,
    *,
    friendly_runtime_name: str,
    user_id: str,
) -> None:
    """Mirror of the H-1 fix in deployment_handler.handle_deploy.

    Raises ``PermissionError`` (stand-in for HTTP 409) when the friendly
    name is already owned by a different sub.
    """
    slots = slots_store.get(friendly_runtime_name)
    if slots is not None and slots.owner_sub and slots.owner_sub != user_id:
        raise PermissionError(
            f"Runtime name '{friendly_runtime_name}' is already in use by another tenant."
        )
    versions = versions_store.list_for_runtime(friendly_runtime_name)
    for v in versions:
        if v.owner_sub and v.owner_sub != user_id and (v.status or "pending") in {"pending", "succeeded"}:
            raise PermissionError(
                f"Runtime name '{friendly_runtime_name}' is already in use by another tenant."
            )


def test_alice_can_redeploy_her_own_runtime(stores):
    versions_store, slots_store = stores
    _seed_alice(versions_store, slots_store)
    _deploy_handler_ownership_check(
        versions_store,
        slots_store,
        friendly_runtime_name="alice_bot",
        user_id="alice",
    )  # no exception


def test_bob_cannot_clobber_alices_slot(stores):
    """The H-1 attack: Bob tries to deploy a runtime named 'alice_bot'.

    Without the fix, Bob's deploy would proceed and clobber Alice's slot
    row. With the fix, the ownership check raises before any DDB write.
    """
    versions_store, slots_store = stores
    _seed_alice(versions_store, slots_store)
    with pytest.raises(PermissionError, match="already in use by another tenant"):
        _deploy_handler_ownership_check(
            versions_store,
            slots_store,
            friendly_runtime_name="alice_bot",
            user_id="bob",
        )


def test_bob_blocked_when_only_versions_row_exists(stores):
    """Cover the IN-FLIGHT partial-deploy case: Alice's deploy wrote a `pending`
    versions row but hasn't reached status_update yet (so no slot row). Bob still
    can't take her name. (Bug 192b: a `failed` row would NOT block — see
    test_failed_foreign_deploy_does_not_lock_name — but a `pending` in-flight one
    represents a live claim and MUST block.)"""
    versions_store, slots_store = stores
    versions_store.put(
        AgentVersion(
            runtime_name="alice_bot",
            version_id=new_version_id(),
            owner_sub="alice",
            created_at="2026-05-28T10:00:00+00:00",
            deployment_id="dep-alice-partial",
            agentcore_runtime_name="alice_bot_xx",
            status="pending",
        )
    )
    # No slots row.
    with pytest.raises(PermissionError):
        _deploy_handler_ownership_check(
            versions_store,
            slots_store,
            friendly_runtime_name="alice_bot",
            user_id="bob",
        )


def test_fresh_name_is_allowed_for_anyone(stores):
    versions_store, slots_store = stores
    _deploy_handler_ownership_check(
        versions_store,
        slots_store,
        friendly_runtime_name="brand_new_name",
        user_id="bob",
    )  # no exception


def test_legacy_no_owner_row_passes_through(stores):
    """A legacy version row with no owner_sub attribute (pre-tenancy data)
    is treated as un-owned by the current handler check
    (``if v.owner_sub and v.owner_sub != user_id``). The empty/missing
    owner_sub short-circuits the comparison, so a fresh deploy is allowed.

    This mirrors the deliberate Critic Finding 3 fix in services/auth.py
    which keeps None-owner rows invisible at LIST time but doesn't gate
    write paths against them. Document the current behavior so a future
    fix can be intentional. See lessons.md Bug 122 + Critic Finding 3.
    """
    versions_store, slots_store = stores
    # Seed a legacy row directly via raw DDB (the AgentVersion serializer
    # would reject empty owner_sub as a GSI key).
    boto3.resource("dynamodb", region_name="us-east-1").Table("AgentVersions").put_item(
        Item={
            "runtime_name": "legacy_bot",
            "version_id": new_version_id(),
            "created_at": "2024-01-01T00:00:00+00:00",
            "deployment_id": "dep-legacy",
            "agentcore_runtime_name": "legacy_bot_xx",
            "status": "succeeded",
            # owner_sub deliberately absent
        }
    )
    _deploy_handler_ownership_check(
        versions_store,
        slots_store,
        friendly_runtime_name="legacy_bot",
        user_id="anyone",
    )  # no exception — current behavior


# ---------------------------------------------------------------------------
# Bug 192 — teardown must RELEASE the name (delete slots + versions rows) so a
# later deploy of the same name doesn't 409 after the resource is gone.
# ---------------------------------------------------------------------------


def _teardown_release_name(versions_store, slots_store, *, friendly_runtime_name, caller_sub):
    """Mirror of the Bug-192 name-release in deployment_handler.handle_delete_runtime:
    delete only the caller's (or un-owned) slot+version rows for the name."""
    for v in versions_store.list_for_runtime(friendly_runtime_name):
        if not v.owner_sub or v.owner_sub == (caller_sub or ""):
            versions_store.delete(friendly_runtime_name, v.version_id)
    slot = slots_store.get(friendly_runtime_name)
    if slot is not None and (not slot.owner_sub or slot.owner_sub == (caller_sub or "")):
        slots_store.delete(friendly_runtime_name)


def test_teardown_releases_name_so_redeploy_works(stores):
    """After Alice tears down, the name frees up — a fresh deploy (even by Bob)
    no longer hits the 409 cross-tenant lock (the resource + rows are gone)."""
    versions_store, slots_store = stores
    _seed_alice(versions_store, slots_store)
    # Pre-condition: Bob is blocked while Alice's rows exist.
    with pytest.raises(PermissionError):
        _deploy_handler_ownership_check(
            versions_store, slots_store, friendly_runtime_name="alice_bot", user_id="bob"
        )
    # Alice tears down her deployment.
    _teardown_release_name(
        versions_store, slots_store, friendly_runtime_name="alice_bot", caller_sub="alice"
    )
    # Rows are gone.
    assert slots_store.get("alice_bot") is None
    assert versions_store.list_for_runtime("alice_bot") == []
    # Now anyone can deploy the name again — no 409.
    _deploy_handler_ownership_check(
        versions_store, slots_store, friendly_runtime_name="alice_bot", user_id="bob"
    )


def test_teardown_release_is_tenant_scoped(stores):
    """A teardown by the WRONG owner must NOT release another tenant's name."""
    versions_store, slots_store = stores
    _seed_alice(versions_store, slots_store)
    # Bob's teardown attempt must not delete Alice's rows.
    _teardown_release_name(
        versions_store, slots_store, friendly_runtime_name="alice_bot", caller_sub="bob"
    )
    assert slots_store.get("alice_bot") is not None
    assert len(versions_store.list_for_runtime("alice_bot")) == 1


# ---------------------------------------------------------------------------
# Bug 192b — a FAILED foreign deploy must NOT lock the name (only live claims do).
# ---------------------------------------------------------------------------


def test_failed_foreign_deploy_does_not_lock_name(stores):
    """A prior FAILED deploy by another sub left a versions row but no live
    resource — it must NOT block a fresh deploy of the same name (the 'omar1'
    customer 409). pending/succeeded rows still block."""
    versions_store, slots_store = stores
    versions_store.put(
        AgentVersion(
            runtime_name="omar1",
            version_id=new_version_id(),
            owner_sub="someone_else",
            created_at="2026-06-26T12:27:00+00:00",
            deployment_id="dep-failed",
            agentcore_runtime_name="omar1_xx",
            status="failed",
        )
    )
    # No slots row (deploy never reached status_update). Bob can take the name.
    _deploy_handler_ownership_check(
        versions_store, slots_store, friendly_runtime_name="omar1", user_id="bob"
    )  # no exception


def test_succeeded_foreign_deploy_still_locks_name(stores):
    """Sanity: a SUCCEEDED foreign row (live claim) still blocks (H-1 intact)."""
    versions_store, slots_store = stores
    versions_store.put(
        AgentVersion(
            runtime_name="live_bot",
            version_id=new_version_id(),
            owner_sub="alice",
            created_at="2026-06-26T12:27:00+00:00",
            deployment_id="dep-live",
            agentcore_runtime_name="live_bot_xx",
            status="succeeded",
        )
    )
    with pytest.raises(PermissionError):
        _deploy_handler_ownership_check(
            versions_store, slots_store, friendly_runtime_name="live_bot", user_id="bob"
        )
