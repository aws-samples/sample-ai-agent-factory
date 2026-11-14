"""Workspace policy store, admission decision and the deploy-path gate (plan §3.B, steps 1 and 3).

Three layers, each tested at its own boundary:

* ``workspace_admission.admit`` -- pure: every allow-list rule positive and negative, the
  ``unclassified`` asymmetry, rule order, tier routing, the exact ``policy_denied`` body.
* ``workspace_policy_service`` -- the DynamoDB item format (round trip, sort key grammar, a
  corrupt item refuses) and ``resolve_policy`` (named, fallback to default, unavailable).
* ``POST /api/deploy`` through the REAL deployment app with the proven ``spy`` harness from
  ``test_deploy_gate_ordering``: flag off is byte-for-byte the old behaviour; a business persona's
  canvas with a Jira connector in a read-only workspace is the exact 403 with ZERO side effects;
  the same attempt through the raw API (no UI) is the same 403; an unreadable or missing default
  policy is 503 with zero side effects; an admitted agent deploys and carries its tier.
* The admin API: flag off is the exact 404 of a nonexistent route; a standard user is 403; path and
  body ids must agree; store failures are 503.

Only the boto3 Table is faked (``_FakeTable``), and it is fed through the real
``policy_to_item``/``policy_from_item`` so the stored shape is the one DynamoDB will hold.
"""

from __future__ import annotations

import copy
import json
import pathlib
from typing import Any

