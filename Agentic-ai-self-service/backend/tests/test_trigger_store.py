"""Phase 3 Gap 3F — scheduled / event trigger registry tests.

Two layers:

* Store-level (moto-backed DDB): create+get roundtrip, sortable id shape,
  list_for_runtime ordering, list_for_owner via the owner_sub GSI (cross-runtime
  + cross-tenant isolation), update_status, delete idempotency.

* Router-level (FastAPI TestClient + dependency_overrides[get_caller_sub]):
  create stamps owner_sub + server-derived target_runtime_arn (ignoring any
  body-supplied arn); Bug-122 (Bob can't create a trigger on Alice's
  runtime_name -> 404, no DDB write); cross-tenant GET/DELETE 404; input
  validation (bad runtime_name/trigger_id/cron/oversized pattern); and the SSRF
  negative paths for webhook_out_url (IP literal, RFC1918, non-https rejected).

No AWS, no inline Cognito auth — unit only.
"""

from __future__ import annotations

import sys
from typing import Iterator
from unittest.mock import patch

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from app.services.agent_versions_store import AgentVersion, RuntimeSlots  # noqa: E402
from app.services.auth import get_caller_sub  # noqa: E402
from app.services.trigger_store import (  # noqa: E402
    STATUS_DISABLED,
    STATUS_REGISTERED,
    TYPE_CRON,
    TriggerStore,
    new_trigger_id,
)

TABLE_NAME = "Triggers"
GSI_NAME = "owner_sub-trigger_id-index"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        yield


