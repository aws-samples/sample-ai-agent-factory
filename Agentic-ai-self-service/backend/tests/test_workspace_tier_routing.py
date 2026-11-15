"""Risk-tier routing after admission (plan §3.B, step 3 second half).

* ``workspace_approval`` helpers -- pure: required/approved/pending groups, slot routing.
* ``POST /api/deploy`` through the real deployment app with the gate-ordering spy: a medium-risk
  canvas (a ``write`` connector) lands in STAGING and its rows carry the admission; an explicit
  production request is a 409 with zero side effects; a low-risk canvas and a flag-off deploy are
  unchanged.
* ``POST /api/runtimes/{name}/versions/{id}/approve`` and the promote gate, over a moto-backed REAL
  ``AgentVersionsStore`` so the fenced compare-and-set is what runs: owner self-approval 403, a
  non-member 403, a member's approval recorded once (idempotent), a foreign group 409, promotion
  blocked with the exact pending list until every group approved, then released; the store's
  race handling (stale read, concurrent approver) is driven directly.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from app.routers import versions as versions_router
from app.schemas.workspace_policy import WorkspacePolicy
from app.services import spec_service
from app.services import workspace_approval as wa
from app.services.agent_versions_store import (
    AdmissionApprovalConflict,
    AgentVersion,
    AgentVersionsStore,
    RuntimeSlots,
)
from app.services.spec_migration import migrate_v1_flow
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_deploy_gate_ordering import OWNER, _body, _client
from tests.test_deploy_gate_ordering import spy as _gate_spy
from tests.test_workspace_admission import FLOW_ID, JIRA, _business_canvas, _flags
from tests.test_workspace_admission import table as _policy_table

pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

#: The gate-ordering side-effect log and the fake policy table, under the names the tests use.
spy = pytest.fixture(_gate_spy.__wrapped__)
table = pytest.fixture(_policy_table.__wrapped__)

RUNTIME = "finance_helper"
VERSION = "v-20260101T000000Z-aaaa1111"
APPROVER = "approver-sub-1"
SECURITY = "security-sub-1"


@pytest.fixture(autouse=True)
def _fresh_flag_cache():
    from app.services import feature_flags as ff

    ff.reset_feature_flag_cache()
    yield
    ff.reset_feature_flag_cache()


# --------------------------------------------------------------------------- helpers


def _admission(approvers: list[str], tier: str = "medium") -> dict[str, Any]:
    return {
        "outcome": "admitted",
        "workspaceId": "default",
        "tier": tier,
        "reasons": [{"code": "write_tool", "kind": "floor", "tier": "medium", "message": "write tool"}],
        "approvers": approvers,
        "violations": [],
        "admissionVersion": 1,
        "classifierVersion": 1,
    }


def test_pending_groups_follow_policy_order_and_complete_records_count():
    admission = _admission(["g-admins-security", "g-approvers"], tier="high")
    assert wa.required_approver_groups(admission) == ["g-admins-security", "g-approvers"]
    assert wa.pending_groups(admission, None) == ["g-admins-security", "g-approvers"]
    approvals = [wa.approval_record("g-approvers", APPROVER, "t"), wa.approval_record("g-other", "x", "t")]
    assert wa.pending_groups(admission, approvals) == ["g-admins-security"]
    assert wa.approved_groups(approvals) == {"g-approvers", "g-other"}
    assert wa.requires_approval(admission) is True
    assert wa.admission_tier(admission) == "high"


@pytest.mark.parametrize("admission", [None, _admission([], tier="low"), _admission([], tier="medium")])
def test_no_approver_groups_means_no_approval_requirement(admission):
    assert wa.required_approver_groups(admission) == []
    assert wa.requires_approval(admission) is False
    assert wa.route_deployment_slot(None, "production", admission) == "production"
    assert wa.route_deployment_slot("production", "production", admission) == "production"
    assert wa.route_deployment_slot("staging", "staging", admission) == "staging"


@pytest.mark.parametrize(
    "admission",
    [
        {},  # present but says nothing about approvers
        {"approvers": "g-x"},  # a string, not a list
        {"approvers": ["g-approvers", 7]},  # a non-string member
        {"approvers": [""]},  # an empty name
        {"approvers": ["bad group!"]},  # a name no approver could ever satisfy
        {"approvers": []},  # an incomplete decision: no outcome, tier, workspace or reasons
        {**_admission(["g-approvers"]), "outcome": "denied"},  # a denied decision is never persisted
        {**_admission(["g-approvers"]), "admissionVersion": "x"},  # a corrupt version marker
        {**_admission(["g-approvers"]), "admissionVersion": 999},  # a version this build does not know
        {**_admission(["g-approvers"]), "classifierVersion": 999},
        {k: v for k, v in _admission(["g-approvers"]).items() if k != "admissionVersion"},  # marker omitted
        "not-an-object",
    ],
)
def test_a_present_but_malformed_admission_fails_closed(admission):
    """A requirement that cannot be read cannot be shown to be satisfied: it raises, so promotion
    and routing block rather than release."""
    with pytest.raises(wa.MalformedAdmissionError):
        wa.required_approver_groups(admission)
    with pytest.raises(wa.MalformedAdmissionError):
        wa.pending_groups(admission, None)
    with pytest.raises(wa.MalformedAdmissionError):
        wa.route_deployment_slot(None, "production", admission)


@pytest.mark.parametrize(
    "approvals",
    [
        [{"group": "g-approvers"}],  # no approver, no timestamp
        [{"group": "g-approvers", "approverSub": "", "approvedAt": "t"}],
        [{"group": "", "approverSub": "x", "approvedAt": "t"}],
        ["junk"],
        {"group": "g-approvers"},  # not a list
    ],
)
def test_an_incomplete_approval_record_never_counts_as_an_approval(approvals):
    with pytest.raises(wa.MalformedAdmissionError):
        wa.approved_groups(approvals)
    with pytest.raises(wa.MalformedAdmissionError):
        wa.pending_groups(_admission(["g-approvers"]), approvals)


def test_an_approval_record_naming_the_owner_is_tampering_and_blocks(seeded, monkeypatch):
    """Finding 4 (round 3): the store never writes an owner's approval, so one on the row is corruption;
    it must not count towards release, and promotion stays blocked."""
    forged = [wa.approval_record("g-admins-security", OWNER, "t"), wa.approval_record("g-approvers", APPROVER, "t")]
    with pytest.raises(wa.MalformedAdmissionError):
        wa.approved_groups(forged, owner_sub=OWNER)
    assert wa.approved_groups(forged) == {
        "g-admins-security",
        "g-approvers",
    }  # without the owner nothing can be checked
    seeded._table.update_item(
        Key={"runtime_name": RUNTIME, "version_id": VERSION},
        UpdateExpression="SET admission_approvals = :a",
        ExpressionAttributeValues={":a": forged},
    )
    owner = _router_client(monkeypatch, seeded, sub=OWNER, groups=("g-users-default",))
    blocked = owner.post(PROMOTE, json={"slot": "production"})
    assert blocked.status_code == 409, blocked.text
    assert "malformed" in blocked.json()["detail"]
    owner.slot_write.assert_not_called()


def test_an_approval_requirement_routes_to_staging_and_refuses_explicit_production():
    admission = _admission(["g-approvers"])
    assert wa.route_deployment_slot(None, "production", admission) == "staging"
    assert wa.route_deployment_slot("staging", "staging", admission) == "staging"
    with pytest.raises(wa.ProductionRequiresApprovalError) as excinfo:
        wa.route_deployment_slot("production", "production", admission)
    assert excinfo.value.tier == "medium"
    assert excinfo.value.groups == ["g-approvers"]
    assert "Nothing was created" in str(excinfo.value)


# --------------------------------------------------------------------------- deploy path


def _medium_risk_v2_document() -> dict[str, Any]:
    """The business canvas (web search + Jira connector) stored as Spec v2 with the Jira tool
    classified ``write``: the classification survives re-projection, so admission sees it."""
    spec = spec_service.normalize(migrate_v1_flow(_business_canvas(), spec_id=FLOW_ID, name="Support", owner_sub=OWNER))
    tools = []
    for tool in spec.tools:
        tools.append(tool.model_copy(update={"risk_class": "write"}) if tool.type == "connector" else tool)
    spec = spec.model_copy(update={"tools": tools})
    document = spec.model_dump(by_alias=True, mode="json")
    assert spec_service.load(document).tools[0].risk_class == "write"
    return document


@pytest.fixture
def medium_flow(monkeypatch):
    import importlib
    from datetime import UTC, datetime

    from app.models.enums import DeploymentStatus
    from app.models.flow import Flow

    module = importlib.import_module("app.services.flow_storage")
    store = module.FlowStorage()
    now = datetime.now(UTC)
    flow = Flow(
        id=FLOW_ID,
        name="Support agent",
        workflow=_medium_risk_v2_document(),
        deployment_status=DeploymentStatus.NOT_DEPLOYED,
        created_at=now,
        updated_at=now,
        owner_sub=OWNER,
    )
    store._flows[flow.id] = flow
    monkeypatch.setattr(module, "_active_storage", store)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    return flow


def _standard_policy(**overrides: Any) -> WorkspacePolicy:
    document: dict[str, Any] = {
        "workspaceId": "default",
        "name": "Standard",
        "approval": {"medium": ["g-approvers"], "high": ["g-admins-security", "g-approvers"]},
    }
    document.update(overrides)
    return WorkspacePolicy.model_validate(document)


def test_a_medium_risk_canvas_lands_in_staging_and_its_rows_carry_the_admission(spy, medium_flow, table, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put("default", _standard_policy(), expected_revision=None)

    response = _client().post("/api/deploy", json=_body(flowId=FLOW_ID, connectors=[JIRA]))

    assert response.status_code == 202, response.text
    assert "staging slot" in response.json()["message"]
    assert "g-approvers" in response.json()["message"]
    state = spy.store.create.call_args[0][0]
    assert state.deployment_slot == "staging"
    assert state.admission["tier"] == "medium"
    assert state.admission["approvers"] == ["g-approvers"]
    assert [reason["code"] for reason in state.admission["reasons"]] == ["write_tool"]
    version = spy.versions.put.call_args[0][0]
    assert isinstance(version, AgentVersion)
    assert version.admission == state.admission
    assert version.admission_approvals is None
    assert spy.calls.count("StartExecution") == 1


def test_an_explicit_production_request_for_a_medium_risk_canvas_is_409_with_zero_side_effects(
    spy, medium_flow, table, monkeypatch
):
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put("default", _standard_policy(), expected_revision=None)

    response = _client().post("/api/deploy", json=_body(flowId=FLOW_ID, connectors=[JIRA], deploymentSlot="production"))

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == wa.production_requires_approval_message("medium", ["g-approvers"])
    assert spy.side_effects == [], spy.calls


def test_a_low_risk_canvas_still_lands_in_production_with_an_auto_approved_admission(
    spy, medium_flow, table, monkeypatch
):
    """Same canvas, but the policy does not route medium anywhere: the agent is still medium risk
    (the classifier is policy-independent) yet has no approver, so it deploys as before."""
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put("default", _standard_policy(approval={}), expected_revision=None)

    response = _client().post("/api/deploy", json=_body(flowId=FLOW_ID, connectors=[JIRA]))

    assert response.status_code == 202, response.text
    assert response.json()["message"] == "Deployment started"
    state = spy.store.create.call_args[0][0]
    assert state.deployment_slot == "production"
    assert state.admission["tier"] == "medium"
    assert state.admission["approvers"] == []


def test_flag_off_persists_no_admission_and_keeps_the_production_slot(spy, medium_flow, table, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": False})

    response = _client().post("/api/deploy", json=_body(flowId=FLOW_ID, connectors=[JIRA]))

    assert response.status_code == 202, response.text
    assert response.json()["message"] == "Deployment started"
    state = spy.store.create.call_args[0][0]
    assert state.deployment_slot == "production"
    assert state.admission is None
    assert spy.versions.put.call_args[0][0].admission is None


# --------------------------------------------------------------------------- approve + promote, real store


@pytest.fixture
def versions_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="AgentVersions",
            KeySchema=[
                {"AttributeName": "runtime_name", "KeyType": "HASH"},
                {"AttributeName": "version_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "runtime_name", "AttributeType": "S"},
                {"AttributeName": "version_id", "AttributeType": "S"},
                {"AttributeName": "owner_sub", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "owner_sub-version_id-index",
                    "KeySchema": [
                        {"AttributeName": "owner_sub", "KeyType": "HASH"},
                        {"AttributeName": "version_id", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield AgentVersionsStore(table_name="AgentVersions", region="us-east-1")


def _version(**overrides: Any) -> AgentVersion:
    values: dict[str, Any] = {
        "runtime_name": RUNTIME,
        "version_id": VERSION,
        "owner_sub": OWNER,
        "created_at": "2026-01-01T00:00:00+00:00",
        "deployment_id": "dep-1111",
        "agentcore_runtime_name": f"{RUNTIME}_aaaa1111",
        "runtime_id": "rt-1111",
        "runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/rt-1111",
        "status": "succeeded",
        "admission": _admission(["g-admins-security", "g-approvers"], tier="high"),
    }
    values.update(overrides)
    return AgentVersion(**values)


@pytest.fixture
def seeded(versions_table: AgentVersionsStore) -> AgentVersionsStore:
    versions_table.put(_version())
    return versions_table


def _router_client(monkeypatch, store: AgentVersionsStore, *, sub: str, groups: tuple[str, ...]) -> TestClient:
    """The real versions router over the moto store, with ``sub``/``groups`` injected the way the
    JWT authorizer delivers them, RBAC enforcing, and only the slot write faked."""
    monkeypatch.setenv("RBAC_ENFORCE", "true")
    slots = MagicMock()
    slots.get.return_value = RuntimeSlots(runtime_name=RUNTIME, owner_sub=OWNER, staging_version_id=VERSION)
    write = MagicMock(return_value=None)
    monkeypatch.setattr(versions_router, "get_versions_store", lambda: store)
    monkeypatch.setattr(versions_router, "get_slots_store", lambda: slots)
    monkeypatch.setattr(versions_router, "set_slot_pointers_atomically", write)
    app = FastAPI()
    app.include_router(versions_router.router)
    claims = {"sub": sub, "cognito:groups": list(groups)}

    @app.middleware("http")
    async def _inject(request, call_next):
        request.scope["aws.event"] = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}
        return await call_next(request)

    client = TestClient(app, raise_server_exceptions=False)
    client.slot_write = write  # type: ignore[attr-defined]
    return client


APPROVE = f"/api/runtimes/{RUNTIME}/versions/{VERSION}/approve"
PROMOTE = f"/api/runtimes/{RUNTIME}/versions/{VERSION}/promote"


def test_the_owner_cannot_approve_their_own_version_even_as_a_super_admin(seeded, monkeypatch):
    client = _router_client(monkeypatch, seeded, sub=OWNER, groups=("g-admins-super", "g-admins-security"))
    response = client.post(APPROVE, json={"group": "g-admins-security"})
    assert response.status_code == 403, response.text
    assert "owner cannot approve their own version" in response.json()["detail"]
    assert seeded.get(RUNTIME, VERSION).admission_approvals is None


def test_a_caller_without_the_approval_scope_is_refused_before_anything_is_read(seeded, monkeypatch):
    client = _router_client(monkeypatch, seeded, sub=APPROVER, groups=("g-users-default", "g-approvers"))
    response = client.post(APPROVE, json={"group": "g-approvers"})
    assert response.status_code == 403, response.text
    assert seeded.get(RUNTIME, VERSION).admission_approvals is None


def test_a_scope_holder_outside_every_required_group_sees_the_same_404_as_a_missing_version(seeded, monkeypatch):
    """Existence non-disclosure: hitl:write alone does not let a caller learn that another tenant's
    version exists or how it routes."""
    client = _router_client(monkeypatch, seeded, sub=SECURITY, groups=("g-admins-super",))
    response = client.post(APPROVE, json={"group": "g-approvers"})
    missing = client.post(APPROVE.replace(VERSION, "v-20260101T000000Z-ffff9999"), json={"group": "g-approvers"})
    assert response.status_code == 404, response.text
    assert response.json() == missing.json() == {"detail": "Not found"}
    assert seeded.get(RUNTIME, VERSION).admission_approvals is None


def test_a_member_of_one_required_group_naming_a_group_the_admission_did_not_route_to_is_409(seeded, monkeypatch):
    client = _router_client(monkeypatch, seeded, sub=APPROVER, groups=("g-admins-super", "g-approvers", "g-finance"))
    response = client.post(APPROVE, json={"group": "g-finance"})
    assert response.status_code == 409, response.text
    assert "not an approver group" in response.json()["detail"]


def test_a_member_of_one_required_group_naming_another_required_group_they_are_not_in_is_403(seeded, monkeypatch):
    client = _router_client(monkeypatch, seeded, sub=APPROVER, groups=("g-admins-super", "g-approvers"))
    response = client.post(APPROVE, json={"group": "g-admins-security"})
    assert response.status_code == 403, response.text
    assert "not a member of the approver group 'g-admins-security'" in response.json()["detail"]
    assert seeded.get(RUNTIME, VERSION).admission_approvals is None


def test_a_version_without_an_approval_requirement_is_409_for_its_owner_and_404_for_others(versions_table, monkeypatch):
    versions_table.put(_version(admission=None))
    owner = _router_client(monkeypatch, versions_table, sub=OWNER, groups=("g-admins-super", "g-approvers"))
    response = owner.post(APPROVE, json={"group": "g-approvers"})
    assert response.status_code == 409
    assert "does not require workspace admission approval" in response.json()["detail"]
    other = _router_client(monkeypatch, versions_table, sub=APPROVER, groups=("g-admins-super", "g-approvers"))
    assert other.post(APPROVE, json={"group": "g-approvers"}).status_code == 404


def test_an_unknown_version_is_404(versions_table, monkeypatch):
    client = _router_client(monkeypatch, versions_table, sub=APPROVER, groups=("g-admins-super", "g-approvers"))
    assert client.post(APPROVE, json={"group": "g-approvers"}).status_code == 404


def test_an_approval_request_refuses_unknown_keys(seeded, monkeypatch):
    client = _router_client(monkeypatch, seeded, sub=APPROVER, groups=("g-admins-super", "g-approvers"))
    assert client.post(APPROVE, json={"group": "g-approvers", "force": True}).status_code == 422
    assert client.post(APPROVE, json={"group": "bad group!"}).status_code == 422


def test_promotion_is_blocked_until_every_group_approved_then_released(seeded, monkeypatch):
    owner = _router_client(monkeypatch, seeded, sub=OWNER, groups=("g-users-default",))
    blocked = owner.post(PROMOTE, json={"slot": "production"})
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"] == wa.promotion_blocked_message(
        VERSION, "high", ["g-admins-security", "g-approvers"]
    )
    owner.slot_write.assert_not_called()

    # Staging moves are not gated: approval is about the production pointer.
    assert owner.post(PROMOTE, json={"slot": "staging"}).status_code == 200

    security = _router_client(monkeypatch, seeded, sub=SECURITY, groups=("g-admins-super", "g-admins-security"))
    first = security.post(APPROVE, json={"group": "g-admins-security"})
    assert first.status_code == 200, first.text
    assert [a["group"] for a in first.json()["admission_approvals"]] == ["g-admins-security"]
    assert first.json()["admission_approvals"][0]["approverSub"] == SECURITY

    still_blocked = owner.post(PROMOTE, json={"slot": "production"})
    assert still_blocked.status_code == 409
    assert still_blocked.json()["detail"] == wa.promotion_blocked_message(VERSION, "high", ["g-approvers"])

    # Idempotent: the same group again changes nothing.
    again = security.post(APPROVE, json={"group": "g-admins-security"})
    assert again.status_code == 200
    assert len(again.json()["admission_approvals"]) == 1

    approver = _router_client(monkeypatch, seeded, sub=APPROVER, groups=("g-admins-super", "g-approvers"))
    second = approver.post(APPROVE, json={"group": "g-approvers"})
    assert second.status_code == 200, second.text
    assert [a["group"] for a in second.json()["admission_approvals"]] == ["g-admins-security", "g-approvers"]

    released = owner.post(PROMOTE, json={"slot": "production"})
    assert released.status_code == 200, released.text
    owner.slot_write.assert_called_once()
    row = seeded.get(RUNTIME, VERSION, consistent=True)
    assert wa.pending_groups(row.admission, row.admission_approvals) == []


def test_versions_list_serves_the_admission_and_its_approvals(seeded, monkeypatch):
    security = _router_client(monkeypatch, seeded, sub=SECURITY, groups=("g-admins-super", "g-admins-security"))
    assert security.post(APPROVE, json={"group": "g-admins-security"}).status_code == 200
    owner = _router_client(monkeypatch, seeded, sub=OWNER, groups=("g-users-default",))
    listed = owner.get(f"/api/runtimes/{RUNTIME}/versions")
    assert listed.status_code == 200, listed.text
    (version,) = listed.json()
    assert version["admission"]["tier"] == "high"
    assert [a["group"] for a in version["admission_approvals"]] == ["g-admins-security"]


# --------------------------------------------------------------------------- the store's fence


def test_the_store_refuses_the_owner_and_fences_the_row_identity(seeded):
    with pytest.raises(ValueError):
        seeded.record_admission_approval(
            RUNTIME,
            VERSION,
            expected_owner_sub=OWNER,
            expected_deployment_id="dep-1111",
            expected_admission=_admission(["g-admins-security", "g-approvers"], tier="high"),
            group="g-approvers",
            approver_sub=OWNER,
            approved_at="t",
        )
    with pytest.raises(AdmissionApprovalConflict):
        seeded.record_admission_approval(
            RUNTIME,
            VERSION,
            expected_owner_sub="not-the-owner",
            expected_deployment_id="dep-1111",
            expected_admission=_admission(["g-admins-security", "g-approvers"], tier="high"),
            group="g-approvers",
            approver_sub=APPROVER,
            approved_at="t",
        )
    with pytest.raises(ValueError):  # the admission being approved does not route to that group
        seeded.record_admission_approval(
            RUNTIME,
            VERSION,
            expected_owner_sub=OWNER,
            expected_deployment_id="dep-1111",
            expected_admission=_admission(["g-admins-security", "g-approvers"], tier="high"),
            group="g-finance",
            approver_sub=APPROVER,
            approved_at="t",
        )
    with pytest.raises(AdmissionApprovalConflict):  # a different deploy produced the row the route saw
        seeded.record_admission_approval(
            RUNTIME,
            VERSION,
            expected_owner_sub=OWNER,
            expected_deployment_id="dep-9999",
            expected_admission=_admission(["g-admins-security", "g-approvers"], tier="high"),
            group="g-approvers",
            approver_sub=APPROVER,
            approved_at="t",
        )
    with pytest.raises(AdmissionApprovalConflict):  # the route validated a different admission than the row carries
        seeded.record_admission_approval(
            RUNTIME,
            VERSION,
            expected_owner_sub=OWNER,
            expected_deployment_id="dep-1111",
            expected_admission=_admission(["g-approvers"], tier="medium"),
            group="g-approvers",
            approver_sub=APPROVER,
            approved_at="t",
        )
    assert seeded.get(RUNTIME, VERSION).admission_approvals is None


def test_two_approvers_of_the_same_group_racing_leave_exactly_one_record(seeded, monkeypatch):
    """The first approver's read is stale by the time it writes (a peer approved meanwhile): the
    compare-and-set fails, the re-read finds the group approved, and the call is idempotent."""
    real_get = seeded.get
    peer_wrote = {"done": False}

    def stale_then_real(runtime_name, version_id, *, consistent=False):
        row = real_get(runtime_name, version_id, consistent=consistent)
        if not peer_wrote["done"]:
            peer_wrote["done"] = True
            # Our read returned the row with no approvals; a peer now approves for the same group.
            seeded._table.update_item(
                Key={"runtime_name": RUNTIME, "version_id": VERSION},
                UpdateExpression="SET admission_approvals = :a",
                ExpressionAttributeValues={":a": [wa.approval_record("g-approvers", "peer", "t0")]},
            )
        return row

    monkeypatch.setattr(seeded, "get", stale_then_real)
    result = seeded.record_admission_approval(
        RUNTIME,
        VERSION,
        expected_owner_sub=OWNER,
        expected_deployment_id="dep-1111",
        expected_admission=_admission(["g-admins-security", "g-approvers"], tier="high"),
        group="g-approvers",
        approver_sub=APPROVER,
        approved_at="t1",
    )
    assert [a["approverSub"] for a in result.admission_approvals] == ["peer"]


def test_a_concurrent_approval_for_another_group_is_a_conflict_not_a_lost_record(seeded, monkeypatch):
    """Stale read, but the peer approved a DIFFERENT group: the write must not clobber it, and the
    caller is told to retry rather than being credited."""
    real_get = seeded.get
    peer_wrote = {"done": False}

    def stale_then_real(runtime_name, version_id, *, consistent=False):
        row = real_get(runtime_name, version_id, consistent=consistent)
        if not peer_wrote["done"]:
            peer_wrote["done"] = True
            seeded._table.update_item(
                Key={"runtime_name": RUNTIME, "version_id": VERSION},
                UpdateExpression="SET admission_approvals = :a",
                ExpressionAttributeValues={":a": [wa.approval_record("g-admins-security", "peer", "t0")]},
            )
        return row

    monkeypatch.setattr(seeded, "get", stale_then_real)
    with pytest.raises(AdmissionApprovalConflict):
        seeded.record_admission_approval(
            RUNTIME,
            VERSION,
            expected_owner_sub=OWNER,
            expected_deployment_id="dep-1111",
            expected_admission=_admission(["g-admins-security", "g-approvers"], tier="high"),
            group="g-approvers",
            approver_sub=APPROVER,
            approved_at="t1",
        )
    monkeypatch.undo()
    row = seeded.get(RUNTIME, VERSION, consistent=True)
    assert [a["group"] for a in row.admission_approvals] == ["g-admins-security"]


def test_admission_fields_round_trip_through_the_item_format(versions_table):
    versions_table.put(_version(admission_approvals=[wa.approval_record("g-approvers", APPROVER, "t")]))
    row = versions_table.get(RUNTIME, VERSION, consistent=True)
    assert row.admission == _admission(["g-admins-security", "g-approvers"], tier="high")
    assert row.admission_approvals == [{"group": "g-approvers", "approverSub": APPROVER, "approvedAt": "t"}]
    assert json.dumps(row.admission)  # plain JSON types after Decimal decoding


@pytest.mark.parametrize(
    "admission",
    [
        {"tier": "high", "approvers": ["g-approvers", 7, ""]},
        {"tier": "high", "approvers": "g-approvers"},
        {"tier": "high"},
    ],
)
def test_a_corrupt_admission_document_blocks_promotion_and_approval_rather_than_releasing(
    versions_table, monkeypatch, admission
):
    """A present-but-malformed record is a requirement that cannot be verified: promotion stays
    blocked with a message that sends an admin to look, and nobody can approve against it."""
    versions_table.put(_version(admission=admission))
    owner = _router_client(monkeypatch, versions_table, sub=OWNER, groups=("g-users-default",))
    blocked = owner.post(PROMOTE, json={"slot": "production"})
    assert blocked.status_code == 409, blocked.text
    assert "malformed" in blocked.json()["detail"]
    owner.slot_write.assert_not_called()
    assert owner.post(PROMOTE, json={"slot": "staging"}).status_code == 200  # staging is never gated
    # No caller is a named approver of an unreadable record, so an outsider sees the same 404 as for
    # a missing version (a malformed record is not an existence oracle); the owner is told to call an admin.
    approver = _router_client(monkeypatch, versions_table, sub=APPROVER, groups=("g-admins-super", "g-approvers"))
    assert approver.post(APPROVE, json={"group": "g-approvers"}).status_code == 404
    owner_with_scope = _router_client(monkeypatch, versions_table, sub=OWNER, groups=("g-admins-super",))
    owner_attempt = owner_with_scope.post(APPROVE, json={"group": "g-approvers"})
    assert owner_attempt.status_code == 409
    assert "malformed" in owner_attempt.json()["detail"]


def test_the_production_promote_pins_the_validated_admission_and_approvals_into_the_slot_transaction(
    seeded, monkeypatch
):
    """Finding 6: the slot write is conditioned on exactly the admission and approvals the route
    validated, so a requirement tightened or an approval replaced after the read cannot be promoted past."""
    security = _router_client(monkeypatch, seeded, sub=SECURITY, groups=("g-admins-super", "g-admins-security"))
    assert security.post(APPROVE, json={"group": "g-admins-security"}).status_code == 200
    approver = _router_client(monkeypatch, seeded, sub=APPROVER, groups=("g-admins-super", "g-approvers"))
    assert approver.post(APPROVE, json={"group": "g-approvers"}).status_code == 200
    row = seeded.get(RUNTIME, VERSION, consistent=True)

    owner = _router_client(monkeypatch, seeded, sub=OWNER, groups=("g-users-default",))
    assert owner.post(PROMOTE, json={"slot": "production"}).status_code == 200
    ((_, kwargs),) = [c for c in [owner.slot_write.call_args] if c]
    fence = kwargs["require_version"]
    assert fence.admission_fenced is True
    assert fence.admission == row.admission
    assert fence.admission_approvals == row.admission_approvals
    assert [a["group"] for a in fence.admission_approvals] == ["g-admins-security", "g-approvers"]

    owner.slot_write.reset_mock()
    assert owner.post(PROMOTE, json={"slot": "staging"}).status_code == 200
    staging_fence = owner.slot_write.call_args.kwargs["require_version"]
    assert staging_fence.admission_fenced is False  # staging moves are not gated, so they are not fenced on it


def test_the_slot_transaction_refuses_a_row_whose_admission_changed_after_the_read(versions_table):
    """The fence itself, against the real transaction: a promotion validated against one admission does
    not land on a row that now carries another (or whose approvals were replaced)."""
    import boto3
    from app.services.agent_versions_store import (
        RuntimeSlots,
        SlotWriteConflict,
        VersionFence,
        get_slots_store,
        set_slot_pointers_atomically,
    )

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="RuntimeSlots",
        KeySchema=[{"AttributeName": "runtime_name", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "runtime_name", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    import app.services.agent_versions_store as avs

    slots_store = avs.RuntimeSlotsStore(table_name="RuntimeSlots", region="us-east-1")
    avs._versions_store = versions_table  # the module-level singletons the transaction helper reads
    avs._slots_store = slots_store
    try:
        validated = _version(
            admission_approvals=[
                wa.approval_record("g-admins-security", SECURITY, "t"),
                wa.approval_record("g-approvers", APPROVER, "t"),
            ]
        )
        versions_table.put(validated)
        base = RuntimeSlots(runtime_name=RUNTIME, owner_sub=OWNER)

        def fence(admission, approvals):
            return VersionFence(
                version_id=VERSION,
                owner_sub=OWNER,
                status="succeeded",
                created_at=validated.created_at,
                slot="production",
                deployment_id=validated.deployment_id,
                runtime_id=validated.runtime_id,
                runtime_arn=validated.runtime_arn,
                admission_fenced=True,
                admission=admission,
                admission_approvals=approvals,
            )

        # Validated against a stale view: the row's approvals were replaced after the read.
        stale = fence(validated.admission, [wa.approval_record("g-approvers", APPROVER, "t")])
        with pytest.raises(SlotWriteConflict):
            set_slot_pointers_atomically(
                RUNTIME,
                expected=None,
                new=RuntimeSlots(runtime_name=RUNTIME, owner_sub=OWNER, production_version_id=VERSION),
                require_version=stale,
            )
        assert get_slots_store().get(RUNTIME, consistent=True) is None

        # Validated against exactly what the row carries: the pointer moves.
        exact = fence(validated.admission, validated.admission_approvals)
        set_slot_pointers_atomically(
            RUNTIME,
            expected=None,
            new=RuntimeSlots(runtime_name=RUNTIME, owner_sub=OWNER, production_version_id=VERSION),
            require_version=exact,
        )
        assert get_slots_store().get(RUNTIME, consistent=True).production_version_id == VERSION
        del base
    finally:
        avs._versions_store = None
        avs._slots_store = None
