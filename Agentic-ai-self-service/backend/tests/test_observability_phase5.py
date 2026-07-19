"""Phase 5: trace waterfall shaping + audit store/classifier.

build_waterfall + classify_action are pure; AuditStore is moto-backed.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from app.services import audit_store as as_mod
from app.services.audit_store import (
    AuditEvent,
    AuditStore,
    classify_action,
)
from app.services.trace_query import build_waterfall

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

# -- build_waterfall (pure) --------------------------------------------------


def _span(sid, parent, name, start_ms, dur_ms):
    return {
        "spanId": sid,
        "parentSpanId": parent,
        "name": name,
        "traceId": "t1",
        "startTimeUnixNano": str(start_ms * 1_000_000),
        "endTimeUnixNano": str((start_ms + dur_ms) * 1_000_000),
    }


def test_waterfall_empty():
    wf = build_waterfall([])
    assert wf["spans"] == [] and wf["total_ms"] == 0


def test_waterfall_nesting_and_offsets():
    spans = [
        _span("root", "", "invoke", 1000, 500),
        _span("child1", "root", "llm.call", 1100, 200),
        _span("child2", "root", "tool.call", 1350, 100),
    ]
    wf = build_waterfall(spans)
    assert wf["trace_id"] == "t1"
    assert len(wf["spans"]) == 1  # one root
    root = wf["spans"][0]
    assert root["name"] == "invoke" and root["offset_ms"] == 0.0
    assert root["duration_ms"] == 500.0
    assert len(root["children"]) == 2
    # children offsets are relative to the earliest start (root @1000ms)
    assert root["children"][0]["offset_ms"] == 100.0  # child1 @1100
    assert root["children"][0]["depth"] == 1
    assert root["children"][1]["offset_ms"] == 350.0  # child2 @1350
    assert wf["total_ms"] == 500.0


def test_waterfall_orphan_is_root():
    # parent missing → treated as a root, not dropped.
    spans = [_span("a", "ghost", "orphan", 2000, 50)]
    wf = build_waterfall(spans)
    assert len(wf["spans"]) == 1 and wf["spans"][0]["name"] == "orphan"


def test_waterfall_multiple_roots_sorted_by_offset():
    spans = [
        _span("b", "", "second", 1500, 10),
        _span("a", "", "first", 1000, 10),
    ]
    wf = build_waterfall(spans)
    assert [s["name"] for s in wf["spans"]] == ["first", "second"]


# -- classify_action (pure) --------------------------------------------------


def test_classify_deploy():
    assert classify_action("POST", "/api/deploy") == "agent.deploy"


def test_classify_longest_prefix_wins():
    # tag-profiles must beat the shorter /api/settings/tags rule.
    assert classify_action("POST", "/api/settings/tag-profiles") == "tag.profile.write"
    assert classify_action("POST", "/api/settings/tags") == "tag.policy.write"


def test_classify_reads_are_ignored():
    assert classify_action("GET", "/api/deployments") is None
    assert classify_action("GET", "/api/cost/budgets") is None


def test_classify_budget_delete():
    assert classify_action("DELETE", "/api/cost/budgets/owner/me") == "budget.delete"


# -- AuditStore (moto) -------------------------------------------------------


def _create_table() -> None:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="Audit",
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
        s = AuditStore(table_name="Audit", region="us-east-1")
        as_mod._store = s
        yield s


def test_record_and_summarize(store: AuditStore):
    for i in range(3):
        store.record(
            AuditEvent(
                org_id="default",
                actor_sub="alice",
                action="agent.deploy",
                method="POST",
                path="/api/deploy",
                status_code=202,
                ts=f"2026-07-15T10:0{i}:00Z",
            )
        )
    store.record(
        AuditEvent(
            org_id="default",
            actor_sub="bob",
            action="budget.write",
            method="POST",
            path="/api/cost/budgets",
            status_code=200,
            ts="2026-07-15T10:05:00Z",
        )
    )
    summ = store.summarize("default")
    assert summ["total"] == 4
    assert summ["by_action"]["agent.deploy"] == 3
    assert summ["by_actor"]["alice"] == 3 and summ["by_actor"]["bob"] == 1


def test_list_recent_newest_first(store: AuditStore):
    store.record(
        AuditEvent(
            org_id="default",
            actor_sub="a",
            action="x",
            method="POST",
            path="/p",
            status_code=200,
            ts="2026-07-15T10:00:00Z",
        )
    )
    store.record(
        AuditEvent(
            org_id="default",
            actor_sub="a",
            action="y",
            method="POST",
            path="/p",
            status_code=200,
            ts="2026-07-15T11:00:00Z",
        )
    )
    recent = store.list_recent("default")
    assert recent[0].action == "y"  # newest first
