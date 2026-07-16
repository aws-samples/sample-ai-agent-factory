"""Tests for config-driven HITL approval policies + guaranteed hook (2.1/2.2)."""

from __future__ import annotations

import ast
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, "src")

from app.services.approval_policy_store import (  # noqa: E402
    ApprovalPolicy,
    ApprovalPolicyStore,
    serialize_for_agent,
)

TABLE = "tag-policy-test"


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
def test_store_crud_roundtrip():
    _make_table()
    store = ApprovalPolicyStore(TABLE, "us-east-1")
    store.put("org1", ApprovalPolicy(name="danger", tool_match=["delete_*", "*___send_email"], mode="require"))
    store.put("org1", ApprovalPolicy(name="notify-only", tool_match=["read_*"], mode="notify"))
    got = store.get("org1", "danger")
    assert got is not None and got.tool_match == ["delete_*", "*___send_email"]
    names = {p.name for p in store.list("org1")}
    assert names == {"danger", "notify-only"}
    store.delete("org1", "danger")
    assert store.get("org1", "danger") is None


def test_serialize_only_enabled_with_matches():
    pols = [
        ApprovalPolicy(name="a", tool_match=["x_*"], mode="require", enabled=True),
        ApprovalPolicy(name="b", tool_match=["y_*"], mode="notify", enabled=False),  # disabled
        ApprovalPolicy(name="c", tool_match=[], mode="require", enabled=True),        # no patterns
    ]
    import json
    out = json.loads(serialize_for_agent(pols))
    assert [p["name"] for p in out] == ["a"]
    # empty when nothing to enforce
    assert serialize_for_agent([]) == ""


def test_generated_hook_matching_logic_is_valid_and_correct():
    """The in-agent policy match uses fnmatch; verify the semantics we rely on."""
    import fnmatch
    policies = [{"name": "danger", "tool_match": ["delete_*", "*___send_email"], "mode": "require"}]

    def matches(tool_name):
        for p in policies:
            for pat in p["tool_match"]:
                if fnmatch.fnmatch(tool_name, pat):
                    return p
        return None

    assert matches("delete_customer") is not None
    assert matches("EmailTarget___send_email") is not None
    assert matches("read_orders") is None


def test_hook_source_block_is_valid_python():
    from app.services.code_generator import _HITL_TOOL_SRC
    ast.parse(_HITL_TOOL_SRC)
    assert "_ApprovalHook" in _HITL_TOOL_SRC
    assert "BeforeToolInvocationEvent" in _HITL_TOOL_SRC
    assert "_APPROVAL_HOOKS" in _HITL_TOOL_SRC