import pytest
from app import deployment_handler as dh
from app.schemas.agent_spec_v2 import AgentSpecV2
from app.schemas.workspace_policy import WorkspacePolicy
from app.services import workspace_policy_service as wps
from app.services.workspace_admission import (
    POLICY_DENIED,
    AdmissionDecision,
    admit,
    approvers_for,
    denial_detail,
    violations_for,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_deploy_gate_ordering import OWNER, _body, _client
from tests.test_deploy_gate_ordering import spy as _gate_spy

#: The ordered side-effect log of ``test_deploy_gate_ordering`` exposed under the same name, so
#: the deploy-path tests below read "zero side effects" from the proven harness.
spy = pytest.fixture(_gate_spy.__wrapped__)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "spec_v2" / "templates"
FLOW_ID = "flow-finance-1"


# --------------------------------------------------------------------------- builders


def _spec(**overrides: Any) -> AgentSpecV2:
    document: dict[str, Any] = {
        "id": "agent-1",
        "name": "Agent",
        "model": {"provider": "bedrock", "modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0"},
    }
    document.update(overrides)
    return AgentSpecV2.model_validate(document)


def _policy(**overrides: Any) -> WorkspacePolicy:
    document: dict[str, Any] = {"workspaceId": "finance", "name": "Finance"}
    document.update(overrides)
    return WorkspacePolicy.model_validate(document)


def _tool(tool_id: str, risk: str = "unclassified", *, kind: str = "lambda", ref: str | None = None) -> dict:
    return {"id": tool_id, "type": kind, "ref": ref or tool_id, "riskClass": risk}


def _codes(spec: AgentSpecV2, policy: WorkspacePolicy) -> list[str]:
    return [violation.code for violation in violations_for(spec, policy)]


# --------------------------------------------------------------------------- allow-list rules


def test_an_empty_policy_admits_anything_and_routes_low_to_auto_approve():
    decision = admit(_spec(), _policy())
    assert decision.outcome == "admitted"
    assert decision.tier == "low"
    assert decision.approvers == []
    assert decision.violations == []
    assert decision.requires_approval is False


@pytest.mark.parametrize(
    ("overrides", "allowed", "code", "path"),
    [
        (
            {"model": {"provider": "openai", "modelId": "gpt"}},
            {"providers": ["bedrock"]},
            "provider_not_allowed",
            "/model/provider",
        ),
        (
            {"model": {"provider": "bedrock", "modelId": "other"}},
            {"models": ["allowed-model"]},
            "model_not_allowed",
            "/model/modelId",
        ),
        (
            {"tools": [_tool("jira", "read", kind="connector")]},
            {"connectors": ["slack"]},
            "connector_not_allowed",
            "/tools/0/ref",
        ),
        (
            {"tools": [_tool("t", "write")]},
            {"toolRiskClasses": ["read"]},
            "tool_risk_class_not_allowed",
            "/tools/0/riskClass",
        ),
        ({"tools": [_tool("t")]}, {"toolRiskClasses": ["read"]}, "tool_unclassified", "/tools/0/riskClass"),
        (
            {"knowledge": [{"id": "k", "type": "customKb", "dataClass": "pii"}]},
            {"dataClasses": ["public"]},
            "data_class_not_allowed",
            "/knowledge/0/dataClass",
        ),
        (
            {"knowledge": [{"id": "k", "type": "customKb"}]},
            {"dataClasses": ["public"]},
            "knowledge_unclassified",
            "/knowledge/0/dataClass",
        ),
        (
            {"channels": [{"id": "c", "type": "slack"}]},
            {"channels": ["teams"]},
            "channel_not_allowed",
            "/channels/0/type",
        ),
        (
            {"deployment": {"region": "eu-west-1"}},
            {"regions": ["us-east-1"]},
            "region_not_allowed",
            "/deployment/region",
        ),
    ],
)
def test_each_allow_list_rule_refuses_exactly_what_it_guards(overrides, allowed, code, path):
    violations = violations_for(_spec(**overrides), _policy(allowed=allowed))
    assert [(v.code, v.path) for v in violations] == [(code, path)]


@pytest.mark.parametrize(
    ("overrides", "allowed"),
    [
        ({"model": {"provider": "bedrock", "modelId": "m"}}, {"providers": ["bedrock"]}),
        ({"model": {"provider": "bedrock", "modelId": "m"}}, {"models": ["m"]}),
        ({"tools": [_tool("slack", "read", kind="connector")]}, {"connectors": ["slack"]}),
        ({"tools": [_tool("t", "read")]}, {"toolRiskClasses": ["read", "write"]}),
        ({"knowledge": [{"id": "k", "type": "customKb", "dataClass": "public"}]}, {"dataClasses": ["public"]}),
        ({"channels": [{"id": "c", "type": "teams"}]}, {"channels": ["teams"]}),
        ({"deployment": {"region": "us-east-1"}}, {"regions": ["us-east-1"]}),
        # Unconstrained axes pass everything, including unclassified items and foreign Regions.
        ({"tools": [_tool("t"), _tool("jira", "pay", kind="connector")]}, {}),
        ({"knowledge": [{"id": "k", "type": "customKb"}]}, {}),
        ({"deployment": {"region": "eu-west-3"}}, {}),
        # No model at all cannot violate a provider or model list.
        ({"model": None}, {"providers": ["bedrock"], "models": ["m"]}),
        # A non-connector tool is not subject to the connector list.
        ({"tools": [_tool("web_search", "read")]}, {"connectors": ["slack"]}),
        # A Region-less spec is not subject to the Region list (the request gates decide the Region).
        ({}, {"regions": ["us-east-1"]}),
    ],
)
def test_permitted_or_unconstrained_use_passes(overrides, allowed):
    assert _codes(_spec(**overrides), _policy(allowed=allowed)) == []


@pytest.mark.parametrize(
    ("requested", "ceiling", "expected"),
    [(50, 100, []), (100, 100, []), (100.01, 100, ["budget_exceeds_workspace"]), (None, 100, []), (500, None, [])],
)
def test_the_workspace_budget_ceiling(requested, ceiling, expected):
    spec = _spec(policy={"budget": {"monthlyUsd": requested}})
    assert _codes(spec, _policy(budgets={"perWorkspaceMonthlyUsd": ceiling})) == expected


def test_violations_are_reported_in_rule_order_one_per_offending_item():
    spec = _spec(
        model={"provider": "openai", "modelId": "gpt"},
        tools=[_tool("jira", "write", kind="connector"), _tool("github", "unclassified", kind="connector")],
        knowledge=[{"id": "k", "type": "customKb", "dataClass": "pii"}],
        channels=[{"id": "c", "type": "slack"}],
        deployment={"region": "eu-west-1"},
        policy={"budget": {"monthlyUsd": 500}},
    )
    policy = _policy(
        allowed={
            "providers": ["bedrock"],
            "models": ["m"],
            "connectors": ["slack"],
            "toolRiskClasses": ["read"],
            "dataClasses": ["public"],
            "channels": ["teams"],
            "regions": ["us-east-1"],
        },
        budgets={"perWorkspaceMonthlyUsd": 100},
    )
    assert _codes(spec, policy) == [
        "provider_not_allowed",
        "model_not_allowed",
        "connector_not_allowed",
        "connector_not_allowed",
        "tool_risk_class_not_allowed",
        "tool_unclassified",
        "data_class_not_allowed",
        "channel_not_allowed",
        "region_not_allowed",
        "budget_exceeds_workspace",
    ]


# --------------------------------------------------------------------------- routing and denial body


@pytest.mark.parametrize(
    ("spec_overrides", "tier", "approvers"),
    [
        ({}, "low", []),
        ({"tools": [_tool("t", "write")]}, "medium", ["g-approvers"]),
        ({"tools": [_tool("t", "pay")]}, "high", ["g-admins-security", "g-approvers"]),
    ],
)
def test_the_tier_routes_to_the_policys_approver_groups(spec_overrides, tier, approvers):
    policy = _policy(approval={"medium": ["g-approvers"], "high": ["g-admins-security", "g-approvers"]})
    decision = admit(_spec(**spec_overrides), policy)
    assert decision.outcome == "admitted"
    assert (decision.tier, decision.approvers) == (tier, approvers)
    assert decision.requires_approval is bool(approvers)
    assert approvers_for(tier, policy) == approvers


def test_a_denied_agent_still_carries_its_tier_and_reasons_for_the_badge():
    decision = admit(_spec(tools=[_tool("t", "pay")]), _policy(allowed={"toolRiskClasses": ["read"]}))
    assert decision.outcome == "denied"
    assert decision.tier == "high"
    assert [reason.code for reason in decision.reasons] == ["pay_tool"]
    assert decision.requires_approval is False


def test_the_denial_body_is_the_exact_policy_denied_contract():
    decision = admit(
        _spec(tools=[_tool("jira", "write", kind="connector")]), _policy(allowed={"connectors": ["slack"]})
    )
    detail = denial_detail(decision)
    assert detail["code"] == POLICY_DENIED == "policy_denied"
    assert detail["workspaceId"] == "finance"
    assert detail["tier"] == "medium"
    assert detail["violations"] == [
        {
            "code": "connector_not_allowed",
            "path": "/tools/0/ref",
            "message": "connector 'jira' (tool 'jira') is not allowed in this workspace",
        }
    ]
    assert "Nothing was created" in detail["message"]
    assert set(detail) == {"code", "workspaceId", "tier", "violations", "message"}


def test_the_decision_serializes_camel_case_for_the_registry_record():
    document = admit(_spec(), _policy()).model_dump(by_alias=True)
    assert set(document) == {
        "outcome",
        "workspaceId",
        "tier",
        "reasons",
        "approvers",
        "violations",
        "admissionVersion",
        "classifierVersion",
    }
    assert AdmissionDecision.model_validate(document) == admit(_spec(), _policy())


# --------------------------------------------------------------------------- store: item format and resolution


class _FakeTable:
    """The boto3 Table surface the store uses, over a dict keyed by (org_id, sk)."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.fail: Exception | None = None

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    def put_item(self, Item):
        self._check()
        self.items[(Item["org_id"], Item["sk"])] = copy.deepcopy(Item)

    def get_item(self, Key, ConsistentRead=False):
        self._check()
        assert ConsistentRead is True, "policy reads must be strongly consistent"
        item = self.items.get((Key["org_id"], Key["sk"]))
        return {"Item": copy.deepcopy(item)} if item else {}

    def delete_item(self, Key):
        self._check()
        self.items.pop((Key["org_id"], Key["sk"]), None)

    def query(self, KeyConditionExpression, ExpressionAttributeValues, ExclusiveStartKey=None):
        self._check()
        org, prefix = ExpressionAttributeValues[":o"], ExpressionAttributeValues[":p"]
        items = [copy.deepcopy(v) for (o, sk), v in sorted(self.items.items()) if o == org and sk.startswith(prefix)]
        # Two pages, so pagination is exercised on every list.
        if ExclusiveStartKey is None and len(items) > 1:
            return {"Items": items[:1], "LastEvaluatedKey": {"org_id": org, "sk": items[0]["sk"]}}
        if ExclusiveStartKey is not None:
            return {"Items": items[1:]}
        return {"Items": items}


@pytest.fixture
def table(monkeypatch) -> _FakeTable:
    fake = _FakeTable()
    store = wps.WorkspacePolicyStore.__new__(wps.WorkspacePolicyStore)
    store._table = fake
    monkeypatch.setattr(wps, "_store", store)
    monkeypatch.setattr(wps, "get_workspace_policy_store", lambda: store)
    fake.store = store
    return fake


def test_policy_items_round_trip_through_the_org_config_table_shape(table):
    policy = _policy(allowed={"connectors": ["slack"]}, approval={"high": ["g-admins-security"]})
    table.store.put("default", policy)
    item = table.items[("default", "WORKSPACE#finance#POLICY")]
    assert set(item) == {"org_id", "sk", "policy"}
    assert item["policy"]["allowed"]["connectors"] == ["slack"]  # camelCase document, by alias
    assert table.store.get("default", "finance") == policy
    assert wps.workspace_id_from_sort_key(item["sk"]) == "finance"


@pytest.mark.parametrize("bad", ["", "with#hash", "a" * 129, "sp ace", "slash/x"])
def test_workspace_ids_that_cannot_form_a_sort_key_are_refused(bad):
    with pytest.raises(wps.InvalidWorkspaceIdError):
        wps.policy_sort_key(bad)


def test_workspace_id_from_sort_key_ignores_other_items():
    assert wps.workspace_id_from_sort_key("APPROVAL#x") is None
    assert wps.workspace_id_from_sort_key("WORKSPACE#x") is None
    assert wps.workspace_id_from_sort_key("WORKSPACE##POLICY") is None


def test_a_corrupt_stored_policy_refuses_rather_than_admitting_on_it(table):
    table.items[("default", "WORKSPACE#finance#POLICY")] = {
        "org_id": "default",
        "sk": "WORKSPACE#finance#POLICY",
        "policy": {"workspaceId": "finance", "name": "F", "allowed": {"toolRiskClasses": ["unclassified"]}},
    }
    with pytest.raises(wps.WorkspacePolicyError):
        table.store.get("default", "finance")
    with pytest.raises(wps.WorkspacePolicyUnavailableError):
        wps.resolve_policy(table.store, "default", "finance")


def test_list_paginates_sorts_and_skips_foreign_items(table):
    table.store.put("default", _policy(workspaceId="zeta", name="Z"))
    table.store.put("default", _policy(workspaceId="alpha", name="A"))
    table.items[("default", "APPROVAL#x")] = {"org_id": "default", "sk": "APPROVAL#x", "name": "x"}
    table.items[("other-org", "WORKSPACE#alpha#POLICY")] = wps.policy_to_item(
        "other-org", _policy(workspaceId="alpha", name="O")
    )
    assert [p.workspace_id for p in table.store.list("default")] == ["alpha", "zeta"]


def test_resolution_prefers_the_named_workspace_then_falls_back_to_default(table):
    table.store.put("default", _policy(workspaceId="default", name="Org default"))
    table.store.put("default", _policy(workspaceId="finance", name="Finance"))
    assert wps.resolve_policy(table.store, "default", "finance") == (table.store.get("default", "finance"), False)
    assert wps.resolve_policy(table.store, "default", "marketing")[0].workspace_id == "default"
    assert wps.resolve_policy(table.store, "default", "marketing")[1] is True
    assert wps.resolve_policy(table.store, "default", None)[0].workspace_id == "default"


def test_resolution_is_unavailable_without_a_default_policy_or_with_a_failing_store(table):
    with pytest.raises(wps.WorkspacePolicyUnavailableError):
        wps.resolve_policy(table.store, "default", "finance")
    table.store.put("default", _policy(workspaceId="default", name="D"))
    table.fail = RuntimeError("ProvisionedThroughputExceededException tenant-sensitive")
    with pytest.raises(wps.WorkspacePolicyUnavailableError) as excinfo:
        wps.resolve_policy(table.store, "default", "finance")
    assert "tenant-sensitive" not in str(excinfo.value)


# --------------------------------------------------------------------------- the deploy gate, through the real app


def _flags(monkeypatch, flags: dict[str, bool]) -> None:
    from app.services import feature_flags as ff

    monkeypatch.setenv(ff.LOCAL_ENV_VAR, json.dumps(flags))
    monkeypatch.setenv("ENVIRONMENT", "local")
    ff.reset_feature_flag_cache()


def _business_canvas() -> dict:
    """The web-search gallery canvas with a Jira connector attached to its Gateway: what a business
    persona produces by dragging the Jira connector onto the template."""
    document = json.loads((FIXTURES / "web-search-agent.flow.json").read_text())
    gateway = next(node for node in document["nodes"] if node["data"].get("component_type") == "gateway")
    document["nodes"].append(
        {
            "id": "tool-jira",
            "type": "tool",
            "data": {
                "name": "Jira",
                "tool_id": "connector:jira",
                "is_connector": True,
                "connector_id": "jira",
                "auth_method": "api_key",
                "enabled": True,
                "component_type": "tool",
            },
            "position": {"x": 0, "y": 0},
        }
    )
    document["edges"].append({"id": "e-jira", "source": gateway["id"], "target": "tool-jira"})
    return document


@pytest.fixture
def owned_flow(monkeypatch):
    """The business persona's saved canvas, owned by the deploying caller, in the store the
    deployment Lambda resolves for ownership (local mode: the in-memory flow store)."""
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
        workflow=_business_canvas(),
        deployment_status=DeploymentStatus.NOT_DEPLOYED,
        created_at=now,
        updated_at=now,
        owner_sub=OWNER,
    )
    store._flows[flow.id] = flow
    monkeypatch.setattr(module, "_active_storage", store)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    return flow


def _deploy_body() -> dict:
    return _body(flowId=FLOW_ID)


def test_the_migrated_business_canvas_carries_the_jira_connector(owned_flow):
    from app.routers.specs import spec_for_flow

    spec = spec_for_flow(owned_flow, OWNER)
    connectors = [(tool.ref, tool.risk_class) for tool in spec.tools if tool.type == "connector"]
    assert connectors == [("jira", "unclassified")]


def test_flag_off_leaves_admission_exactly_as_it_was(spy, owned_flow, table, monkeypatch):
    """With the flag off the gate returns before reading anything: no policy read, the old 202."""
    _flags(monkeypatch, {"workspacePolicy": False})
    reads = []
    monkeypatch.setattr(wps, "resolve_policy", lambda *a, **k: reads.append(a) or (_policy(), True))

    response = _client().post("/api/deploy", json=_deploy_body())

    assert response.status_code == 202, response.text
    assert reads == []
    assert spy.calls.count("StartExecution") == 1


def test_a_business_persona_adding_jira_in_a_read_only_workspace_is_the_exact_403_with_zero_side_effects(
    spy, owned_flow, table, monkeypatch
):
    """The plan's live adversarial twin, offline: the canvas names a connector the workspace does not
    allow. Exact 403 policy_denied, one reason code, and NOTHING persisted or started."""
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put("default", _policy(workspaceId="default", name="Read-only", allowed={"connectors": ["slack"]}))

    response = _client().post("/api/deploy", json=_deploy_body())

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "policy_denied"
    assert detail["workspaceId"] == "default"
    assert [v["code"] for v in detail["violations"]] == ["connector_not_allowed"]
    assert detail["violations"][0]["path"].endswith("/ref")
    assert spy.side_effects == [], spy.calls


def test_the_same_attempt_through_the_raw_api_is_the_same_403(spy, owned_flow, table, monkeypatch):
    """No UI in the loop at all: a hand-built request naming the flow gets the same refusal, so the
    control is the server, not the deploy panel."""
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put("default", _policy(workspaceId="default", name="Read-only", allowed={"connectors": ["slack"]}))
    raw = {
        "nodeId": "node-1",
        "flowId": FLOW_ID,
        "config": {"name": "rawapi", "model": {"modelId": "us.anthropic.claude-sonnet-5"}},
    }

    response = _client().post("/api/deploy", json=raw)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "policy_denied"
    assert spy.side_effects == []


def test_an_unclassified_tool_in_a_risk_constrained_workspace_is_refused_with_its_own_code(
    spy, owned_flow, table, monkeypatch
):
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put(
        "default", _policy(workspaceId="default", name="Classified only", allowed={"toolRiskClasses": ["read"]})
    )

    response = _client().post("/api/deploy", json=_deploy_body())

    assert response.status_code == 403
    codes = {v["code"] for v in response.json()["detail"]["violations"]}
    assert codes == {"tool_unclassified"}
    assert spy.side_effects == []


def test_an_admitted_canvas_deploys_and_carries_its_tier(spy, owned_flow, table, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put(
        "default",
        _policy(
            workspaceId="default",
            name="Standard",
            allowed={"connectors": ["jira", "slack"]},
            approval={"medium": ["g-approvers"]},
        ),
    )
    seen: list[dict | None] = []
    original = dh._reject_workspace_policy_violation

    def _capture(request, caller_sub):
        original(request, caller_sub)
        seen.append(request.admission_decision)

    monkeypatch.setattr(dh, "_reject_workspace_policy_violation", _capture)

    response = _client().post("/api/deploy", json=_deploy_body())

    assert response.status_code == 202, response.text
    assert spy.calls.count("StartExecution") == 1
    assert seen and seen[0]["outcome"] == "admitted"
    assert seen[0]["workspaceId"] == "default"
    assert seen[0]["tier"] == "low"  # a read-only-looking connector classified nowhere: no floor fires
    assert seen[0]["approvers"] == []


def test_without_a_default_policy_admission_fails_closed_with_zero_side_effects(spy, owned_flow, table, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})

    response = _client().post("/api/deploy", json=_deploy_body())

    assert response.status_code == 503, response.text
    assert "Nothing was created" in response.json()["detail"]
    assert spy.side_effects == []


def test_an_unreadable_policy_store_fails_closed_without_echoing_the_error(spy, owned_flow, table, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    table.fail = RuntimeError("AccessDeniedException request carried tenant-sensitive-values")

    response = _client().post("/api/deploy", json=_deploy_body())

    assert response.status_code == 503
    assert "tenant-sensitive" not in response.text
    assert spy.side_effects == []


def test_a_flow_less_deploy_is_governed_by_the_default_policy_too(spy, table, monkeypatch):
    """No flowId (an unsaved canvas): the request's own provider and Region are still judged."""
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put("default", _policy(workspaceId="default", name="Bedrock only", allowed={"providers": ["bedrock"]}))

    admitted = _client().post("/api/deploy", json=_body())
    assert admitted.status_code == 202, admitted.text

    spy.calls.clear()
    table.store.put("default", _policy(workspaceId="default", name="No Bedrock", allowed={"providers": ["openai"]}))
    refused = _client().post("/api/deploy", json=_body())
    assert refused.status_code == 403
    assert [v["code"] for v in refused.json()["detail"]["violations"]] == ["provider_not_allowed"]
    assert spy.side_effects == []


def test_the_gate_runs_after_ownership_so_a_foreign_flow_is_404_not_a_policy_answer(
    spy, owned_flow, table, monkeypatch
):
    """Existence non-disclosure holds: someone else's flow is 404 before any policy is consulted."""
    _flags(monkeypatch, {"workspacePolicy": True})
    reads = []
    monkeypatch.setattr(wps, "resolve_policy", lambda *a, **k: reads.append(a) or (_policy(), True))

    response = _client(sub="someone-else").post("/api/deploy", json=_deploy_body())

    assert response.status_code == 404, response.text
    assert reads == []
    assert spy.side_effects == []


# --------------------------------------------------------------------------- the admin API


def _admin_client(monkeypatch, *, flags: dict[str, bool], groups=("g-admins-super",)) -> TestClient:
    from app.routers import workspace_policies

    monkeypatch.setenv("RBAC_ENFORCE", "true")
    _flags(monkeypatch, flags)
    app = FastAPI()
    app.include_router(workspace_policies.router)
    claims = {"sub": "admin-1", "cognito:groups": list(groups)}

    @app.middleware("http")
    async def _inject(request, call_next):
        request.scope["aws.event"] = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


_POLICY_DOC = {"workspaceId": "finance", "name": "Finance", "allowed": {"connectors": ["slack"]}}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/workspaces/policies"),
        ("GET", "/api/workspaces/finance/policy"),
        ("PUT", "/api/workspaces/finance/policy"),
        ("DELETE", "/api/workspaces/finance/policy"),
    ],
)
def test_flag_off_is_the_exact_404_of_a_nonexistent_route_for_every_admin_route(table, monkeypatch, method, path):
    client = _admin_client(monkeypatch, flags={"workspacePolicy": False})
    response = client.request(method, path, json=_POLICY_DOC if method == "PUT" else None)
    missing = client.request(method, "/api/workspaces/no-such-route-here")
    assert response.status_code == 404
    assert response.json() == missing.json() == {"detail": "Not Found"}


