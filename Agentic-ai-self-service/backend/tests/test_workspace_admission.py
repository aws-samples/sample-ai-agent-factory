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
import re
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
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_deploy_gate_ordering import MODEL_ID, OWNER, _body, _client
from tests.test_deploy_gate_ordering import spy as _gate_spy

#: The ordered side-effect log of ``test_deploy_gate_ordering`` exposed under the same name, so
#: the deploy-path tests below read "zero side effects" from the proven harness.
spy = pytest.fixture(_gate_spy.__wrapped__)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "spec_v2" / "templates"
FLOW_ID = "flow-finance-1"
#: The version hand-built policy items carry in these tests (any valid incarnation will do).
_VERSION = wps.PolicyVersion(incarnation="0123456789abcdef", revision=1)


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
    """A workspace that trusts builder classifications, so allow-list tests judge asserted classes;
    the untrusted default has its own tests (``test_by_default_...``)."""
    document: dict[str, Any] = {
        "workspaceId": "finance",
        "name": "Finance",
        "defaults": {"trustBuilderClassifications": True},
    }
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
    ("requested", "ceiling", "per_agent_default", "expected"),
    [
        (50, 100, None, []),
        (100, 100, None, []),
        (100.01, 100, None, ["budget_exceeds_workspace"]),
        (500, None, None, []),
        # Omitting the budget is not a way around the cap: the workspace default applies, and with
        # no default either the agent must declare one.
        (None, 100, 80, []),
        (None, 100, 120, ["budget_exceeds_workspace"]),
        (None, 100, None, ["budget_undeclared"]),
    ],
)
def test_the_workspace_budget_ceiling(requested, ceiling, per_agent_default, expected):
    spec = _spec(policy={"budget": {"monthlyUsd": requested}})
    budgets = {"maxAgentMonthlyUsd": ceiling, "perAgentMonthlyUsd": per_agent_default}
    assert _codes(spec, _policy(budgets=budgets)) == expected


