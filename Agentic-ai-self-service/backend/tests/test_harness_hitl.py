"""Tests for HITL on managed (harness) agents (Loom-study 2.4).

The harness invoke path can't inject a BeforeToolInvocation hook (the tool runs
in AWS's managed loop), so it detects policy-matched tool_use at the invoke
boundary and records PENDING approvals. These tests exercise that matcher.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

import app.services.harness_deployer as hd  # noqa: E402
from app.services.approval_policy_store import ApprovalPolicy  # noqa: E402


def _patch(monkeypatch, policies, recorded):
    class _Store:
        def __init__(self, *a, **k):
            pass
        def list(self, org):  # noqa: ARG002
            return policies

    # _harness_approval_check imports ApprovalPolicyStore locally, so patch it on
    # the SOURCE module (that's what the local import resolves).
    import app.services.approval_policy_store as aps
    monkeypatch.setattr(aps, "ApprovalPolicyStore", _Store)
    # capture pending records instead of hitting DDB
    monkeypatch.setattr(hd, "_record_harness_pending",
                        lambda region, table, name, sid: recorded.append(name))
    monkeypatch.setenv("TAG_POLICY_TABLE_NAME", "tp")
    monkeypatch.setenv("HITL_REQUESTS_TABLE_NAME", "hitl")


def test_no_tools_no_approval(monkeypatch):
    recorded: list = []
    _patch(monkeypatch, [ApprovalPolicy(name="p", tool_match=["*"], mode="require")], recorded)
    assert hd._harness_approval_check("us-east-1", [], "sess") == []
    assert recorded == []


def test_require_match_records_pending(monkeypatch):
    recorded: list = []
    _patch(monkeypatch, [ApprovalPolicy(name="danger", tool_match=["delete_*"], mode="require")], recorded)
    matched = hd._harness_approval_check("us-east-1", ["delete_customer", "read_orders"], "sess")
    assert matched == ["delete_customer"]
    assert recorded == ["delete_customer"]  # PENDING recorded for the require match


def test_notify_match_flags_but_does_not_record(monkeypatch):
    recorded: list = []
    _patch(monkeypatch, [ApprovalPolicy(name="watch", tool_match=["send_*"], mode="notify")], recorded)
    matched = hd._harness_approval_check("us-east-1", ["send_email"], "sess")
    assert matched == ["send_email"]      # surfaced as approval_required
    assert recorded == []                  # notify mode does not record a PENDING


def test_no_policy_table_is_noop(monkeypatch):
    recorded: list = []
    _patch(monkeypatch, [], recorded)
    monkeypatch.delenv("TAG_POLICY_TABLE_NAME", raising=False)
    assert hd._harness_approval_check("us-east-1", ["delete_x"], "sess") == []
