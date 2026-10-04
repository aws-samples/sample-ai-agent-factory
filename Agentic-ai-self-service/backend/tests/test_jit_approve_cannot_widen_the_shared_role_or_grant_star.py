"""F-05 (backend half): a JIT permission request cannot target the shared runtime role or grant
write actions on ``Resource: "*"``.

Infra now denies every role-mutating verb on the two shared runtime roles, so an approve that
named one would 502 at ``PutRolePolicy`` and leave the request PENDING forever. The router refuses
it at creation (400, actionable) and again at approval (a tampered row must not reach IAM). The
``Resource`` shape was never validated: ``["*"]`` was the default and any string was accepted.
Now every resource is an ARN, and a bare ``"*"`` is accepted only when every action is read-only
(Get*/List*/Describe*/BatchGet*/Query/Scan/Head* with no wildcard in the verb).

Fixture values are fake.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.routers import permissions
from app.routers.permissions import CreateRequest, _validate_request
from app.services.auth import get_caller_sub
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

SHARED = "arn:aws:iam::123456789012:role/AgentCoreRuntime-acfe2e-p0920-shared"
SHARED_MCP = "arn:aws:iam::123456789012:role/AgentCoreRuntime-acfe2e-p0920-mcp-shared"
BUCKET = "arn:aws:s3:::probe-bucket/*"
TABLE = "arn:aws:dynamodb:us-east-1:123456789012:table/ProbeTable"


@pytest.fixture(autouse=True)
def _shared_roles(monkeypatch):
    monkeypatch.setenv("SHARED_RUNTIME_ROLE_ARN", SHARED)
    monkeypatch.setenv("SHARED_MCP_RUNTIME_ROLE_ARN", SHARED_MCP)


def _req(role="AgentCoreRuntime-support", actions=("s3:GetObject",), resources=None) -> CreateRequest:
    kwargs = {"roleName": role, "actions": list(actions), "justification": "probe"}
    if resources is not None:
        kwargs["resources"] = list(resources)
    return CreateRequest(**kwargs)


def _refused(req: CreateRequest) -> str:
    with pytest.raises(HTTPException) as excinfo:
        _validate_request(req)
    assert excinfo.value.status_code == 400
    return str(excinfo.value.detail)


# ---------------------------------------------------------------------------
# Resource shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "actions",
    [
        ("s3:GetObject",),
        ("s3:ListBucket", "s3:GetObject"),
        ("dynamodb:Query", "dynamodb:Scan"),
        ("ec2:DescribeInstances",),
    ],
)
def test_a_bare_star_resource_is_accepted_for_a_read_only_action_set(actions):
    _validate_request(_req(actions=actions, resources=["*"]))


@pytest.mark.parametrize(
    "actions",
    [
        ("s3:PutObject",),
        ("s3:GetObject", "s3:DeleteObject"),
        ("bedrock:InvokeModel",),
        ("s3:Get*",),  # a wildcard verb is not provably read-only
        ("s3:*",),
        ("dynamodb:UpdateItem",),
    ],
)
def test_a_bare_star_resource_is_refused_for_anything_that_writes(actions):
    detail = _refused(_req(actions=actions, resources=["*"]))
    assert "read-only" in detail


@pytest.mark.parametrize("actions", [("s3:PutObject",), ("bedrock:InvokeModel",), ("s3:Get*",)])
def test_a_write_action_is_fine_on_a_named_arn(actions):
    _validate_request(_req(actions=actions, resources=[BUCKET]))


@pytest.mark.parametrize(
    "resources",
    [["probe-bucket/*"], [""], ["arn:aws:s3"], ["*", "probe-bucket"], ["arn:aws:s3:::probe-bucket/*", "not an arn"]],
)
def test_a_resource_that_is_not_an_arn_is_refused(resources):
    detail = _refused(_req(actions=("s3:GetObject",), resources=resources))
    assert "ARN" in detail


def test_named_arns_of_every_common_shape_are_accepted():
    _validate_request(
        _req(
            actions=("s3:PutObject", "dynamodb:PutItem", "secretsmanager:GetSecretValue"),
            resources=[
                BUCKET,
                "arn:aws:s3:::probe-bucket",
                TABLE,
                "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-connector/x/*",
                "arn:aws-us-gov:s3:::gov-bucket/*",
            ],
        )
    )


def test_an_empty_resource_list_is_refused():
    with pytest.raises(ValueError):
        _req(resources=[])


def test_the_default_resource_is_still_star_and_still_needs_read_only_actions():
    """The pre-existing test in test_permission_requests relies on the default passing for s3:GetObject."""
    _validate_request(_req())
    _refused(_req(actions=("s3:PutObject",)))


# ---------------------------------------------------------------------------
# The shared runtime roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    [
        "AgentCoreRuntime-acfe2e-p0920-shared",  # SHARED_RUNTIME_ROLE_ARN's name
        "AgentCoreRuntime-acfe2e-p0920-mcp-shared",  # SHARED_MCP_RUNTIME_ROLE_ARN's name
        "AgentCoreRuntime-otherstack-prod-shared",  # another stack's, by suffix
        "AgentCoreRuntime-otherstack-prod-mcp-shared",
        "AgentCoreFlowsRuntimeRole",  # the legacy shared name the Bug-62 guard also skips
    ],
)
def test_a_request_naming_a_shared_runtime_role_is_refused(role):
    detail = _refused(_req(role=role, actions=("s3:GetObject",), resources=[BUCKET]))
    assert "shared" in detail


def test_a_per_agent_or_tool_role_is_still_a_valid_target():
    for role in ("AgentCoreRuntime-support", "AgentCoreRuntime-support-shared-tools", "AgentCoreGateway-x"):
        _validate_request(_req(role=role, actions=("s3:GetObject",), resources=[BUCKET]))


def test_the_shared_names_are_read_from_the_environment_when_they_do_not_carry_the_suffix(monkeypatch):
    monkeypatch.setenv("SHARED_RUNTIME_ROLE_ARN", "arn:aws:iam::123456789012:role/AgentCoreRuntime-oddly-named")
    _refused(_req(role="AgentCoreRuntime-oddly-named", resources=[BUCKET]))


# ---------------------------------------------------------------------------
# Approve re-validates the stored row before anything reaches IAM
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self, row):
        self.row = row
        self.decided: list[tuple] = []

    def get(self, org_id, request_id):
        return self.row

    def decide(self, org_id, request_id, *, status, decided_by, reason=""):
        self.decided.append((request_id, status, decided_by))
        return SimpleNamespace(status=status)


def _client(monkeypatch, row) -> tuple[TestClient, _Store, list]:
    from app.services import iam_manager

    store = _Store(row)
    puts: list[tuple] = []
    monkeypatch.setattr(permissions, "_get_store", lambda: store)
    monkeypatch.setattr(iam_manager, "_create_iam_client", lambda: object())
    monkeypatch.setattr(
        iam_manager, "_put_role_inline_policy", lambda iam, role, name, doc: puts.append((role, name, doc))
    )
    app = FastAPI()
    app.include_router(permissions.router)
    app.dependency_overrides[get_caller_sub] = lambda: "admin-1"
    app.dependency_overrides[permissions.caller_is_admin] = lambda: True
    return TestClient(app), store, puts


def _row(**overrides):
    base = {
        "request_id": "req-1",
        "role_name": "AgentCoreRuntime-support",
        "actions": ["s3:GetObject"],
        "resources": [BUCKET],
        "status": "PENDING",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_a_valid_row_is_applied_with_exactly_its_resources_then_approved(monkeypatch):
    client, store, puts = _client(monkeypatch, _row())
    response = client.post("/api/permissions/requests/req-1/approve", json={"reason": "ok"})
    assert response.status_code == 200, response.text
    assert puts == [
        (
            "AgentCoreRuntime-support",
            "JIT-req-1",
            {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": [BUCKET]}],
            },
        )
    ]
    assert store.decided == [("req-1", "APPROVED", "admin-1")]


@pytest.mark.parametrize(
    "tamper",
    [
        {"role_name": "AgentCoreRuntime-acfe2e-p0920-shared"},
        {"role_name": "AgentCoreRuntime-x-shared"},
        {"resources": ["*"], "actions": ["s3:PutObject"]},
        {"resources": ["probe-bucket"]},
        {"resources": []},
    ],
)
def test_a_tampered_row_is_refused_at_approval_and_never_reaches_iam(monkeypatch, tamper):
    client, store, puts = _client(monkeypatch, _row(**tamper))
    response = client.post("/api/permissions/requests/req-1/approve", json={"reason": "ok"})
    assert response.status_code == 400, response.text
    assert puts == []
    assert store.decided == []
