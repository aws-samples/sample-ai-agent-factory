"""Tests for the extended audit analytics summarize (Loom-study 5.2)."""

from __future__ import annotations

import sys

import boto3
from moto import mock_aws

sys.path.insert(0, "src")

from app.services.audit_store import AuditEvent, AuditStore  # noqa: E402

TABLE = "audit-test"


def _make_table():
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "org_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
    )


@mock_aws
def test_summarize_adds_by_day_and_distinct_counts():
    _make_table()
    store = AuditStore(TABLE, "us-east-1")
    # two days, two actors, two sessions
    store.record(AuditEvent(org_id="default", actor_sub="alice", action="deploy", method="POST", path="/x", status_code=200, ts="2026-07-15T10:00:00Z", session_uuid="s1"))
    store.record(AuditEvent(org_id="default", actor_sub="alice", action="deploy", method="POST", path="/x", status_code=200, ts="2026-07-15T11:00:00Z", session_uuid="s1"))
    store.record(AuditEvent(org_id="default", actor_sub="bob", action="delete", method="DELETE", path="/y", status_code=200, ts="2026-07-16T09:00:00Z", session_uuid="s2"))

    s = store.summarize("default")
    assert s["total"] == 3
    assert s["distinct_actors"] == 2
    assert s["distinct_sessions"] == 2
    # by_day is a sorted chart-ready series
    days = {d["day"]: d["count"] for d in s["by_day"]}
    assert days == {"2026-07-15": 2, "2026-07-16": 1}
    assert [d["day"] for d in s["by_day"]] == ["2026-07-15", "2026-07-16"]  # sorted
    # original rollups preserved
    assert s["by_action"] == {"deploy": 2, "delete": 1}