def test_a_standard_user_cannot_read_or_write_policies(table, monkeypatch):
    client = _admin_client(monkeypatch, flags={"workspacePolicy": True}, groups=("g-users-default",))
    assert client.get("/api/workspaces/policies").status_code == 403
    assert client.put("/api/workspaces/finance/policy", json=_POLICY_DOC).status_code == 403
    assert table.items == {}


def test_an_admin_can_put_get_list_and_delete_a_policy(table, monkeypatch):
    client = _admin_client(monkeypatch, flags={"workspacePolicy": True})

    assert client.get("/api/workspaces/finance/policy").status_code == 404
    put = client.put("/api/workspaces/finance/policy", json=_POLICY_DOC)
    assert put.status_code == 200, put.text
    assert put.json()["allowed"]["connectors"] == ["slack"]
    assert put.json()["approval"] == {"low": [], "medium": [], "high": []}
    assert client.get("/api/workspaces/finance/policy").json() == put.json()
    assert [p["workspaceId"] for p in client.get("/api/workspaces/policies").json()] == ["finance"]
    assert client.delete("/api/workspaces/finance/policy").status_code == 204
    assert client.delete("/api/workspaces/finance/policy").status_code == 204  # idempotent
    assert client.get("/api/workspaces/finance/policy").status_code == 404


