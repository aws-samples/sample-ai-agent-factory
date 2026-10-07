"""Append-only production audit contract tests."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest
from app.routers import admin
from app.services import audit_middleware, audit_store
from app.services.audit_middleware import audit_http_request
from app.services.audit_store import AuditEvent, AuditIntegrityError, AuditStore, classify_action
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from moto import mock_aws

TABLE = "AuditAppendOnly"


def _create_table() -> None:
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "org_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def store() -> Iterator[AuditStore]:
    with mock_aws():
        _create_table()
        yield AuditStore(TABLE, "us-east-1", ttl_seconds=None)


def _event(*, phase: str = "outcome", correlation: str = "corr-1") -> AuditEvent:
    return AuditEvent(
        org_id="default",
        actor_sub="user-1",
        action="workflow.write",
        method="POST",
        path="/api/workflows",
        status_code=200 if phase == "outcome" else 0,
        ts="2026-10-07T10:00:00+00:00",
        event_id=f"event-{phase}",
        phase=phase,
        correlation_id=correlation,
    )


def test_production_record_has_digest_and_no_ttl(store: AuditStore):
    event = _event()
    store.record(event)
    item = store._table.get_item(Key={"org_id": event.org_id, "sk": event.sk})["Item"]
    assert "ttl" not in item
    assert item["integrity_sha256"] == event.integrity_sha256()


def test_duplicate_key_cannot_overwrite_audit_event(store: AuditStore):
    event = _event()
    store.record(event)
    with pytest.raises(ClientError) as exc:
        store.record(event)
    assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"


def test_tampered_record_fails_integrity_verification(store: AuditStore):
    event = _event()
    store.record(event)
    store._table.update_item(
        Key={"org_id": event.org_id, "sk": event.sk},
        UpdateExpression="SET #path = :path",
        ExpressionAttributeNames={"#path": "path"},
        ExpressionAttributeValues={":path": "/api/tampered"},
    )
    with pytest.raises(AuditIntegrityError, match="failed integrity"):
        store.list_recent("default")


def test_summary_separates_outcomes_and_incomplete_attempts(store: AuditStore):
    store.record(_event(phase="attempt", correlation="complete"))
    outcome = _event(phase="outcome", correlation="complete")
    outcome.event_id = "event-outcome-complete"
    store.record(outcome)
    incomplete = _event(phase="attempt", correlation="incomplete")
    incomplete.event_id = "event-attempt-incomplete"
    store.record(incomplete)

    summary = store.summarize("default")
    assert summary["total"] == 1
    assert summary["audit_records"] == 3
    assert summary["incomplete_attempts"] == 1
    assert summary["events"][0]["phase"] == "outcome"
    assert summary["incomplete_events"][0]["phase"] == "attempt"
    assert "ttl" not in summary["events"][0]


def test_legacy_row_without_digest_is_visible_but_unverified(store: AuditStore):
    legacy = _event()
    item = legacy._integrity_document()
    item["ttl"] = 2_000_000_000
    store._table.put_item(Item=item)

    summary = store.summarize("default")

    assert summary["total"] == 1
    assert summary["unverified_legacy_events"] == 1
    assert summary["events"][0]["integrity_verified"] is False
    assert summary["events"][0]["integrity_sha256"] is None


class _FakeStore:
    def __init__(self, fail_on: int | None = None) -> None:
        self.events: list[AuditEvent] = []
        self.fail_on = fail_on

    def record(self, event: AuditEvent) -> None:
        if self.fail_on == len(self.events) + 1:
            raise RuntimeError("audit unavailable")
        self.events.append(event)


def _app(mutations: list[str]) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _audit(request, call_next):
        return await audit_http_request(request, call_next)

    @app.post("/api/workflows")
    async def _write():
        mutations.append("mutated")
        return {"ok": True}

    return app


def test_production_audit_outage_blocks_route_before_mutation(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    fake = _FakeStore(fail_on=1)
    monkeypatch.setattr(audit_middleware, "get_audit_store", lambda: fake)
    mutations: list[str] = []

    response = TestClient(_app(mutations)).post("/api/workflows")

    assert response.status_code == 503
    assert mutations == []


def test_production_write_appends_correlated_attempt_and_outcome(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    fake = _FakeStore()
    monkeypatch.setattr(audit_middleware, "get_audit_store", lambda: fake)
    mutations: list[str] = []

    response = TestClient(_app(mutations)).post("/api/workflows")

    assert response.status_code == 200
    assert mutations == ["mutated"]
    assert [event.phase for event in fake.events] == ["attempt", "outcome"]
    assert fake.events[0].correlation_id == fake.events[1].correlation_id
    assert fake.events[1].status_code == 200


def test_development_audit_outage_does_not_break_iteration(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    fake = _FakeStore(fail_on=1)
    monkeypatch.setattr(audit_middleware, "get_audit_store", lambda: fake)
    mutations: list[str] = []

    response = TestClient(_app(mutations)).post("/api/workflows")

    assert response.status_code == 200
    assert mutations == ["mutated"]


@pytest.mark.parametrize(
    ("method", "path", "action"),
    [
        ("POST", "/api/workflows", "workflow.write"),
        ("PUT", "/api/flows/f-1", "flow.write"),
        ("POST", "/api/provider-credentials", "provider.credential.write"),
        ("POST", "/api/observability/credentials", "observability.write"),
        ("POST", "/api/runtimes/a/versions/v/promote", "agent.lifecycle.write"),
        ("DELETE", "/api/settings/vpc-profiles/p", "vpc.profile.write"),
        ("POST", "/hooks/r/t", "trigger.invoke"),
    ],
)
def test_both_application_write_surfaces_are_classified(method, path, action):
    assert classify_action(method, path) == action


def test_production_admin_read_never_fabricates_empty_audit(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")

    class _Broken:
        def summarize(self, *_args, **_kwargs):
            raise AuditIntegrityError("tampered")

    monkeypatch.setattr(audit_store, "get_audit_store", lambda: _Broken())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.get_audit(limit=100, _caller_sub="admin"))
    assert exc.value.status_code == 503


def test_every_write_route_on_both_apps_is_audited():
    from app.deployment_handler import deployment_app
    from app.main import app as workflow_app

    missing: list[tuple[str, str]] = []
    for app in (workflow_app, deployment_app):
        for route in app.routes:
            path = getattr(route, "path", "")
            for method in getattr(route, "methods", set()) or set():
                if method in {"POST", "PUT", "PATCH", "DELETE"} and classify_action(method, path) is None:
                    missing.append((method, path))
    assert missing == []


def _webhook_app() -> FastAPI:
    from app.routers.triggers import webhook_router

    app = FastAPI()

    @app.middleware("http")
    async def _audit(request, call_next):
        return await audit_http_request(request, call_next)

    app.include_router(webhook_router)
    return app


def _wire_webhook(monkeypatch, audit: _FakeStore, calls: list[str]):
    from app.routers import triggers

    trigger = SimpleNamespace(owner_sub="webhook-owner")
    store = MagicMock()
    store.get.return_value = trigger
    monkeypatch.setattr(triggers, "get_trigger_store", lambda: store)
    monkeypatch.setattr(triggers, "get_audit_store", lambda: audit)
    monkeypatch.setattr(audit_middleware, "get_audit_store", lambda: audit)
    monkeypatch.setattr(triggers, "webhook_delivery_event", lambda **_kwargs: {"delivery": "ok"})
    monkeypatch.setattr(
        triggers,
        "enqueue_webhook_dispatch",
        lambda **_kwargs: calls.append("enqueue"),
    )
    return triggers


def test_unauthenticated_webhook_cannot_create_audit_rows(monkeypatch):
    from app.services.trigger_runtime import WebhookAuthenticationError

    monkeypatch.setenv("ENVIRONMENT", "prod")
    audit = _FakeStore()
    calls: list[str] = []
    triggers = _wire_webhook(monkeypatch, audit, calls)
    monkeypatch.setattr(
        triggers,
        "authenticate_webhook",
        MagicMock(side_effect=WebhookAuthenticationError("invalid")),
    )

    response = TestClient(_webhook_app()).post("/hooks/runtime_a/trigger-1", content=b"{}")

    assert response.status_code == 401
    assert audit.events == []
    assert calls == []


def test_authenticated_webhook_audit_precedes_enqueue(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    calls: list[str] = []

    class _OrderedAudit(_FakeStore):
        def record(self, event: AuditEvent) -> None:
            super().record(event)
            calls.append(f"audit:{event.phase}")

    audit = _OrderedAudit()
    triggers = _wire_webhook(monkeypatch, audit, calls)
    monkeypatch.setattr(triggers, "authenticate_webhook", lambda *_args, **_kwargs: "delivery-1")

    response = TestClient(_webhook_app()).post("/hooks/runtime_a/trigger-1", content=b"{}")

    assert response.status_code == 202
    assert calls == ["audit:attempt", "enqueue", "audit:outcome"]
    assert audit.events[0].correlation_id == audit.events[1].correlation_id


def test_authenticated_webhook_audit_outage_blocks_enqueue(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    audit = _FakeStore(fail_on=1)
    calls: list[str] = []
    triggers = _wire_webhook(monkeypatch, audit, calls)
    monkeypatch.setattr(triggers, "authenticate_webhook", lambda *_args, **_kwargs: "delivery-1")

    response = TestClient(_webhook_app()).post("/hooks/runtime_a/trigger-1", content=b"{}")

    assert response.status_code == 503
    assert calls == []
