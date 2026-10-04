"""F-31: ``GET /api/hitl/logs`` returns the caller's own approval decisions, not the org's.

It returned every user's ``hitl_approved`` / ``hitl_rejected`` events -- each approver's ``sub``
and the request path they acted on -- to any ``hitl:read`` caller. Now a tenant sees only the
decisions they made; the org-wide view needs the ``admin`` scope ``services/rbac.py`` already
defines (same pattern as the F-12 budgets fix).
"""

from __future__ import annotations

import pytest
from app.routers import hitl as hitl_router
from app.services import audit_store as audit_mod
from app.services.audit_store import AuditEvent
from app.services.auth import get_caller_sub
from fastapi import FastAPI
from fastapi.testclient import TestClient

ALICE = "alice-sub-1111"
BOB = "bob-sub-2222"


def _event(actor: str, action: str, path: str, ts: str) -> AuditEvent:
    return AuditEvent(
        org_id="default", actor_sub=actor, action=action, method="POST", path=path, status_code=200, ts=ts
    )


EVENTS = [
    _event(ALICE, "hitl_approved", "/api/hitl/requests/r-alice-1/approve", "2026-09-28T10:00:00Z"),
    _event(BOB, "hitl_rejected", "/api/hitl/requests/r-bob-1/reject", "2026-09-28T09:00:00Z"),
    _event(BOB, "hitl_approved", "/api/hitl/requests/r-bob-2/approve", "2026-09-28T08:00:00Z"),
    _event(ALICE, "deploy.start", "/api/deploy", "2026-09-28T07:00:00Z"),
]


class _FakeAudit:
    def list_recent(self, org_id, limit=200):
        assert org_id == "default"
        return EVENTS[:limit]


@pytest.fixture(autouse=True)
def _audit(monkeypatch):
    monkeypatch.setattr(audit_mod, "get_audit_store", lambda: _FakeAudit())


def _client(sub: str, *, admin: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(hitl_router.router)
    app.dependency_overrides[get_caller_sub] = lambda: sub
    app.dependency_overrides[hitl_router._caller_is_hitl_admin] = lambda: admin
    return TestClient(app)


def _paths(client: TestClient, **params) -> list[str]:
    r = client.get("/api/hitl/logs", params=params)
    assert r.status_code == 200, r.text
    return [row["path"] for row in r.json()]


def test_a_tenant_sees_only_their_own_decisions():
    assert _paths(_client(ALICE)) == ["/api/hitl/requests/r-alice-1/approve"]
    assert _paths(_client(BOB)) == ["/api/hitl/requests/r-bob-1/reject", "/api/hitl/requests/r-bob-2/approve"]


def test_no_other_approvers_sub_appears_in_a_tenants_body():
    r = _client(ALICE).get("/api/hitl/logs")
    assert BOB not in r.text


def test_the_status_filter_still_applies_within_the_callers_rows():
    assert _paths(_client(BOB), status="approved") == ["/api/hitl/requests/r-bob-2/approve"]
    assert _paths(_client(ALICE), status="rejected") == []


def test_an_admin_sees_the_org_wide_history():
    assert _paths(_client("admin-sub-9999", admin=True)) == [
        "/api/hitl/requests/r-alice-1/approve",
        "/api/hitl/requests/r-bob-1/reject",
        "/api/hitl/requests/r-bob-2/approve",
    ]


def test_the_admin_check_is_the_org_wide_admin_scope():
    from starlette.requests import Request

    def _req(groups):
        return Request(
            {
                "type": "http",
                "path": "/api/hitl/logs",
                "headers": [],
                "aws.event": {"requestContext": {"authorizer": {"jwt": {"claims": {"cognito:groups": groups}}}}},
            }
        )

    assert hitl_router._caller_is_hitl_admin(_req(["g-admins-super"])) is True
    assert hitl_router._caller_is_hitl_admin(_req(["g-admins-security"])) is False
    assert hitl_router._caller_is_hitl_admin(_req([])) is False
