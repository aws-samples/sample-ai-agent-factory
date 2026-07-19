"""Tests for importing an existing AgentCore Runtime by ARN (Loom-study 1.5).

POST /api/runtime/import adopts an externally-built runtime as a caller-owned
SUCCEEDED deployment without codegen/deploy. Uses TestClient with boto3 +
_get_state_store monkeypatched (no AWS).
"""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

import app.deployment_handler as dh  # noqa: E402

VALID_ARN = "arn:aws:bedrock-agentcore:us-east-1:166827918465:runtime/myagent_abc123"


class _FakeCtrl:
    def get_agent_runtime(self, agentRuntimeId):  # noqa: N803
        return {"agentRuntimeName": agentRuntimeId, "status": "READY"}


class _FakeStore:
    def __init__(self, existing=None):
        self._existing = existing
        self.created = None
        self._table = object()

    def create(self, state):
        self.created = state
        return state


@pytest.fixture
def client(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: None)
    monkeypatch.setattr(dh.boto3, "client", lambda *a, **k: _FakeCtrl())
    monkeypatch.setattr(dh, "_get_user_id", lambda req: "tester")
    c = TestClient(dh.deployment_app)
    c._store = store  # type: ignore[attr-defined]
    return c


def test_import_valid_arn_creates_succeeded_deployment(client):
    r = client.post("/api/runtime/import", json={"runtimeArn": VALID_ARN})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["imported"] is True
    assert body["runtimeId"] == "myagent_abc123"
    # a SUCCEEDED, caller-owned record was written
    state = client._store.created
    assert state is not None
    assert state.runtime_arn == VALID_ARN
    assert state.user_id == "tester"
    assert state.status.value == "succeeded"


def test_import_rejects_bad_arn(client):
    # Long enough to pass min_length, but not a valid AgentCore runtime ARN → 400
    # from the endpoint's regex (a too-short string is a 422 model-validation
    # reject, also correct — both refuse the import).
    r = client.post("/api/runtime/import", json={"runtimeArn": "arn:aws:s3:::some-bucket-that-is-not-a-runtime-arn"})
    assert r.status_code == 400
    assert client.post("/api/runtime/import", json={"runtimeArn": "short"}).status_code == 422


def test_import_conflict_when_owned_by_another(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(dh, "_get_state_store", lambda: store)
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: {"user_id": "someone-else"})
    monkeypatch.setattr(dh.boto3, "client", lambda *a, **k: _FakeCtrl())
    monkeypatch.setattr(dh, "_get_user_id", lambda req: "tester")
    c = TestClient(dh.deployment_app)
    r = c.post("/api/runtime/import", json={"runtimeArn": VALID_ARN})
    assert r.status_code == 409
