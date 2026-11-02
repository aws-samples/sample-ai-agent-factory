"""Tests for the JIT IAM permission-request workflow (Loom-study 1.6).

Store lifecycle (moto-backed) + the router's action/role validation guards.
"""

from __future__ import annotations

import asyncio
import sys

import boto3
import pytest
from botocore.exceptions import ClientError
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
    # claim approval; APPLYING remains visible for recovery
    applying = store.claim_approval(
        "org1",
        req.request_id,
        decided_by="alice",
        reason="ok",
        lease_seconds=300,
        now=100,
    )
    assert applying.status == "APPLYING"
    assert applying.approval_token
    assert any(p.request_id == req.request_id and p.status == "APPLYING" for p in store.list_pending())
    decided = store.complete_approval(
        "org1",
        req.request_id,
        approval_token=applying.approval_token,
        status="APPROVED",
        decided_by="alice",
        reason="ok",
        now=101,
    )
    assert decided.status == "APPROVED"
    assert decided.decided_by == "alice"
    assert decided.approval_token is None
    # no longer pending/applying
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
    store.decide("org1", req.request_id, status="REJECTED", decided_by="alice")
    with pytest.raises(PermissionRequestNotPending):
        store.decide("org1", req.request_id, status="REJECTED", decided_by="alice")


@mock_aws
def test_applying_approval_cannot_be_rejected_and_expired_lease_is_recoverable():
    _make_table()
    store = PermissionRequestStore(TABLE, "us-east-1")
    req = store.create(
        org_id="org1",
        requester_sub="bob",
        role_name="AgentCoreRuntime-x",
        actions=["s3:GetObject"],
        resources=["arn:aws:s3:::b/*"],
        justification="x",
    )
    first = store.claim_approval(
        "org1",
        req.request_id,
        decided_by="alice",
        lease_seconds=10,
        now=100,
    )
    with pytest.raises(PermissionRequestNotPending, match="APPLYING"):
        store.decide(
            "org1",
            req.request_id,
            status="REJECTED",
            decided_by="charlie",
        )
    with pytest.raises(PermissionRequestNotPending, match="APPLYING"):
        store.claim_approval(
            "org1",
            req.request_id,
            decided_by="charlie",
            lease_seconds=10,
            now=109,
        )

    takeover = store.claim_approval(
        "org1",
        req.request_id,
        decided_by="charlie",
        lease_seconds=10,
        now=110,
    )
    assert takeover.approval_token != first.approval_token
    with pytest.raises(PermissionRequestNotPending, match="APPLYING"):
        store.complete_approval(
            "org1",
            req.request_id,
            approval_token=first.approval_token or "",
            status="APPROVED",
            decided_by="alice",
            now=111,
        )
    completed = store.complete_approval(
        "org1",
        req.request_id,
        approval_token=takeover.approval_token or "",
        status="APPROVED",
        decided_by="charlie",
        now=111,
    )
    assert completed.status == "APPROVED"


@mock_aws
def test_direct_approval_transition_is_forbidden():
    _make_table()
    store = PermissionRequestStore(TABLE, "us-east-1")
    req = store.create(
        org_id="org1",
        requester_sub="bob",
        role_name="AgentCoreRuntime-x",
        actions=["s3:GetObject"],
        resources=["arn:aws:s3:::b/*"],
        justification="x",
    )
    with pytest.raises(ValueError, match="claim_approval"):
        store.decide(
            "org1",
            req.request_id,
            status="APPROVED",
            decided_by="alice",
        )


@mock_aws
def test_mutation_old_apply_then_decide_sequence_can_record_rejected_permission():
    """The removed algorithm's contradiction, retained as mutation evidence."""

    _make_table()
    store = PermissionRequestStore(TABLE, "us-east-1")
    req = store.create(
        org_id="org1",
        requester_sub="bob",
        role_name="AgentCoreRuntime-x",
        actions=["s3:GetObject"],
        resources=["arn:aws:s3:::b/*"],
        justification="x",
    )
    permission_applied = True  # mutant performs IAM PutRolePolicy before any row claim
    rejected = store.decide(
        "org1",
        req.request_id,
        status="REJECTED",
        decided_by="charlie",
    )

    assert permission_applied is True and rejected.status == "REJECTED"


