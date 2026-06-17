"""Negative-path tenant-isolation tests (Critic Finding 3 fix).

These tests fail BEFORE the fix lands and pass after:

  1. ``X-Test-Sub`` header injection is ignored — production paths cannot
     trust caller-supplied identity headers because the JWT authorizer at
     API Gateway does NOT inject that header.
  2. ``GET /api/flows/{id}`` for a record owned by a different sub returns
     404 (not 403 — preserves existence-non-disclosure).
  3. ``GET /api/flows/{id}`` for a legacy (None-owner) record returns 404
     for every caller. Pre-isolation rows are invisible until backfilled.
  4. ``GET /api/flows`` excludes legacy records and other-tenant records.

Tests use ``app.dependency_overrides[get_caller_sub]`` to inject the
caller's sub instead of an HTTP header — the post-fix code refuses to
honour ``X-Test-Sub`` even in local dev.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Flow
from app.models.enums import DeploymentStatus
from app.services.auth import _LOCAL_DEV_SUB, get_caller_sub
from app.services.flow_storage import FlowStorage, get_flow_storage, set_flow_storage


client = TestClient(app)


def _make_flow(flow_id: str, owner_sub):
    """Construct a Flow with an explicit ``owner_sub`` (or None for legacy)."""
    now = datetime.now(timezone.utc)
    return Flow(
        id=flow_id,
        name=f"flow-{flow_id}",
        workflow={
            "id": flow_id,
            "name": f"wf-{flow_id}",
            "version": "1.0.0",
            "description": "",
            "nodes": [],
            "edges": [],
            "viewport": {"x": 0, "y": 0, "zoom": 1.0},
            "metadata": {
                "author": "test",
                "aws_region": "us-east-1",
                "tags": [],
                "deployment_status": "not_deployed",
            },
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
        deployment_status=DeploymentStatus.NOT_DEPLOYED,
        created_at=now,
        updated_at=now,
        owner_sub=owner_sub,
    )


@pytest.fixture
def isolated_storage():
    """Swap in a fresh in-memory FlowStorage for each test."""
    previous = get_flow_storage()
    fresh = FlowStorage()
    set_flow_storage(fresh)
    try:
        yield fresh
    finally:
        set_flow_storage(previous)


@pytest.fixture
def caller_alice():
    """Override get_caller_sub to return Alice's sub for the duration."""
    app.dependency_overrides[get_caller_sub] = lambda: "alice-sub"
    try:
        yield "alice-sub"
    finally:
        app.dependency_overrides.pop(get_caller_sub, None)


@pytest.fixture
def caller_bob():
    """Override get_caller_sub to return Bob's sub."""
    app.dependency_overrides[get_caller_sub] = lambda: "bob-sub"
    try:
        yield "bob-sub"
    finally:
        app.dependency_overrides.pop(get_caller_sub, None)


# ---------------------------------------------------------------------------
# 1. X-Test-Sub header is ignored (header-trust regression test)
# ---------------------------------------------------------------------------


def test_x_test_sub_header_is_ignored(isolated_storage, caller_alice):
    """A request that smuggles ``X-Test-Sub: bob-sub`` must NOT be treated
    as bob — the dependency override (alice) is the only trusted source."""
    # Bob owns a flow; Alice is the caller. If the header were honoured,
    # the listing would include Bob's flow. It must not.
    isolated_storage._flows["f-bob"] = _make_flow("f-bob", "bob-sub")
    isolated_storage._flows["f-alice"] = _make_flow("f-alice", "alice-sub")

    response = client.get("/api/flows", headers={"X-Test-Sub": "bob-sub"})
    assert response.status_code == 200
    flow_ids = {f["id"] for f in response.json()["flows"]}
    assert flow_ids == {"f-alice"}, (
        f"X-Test-Sub header leaked cross-tenant rows: {flow_ids}"
    )


