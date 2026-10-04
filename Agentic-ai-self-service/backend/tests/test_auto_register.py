"""Tests for AWS Agent Registry auto-registration on deploy (Loom-study 0.4).

register() previously had ZERO callers, so deployed agents were never federated
into the registry. The status_update step now calls _auto_register_in_aws_registry
on the SUCCEEDED path. These tests exercise that helper with a mocked registry +
store (no AWS).
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "src")

from app.services.deployment_state_store import DeploymentLifecycleConflict  # noqa: E402
from app.step_handlers import status_update_step as sus  # noqa: E402


class _FakeStore:
    def __init__(self, existing_record=None):
        self._existing = existing_record
        self.saved = None
        self.finalizer_token = None

    def get_registry_record_id(self, deployment_id):  # noqa: ARG002
        return self._existing

    def set_registry_record(
        self,
        deployment_id,
        record_id,
        status,
        *,
        finalizer_token=None,
    ):
        self.saved = (deployment_id, record_id, status)
        self.finalizer_token = finalizer_token


class _FakeRegistry:
    def __init__(self, *, updated=False):
        self.registered = None
        self.updated = updated
        self.deleted = []

    def register(self, name, record_type, descriptors, description=""):  # noqa: ARG002
        # GA signature: `record_type` (was `descriptor_type`), carrying a value
        # from the recordType enum MCP/AGENT/CUSTOM/SKILL.
        self.registered = {"name": name, "type": record_type, "descriptors": descriptors}
        result = {
            "record_id": "rec4567890ab",
            "arn": "arn:aws:agent-registry:us-east-1:123456789012:registry/reg1/record/rec4567890ab",
            "status": "DRAFT",
            "record_type": record_type,
        }
        if self.updated:
            result["updated"] = True
        return result

    def delete(self, record_id):
        self.deleted.append(record_id)


class _RejectingStore(_FakeStore):
    def set_registry_record(
        self,
        deployment_id,
        record_id,
        status,
        *,
        finalizer_token=None,
    ):
        raise DeploymentLifecycleConflict("ownership changed")


def _patch_registry(monkeypatch, registry):
    monkeypatch.setattr("app.services.aws_agent_registry.get_registry", lambda: registry, raising=True)


def test_no_op_when_registry_disabled(monkeypatch):
    _patch_registry(monkeypatch, None)
    store = _FakeStore()
    sus._auto_register_in_aws_registry(
        store=store,
        deployment_id="d1",
        runtime_arn="arn:rt",
        runtime_endpoint="https://e",
        friendly_runtime_name="agent1",
        is_a2a=False,
    )
    assert store.saved is None  # nothing registered


def test_idempotent_when_already_registered(monkeypatch):
    reg = _FakeRegistry()
    _patch_registry(monkeypatch, reg)
    store = _FakeStore(existing_record="rec-existing")
    sus._auto_register_in_aws_registry(
        store=store,
        deployment_id="d1",
        runtime_arn="arn:rt",
        runtime_endpoint="https://e",
        friendly_runtime_name="agent1",
        is_a2a=False,
    )
    assert reg.registered is None  # skipped — already has a record
    assert store.saved is None


def test_custom_descriptor_for_non_a2a(monkeypatch):
    reg = _FakeRegistry()
    _patch_registry(monkeypatch, reg)
    store = _FakeStore()
    sus._auto_register_in_aws_registry(
        store=store,
        deployment_id="d1",
        runtime_arn="arn:rt",
        runtime_endpoint="https://e",
        friendly_runtime_name="agent1",
        is_a2a=False,
    )
    assert reg.registered["type"] == "CUSTOM"
    assert "custom" in reg.registered["descriptors"]
    assert store.saved == ("d1", "rec4567890ab", "DRAFT")


def test_a2a_descriptor_for_a2a_runtime(monkeypatch):
    reg = _FakeRegistry()
    _patch_registry(monkeypatch, reg)
    store = _FakeStore()
    sus._auto_register_in_aws_registry(
        store=store,
        deployment_id="d2",
        runtime_arn="arn:rt2",
        runtime_endpoint="https://e2",
        friendly_runtime_name="peer-agent",
        is_a2a=True,
    )
    # GA: an A2A runtime becomes an AGENT record carrying an a2aAgentCard
    # descriptor. The preview "A2A" recordType no longer exists.
    assert reg.registered["type"] == "AGENT"
    assert "a2aAgentCard" in reg.registered["descriptors"]
    assert "a2a" not in reg.registered["descriptors"]
    assert store.saved[1] == "rec4567890ab"


def test_registry_pointer_write_carries_the_finalizer_token(monkeypatch):
    reg = _FakeRegistry()
    _patch_registry(monkeypatch, reg)
    store = _FakeStore()

    sus._auto_register_in_aws_registry(
        store=store,
        deployment_id="d-token",
        runtime_arn="arn:rt",
        runtime_endpoint="https://e",
        friendly_runtime_name="agent-token",
        is_a2a=False,
        finalizer_token="lease-token",
    )

    assert store.finalizer_token == "lease-token"


def test_a_new_registry_record_is_compensated_when_local_ownership_is_lost(
    monkeypatch,
):
    reg = _FakeRegistry()
    _patch_registry(monkeypatch, reg)

    with pytest.raises(DeploymentLifecycleConflict):
        sus._auto_register_in_aws_registry(
            store=_RejectingStore(),
            deployment_id="d-race",
            runtime_arn="arn:rt",
            runtime_endpoint="https://e",
            friendly_runtime_name="agent-race",
            is_a2a=False,
            finalizer_token="stale-token",
        )

    assert reg.deleted == ["rec4567890ab"]


def test_a_refreshed_registry_record_is_never_compensation_deleted(
    monkeypatch,
):
    reg = _FakeRegistry(updated=True)
    _patch_registry(monkeypatch, reg)

    with pytest.raises(DeploymentLifecycleConflict):
        sus._auto_register_in_aws_registry(
            store=_RejectingStore(),
            deployment_id="d-race",
            runtime_arn="arn:rt",
            runtime_endpoint="https://e",
            friendly_runtime_name="agent-race",
            is_a2a=False,
            finalizer_token="stale-token",
        )

    assert reg.deleted == []
