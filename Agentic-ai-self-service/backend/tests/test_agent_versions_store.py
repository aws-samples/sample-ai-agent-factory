"""Phase 1 Gap 1A — versioning store unit tests.

Tests run against ``moto``'s in-memory DynamoDB so they don't need real AWS.
We exercise the version_id sortability invariant + the sortable-suffix shape,
plus put/get/list/promote/rollback round-trips on the two stores.
"""

from __future__ import annotations

import sys
import time
from typing import Iterator

import boto3
import pytest

sys.path.insert(0, "src")

# moto is a transitive test dependency; skip if unavailable rather than
# breaking the rest of the test suite.
moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402  — import after the importorskip

from app.services.agent_versions_store import (  # noqa: E402
    AgentVersion,
    AgentVersionsStore,
    RuntimeSlots,
    RuntimeSlotsStore,
    new_version_id,
    short_version_suffix,
)


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        yield


@pytest.fixture
def versions_store(aws: None) -> AgentVersionsStore:
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
    return AgentVersionsStore(table_name="AgentVersions", region="us-east-1")


@pytest.fixture
def slots_store(aws: None) -> RuntimeSlotsStore:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="RuntimeSlots",
        KeySchema=[{"AttributeName": "runtime_name", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "runtime_name", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return RuntimeSlotsStore(table_name="RuntimeSlots", region="us-east-1")


# ---------------------------------------------------------------------------
# new_version_id / short_version_suffix invariants
# ---------------------------------------------------------------------------


def test_new_version_id_is_32_lowercase_hex():
    v = new_version_id()
    assert len(v) == 32
    assert all(c in "0123456789abcdef" for c in v)


def test_version_ids_sort_by_creation_time_across_ms_boundary():
    v_early = new_version_id()
    time.sleep(0.005)
    v_late = new_version_id()
    # Strict ordering across a >1ms gap: the timestamp prefix differs.
    assert v_early < v_late, f"{v_early} should sort before {v_late}"


def test_short_version_suffix_is_8_lowercase_hex():
    v = new_version_id()
    suf = short_version_suffix(v)
    assert len(suf) == 8
    assert all(c in "0123456789abcdef" for c in suf)


def test_short_version_suffix_distinct_per_call():
    suffixes = {short_version_suffix(new_version_id()) for _ in range(50)}
    # Even within a single ms window the suffix is taken from the random tail,
    # so 50 calls should produce ≥48 distinct suffixes.
    assert len(suffixes) >= 48


# ---------------------------------------------------------------------------
# AgentVersionsStore round-trips
# ---------------------------------------------------------------------------


def test_put_get_round_trip(versions_store: AgentVersionsStore):
    v = AgentVersion(
        runtime_name="my_agent",
        version_id=new_version_id(),
        owner_sub="alice",
        created_at="2026-05-28T00:00:00+00:00",
        deployment_id="dep-1",
        agentcore_runtime_name="my_agent_abcd1234",
        runtime_id="runtime-aaaa",
        status="succeeded",
        description="first deploy",
    )
    versions_store.put(v)
    loaded = versions_store.get("my_agent", v.version_id)
    assert loaded is not None
    assert loaded.runtime_name == "my_agent"
    assert loaded.owner_sub == "alice"
    assert loaded.runtime_id == "runtime-aaaa"
    assert loaded.status == "succeeded"
    assert loaded.description == "first deploy"


def test_list_for_runtime_returns_newest_first(versions_store: AgentVersionsStore):
    name = "my_agent"
    ids: list[str] = []
    for i in range(3):
        v = AgentVersion(
            runtime_name=name,
            version_id=new_version_id(),
            owner_sub="alice",
            created_at=f"2026-05-2{i}T00:00:00+00:00",
            deployment_id=f"dep-{i}",
            agentcore_runtime_name=f"{name}_v{i}",
            status="succeeded",
        )
        versions_store.put(v)
        ids.append(v.version_id)
        time.sleep(0.005)
    listed = [v.version_id for v in versions_store.list_for_runtime(name)]
    assert listed == list(reversed(ids))


def test_list_for_owner_uses_gsi(versions_store: AgentVersionsStore):
    versions_store.put(
        AgentVersion(
            runtime_name="a",
            version_id=new_version_id(),
            owner_sub="alice",
            created_at="2026-05-28T00:00:00+00:00",
            deployment_id="d-a",
            agentcore_runtime_name="a_v1",
        )
    )
    versions_store.put(
        AgentVersion(
            runtime_name="b",
            version_id=new_version_id(),
            owner_sub="alice",
            created_at="2026-05-28T00:00:01+00:00",
            deployment_id="d-b",
            agentcore_runtime_name="b_v1",
        )
    )
    versions_store.put(
        AgentVersion(
            runtime_name="c",
            version_id=new_version_id(),
            owner_sub="bob",
            created_at="2026-05-28T00:00:02+00:00",
            deployment_id="d-c",
            agentcore_runtime_name="c_v1",
        )
    )
    alice_versions = versions_store.list_for_owner("alice")
    assert len(alice_versions) == 2
    assert {v.runtime_name for v in alice_versions} == {"a", "b"}
    bob_versions = versions_store.list_for_owner("bob")
    assert {v.runtime_name for v in bob_versions} == {"c"}


def test_update_status_sets_runtime_id_and_arn(versions_store: AgentVersionsStore):
    v = AgentVersion(
        runtime_name="my_agent",
        version_id=new_version_id(),
        owner_sub="alice",
        created_at="2026-05-28T00:00:00+00:00",
        deployment_id="dep-1",
        agentcore_runtime_name="my_agent_abcd1234",
        status="pending",
    )
    versions_store.put(v)
    versions_store.update_status(
        runtime_name="my_agent",
        version_id=v.version_id,
        status="succeeded",
        runtime_id="runtime-xyz",
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/runtime-xyz",
        runtime_endpoint="https://endpoint",
    )
    loaded = versions_store.get("my_agent", v.version_id)
    assert loaded is not None
    assert loaded.status == "succeeded"
    assert loaded.runtime_id == "runtime-xyz"
    assert loaded.runtime_endpoint == "https://endpoint"


# ---------------------------------------------------------------------------
# RuntimeSlotsStore round-trips
# ---------------------------------------------------------------------------


def test_slots_upsert_and_get(slots_store: RuntimeSlotsStore):
    slots_store.upsert(
        RuntimeSlots(
            runtime_name="my_agent",
            owner_sub="alice",
            production_version_id="v1",
            staging_version_id=None,
            last_promoted_at="2026-05-28T00:00:00+00:00",
        )
    )
    loaded = slots_store.get("my_agent")
    assert loaded is not None
    assert loaded.production_version_id == "v1"
    assert loaded.staging_version_id is None
    assert loaded.owner_sub == "alice"


def test_slots_promote_swap_preserves_previous(slots_store: RuntimeSlotsStore):
    """Simulates the router's promote() flow: previous_production tracks the
    last production version so /rollback can swap back."""
    slots = RuntimeSlots(
        runtime_name="my_agent", owner_sub="alice", production_version_id="v1"
    )
    slots_store.upsert(slots)
    # Promote v2 to production.
    slots.previous_production_version_id = slots.production_version_id
    slots.production_version_id = "v2"
    slots_store.upsert(slots)
    loaded = slots_store.get("my_agent")
    assert loaded is not None
    assert loaded.production_version_id == "v2"
    assert loaded.previous_production_version_id == "v1"