def test_violations_are_reported_in_rule_order_one_per_offending_item():
    spec = _spec(
        model={"provider": "openai", "modelId": "gpt"},
        tools=[_tool("jira", "write", kind="connector"), _tool("acme-crm", "unclassified", kind="connector")],
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
        budgets={"maxAgentMonthlyUsd": 100},
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
        "ingressSensitiveCapabilities",
        "inventory",
        "approvers",
        "violations",
        "admissionVersion",
        "classifierVersion",
    }
    assert document["admissionVersion"] == 3
    assert document["inventory"]["id"] == "agent-1"
    # The writer produces EVERY inventory key (empty lists and nulls included): the reader requires each to
    # be present, so a truncated record is malformed rather than an agent with fewer capabilities.
    assert set(document["inventory"]) == {
        "id",
        "name",
        "workspaceId",
        "model",
        "tools",
        "knowledge",
        "channels",
        "triggers",
        "customCode",
        "region",
        "monthlyBudgetUsd",
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

    def _require(self, operation, key, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        """Only the three conditions the store writes are understood, evaluated the way DynamoDB would; an
        unconditional write or delete is refused, because the store must never issue one."""
        assert ConditionExpression is not None, f"every workspace policy {operation} must be conditional"
        expression = ConditionExpression
        for alias, name in (ExpressionAttributeNames or {}).items():
            expression = expression.replace(alias, name)
        current = self.items.get(key)
        values = ExpressionAttributeValues or {}
        if expression == "attribute_not_exists(sk)":
            holds = current is None
        elif (
            expression
            == "attribute_exists(sk) AND attribute_not_exists(revision) AND attribute_not_exists(incarnation)"
        ):
            holds = current is not None and "revision" not in current and "incarnation" not in current
        elif expression == "incarnation = :inc AND revision = :rev":
            holds = (
                current is not None
                and current.get("incarnation") == values[":inc"]
                and current.get("revision") == values[":rev"]
            )
        else:
            raise AssertionError(f"unexpected policy {operation} condition {ConditionExpression!r}")
        if not holds:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}},
                operation,
            )

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        self._check()
        key = (Item["org_id"], Item["sk"])
        self._require("PutItem", key, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues)
        self.items[key] = copy.deepcopy(Item)

    def get_item(self, Key, ConsistentRead=False):
        self._check()
        assert ConsistentRead is True, "policy reads must be strongly consistent"
        item = self.items.get((Key["org_id"], Key["sk"]))
        return {"Item": copy.deepcopy(item)} if item else {}

    def delete_item(self, Key, ConditionExpression=None, ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        self._check()
        key = (Key["org_id"], Key["sk"])
        self._require("DeleteItem", key, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues)
        self.items.pop(key, None)

    def query(self, KeyConditionExpression, ExpressionAttributeValues, ExclusiveStartKey=None, ConsistentRead=False):
        self._check()
        assert ConsistentRead is True, (
            "policy lists must be strongly consistent: admission resolves the governing policy from them"
        )
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
    table.store.put("default", policy, expected=None)
    item = table.items[("default", "WORKSPACE#finance#POLICY")]
    assert set(item) == {"org_id", "sk", "revision", "incarnation", "policy"}
    assert item["revision"] == 1 and re.fullmatch(r"[0-9a-f]{16}", item["incarnation"])
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
    table.store.put("default", _policy(workspaceId="zeta", name="Z"), expected=None)
    table.store.put("default", _policy(workspaceId="alpha", name="A"), expected=None)
    table.items[("default", "APPROVAL#x")] = {"org_id": "default", "sk": "APPROVAL#x", "name": "x"}
    table.items[("other-org", "WORKSPACE#alpha#POLICY")] = wps.policy_to_item(
        "other-org", _policy(workspaceId="alpha", name="O"), _VERSION
    )
    assert [p.workspace_id for p in table.store.list("default")] == ["alpha", "zeta"]


def test_resolution_follows_the_deployers_memberships_not_the_spec(table):
    """A member of exactly one workspace is governed by it however the spec is written (named,
    unnamed, naming an unconfigured workspace, or naming ``default``), so dropping the flow or clearing
    ``workspaceId`` is not an escape to the org default; a caller in no workspace gets the default."""
    table.store.put("default", _policy(workspaceId="default", name="Org default"), expected=None)
    table.store.put(
        "default", _policy(workspaceId="finance", name="Finance", memberGroups=["g-finance"]), expected=None
    )
    finance = table.store.get("default", "finance")
    member = ["g-users-default", "g-finance"]
    for named in ("finance", "marketing", "default", None):
        assert wps.resolve_policy(table.store, "default", named, member) == (finance, False), named
    for named in ("marketing", "default", None):
        policy, fell_back = wps.resolve_policy(table.store, "default", named, ["g-users-default"])
        assert (policy.workspace_id, fell_back) == ("default", True), named


def test_a_member_of_several_workspaces_must_name_one_of_them(table):
    table.store.put("default", _policy(workspaceId="default", name="Org default"), expected=None)
    table.store.put(
        "default", _policy(workspaceId="finance", name="Finance", memberGroups=["g-finance"]), expected=None
    )
    table.store.put("default", _policy(workspaceId="hr", name="HR", memberGroups=["g-hr"]), expected=None)
    both = ["g-finance", "g-hr"]
    assert wps.resolve_policy(table.store, "default", "hr", both)[0].workspace_id == "hr"
    assert wps.resolve_policy(table.store, "default", "finance", both)[0].workspace_id == "finance"
    with pytest.raises(wps.WorkspaceSelectionRequiredError) as excinfo:
        wps.resolve_policy(table.store, "default", None, both)
    assert excinfo.value.choices == ["finance", "hr"]
    with pytest.raises(wps.WorkspaceSelectionRequiredError):
        wps.resolve_policy(table.store, "default", "default", both)


def test_a_spec_cannot_select_a_workspace_its_deployer_is_not_a_member_of(table):
    """The spec's workspaceId is deployer-controlled, so it can only pick a policy the deployer
    is entitled to: naming a permissive workspace one is not in is refused, never re-routed."""
    table.store.put(
        "default",
        _policy(workspaceId="default", name="Locked", allowed={"providers": ["bedrock"]}),
        expected=None,
    )
    table.store.put(
        "default", _policy(workspaceId="devs", name="Developers", memberGroups=["g-developers"]), expected=None
    )
    with pytest.raises(wps.WorkspaceMembershipError) as excinfo:
        wps.resolve_policy(table.store, "default", "devs", ["g-users-default"])
    assert excinfo.value.workspace_id == "devs"
    # A workspace with no member groups at all admits nobody by name (only default is open to all).
    table.store.put("default", _policy(workspaceId="closed", name="Closed"), expected=None)
    with pytest.raises(wps.WorkspaceMembershipError):
        wps.resolve_policy(table.store, "default", "closed", ["g-admins-super"])
    assert wps.is_member(table.store.get("default", "default"), []) is True
    # Nothing is admitted when no default exists and the caller belongs nowhere.
    table.store.delete("default", "default", expected=table.store.read("default", "default").version)
    with pytest.raises(wps.WorkspacePolicyUnavailableError):
        wps.resolve_policy(table.store, "default", None, ["g-users-default"])


def test_resolution_is_unavailable_without_a_default_policy_or_with_a_failing_store(table):
    with pytest.raises(wps.WorkspacePolicyUnavailableError):
        wps.resolve_policy(table.store, "default", "finance")
    table.store.put("default", _policy(workspaceId="default", name="D"), expected=None)
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


@pytest.fixture(autouse=True)
def _fresh_flag_cache():
    """The resolved flag set is cached for its TTL. Without this, a test that turned the flag ON would
    leave it on for every later test in the process, including other files' deploy-path suites."""
    from app.services import feature_flags as ff

    ff.reset_feature_flag_cache()
    yield
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


#: The connector the business canvas carries, as the deploy panel sends it in the REQUEST BODY. Admission
#: judges the body (what is deployed), so the body must carry the connector for it to be judged; the
#: saved flow only supplies the classification a human gave it.
JIRA = {"connectorId": "jira", "authMethod": "api_key"}


def _deploy_body(**extra) -> dict:
    return _body(flowId=FLOW_ID, connectors=[JIRA], **extra)


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
    table.store.put(
        "default",
        _policy(workspaceId="default", name="Read-only", allowed={"connectors": ["slack"]}),
        expected=None,
    )

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
    table.store.put(
        "default",
        _policy(workspaceId="default", name="Read-only", allowed={"connectors": ["slack"]}),
        expected=None,
    )
    raw = {
        "nodeId": "node-1",
        "flowId": FLOW_ID,
        "config": {"name": "rawapi", "model": {"modelId": "us.anthropic.claude-sonnet-5"}},
        "connectors": [JIRA],
    }

    response = _client().post("/api/deploy", json=raw)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "policy_denied"
    assert spy.side_effects == []


def test_an_unclassified_tool_in_a_risk_constrained_workspace_is_refused_with_its_own_code(
    spy, owned_flow, table, monkeypatch
):
    """A connector the catalog has no entry for (generic_openapi is accepted by the request model but
    not catalogued) has no floor, so it stays unclassified; a workspace that constrains tool classes
    refuses it with the code that tells the builder to classify it."""
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put(
        "default",
        _policy(workspaceId="default", name="Classified only", allowed={"toolRiskClasses": ["read"]}),
        expected=None,
    )

    response = _client().post(
        "/api/deploy",
        json=_body(flowId=FLOW_ID, connectors=[{"connectorId": "generic_openapi", "authMethod": "api_key"}]),
    )

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
        expected=None,
    )
    seen: list[dict | None] = []
    original = dh._reject_workspace_policy_violation

    def _capture(request, caller_sub, raw_request=None):
        original(request, caller_sub, raw_request)
        seen.append(request.admission_decision)

    monkeypatch.setattr(dh, "_reject_workspace_policy_violation", _capture)

    response = _client().post("/api/deploy", json=_deploy_body())

    assert response.status_code == 202, response.text
    assert spy.calls.count("StartExecution") == 1
    assert seen and seen[0]["outcome"] == "admitted"
    assert seen[0]["workspaceId"] == "default"
    # Jira is a write connector by its catalog operations (create_ticket), whatever the canvas says,
    # so the agent is medium and routes to the policy's medium approvers.
    assert seen[0]["tier"] == "medium"
    assert [r["code"] for r in seen[0]["reasons"]] == ["write_tool"]
    assert seen[0]["approvers"] == ["g-approvers"]
    assert spy.store.create.call_args[0][0].deployment_slot == "staging"


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
    first = table.store.put(
        "default",
        _policy(workspaceId="default", name="Bedrock only", allowed={"providers": ["bedrock"]}),
        expected=None,
    )

    admitted = _client().post("/api/deploy", json=_body())
    assert admitted.status_code == 202, admitted.text

    spy.calls.clear()
    table.store.put(
        "default",
        _policy(workspaceId="default", name="No Bedrock", allowed={"providers": ["openai"]}),
        expected=first.version,
    )
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
#: The precondition of a PUT that creates a policy; replacements name the version they read
#: (``If-Match``). The two-editor and precondition cases live in ``test_workspace_policy_revisions``.
_CREATE = {"If-None-Match": "*"}


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
    put = client.put("/api/workspaces/finance/policy", json=_POLICY_DOC, headers=_CREATE)
    assert put.status_code == 200, put.text
    assert put.json()["allowed"]["connectors"] == ["slack"]
    assert put.json()["approval"] == {"low": [], "medium": [], "high": []}
    assert re.fullmatch(r'"[0-9a-f]{16}\.1"', put.headers["ETag"]), "the version a replacement must name"
    got = client.get("/api/workspaces/finance/policy")
    assert got.json() == put.json()
    assert got.headers["ETag"] == put.headers["ETag"]
    assert [p["workspaceId"] for p in client.get("/api/workspaces/policies").json()] == ["finance"]
    assert client.delete("/api/workspaces/finance/policy").status_code == 428, "a delete names the version it saw"
    assert client.delete("/api/workspaces/finance/policy", headers={"If-Match": got.headers["ETag"]}).status_code == 204
    assert client.get("/api/workspaces/finance/policy").status_code == 404
    # The same confirmation cannot fire twice: the policy it named is gone.
    again = client.delete("/api/workspaces/finance/policy", headers={"If-Match": got.headers["ETag"]})
    assert again.status_code == 412 and again.json()["detail"]["currentVersion"] is None


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
    client = _admin_client(monkeypatch, flags={"workspacePolicy": True})
    table.fail = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "tenant-sensitive"}}, "PutItem")
    response = client.put("/api/workspaces/finance/policy", json=_POLICY_DOC, headers=_CREATE)
    assert response.status_code == 503
    assert "tenant-sensitive" not in response.text
    assert table.items == {}