@pytest.fixture
def store(aws: None) -> TriggerStore:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "runtime_name", "KeyType": "HASH"},
            {"AttributeName": "trigger_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "runtime_name", "AttributeType": "S"},
            {"AttributeName": "trigger_id", "AttributeType": "S"},
            {"AttributeName": "owner_sub", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": GSI_NAME,
                "KeySchema": [
                    {"AttributeName": "owner_sub", "KeyType": "HASH"},
                    {"AttributeName": "trigger_id", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return TriggerStore(table_name=TABLE_NAME, region="us-east-1")


# ---------------------------------------------------------------------------
# Store-level tests
# ---------------------------------------------------------------------------


def test_new_trigger_id_is_32_char_hex():
    tid = new_trigger_id()
    assert len(tid) == 32
    int(tid, 16)  # raises if not hex


def test_new_trigger_ids_are_sortable():
    import time

    a = new_trigger_id()
    time.sleep(0.005)
    b = new_trigger_id()
    assert a < b  # lex order == chronological


def test_create_get_roundtrip_preserves_fields(store: TriggerStore):
    trig = store.create_trigger(
        runtime_name="alice_bot",
        owner_sub="alice",
        type=TYPE_CRON,
        target_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/alice",
        schedule="cron(0 12 * * ? *)",
    )
    fetched = store.get("alice_bot", trig.trigger_id)
    assert fetched is not None
    assert fetched.runtime_name == "alice_bot"
    assert fetched.owner_sub == "alice"
    assert fetched.type == TYPE_CRON
    assert fetched.schedule == "cron(0 12 * * ? *)"
    # Bug 139: new triggers are REGISTERED (recorded but not yet provisioned/firing),
    # not ACTIVE — the platform doesn't create the EventBridge/Scheduler resource yet,
    # so claiming "active" would mislead the user.
    assert fetched.status == STATUS_REGISTERED
    assert fetched.target_runtime_arn.endswith("runtime/alice")
    assert fetched.created_at > 0
    assert fetched.updated_at > 0


def test_create_preserves_pattern_dict(store: TriggerStore):
    pattern = {"source": ["aws.s3"], "detail": {"bucket": {"name": ["my-bucket"]}}}
    trig = store.create_trigger(
        runtime_name="alice_bot",
        owner_sub="alice",
        type="s3",
        target_runtime_arn="arn:rt:alice",
        pattern=pattern,
    )
    fetched = store.get("alice_bot", trig.trigger_id)
    assert fetched is not None
    assert fetched.pattern == pattern


def test_list_for_runtime_newest_first(store: TriggerStore):
    import time

    ids = []
    for _ in range(3):
        t = store.create_trigger(
            runtime_name="alice_bot",
            owner_sub="alice",
            type=TYPE_CRON,
            target_runtime_arn="arn:rt:alice",
            schedule="cron(0 12 * * ? *)",
        )
        ids.append(t.trigger_id)
        time.sleep(0.005)
    rows = store.list_for_runtime("alice_bot")
    assert [r.trigger_id for r in rows] == list(reversed(ids))  # newest first


def test_list_for_owner_via_gsi_cross_runtime_and_isolated(store: TriggerStore):
    store.create_trigger(
        runtime_name="alice_bot",
        owner_sub="alice",
        type=TYPE_CRON,
        target_runtime_arn="arn:rt:alice1",
        schedule="cron(0 12 * * ? *)",
    )
    store.create_trigger(
        runtime_name="alice_other",
        owner_sub="alice",
        type=TYPE_CRON,
        target_runtime_arn="arn:rt:alice2",
        schedule="cron(0 6 * * ? *)",
    )
    store.create_trigger(
        runtime_name="bob_bot",
        owner_sub="bob",
        type=TYPE_CRON,
        target_runtime_arn="arn:rt:bob",
        schedule="cron(0 1 * * ? *)",
    )
    alice_rows = store.list_for_owner("alice")
    assert len(alice_rows) == 2
    assert {r.runtime_name for r in alice_rows} == {"alice_bot", "alice_other"}
    assert all(r.owner_sub == "alice" for r in alice_rows)

    bob_rows = store.list_for_owner("bob")
    assert len(bob_rows) == 1
    assert bob_rows[0].runtime_name == "bob_bot"


def test_update_status_flips_and_stamps_handles(store: TriggerStore):
    trig = store.create_trigger(
        runtime_name="alice_bot",
        owner_sub="alice",
        type=TYPE_CRON,
        target_runtime_arn="arn:rt:alice",
        schedule="cron(0 12 * * ? *)",
    )
    updated = store.update_status(
        runtime_name="alice_bot",
        trigger_id=trig.trigger_id,
        status=STATUS_DISABLED,
        scheduler_name="sched-123",
    )
    assert updated is not None
    assert updated.status == STATUS_DISABLED
    assert updated.scheduler_name == "sched-123"


def test_update_status_missing_row_returns_none(store: TriggerStore):
    assert (
        store.update_status(
            runtime_name="alice_bot",
            trigger_id="does-not-exist",
            status=STATUS_DISABLED,
        )
        is None
    )


def test_delete_is_idempotent(store: TriggerStore):
    trig = store.create_trigger(
        runtime_name="alice_bot",
        owner_sub="alice",
        type=TYPE_CRON,
        target_runtime_arn="arn:rt:alice",
        schedule="cron(0 12 * * ? *)",
    )
    store.delete("alice_bot", trig.trigger_id)
    assert store.get("alice_bot", trig.trigger_id) is None
    # Second delete must not raise.
    store.delete("alice_bot", trig.trigger_id)


# ---------------------------------------------------------------------------
# Router-level tests
# ---------------------------------------------------------------------------


ALICE = "alice-sub"
BOB = "bob-sub"


def _make_client(caller_sub: str) -> TestClient:
    from app.routers.triggers import router as triggers_router

    app = FastAPI()
    app.include_router(triggers_router)
    app.dependency_overrides[get_caller_sub] = lambda: caller_sub
    return TestClient(app)


def _seed_slot(owner_sub: str, runtime_name: str = "alice_bot"):
    """Return (slots_obj, version_obj) for a production slot owned by owner_sub."""
    slots = RuntimeSlots(
        runtime_name=runtime_name,
        owner_sub=owner_sub,
        production_version_id="v1",
    )
    version = AgentVersion(
        runtime_name=runtime_name,
        version_id="v1",
        owner_sub=owner_sub,
        created_at="2026-05-28T00:00:00+00:00",
        deployment_id="d1",
        agentcore_runtime_name=f"{runtime_name}_abcd1234",
        runtime_id="rt-abcd1234",
        runtime_arn=f"arn:aws:bedrock-agentcore:us-east-1:1:runtime/{runtime_name}",
    )
    return slots, version


def test_create_stamps_owner_and_server_derived_arn(store: TriggerStore):
    slots, version = _seed_slot(ALICE)
    client = _make_client(ALICE)
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_versions_store"
    ) as versions_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = slots
        versions_mock.return_value.get.return_value = version
        resp = client.post(
            "/api/runtimes/alice_bot/triggers",
            json={
                "type": "cron",
                "schedule": "cron(0 12 * * ? *)",
                # An attacker-supplied arn must be IGNORED.
                "target_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:9:runtime/evil",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Server derived the ARN from the owned version; the body value is ignored.
    assert body["target_runtime_arn"] == (
        "arn:aws:bedrock-agentcore:us-east-1:1:runtime/alice_bot"
    )
    assert "evil" not in body["target_runtime_arn"]
    # owner_sub is stamped on the persisted row.
    stored = store.get("alice_bot", body["trigger_id"])
    assert stored is not None
    assert stored.owner_sub == ALICE


def test_bug122_bob_cannot_create_trigger_on_alices_runtime(store: TriggerStore):
    """Bob POSTs to Alice's runtime_name -> 404 and no DDB row is written."""
    slots, version = _seed_slot(ALICE)  # slot owned by alice
    client = _make_client(BOB)
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_versions_store"
    ) as versions_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = slots
        versions_mock.return_value.get.return_value = version
        resp = client.post(
            "/api/runtimes/alice_bot/triggers",
            json={"type": "cron", "schedule": "cron(0 12 * * ? *)"},
        )
    assert resp.status_code == 404
    # No row written for Alice's runtime.
    assert store.list_for_runtime("alice_bot") == []


def test_create_404_when_no_slot(store: TriggerStore):
    client = _make_client(ALICE)
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = None
        resp = client.post(
            "/api/runtimes/alice_bot/triggers",
            json={"type": "cron", "schedule": "cron(0 12 * * ? *)"},
        )
    assert resp.status_code == 404


def test_list_only_returns_callers_triggers(store: TriggerStore):
    # Seed one trigger for alice and one for bob on the same runtime_name row.
    store.create_trigger(
        runtime_name="alice_bot",
        owner_sub=ALICE,
        type=TYPE_CRON,
        target_runtime_arn="arn:rt:alice",
        schedule="cron(0 12 * * ? *)",
    )
    store.create_trigger(
        runtime_name="alice_bot",
        owner_sub=BOB,  # a (hypothetical) stray cross-tenant row
        type=TYPE_CRON,
        target_runtime_arn="arn:rt:bob",
        schedule="cron(0 1 * * ? *)",
    )
    slots, version = _seed_slot(ALICE)
    client = _make_client(ALICE)
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_versions_store"
    ) as versions_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = slots
        versions_mock.return_value.get.return_value = version
        resp = client.get("/api/runtimes/alice_bot/triggers")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["target_runtime_arn"] == "arn:rt:alice"


def test_delete_own_trigger_succeeds(store: TriggerStore):
    trig = store.create_trigger(
        runtime_name="alice_bot",
        owner_sub=ALICE,
        type=TYPE_CRON,
        target_runtime_arn="arn:rt:alice",
        schedule="cron(0 12 * * ? *)",
    )
    slots, version = _seed_slot(ALICE)
    client = _make_client(ALICE)
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_versions_store"
    ) as versions_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = slots
        versions_mock.return_value.get.return_value = version
        resp = client.delete(f"/api/runtimes/alice_bot/triggers/{trig.trigger_id}")
    assert resp.status_code == 200, resp.text
    assert store.get("alice_bot", trig.trigger_id) is None


def test_delete_cross_tenant_trigger_returns_404(store: TriggerStore):
    """A trigger row owned by another sub -> 404 (existence-non-disclosure)."""
    # Alice owns the slot, but the trigger row is (somehow) owned by bob.
    trig = store.create_trigger(
        runtime_name="alice_bot",
        owner_sub=BOB,
        type=TYPE_CRON,
        target_runtime_arn="arn:rt:bob",
        schedule="cron(0 12 * * ? *)",
    )
    slots, version = _seed_slot(ALICE)
    client = _make_client(ALICE)
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_versions_store"
    ) as versions_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = slots
        versions_mock.return_value.get.return_value = version
        resp = client.delete(f"/api/runtimes/alice_bot/triggers/{trig.trigger_id}")
    assert resp.status_code == 404
    # Row still present (not deleted).
    assert store.get("alice_bot", trig.trigger_id) is not None


def test_invalid_runtime_name_rejected(store: TriggerStore):
    client = _make_client(ALICE)
    resp = client.post(
        "/api/runtimes/has-hyphens/triggers",
        json={"type": "cron", "schedule": "cron(0 12 * * ? *)"},
    )
    assert resp.status_code == 400


def test_invalid_trigger_id_rejected_on_delete(store: TriggerStore):
    slots, version = _seed_slot(ALICE)
    client = _make_client(ALICE)
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_versions_store"
    ) as versions_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = slots
        versions_mock.return_value.get.return_value = version
        resp = client.delete("/api/runtimes/alice_bot/triggers/bad%20id")
    assert resp.status_code == 400


