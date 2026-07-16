"""Phase 6: AWS Agent Registry adapter.

Pure helpers (descriptor builders, ARN parsing) + adapter behavior against a
fake control/data client that captures kwargs. No real AWS.
"""

from __future__ import annotations

import json

from app.services import aws_agent_registry as ar
from app.services.aws_agent_registry import (
    AwsAgentRegistry,
    build_a2a_descriptor,
    build_custom_descriptor,
)


# -- pure helpers ------------------------------------------------------------


def test_record_id_from_arn():
    arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:registry/reg1/record/rec-abc"
    assert ar._record_id_from_arn(arn) == "rec-abc"
    assert ar._record_id_from_arn("") == ""


def test_a2a_descriptor_shape():
    d = build_a2a_descriptor("bot", "does things", "https://x/invoke",
                             skills=[{"id": "s1", "name": "search"}])
    # Wrapped under the "a2a" type key (API-required — caught live).
    assert set(d.keys()) == {"a2a"}
    ac = d["a2a"]["agentCard"]
    card = json.loads(ac["inlineContent"])
    assert ac["schemaVersion"] == "0.3"
    assert card["name"] == "bot" and card["url"] == "https://x/invoke"
    assert card["protocolVersion"] == "0.3"
    assert card["skills"] == [{"id": "s1", "name": "search"}]


def test_a2a_description_capped_at_100():
    d = build_a2a_descriptor("bot", "x" * 200, "https://x")
    card = json.loads(d["a2a"]["agentCard"]["inlineContent"])
    assert len(card["description"]) == 100


def test_custom_descriptor_roundtrips():
    d = build_custom_descriptor({"framework": "strands", "model": "claude"})
    assert set(d.keys()) == {"custom"}
    assert json.loads(d["custom"]["inlineContent"])["framework"] == "strands"


# -- adapter (fake client) ---------------------------------------------------


class _FakeControl:
    def __init__(self):
        self.calls = []

    def get_registry(self, **kw):
        self.calls.append(("get_registry", kw))
        return {"registryId": kw["registryId"]}

    def create_registry_record(self, **kw):
        self.calls.append(("create_registry_record", kw))
        return {"recordArn": "arn:aws:bedrock-agentcore:us-east-1:1:registry/r/record/rec-1",
                "status": "CREATING"}

    def submit_registry_record_for_approval(self, **kw):
        self.calls.append(("submit", kw))

    def update_registry_record_status(self, **kw):
        self.calls.append(("update_status", kw))


class _FakeData:
    def __init__(self, results):
        self._results = results

    def search_registry_records(self, **kw):
        return {"registryRecords": self._results}


def _adapter(results=None):
    a = AwsAgentRegistry.__new__(AwsAgentRegistry)
    a.registry_id = "reg1"
    a.control = _FakeControl()
    a.data = _FakeData(results or [])
    return a


def test_available_true_when_get_registry_ok():
    a = _adapter()
    assert a.available() is True


def test_register_parses_record_id_from_arn():
    a = _adapter()
    out = a.register("bot", "A2A", build_a2a_descriptor("bot", "d", "https://x"))
    assert out["record_id"] == "rec-1"
    assert out["status"] == "CREATING"
    # correct API params were sent
    _, kw = a.control.calls[-1]
    assert kw["registryId"] == "reg1" and kw["descriptorType"] == "A2A"


def test_set_status_sends_reason():
    a = _adapter()
    a.set_status("rec-1", "APPROVED", "ok via platform")
    name, kw = a.control.calls[-1]
    assert name == "update_status"
    assert kw["status"] == "APPROVED" and kw["statusReason"] == "ok via platform"


def test_submit_for_approval():
    a = _adapter()
    a.submit_for_approval("rec-1")
    assert a.control.calls[-1][0] == "submit"


def test_search_returns_records():
    a = _adapter(results=[{"name": "found-agent"}])
    assert a.search("agent")[0]["name"] == "found-agent"
