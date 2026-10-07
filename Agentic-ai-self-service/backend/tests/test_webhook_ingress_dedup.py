"""Durable signed-webhook ingress idempotency before SQS enqueue."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import boto3
import pytest
from app.routers.triggers import webhook_router
from app.services.trigger_runtime import TriggerDispatchError
from app.services.trigger_store import (
    STATUS_ACTIVE,
    TYPE_WEBHOOK,
    TriggerStore,
    WebhookIngressClaim,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

RUNTIME_NAME = "orders_bot"
TRIGGER_ID = "trigger-1"
DELIVERY_ID = "delivery-1"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/orders_bot-AbCdEf1234"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(webhook_router)
    return TestClient(app)


def _store() -> TriggerStore:
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName="Triggers",
        KeySchema=[
            {"AttributeName": "runtime_name", "KeyType": "HASH"},
            {"AttributeName": "trigger_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "runtime_name", "AttributeType": "S"},
            {"AttributeName": "trigger_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    store = TriggerStore("Triggers", "us-east-1")
    store.put_trigger_unfenced(
        runtime_name=RUNTIME_NAME,
        trigger_id=TRIGGER_ID,
        owner_sub="owner-1",
        type=TYPE_WEBHOOK,
        target_runtime_arn=RUNTIME_ARN,
        version_id="version-1",
        deployment_id="deployment-1",
        status=STATUS_ACTIVE,
    )
    return store


def _headers() -> dict[str, str]:
    return {
        "x-agentcore-timestamp": "100",
        "x-agentcore-delivery-id": DELIVERY_ID,
        "x-agentcore-signature": "v1=" + "0" * 64,
        "content-type": "application/json",
    }


def _route_patches(store: TriggerStore, enqueue: MagicMock):
    return (
        patch("app.routers.triggers.get_trigger_store", return_value=store),
        patch("app.routers.triggers.authenticate_webhook", return_value=DELIVERY_ID),
        patch("app.routers.triggers.enqueue_webhook_dispatch", enqueue),
        patch("app.routers.triggers._record_webhook_audit", return_value=None),
    )


@mock_aws
def test_same_signed_delivery_is_queued_once_across_requests(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    store = _store()
    enqueue = MagicMock()
    patches = _route_patches(store, enqueue)
    with patches[0], patches[1], patches[2], patches[3]:
        first = _client().post(
            f"/hooks/{RUNTIME_NAME}/{TRIGGER_ID}",
            content=b'{"order":1}',
            headers=_headers(),
        )
        second = _client().post(
            f"/hooks/{RUNTIME_NAME}/{TRIGGER_ID}",
            content=b'{"order":1}',
            headers=_headers(),
        )

    assert first.status_code == 202 and first.json()["duplicate"] is False
    assert second.status_code == 202 and second.json()["duplicate"] is True
    assert enqueue.call_count == 1


@mock_aws
def test_same_delivery_id_with_different_body_is_exact_conflict(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    store = _store()
    enqueue = MagicMock()
    patches = _route_patches(store, enqueue)
    with patches[0], patches[1], patches[2], patches[3]:
        first = _client().post(
            f"/hooks/{RUNTIME_NAME}/{TRIGGER_ID}",
            content=b'{"order":1}',
            headers=_headers(),
        )
        conflict = _client().post(
            f"/hooks/{RUNTIME_NAME}/{TRIGGER_ID}",
            content=b'{"order":2}',
            headers=_headers(),
        )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == ("Webhook delivery ID was already used for a different payload")
    assert enqueue.call_count == 1


@mock_aws
def test_known_enqueue_failure_releases_ingress_for_retry(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    store = _store()
    enqueue = MagicMock(side_effect=[TriggerDispatchError("failed"), None])
    patches = _route_patches(store, enqueue)
    with patches[0], patches[1], patches[2], patches[3]:
        failed = _client().post(
            f"/hooks/{RUNTIME_NAME}/{TRIGGER_ID}",
            content=b"{}",
            headers=_headers(),
        )
        retried = _client().post(
            f"/hooks/{RUNTIME_NAME}/{TRIGGER_ID}",
            content=b"{}",
            headers=_headers(),
        )

    assert failed.status_code == 503
    assert retried.status_code == 202 and retried.json()["duplicate"] is False
    assert enqueue.call_count == 2


@mock_aws
def test_concurrent_ingress_returns_retryable_conflict(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    store = _store()
    trigger = store.get(RUNTIME_NAME, TRIGGER_ID, consistent=True)
    assert trigger is not None
    claim = store.acquire_webhook_ingress(
        trigger=trigger,
        delivery_id=DELIVERY_ID,
        payload_sha256="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        lease_seconds=60,
        retention_seconds=86_400,
        now=100,
    )
    assert claim is not None
    enqueue = MagicMock()
    patches = _route_patches(store, enqueue)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patch(
            "app.services.trigger_store.time.time",
            return_value=101,
        ),
    ):
        response = _client().post(
            f"/hooks/{RUNTIME_NAME}/{TRIGGER_ID}",
            content=b"{}",
            headers=_headers(),
        )

    assert response.status_code == 409
    assert response.headers["retry-after"] == "1"
    enqueue.assert_not_called()


@mock_aws
def test_mutation_without_durable_ingress_claim_queues_duplicate(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    store = _store()
    counter = 0

    def no_dedup(**_kwargs):
        nonlocal counter
        counter += 1
        return WebhookIngressClaim(
            runtime_name=RUNTIME_NAME,
            trigger_id=TRIGGER_ID,
            delivery_id=DELIVERY_ID,
            token=f"mutant-{counter}",
        )

    monkeypatch.setattr(store, "acquire_webhook_ingress", no_dedup)
    monkeypatch.setattr(store, "complete_webhook_ingress", lambda *_args, **_kwargs: True)
    enqueue = MagicMock()
    patches = _route_patches(store, enqueue)
    with patches[0], patches[1], patches[2], patches[3]:
        for _ in range(2):
            response = _client().post(
                f"/hooks/{RUNTIME_NAME}/{TRIGGER_ID}",
                content=b"{}",
                headers=_headers(),
            )
            assert response.status_code == 202

    assert enqueue.call_count == 2
