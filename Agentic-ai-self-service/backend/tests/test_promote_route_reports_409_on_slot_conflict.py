"""A lost slot compare-and-set surfaces to the caller as 409, not 500.

F-83 made the slot pointer write a compare-and-set; ``SlotWriteConflict`` is what it raises when
the row moved between the route's reads and its write. Until now only the store was tested: nothing
proved the ROUTE turns that into a 409 whose detail says nothing about the other writer, rather than
letting it fall through as an unexplained 500. This drives the real router through TestClient.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from app.routers import versions as versions_router  # noqa: E402
from app.services.agent_versions_store import AgentVersion, RuntimeSlots, SlotWriteConflict  # noqa: E402
from app.services.auth import get_caller_sub  # noqa: E402

CALLER = "caller-sub"
RUNTIME = "orders_probe"
V1 = "01a0d26eb4459a9516d12064ac4a8687"
V2 = "01a0d26f76e7ecc2611fea5a2f6852b3"


def _version(version_id: str) -> AgentVersion:
    return AgentVersion(
        runtime_name=RUNTIME,
        version_id=version_id,
        owner_sub=CALLER,
        created_at="2026-09-24T00:00:00+00:00",
        deployment_id=f"dep-{version_id[-4:]}",
        agentcore_runtime_name=f"{RUNTIME}_{version_id[-8:]}",
        runtime_id=f"rt-{version_id[-4:]}",
        runtime_arn=f"arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/rt-{version_id[-4:]}",
        status="succeeded",
    )


@pytest.fixture
def harness(monkeypatch):
    """The real router over fake stores; the slot write is the one seam under test."""
    vstore = MagicMock()
    vstore.get.side_effect = lambda runtime_name, version_id, **_kw: _version(version_id)
    sstore = MagicMock()
    sstore.get.return_value = RuntimeSlots(
        runtime_name=RUNTIME,
        owner_sub=CALLER,
        production_version_id=V2,
        previous_production_version_id=V1,
    )
    write = MagicMock(return_value=None)
    monkeypatch.setattr(versions_router, "get_versions_store", lambda: vstore)
    monkeypatch.setattr(versions_router, "get_slots_store", lambda: sstore)
    monkeypatch.setattr(versions_router, "set_slot_pointers_atomically", write)
    # RBAC is not the subject; the scope check reads this at call time.
    monkeypatch.setattr("app.services.rbac.has_scopes", lambda request, required: True)
    app = FastAPI()
    app.include_router(versions_router.router)
    app.dependency_overrides[get_caller_sub] = lambda: CALLER
    return TestClient(app), write


def _rollback_path() -> str:
    paths = [r.path for r in versions_router.router.routes if "rollback" in r.path]
    assert len(paths) == 1, paths
    return paths[0].replace("{runtime_name}", RUNTIME)


def test_promote_control_succeeds_when_the_write_lands(harness):
    client, write = harness
    resp = client.post(f"/api/runtimes/{RUNTIME}/versions/{V1}/promote", json={"slot": "production"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted_version_id"] == V1
    write.assert_called_once()


def test_promote_reports_409_when_the_slot_write_loses_the_race(harness):
    client, write = harness
    write.side_effect = SlotWriteConflict("condition failed: row moved")

    resp = client.post(f"/api/runtimes/{RUNTIME}/versions/{V1}/promote", json={"slot": "production"})

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "promote was in flight" in detail
    # The detail is for the caller; it must not echo the store's message, which can carry the
    # cancellation reason and with it the values of the request that lost.
    assert "condition failed" not in detail and "row moved" not in detail


def test_rollback_reports_409_when_the_slot_write_loses_the_race(harness):
    client, write = harness
    write.side_effect = SlotWriteConflict("condition failed: row moved")

    resp = client.post(_rollback_path())

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "rollback was in flight" in detail
    assert "condition failed" not in detail


def test_rollback_control_succeeds_when_the_write_lands(harness):
    client, write = harness
    resp = client.post(_rollback_path())
    assert resp.status_code == 200, resp.text
    write.assert_called_once()