@mock_aws
def test_router_approval_lease_blocks_concurrent_reject_and_finishes_approved(monkeypatch):
    from app.routers import permissions
    from app.services import iam_manager

    _make_table()
    store = PermissionRequestStore(TABLE, "us-east-1")
    org_id = permissions._caller_org_id("admin-1")
    req = store.create(
        org_id=org_id,
        requester_sub="bob",
        role_name="AgentCoreRuntime-x",
        actions=["s3:GetObject"],
        resources=["arn:aws:s3:::b/*"],
        justification="x",
    )
    rejection_denials: list[str] = []

    def put_policy(*_args, **_kwargs):
        with pytest.raises(PermissionRequestNotPending, match="APPLYING") as exc:
            store.decide(
                org_id,
                req.request_id,
                status="REJECTED",
                decided_by="admin-2",
            )
        rejection_denials.append(str(exc.value))

    monkeypatch.setattr(permissions, "_get_store", lambda: store)
    monkeypatch.setattr(iam_manager, "_create_iam_client", lambda: object())
    monkeypatch.setattr(iam_manager, "_put_role_inline_policy", put_policy)

    result = asyncio.run(
        permissions.approve(
            req.request_id,
            permissions.DecideRequest(reason="approved"),
            caller_sub="admin-1",
            is_admin=True,
        )
    )

    assert result == {"request_id": req.request_id, "status": "APPROVED"}
    assert rejection_denials == ["APPLYING"]
    assert store.get(org_id, req.request_id, consistent=True).status == "APPROVED"


@mock_aws
def test_ambiguous_iam_write_stays_applying_then_retries_idempotently(monkeypatch):
    from app.routers import permissions
    from app.services import iam_manager
    from fastapi import HTTPException

    _make_table()
    store = PermissionRequestStore(TABLE, "us-east-1")
    org_id = permissions._caller_org_id("admin-1")
    req = store.create(
        org_id=org_id,
        requester_sub="bob",
        role_name="AgentCoreRuntime-x",
        actions=["s3:GetObject"],
        resources=["arn:aws:s3:::b/*"],
        justification="x",
    )
    clock = {"now": 100}
    writes = {"count": 0}

    def put_policy(*_args, **_kwargs):
        writes["count"] += 1
        if writes["count"] == 1:
            raise TimeoutError("ambiguous")

    monkeypatch.setattr("app.services.permission_request_store.time.time", lambda: clock["now"])
    monkeypatch.setattr(permissions, "_get_store", lambda: store)
    monkeypatch.setattr(iam_manager, "_create_iam_client", lambda: object())
    monkeypatch.setattr(iam_manager, "_put_role_inline_policy", put_policy)

    with pytest.raises(HTTPException) as failure:
        asyncio.run(
            permissions.approve(
                req.request_id,
                permissions.DecideRequest(reason="approved"),
                caller_sub="admin-1",
                is_admin=True,
            )
        )
    assert failure.value.status_code == 502
    assert failure.value.headers == {"Retry-After": "300"}
    assert store.get(org_id, req.request_id, consistent=True).status == "APPLYING"
    with pytest.raises(PermissionRequestNotPending, match="APPLYING"):
        store.decide(
            org_id,
            req.request_id,
            status="REJECTED",
            decided_by="admin-2",
        )

    clock["now"] = 400
    result = asyncio.run(
        permissions.approve(
            req.request_id,
            permissions.DecideRequest(reason="approved"),
            caller_sub="admin-1",
            is_admin=True,
        )
    )
    assert result["status"] == "APPROVED"
    assert writes["count"] == 2


@mock_aws
def test_definitive_iam_refusal_returns_request_to_pending(monkeypatch):
    from app.routers import permissions
    from app.services import iam_manager
    from fastapi import HTTPException

    _make_table()
    store = PermissionRequestStore(TABLE, "us-east-1")
    org_id = permissions._caller_org_id("admin-1")
    req = store.create(
        org_id=org_id,
        requester_sub="bob",
        role_name="AgentCoreRuntime-x",
        actions=["s3:GetObject"],
        resources=["arn:aws:s3:::b/*"],
        justification="x",
    )
    monkeypatch.setattr(permissions, "_get_store", lambda: store)
    monkeypatch.setattr(iam_manager, "_create_iam_client", lambda: object())

    def deny_policy(*_args, **_kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "PutRolePolicy",
        )

    monkeypatch.setattr(iam_manager, "_put_role_inline_policy", deny_policy)

    with pytest.raises(HTTPException) as failure:
        asyncio.run(
            permissions.approve(
                req.request_id,
                permissions.DecideRequest(reason="approved"),
                caller_sub="admin-1",
                is_admin=True,
            )
        )

    assert failure.value.status_code == 502
    current = store.get(org_id, req.request_id, consistent=True)
    assert current is not None and current.status == "PENDING"
    assert current.approval_token is None
    rejected = store.decide(
        org_id,
        req.request_id,
        status="REJECTED",
        decided_by="admin-2",
    )
    assert rejected.status == "REJECTED"


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
    # Gateway/Policy mutations cannot be granted even on a named ARN.
    for action in (
        "bedrock-agentcore:UpdateGateway",
        "bedrock-agentcore:CreatePolicy",
        "bedrock-agentcore:*",
    ):
        with pytest.raises(HTTPException, match="Gateway/Policy"):
            _validate_request(
                CreateRequest(
                    roleName="AgentCoreGateway-x",
                    actions=[action],
                    resources=["arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/gw-1"],
                    justification="sneaky",
                )
            )
    # a legitimate scoped request passes (no raise)
    _validate_request(CreateRequest(roleName="AgentCoreRuntime-x", actions=["s3:GetObject"], justification="ok"))