# --------------------------------------------------------------------------- review findings, pinned
# Codex (SOL/ultra) security review of the branch before push. Each test here is one finding.


def test_admission_judges_the_request_body_not_the_saved_flow(spy, monkeypatch, table):
    """Finding 1: naming a benign owned flow while submitting a body with a forbidden connector gains
    nothing -- the body is what gets deployed, so the body is what is judged."""
    import importlib
    from datetime import UTC, datetime

    from app.models.enums import DeploymentStatus
    from app.models.flow import Flow

    module = importlib.import_module("app.services.flow_storage")
    store = module.FlowStorage()
    now = datetime.now(UTC)
    benign = Flow(
        id=FLOW_ID,
        name="Plain web search",
        workflow=json.loads((FIXTURES / "web-search-agent.flow.json").read_text()),  # no connector at all
        deployment_status=DeploymentStatus.NOT_DEPLOYED,
        created_at=now,
        updated_at=now,
        owner_sub=OWNER,
    )
    store._flows[benign.id] = benign
    monkeypatch.setattr(module, "_active_storage", store)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put(
        "default",
        _policy(workspaceId="default", name="Read-only", allowed={"connectors": ["slack"]}),
        expected=None,
    )

    response = _client().post("/api/deploy", json=_body(flowId=FLOW_ID, connectors=[JIRA]))

    assert response.status_code == 403, response.text
    assert [v["code"] for v in response.json()["detail"]["violations"]] == ["connector_not_allowed"]
    assert spy.side_effects == []