def test_put_refuses_a_body_filed_under_another_workspace(table, monkeypatch):
    client = _admin_client(monkeypatch, flags={"workspacePolicy": True})
    response = client.put("/api/workspaces/marketing/policy", json=_POLICY_DOC)
    assert response.status_code == 400
    assert "must equal" in response.json()["detail"]
    assert table.items == {}


def test_put_refuses_unknown_keys_and_unclassified_in_allow_lists(table, monkeypatch):
    client = _admin_client(monkeypatch, flags={"workspacePolicy": True})
    assert client.put("/api/workspaces/finance/policy", json={**_POLICY_DOC, "extra": 1}).status_code == 422
    bad = {**_POLICY_DOC, "allowed": {"toolRiskClasses": ["unclassified"]}}
    assert client.put("/api/workspaces/finance/policy", json=bad).status_code == 422
    assert table.items == {}


def test_an_invalid_workspace_id_in_the_path_is_400(table, monkeypatch):
    client = _admin_client(monkeypatch, flags={"workspacePolicy": True})
    assert client.get("/api/workspaces/bad%23id/policy").status_code == 400


def test_a_failing_store_is_503_and_writes_nothing(table, monkeypatch):
    from botocore.exceptions import ClientError

    client = _admin_client(monkeypatch, flags={"workspacePolicy": True})
    table.fail = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "tenant-sensitive"}}, "PutItem")
    response = client.put("/api/workspaces/finance/policy", json=_POLICY_DOC)
    assert response.status_code == 503
    assert "tenant-sensitive" not in response.text
    assert table.items == {}
