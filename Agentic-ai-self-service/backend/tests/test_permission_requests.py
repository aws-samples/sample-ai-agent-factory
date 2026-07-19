"""Tests for the JIT IAM permission-request workflow (Loom-study 1.6).

Store lifecycle (moto-backed) + the router's action/role validation guards.
"""

from __future__ import annotations

import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, "src")

from app.services.permission_request_store import (  # noqa: E402
    PermissionRequestNotPending,
    PermissionRequestStore,
)

TABLE = "perm-requests-test"


def _make_table():
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "request_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "org_id", "KeyType": "HASH"},
            {"AttributeName": "request_id", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "status-request_id-index",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "request_id", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )


@mock_aws
def test_create_list_decide_lifecycle():
    _make_table()
    store = PermissionRequestStore(TABLE, "us-east-1")
    req = store.create(
        org_id="org1",
        requester_sub="bob",
        role_name="AgentCoreRuntime-x",
        actions=["s3:GetObject"],
        resources=["arn:aws:s3:::b/*"],
        justification="need read",
    )
    assert req.status == "PENDING"
    # appears in the pending queue
    pending = store.list_pending()
    assert any(p.request_id == req.request_id for p in pending)
    # approve
    decided = store.decide("org1", req.request_id, status="APPROVED", decided_by="alice", reason="ok")
    assert decided.status == "APPROVED"
    assert decided.decided_by == "alice"
    # no longer pending
    assert all(p.request_id != req.request_id for p in store.list_pending())


@mock_aws
def test_double_decide_raises_not_pending():
    _make_table()
    store = PermissionRequestStore(TABLE, "us-east-1")
    req = store.create(
        org_id="org1",
        requester_sub="bob",
        role_name="AgentCoreRuntime-x",
        actions=["s3:GetObject"],
        resources=["*"],
        justification="x",
    )
    store.decide("org1", req.request_id, status="APPROVED", decided_by="alice")
    with pytest.raises(PermissionRequestNotPending):
        store.decide("org1", req.request_id, status="REJECTED", decided_by="alice")


def test_router_validation_blocks_privilege_escalation():
    from app.routers.permissions import CreateRequest, _validate_request
    from fastapi import HTTPException

    # iam:* escalation blocked
    with pytest.raises(HTTPException):
        _validate_request(
            CreateRequest(roleName="AgentCoreRuntime-x", actions=["iam:PassRole"], justification="sneaky")
        )
    # wildcard blocked
    with pytest.raises(HTTPException):
        _validate_request(CreateRequest(roleName="AgentCoreRuntime-x", actions=["*"], justification="all"))
    # non-AgentCore role blocked
    with pytest.raises(HTTPException):
        _validate_request(CreateRequest(roleName="SomeOtherRole", actions=["s3:GetObject"], justification="x"))
    # a legitimate scoped request passes (no raise)
    _validate_request(CreateRequest(roleName="AgentCoreRuntime-x", actions=["s3:GetObject"], justification="ok"))