def test_a_flow_less_body_is_judged_on_everything_it_carries(spy, table, monkeypatch):
    """Finding 1, second half: without a flowId the body's connectors, custom tools, MCP servers and
    knowledge are all judged, not just model and Region."""
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put(
        "default",
        _policy(
            workspaceId="default",
            name="Locked",
            allowed={"connectors": ["slack"], "toolRiskClasses": ["read"], "dataClasses": ["public"]},
        ),
        expected=None,
    )
    body = _body(
        connectors=[JIRA],
        customTools=[{"toolName": "wipe_db", "lambdaCode": "def handler(e, c): return 1"}],
        externalMcpServers=[{"server_id": "corp-mcp", "endpoint": "https://mcp.example.com/sse", "auth_type": "none"}],
        knowledgeBaseConfig={"kbMode": "existing", "knowledgeBaseId": "ABCDEFGHIJ"},
    )

    response = _client().post("/api/deploy", json=body)

    assert response.status_code == 403, response.text
    codes = sorted(v["code"] for v in response.json()["detail"]["violations"])
    assert "connector_not_allowed" in codes  # jira is not slack
    assert "tool_risk_class_not_allowed" in codes  # jira's catalog floor is write; workspace allows read only
    assert "tool_unclassified" in codes  # the custom tool and the MCP server carry no class
    assert "knowledge_unclassified" in codes  # the KB carries no data class
    assert spy.side_effects == []


def test_a_write_connector_labelled_read_on_the_canvas_is_still_refused_by_a_read_only_workspace(
    spy, monkeypatch, table
):
    """Finding 3: the catalog floor, through the whole deploy path."""
    import importlib
    from datetime import UTC, datetime

    from app.models.enums import DeploymentStatus
    from app.models.flow import Flow
    from app.services import spec_service
    from app.services.spec_migration import migrate_v1_flow

    spec = spec_service.normalize(migrate_v1_flow(_business_canvas(), spec_id=FLOW_ID, name="S", owner_sub=OWNER))
    spec = spec.model_copy(
        update={
            "tools": [t.model_copy(update={"risk_class": "read"}) if t.type == "connector" else t for t in spec.tools]
        }
    )
    module = importlib.import_module("app.services.flow_storage")
    store = module.FlowStorage()
    now = datetime.now(UTC)
    flow = Flow(
        id=FLOW_ID,
        name="Mislabelled",
        workflow=spec.model_dump(by_alias=True, mode="json"),
        deployment_status=DeploymentStatus.NOT_DEPLOYED,
        created_at=now,
        updated_at=now,
        owner_sub=OWNER,
    )
    store._flows[flow.id] = flow
    monkeypatch.setattr(module, "_active_storage", store)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put(
        "default",
        _policy(workspaceId="default", name="Read tools only", allowed={"toolRiskClasses": ["read"]}),
        expected=None,
    )

    response = _client().post("/api/deploy", json=_body(flowId=FLOW_ID, connectors=[JIRA]))

    assert response.status_code == 403, response.text
    (violation,) = response.json()["detail"]["violations"]
    assert violation["code"] == "tool_risk_class_not_allowed"
    assert "'write' tool" in violation["message"]
    assert spy.side_effects == []


def test_a_canvas_cannot_pick_a_workspace_its_deployer_is_not_in(spy, monkeypatch, table):
    """Finding 2: the spec's workspaceId is deployer-controlled; naming a permissive workspace one is
    not a member of is the exact 403 workspace_not_member, never a silent re-route."""
    import importlib
    from datetime import UTC, datetime

    from app.models.enums import DeploymentStatus
    from app.models.flow import Flow
    from app.services import spec_service
    from app.services.spec_migration import migrate_v1_flow

    spec = spec_service.normalize(
        migrate_v1_flow(_business_canvas(), spec_id=FLOW_ID, name="S", owner_sub=OWNER, workspace_id="devs")
    )
    module = importlib.import_module("app.services.flow_storage")
    store = module.FlowStorage()
    now = datetime.now(UTC)
    flow = Flow(
        id=FLOW_ID,
        name="Escapee",
        workflow=spec.model_dump(by_alias=True, mode="json"),
        deployment_status=DeploymentStatus.NOT_DEPLOYED,
        created_at=now,
        updated_at=now,
        owner_sub=OWNER,
    )
    store._flows[flow.id] = flow
    monkeypatch.setattr(module, "_active_storage", store)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put(
        "default",
        _policy(workspaceId="default", name="Locked", allowed={"connectors": ["slack"]}),
        expected=None,
    )
    table.store.put(
        "default", _policy(workspaceId="devs", name="Developers", memberGroups=["g-developers"]), expected=None
    )

    response = _client().post("/api/deploy", json=_body(flowId=FLOW_ID, connectors=[JIRA]))

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "policy_denied" and detail["workspaceId"] == "devs" and detail["tier"] is None
    assert [v["code"] for v in detail["violations"]] == ["workspace_not_member"]
    assert spy.side_effects == []