def test_x_test_sub_header_does_not_grant_access_to_other_records(
    isolated_storage, caller_alice
):
    """``GET /api/flows/{bobs_flow}`` with ``X-Test-Sub: bob-sub`` must 404 —
    the override (alice) is the trusted identity, not the header."""
    isolated_storage._flows["f-bob"] = _make_flow("f-bob", "bob-sub")
    response = client.get("/api/flows/f-bob", headers={"X-Test-Sub": "bob-sub"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 2. Cross-tenant get returns 404
# ---------------------------------------------------------------------------


def test_get_other_users_flow_returns_404(isolated_storage, caller_alice):
    """Alice fetching Bob's flow gets 404 (not 403)."""
    isolated_storage._flows["f-bob"] = _make_flow("f-bob", "bob-sub")
    response = client.get("/api/flows/f-bob")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 3. Legacy (None-owner) records are 404 for every caller
# ---------------------------------------------------------------------------


def test_get_legacy_none_owner_flow_returns_404(isolated_storage, caller_alice):
    """A flow with ``owner_sub=None`` (pre-isolation legacy row) must 404 —
    previously it leaked to every authenticated user."""
    isolated_storage._flows["f-legacy"] = _make_flow("f-legacy", None)
    response = client.get("/api/flows/f-legacy")
    assert response.status_code == 404


def test_get_legacy_none_owner_flow_returns_404_for_bob_too(
    isolated_storage, caller_bob
):
    """No magic caller can access legacy rows — even ``local-dev`` /
    ``bob-sub`` must 404. Backfill is the only fix."""
    isolated_storage._flows["f-legacy"] = _make_flow("f-legacy", None)
    response = client.get("/api/flows/f-legacy")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 4. List endpoint excludes legacy rows and other-tenant rows
# ---------------------------------------------------------------------------


def test_list_flows_excludes_legacy_and_other_tenant(isolated_storage, caller_alice):
    """``GET /api/flows`` returns ONLY rows where ``owner_sub == alice``."""
    isolated_storage._flows["f-alice-1"] = _make_flow("f-alice-1", "alice-sub")
    isolated_storage._flows["f-alice-2"] = _make_flow("f-alice-2", "alice-sub")
    isolated_storage._flows["f-bob"] = _make_flow("f-bob", "bob-sub")
    isolated_storage._flows["f-legacy"] = _make_flow("f-legacy", None)
    isolated_storage._flows["f-localdev"] = _make_flow("f-localdev", _LOCAL_DEV_SUB)

    response = client.get("/api/flows")
    assert response.status_code == 200
    flow_ids = {f["id"] for f in response.json()["flows"]}
    assert flow_ids == {"f-alice-1", "f-alice-2"}, (
        "list_flows leaked cross-tenant or legacy rows: " + str(flow_ids)
    )


def test_list_flows_empty_when_caller_owns_nothing(isolated_storage, caller_bob):
    """Bob, who owns nothing, gets an empty list even though legacy rows exist."""
    isolated_storage._flows["f-legacy"] = _make_flow("f-legacy", None)
    isolated_storage._flows["f-alice"] = _make_flow("f-alice", "alice-sub")

    response = client.get("/api/flows")
    assert response.status_code == 200
    assert response.json()["flows"] == []


# ---------------------------------------------------------------------------
# 5. assert_owner unit tests (defensive — the helper is the choke point)
# ---------------------------------------------------------------------------


def test_assert_owner_rejects_none_owner():
    """Direct unit test: legacy / un-owned records must 404."""
    from fastapi import HTTPException

    from app.services.auth import assert_owner

    with pytest.raises(HTTPException) as exc:
        assert_owner(None, "alice-sub")
    assert exc.value.status_code == 404


def test_assert_owner_rejects_local_dev_owner_for_real_caller():
    """A row owned by ``_LOCAL_DEV_SUB`` does not match a real Cognito sub."""
    from fastapi import HTTPException

    from app.services.auth import _LOCAL_DEV_SUB, assert_owner

    with pytest.raises(HTTPException) as exc:
        assert_owner(_LOCAL_DEV_SUB, "alice-cognito-sub")
    assert exc.value.status_code == 404


def test_assert_owner_passes_on_match():
    """No-op when caller owns the record."""
    from app.services.auth import assert_owner

    # Should not raise
    assert_owner("alice-sub", "alice-sub")