def test_malformed_cron_rejected(store: TriggerStore):
    slots, version = _seed_slot(ALICE)
    client = _make_client(ALICE)
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_versions_store"
    ) as versions_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = slots
        versions_mock.return_value.get.return_value = version
        # Not a 6-field cron(...) wrapper.
        resp = client.post(
            "/api/runtimes/alice_bot/triggers",
            json={"type": "cron", "schedule": "every 5 minutes"},
        )
    assert resp.status_code == 400


def test_oversized_pattern_rejected(store: TriggerStore):
    slots, version = _seed_slot(ALICE)
    client = _make_client(ALICE)
    big_pattern = {"detail": {"k": ["x" * 5000]}}  # > 4096 bytes serialized
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_versions_store"
    ) as versions_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = slots
        versions_mock.return_value.get.return_value = version
        resp = client.post(
            "/api/runtimes/alice_bot/triggers",
            json={"type": "eventbridge", "pattern": big_pattern},
        )
    assert resp.status_code == 400


def test_cron_missing_schedule_rejected(store: TriggerStore):
    slots, version = _seed_slot(ALICE)
    client = _make_client(ALICE)
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_versions_store"
    ) as versions_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = slots
        versions_mock.return_value.get.return_value = version
        resp = client.post(
            "/api/runtimes/alice_bot/triggers",
            json={"type": "cron"},
        )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# SSRF negative-path tests for webhook_out_url (Critic Finding 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://example.com/hook",  # non-https
        "https://169.254.169.254/latest/meta-data/",  # IMDS literal
        "https://127.0.0.1/hook",  # loopback literal
        "https://10.0.0.5/hook",  # RFC1918 literal
        "https://192.168.1.1/hook",  # RFC1918 literal
        "ftp://example.com/hook",  # bad scheme
    ],
)
def test_webhook_out_url_ssrf_rejected(store: TriggerStore, bad_url: str):
    slots, version = _seed_slot(ALICE)
    client = _make_client(ALICE)
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_versions_store"
    ) as versions_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = slots
        versions_mock.return_value.get.return_value = version
        resp = client.post(
            "/api/runtimes/alice_bot/triggers",
            json={
                "type": "cron",
                "schedule": "cron(0 12 * * ? *)",
                "webhook_out_url": bad_url,
            },
        )
    assert resp.status_code == 400, resp.text
    # No row written when validation fails.
    assert store.list_for_runtime("alice_bot") == []