def test_a_member_is_governed_by_their_workspace_policy(spy, monkeypatch, table):
    """Finding 2, positive twin: a deployer who IS a member of the named workspace gets its policy
    (the permissive Developers policy admits the Jira connector the locked default forbids)."""
    import importlib
    from datetime import UTC, datetime

    from app.models.enums import DeploymentStatus
    from app.models.flow import Flow
    from app.services import spec_service
    from app.services.spec_migration import migrate_v1_flow

    spec = spec_service.normalize(
        migrate_v1_flow(_business_canvas(), spec_id=FLOW_ID, name="S", owner_sub=OWNER, workspace_id="devs")
    )
    module = importlib.import_module("app.services.flow_storage")
    store = module.FlowStorage()
    now = datetime.now(UTC)
    flow = Flow(
        id=FLOW_ID,
        name="Dev agent",
        workflow=spec.model_dump(by_alias=True, mode="json"),
        deployment_status=DeploymentStatus.NOT_DEPLOYED,
        created_at=now,
        updated_at=now,
        owner_sub=OWNER,
    )
    store._flows[flow.id] = flow
    monkeypatch.setattr(module, "_active_storage", store)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put(
        "default",
        _policy(workspaceId="default", name="Locked", allowed={"connectors": ["slack"]}),
        expected=None,
    )
    table.store.put(
        "default",
        _policy(
            workspaceId="devs",
            name="Developers",
            memberGroups=["g-users-default"],
            defaults={"unclassifiedRiskTier": "low"},
        ),
        expected=None,
    )

    response = _client().post("/api/deploy", json=_body(flowId=FLOW_ID, connectors=[JIRA]))

    assert response.status_code == 202, response.text
    assert spy.store.create.call_args[0][0].admission["workspaceId"] == "devs"


