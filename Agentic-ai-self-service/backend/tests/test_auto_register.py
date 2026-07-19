"""Tests for AWS Agent Registry auto-registration on deploy (Loom-study 0.4).

register() previously had ZERO callers, so deployed agents were never federated
into the registry. The status_update step now calls _auto_register_in_aws_registry
on the SUCCEEDED path. These tests exercise that helper with a mocked registry +
store (no AWS).
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from app.step_handlers import status_update_step as sus  # noqa: E402


class _FakeStore:
    def __init__(self, existing_record=None):
        self._existing = existing_record
        self.saved = None

    def get_registry_record_id(self, deployment_id):  # noqa: ARG002
        return self._existing

    def set_registry_record(self, deployment_id, record_id, status):
        self.saved = (deployment_id, record_id, status)


class _FakeRegistry:
    def __init__(self):
        self.registered = None

    def register(self, name, descriptor_type, descriptors, description=""):  # noqa: ARG002
        self.registered = {"name": name, "type": descriptor_type, "descriptors": descriptors}
        return {"record_id": "rec-123", "arn": "arn:...:record/rec-123", "status": "DRAFT"}


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
    assert reg.registered["type"] == "custom"
    assert "custom" in reg.registered["descriptors"]
    assert store.saved == ("d1", "rec-123", "DRAFT")


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
    assert reg.registered["type"] == "a2a"
    assert "a2a" in reg.registered["descriptors"]
    assert store.saved[1] == "rec-123"