def test_webhook_out_url_private_dns_rejected(store: TriggerStore):
    """A public-looking host that DNS-resolves to a private IP is rejected."""
    slots, version = _seed_slot(ALICE)
    client = _make_client(ALICE)
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_versions_store"
    ) as versions_mock, patch(
        "app.services.gateway_deployer.socket.getaddrinfo"
    ) as gai_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = slots
        versions_mock.return_value.get.return_value = version
        # Resolve the host to an RFC1918 address.
        gai_mock.return_value = [
            (2, 1, 6, "", ("10.1.2.3", 443)),
        ]
        resp = client.post(
            "/api/runtimes/alice_bot/triggers",
            json={
                "type": "cron",
                "schedule": "cron(0 12 * * ? *)",
                "webhook_out_url": "https://internal.example.com/hook",
            },
        )
    assert resp.status_code == 400
    assert store.list_for_runtime("alice_bot") == []


def test_webhook_out_url_valid_public_accepted(store: TriggerStore):
    """A public host that resolves to a public IP is accepted and stored."""
    slots, version = _seed_slot(ALICE)
    client = _make_client(ALICE)
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_versions_store"
    ) as versions_mock, patch(
        "app.services.gateway_deployer.socket.getaddrinfo"
    ) as gai_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = slots
        versions_mock.return_value.get.return_value = version
        gai_mock.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 443)),  # public (example.com)
        ]
        resp = client.post(
            "/api/runtimes/alice_bot/triggers",
            json={
                "type": "cron",
                "schedule": "cron(0 12 * * ? *)",
                "webhook_out_url": "https://hooks.example.com/incoming",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["webhook_out_url"] == "https://hooks.example.com/incoming"


def test_webhook_type_creates_owner_scoped_secret(store: TriggerStore):
    """A webhook trigger mints a Secrets Manager secret; only the ARN is stored."""
    slots, version = _seed_slot(ALICE)
    client = _make_client(ALICE)
    with patch(
        "app.routers.triggers.get_slots_store"
    ) as slots_mock, patch(
        "app.routers.triggers.get_versions_store"
    ) as versions_mock, patch(
        "app.routers.triggers.boto3.client"
    ) as boto_mock, patch(
        "app.routers.triggers.get_trigger_store", return_value=store
    ):
        slots_mock.return_value.get.return_value = slots
        versions_mock.return_value.get.return_value = version
        sm = boto_mock.return_value
        sm.create_secret.return_value = {
            "ARN": "arn:aws:secretsmanager:us-east-1:1:secret:agentcore-trigger/alice-sub-abc"
        }
        resp = client.post(
            "/api/runtimes/alice_bot/triggers",
            json={"type": "webhook"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["webhook_secret_ref"].startswith("arn:aws:secretsmanager")
    # The owner-scoped name + owner_sub tag is used.
    _, kwargs = sm.create_secret.call_args
    assert kwargs["Name"].startswith("agentcore-trigger/")
    assert {"Key": "owner_sub", "Value": ALICE} in kwargs["Tags"]
    # The raw secret value never lands in DDB.
    stored = store.get("alice_bot", body["trigger_id"])
    assert stored is not None
    assert stored.webhook_secret_ref.startswith("arn:aws:secretsmanager")