def test_a_budget_bearing_policy_writes_and_reads_through_the_real_dynamodb_serializer():
    """Finding 7: floats must become Decimal on write (boto3 refuses floats) and numbers again on read."""
    import boto3

    pytest.importorskip("moto")
    from moto import mock_aws

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="OrgConfig",
            KeySchema=[{"AttributeName": "org_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "org_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        store = wps.WorkspacePolicyStore(table_name="OrgConfig", region="us-east-1")
        policy = _policy(budgets={"perAgentMonthlyUsd": 12.5, "perWorkspaceMonthlyUsd": 1000})
        store.put("default", policy, expected=None)
        read = store.get("default", "finance")
        assert read == policy
        assert read.budgets.per_agent_monthly_usd == 12.5 and read.budgets.per_workspace_monthly_usd == 1000
        assert [p.workspace_id for p in store.list("default")] == ["finance"]
        store.delete("default", "finance", expected=store.read("default", "finance").version)
        assert store.get("default", "finance") is None


def test_a_policy_filed_under_another_workspaces_key_is_refused_on_read(table):
    """Finding 4: a document whose workspaceId disagrees with its key governs nothing."""
    item = wps.policy_to_item("default", _policy(workspaceId="finance", name="F"), _VERSION)
    item["sk"] = "WORKSPACE#marketing#POLICY"
    table.items[("default", item["sk"])] = item
    with pytest.raises(wps.WorkspacePolicyError):
        table.store.get("default", "marketing")


def test_empty_or_malformed_group_names_are_refused_at_the_policy_schema():
    """Finding 5: an approver list of [""] must not validate (it would silently remove the gate)."""
    for bad in ([""], ["g-ok", ""], ["has space"], ["#hash"]):
        with pytest.raises(ValueError):
            _policy(approval={"high": bad})
        with pytest.raises(ValueError):
            _policy(memberGroups=bad)


# --------------------------------------------------------------------------- re-review findings, pinned


def _flow_with(monkeypatch, workflow: dict, *, owner_sub: str = OWNER):
    import importlib
    from datetime import UTC, datetime

    from app.models.enums import DeploymentStatus
    from app.models.flow import Flow

    module = importlib.import_module("app.services.flow_storage")
    store = module.FlowStorage()
    now = datetime.now(UTC)
    flow = Flow(
        id=FLOW_ID,
        name="probe",
        workflow=workflow,
        deployment_status=DeploymentStatus.NOT_DEPLOYED,
        created_at=now,
        updated_at=now,
        owner_sub=owner_sub,
    )
    store._flows[flow.id] = flow
    monkeypatch.setattr(module, "_active_storage", store)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    return flow


def _v2_document(monkeypatch, **spec_overrides):
    from app.services import spec_service
    from app.services.spec_migration import migrate_v1_flow

    spec = spec_service.normalize(migrate_v1_flow(_business_canvas(), spec_id=FLOW_ID, name="S", owner_sub=OWNER))
    spec = spec.model_copy(update=spec_overrides)
    return spec.model_dump(by_alias=True, mode="json")


def test_an_omitted_target_region_is_judged_as_the_home_region(spy, table, monkeypatch):
    """Finding 1 (residual): a Region allow-list that excludes the platform's home Region refuses a
    request that simply omits targetRegion, instead of passing it as 'no Region'."""
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put(
        "default",
        _policy(workspaceId="default", name="EU only", allowed={"regions": ["eu-west-1"]}),
        expected=None,
    )

    response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 403, response.text
    (violation,) = response.json()["detail"]["violations"]
    assert violation["code"] == "region_not_allowed"
    assert "'us-east-1'" in violation["message"]
    assert spy.side_effects == []


def test_connected_code_interpreter_and_browser_are_judged_as_tools(spy, table, monkeypatch):
    """Finding 1 (residual): connectedTools carries executable capabilities, not only infrastructure."""
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put(
        "default",
        _policy(workspaceId="default", name="Read only", allowed={"toolRiskClasses": ["read"]}),
        expected=None,
    )

    response = _client().post("/api/deploy", json=_body(connectedTools=["gateway", "code_interpreter", "browser"]))

    assert response.status_code == 403, response.text
    paths = sorted(v["path"] for v in response.json()["detail"]["violations"])
    codes = {v["code"] for v in response.json()["detail"]["violations"]}
    assert codes == {"tool_unclassified"} and len(paths) == 2  # the two capabilities; gateway is not a tool
    assert spy.side_effects == []


def test_a_member_cannot_escape_their_workspace_by_deploying_without_a_flow(spy, table, monkeypatch):
    """Finding 2 (residual): the governing workspace follows the deployer's membership. A member of a
    locked workspace who drops flowId (or saves a canvas with no workspaceId) is still judged by it."""
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put("default", _policy(workspaceId="default", name="Permissive"), expected=None)
    table.store.put(
        "default",
        _policy(
            workspaceId="finance",
            name="Finance (locked)",
            memberGroups=["g-users-default"],
            allowed={"connectors": ["slack"]},
        ),
        expected=None,
    )

    response = _client().post("/api/deploy", json=_body(connectors=[JIRA]))

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["workspaceId"] == "finance"
    assert [v["code"] for v in detail["violations"]] == ["connector_not_allowed"]
    assert spy.side_effects == []


def test_a_member_of_two_workspaces_whose_canvas_names_neither_is_refused_with_selection_required(
    spy, table, monkeypatch
):
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put("default", _policy(workspaceId="default", name="Default"), expected=None)
    table.store.put(
        "default",
        _policy(workspaceId="finance", name="Finance", memberGroups=["g-users-default"]),
        expected=None,
    )
    table.store.put("default", _policy(workspaceId="hr", name="HR", memberGroups=["g-users-default"]), expected=None)

    response = _client().post("/api/deploy", json=_body())

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "policy_denied" and detail["workspaceId"] is None and detail["tier"] is None
    assert [v["code"] for v in detail["violations"]] == ["workspace_selection_required"]
    assert "finance" in detail["message"] and "hr" in detail["message"]
    assert spy.side_effects == []


def _canvas_with_kb(kb_id: str) -> dict:
    """The business canvas plus a knowledge-base tool node on its Gateway, exactly as the deploy panel
    saves it (snake_case data; the compiler sends the camelCase node configuration verbatim)."""
    document = _business_canvas()
    gateway = next(node for node in document["nodes"] if node["data"].get("component_type") == "gateway")
    document["nodes"].append(
        {
            "id": "tool-kb",
            "type": "tool",
            "data": {
                "name": "Knowledge base",
                "tool_id": "knowledge_base",
                "is_knowledge_base": True,
                "kb_mode": "existing",
                "knowledge_base_id": kb_id,
                "enabled": True,
                "component_type": "tool",
            },
            "position": {"x": 0, "y": 0},
        }
    )
    document["edges"].append({"id": "e-kb", "source": gateway["id"], "target": "tool-kb"})
    return document


def _kb_request_config(flow_document: dict) -> dict:
    """What the deploy panel sends as knowledgeBaseConfig for that canvas: the compiled request's value."""
    from app.services.spec_compiler import compile_deploy_request
    from app.services.spec_migration import migrate_v1_flow

    return compile_deploy_request(migrate_v1_flow(flow_document, spec_id=FLOW_ID, name="S", owner_sub=OWNER))[
        "knowledgeBaseConfig"
    ]


def test_a_classification_does_not_travel_to_a_different_resource_with_the_same_id(spy, table, monkeypatch):
    """Finding 10: a reviewed 'public' label on one knowledge base must not carry to another knowledge
    base deployed under the same entry id; the class is bound to the resource's configuration."""
    from app.services import spec_service
    from app.services.spec_migration import migrate_v1_flow

    canvas = _canvas_with_kb("PUBLICKB01")
    spec = spec_service.normalize(migrate_v1_flow(canvas, spec_id=FLOW_ID, name="S", owner_sub=OWNER))
    knowledge = [k.model_copy(update={"data_class": "public"}) if k.type == "customKb" else k for k in spec.knowledge]
    assert any(k.data_class == "public" for k in knowledge)
    _flow_with(monkeypatch, spec.model_copy(update={"knowledge": knowledge}).model_dump(by_alias=True, mode="json"))
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put(
        "default",
        _policy(
            workspaceId="default", name="Public data only", allowed={"dataClasses": ["public"], "connectors": ["jira"]}
        ),
        expected=None,
    )

    same = _client().post(
        "/api/deploy", json=_body(flowId=FLOW_ID, connectors=[JIRA], knowledgeBaseConfig=_kb_request_config(canvas))
    )
    assert same.status_code == 202, same.text  # the SAME knowledge base: the reviewed class carries

    spy.calls.clear()
    other = _client().post(
        "/api/deploy",
        json=_body(
            flowId=FLOW_ID, connectors=[JIRA], knowledgeBaseConfig=_kb_request_config(_canvas_with_kb("SECRETKB99"))
        ),
    )
    assert other.status_code == 403, other.text  # a DIFFERENT knowledge base: unclassified, not public
    assert [v["code"] for v in other.json()["detail"]["violations"]] == ["knowledge_unclassified"]
    assert spy.side_effects == []


def test_the_flows_budget_declaration_reaches_admission(spy, table, monkeypatch):
    """Finding 11: the saved canvas's monthly budget is what the budget rules judge."""
    from app.schemas.agent_spec_v2 import BudgetSpec, PolicySpec

    _flow_with(monkeypatch, _v2_document(monkeypatch, policy=PolicySpec(budget=BudgetSpec(monthly_usd=80))))
    _flags(monkeypatch, {"workspacePolicy": True})

    # A cap with no per-agent default: the flow's own declaration (80) satisfies it.
    first = table.store.put(
        "default",
        _policy(
            workspaceId="default", name="Capped", budgets={"maxAgentMonthlyUsd": 100}, allowed={"connectors": ["jira"]}
        ),
        expected=None,
    )
    assert _client().post("/api/deploy", json=_body(flowId=FLOW_ID, connectors=[JIRA])).status_code == 202

    # A tighter cap refuses that same declaration.
    spy.calls.clear()
    second = table.store.put(
        "default",
        _policy(
            workspaceId="default", name="Tighter", budgets={"maxAgentMonthlyUsd": 50}, allowed={"connectors": ["jira"]}
        ),
        expected=first.version,
    )
    refused = _client().post("/api/deploy", json=_body(flowId=FLOW_ID, connectors=[JIRA]))
    assert refused.status_code == 403, refused.text
    assert [v["code"] for v in refused.json()["detail"]["violations"]] == ["budget_exceeds_workspace"]
    assert spy.side_effects == []

    # The budget-above-default modifier fires on the declared figure: 80 > default 40 raises the tier.
    table.store.put(
        "default",
        _policy(
            workspaceId="default",
            name="Low default",
            budgets={"perAgentMonthlyUsd": 40},
            allowed={"connectors": ["jira"]},
            approval={"high": ["g-admins-security"]},
        ),
        expected=second.version,
    )
    seen: list[dict | None] = []
    original = dh._reject_workspace_policy_violation

    def _capture(request, caller_sub, raw_request=None):
        original(request, caller_sub, raw_request)
        seen.append(request.admission_decision)

    monkeypatch.setattr(dh, "_reject_workspace_policy_violation", _capture)
    assert _client().post("/api/deploy", json=_body(flowId=FLOW_ID, connectors=[JIRA])).status_code == 202
    assert seen[-1]["tier"] == "high"  # jira write (medium) + budget above default -> high
    assert [r["code"] for r in seen[-1]["reasons"]] == ["write_tool", "budget_above_default"]
    assert seen[-1]["approvers"] == ["g-admins-security"]


# --------------------------------------------------------------------------- round-three findings, pinned


def test_the_runtime_protocol_and_a2a_are_judged_as_channels(spy, table, monkeypatch):
    """Finding 1 (round 3): the channels a deployment exposes come from its own configuration."""
    _flags(monkeypatch, {"workspacePolicy": True})
    table.store.put(
        "default",
        _policy(workspaceId="default", name="API only", allowed={"channels": ["api"]}),
        expected=None,
    )

    http = _client().post("/api/deploy", json=_body())
    assert http.status_code == 202, http.text  # the HTTP runtime is the 'api' channel

    spy.calls.clear()
    a2a = _client().post(
        "/api/deploy", json=_body(config={"name": "a2agate", "model": {"modelId": MODEL_ID}, "protocol": "A2A"})
    )
    assert a2a.status_code == 403, a2a.text
    (violation,) = a2a.json()["detail"]["violations"]
    assert (violation["code"], violation["path"]) == ("channel_not_allowed", "/channels/0/type")
    assert "'a2a'" in violation["message"]
    assert spy.side_effects == []


def test_a_custom_tools_classification_dies_with_its_code(spy, table, monkeypatch):
    """Finding 10 (round 3): a classification reviewed for one body of code must not survive the code
    changing under the same tool name."""
    from app.services.admission_spec import spec_for_deploy_request

    body = _body(customTools=[{"toolName": "lookup", "lambdaCode": "def handler(e, c): return 'v1'"}])
    request = dh.DeployRequest.model_validate(body)
    judged = spec_for_deploy_request(request, OWNER, home_region="us-east-1")
    reviewed = judged.model_copy(update={"tools": [t.model_copy(update={"risk_class": "read"}) for t in judged.tools]})

    same_code = spec_for_deploy_request(request, OWNER, classified_from=reviewed, home_region="us-east-1")
    assert [t.risk_class for t in same_code.tools] == ["read"]

    changed = dh.DeployRequest.model_validate(
        _body(customTools=[{"toolName": "lookup", "lambdaCode": "def handler(e, c): return 'v2'"}])
    )
    new_code = spec_for_deploy_request(changed, OWNER, classified_from=reviewed, home_region="us-east-1")
    assert [t.risk_class for t in new_code.tools] == ["unclassified"]
    # And the custom-code floor carries the real hash, not a placeholder.
    assert new_code.code.custom_nodes["lookup"].sha256 != "0" * 64


def test_an_mcp_servers_classification_dies_when_its_endpoint_moves():
    from app.services.admission_spec import spec_for_deploy_request

    def request_for(endpoint: str):
        return dh.DeployRequest.model_validate(
            _body(externalMcpServers=[{"server_id": "corp-mcp", "endpoint": endpoint, "auth_type": "none"}])
        )

    judged = spec_for_deploy_request(request_for("https://mcp.corp.example/sse"), OWNER, home_region="us-east-1")
    reviewed = judged.model_copy(update={"tools": [t.model_copy(update={"risk_class": "read"}) for t in judged.tools]})
    same = spec_for_deploy_request(
        request_for("https://mcp.corp.example/sse"), OWNER, classified_from=reviewed, home_region="us-east-1"
    )
    assert [t.risk_class for t in same.tools] == ["read"]
    moved = spec_for_deploy_request(
        request_for("https://evil.example/sse"), OWNER, classified_from=reviewed, home_region="us-east-1"
    )
    assert [t.risk_class for t in moved.tools] == ["unclassified"]


def test_a_truncated_stored_policy_is_corruption_not_an_unconstrained_workspace(table):
    """Finding 4 (round 3): the writer always stores the complete document, so a stored item missing
    fields (at any level) must refuse rather than reconstruct permissive defaults."""
    full = wps.policy_to_item("default", _policy(workspaceId="default", name="D"), _VERSION)
    assert table.store.put(
        "default", _policy(workspaceId="default", name="D"), expected=None
    )  # the full document reads back
    assert table.store.get("default", "default") is not None

    truncated = {
        "org_id": "default",
        "sk": "WORKSPACE#default#POLICY",
        "policy": {"workspaceId": "default", "name": "D"},
    }
    table.items[("default", truncated["sk"])] = truncated
    with pytest.raises(wps.WorkspacePolicyError) as excinfo:
        table.store.get("default", "default")
    assert "incomplete" in str(excinfo.value) and "allowed" in str(excinfo.value)
    with pytest.raises(wps.WorkspacePolicyUnavailableError):
        wps.resolve_policy(table.store, "default", None, ["g-users-default"])

    nested = copy.deepcopy(full)
    del nested["policy"]["approval"]["high"]
    table.items[("default", truncated["sk"])] = nested
    with pytest.raises(wps.WorkspacePolicyError) as excinfo:
        table.store.get("default", "default")
    assert "approval.high" in str(excinfo.value)


def test_in_a_locked_workspace_a_write_label_cannot_pick_the_listed_class(spy, table, monkeypatch):
    """Round 4: allowed.toolRiskClasses=[write] with trustBuilderClassifications off. A Lambda the builder
    labelled ``write`` is still refused as unclassified (nothing verifies it), while the Jira connector the
    catalog verifies as write is admitted -- and the agent still tiers medium from the label."""
    from app.services import spec_service
    from app.services.spec_migration import migrate_v1_flow

    spec = spec_service.normalize(migrate_v1_flow(_business_canvas(), spec_id=FLOW_ID, name="S", owner_sub=OWNER))
    _flow_with(monkeypatch, spec.model_dump(by_alias=True, mode="json"))
    _flags(monkeypatch, {"workspacePolicy": True})
    locked = WorkspacePolicy.model_validate(
        {
            "workspaceId": "default",
            "name": "Locked",
            "allowed": {"toolRiskClasses": ["write"]},
            "defaults": {"unclassifiedRiskTier": "low"},
        }
    )
    assert locked.defaults.trust_builder_classifications is False
    table.store.put("default", locked, expected=None)

    verified = _client().post("/api/deploy", json=_body(flowId=FLOW_ID, connectors=[JIRA]))
    assert verified.status_code == 202, verified.text  # jira: catalog-verified write

    spy.calls.clear()
    labelled = _client().post(
        "/api/deploy",
        json=_body(
            flowId=FLOW_ID, customTools=[{"toolName": "mutate_db", "lambdaCode": "def handler(e, c): return 1"}]
        ),
    )
    assert labelled.status_code == 403, labelled.text  # the custom tool carries no verifiable class
    assert {v["code"] for v in labelled.json()["detail"]["violations"]} == {"tool_unclassified"}
    assert spy.side_effects == []


def test_an_mcp_server_runtimes_classification_dies_when_its_configuration_changes():
    """Round 4 (finding 10): the standalone MCP server runtime binds to its whole configuration."""
    from app.services.admission_spec import spec_for_deploy_request

    def request_for(tools):
        return dh.DeployRequest.model_validate(_body(mcpServerConfig={"name": "corp-tools", "tools": tools}))

    judged = spec_for_deploy_request(request_for(["lookup"]), OWNER, home_region="us-east-1")
    reviewed = judged.model_copy(update={"tools": [t.model_copy(update={"risk_class": "read"}) for t in judged.tools]})
    same = spec_for_deploy_request(request_for(["lookup"]), OWNER, classified_from=reviewed, home_region="us-east-1")
    assert [t.risk_class for t in same.tools] == ["read"]
    changed = spec_for_deploy_request(
        request_for(["lookup", "wipe"]), OWNER, classified_from=reviewed, home_region="us-east-1"
    )
    assert [t.risk_class for t in changed.tools] == ["unclassified"]
