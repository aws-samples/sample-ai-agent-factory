"""Triggers attached after deployment are RE-ADMITTED under the current workspace policy (plan §3.B).

Three layers, each at its own boundary:

* ``trigger_admission.judge_trigger_attachment`` -- pure and table-driven. A non-ingress trigger is
  allowed without a read. A webhook on an un-admitted version, or on one whose admission predates the
  recorded inventory, is refused. Otherwise the recorded inventory plus the trigger is admitted against
  the CURRENT policy: a read-only agent keeps its tier (and still reads the policy, so the write is
  fenced); an agent with any tool not verified read-only, or with custom code, becomes high and needs
  every current ``approval.high`` group, whatever tier it was admitted at; a connector the current
  policy no longer allows refuses the webhook with the allow-list's own code; a tightened approval for
  an unchanged tier refuses it too; revoking trust in builder classifications revokes the exemption;
  malformed records and missing version-3 fields raise; a missing policy is unavailable, never default.
* The recorded inventory re-admits IDENTICALLY to the original spec (tier, reasons, violations,
  capabilities) on every gallery template and hand-built specs, and the recorded capabilities are
  exactly what the classifier's ingress rule fires on.
* ``POST /api/runtimes/{name}/triggers`` through the real router with every side effect stubbed to a
  sentinel: flag off is the old route; the business persona's webhook on a risky agent is the exact
  ``403 policy_denied`` with ZERO side effects; a schedule is not judged; an approved agent proceeds and
  the row write receives the policy fence; a policy changed between judgement and write is 409; a legacy
  version cannot get a webhook; a malformed record is 409; an unreadable policy is 503; a non-owner 404.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from app.routers import triggers
from app.schemas.agent_spec_v2 import AgentSpecV2, TriggerSpec
from app.schemas.workspace_policy import WorkspacePolicy
from app.services import trigger_admission as ta
from app.services import workspace_policy_service as wps
from app.services.admission_inventory import AdmissionInventory
from app.services.agent_versions_store import AgentVersion, RuntimeSlots
from app.services.auth import get_caller_sub
from app.services.risk_classifier import PUBLIC_INGRESS_WITH_WRITE_CODE, classify, effective_risk_class
from app.services.spec_migration import migrate_v1_flow
from app.services.trigger_store import (
    TYPE_CRON,
    TYPE_EVENTBRIDGE,
    TYPE_S3,
    TYPE_WEBHOOK,
    RuntimeClaim,
    TriggerClaimConflict,
    TriggerPolicyConflict,
)
from app.services.workspace_admission import ADMISSION_VERSION, POLICY_DENIED, admit
from app.services.workspace_approval import MalformedAdmissionError, approval_record
from app.services.workspace_policy_service import PolicyFence, PolicyVersion, WorkspacePolicyUnavailableError
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests.test_workspace_admission import FIXTURES, _policy, _spec, _tool

OWNER = "sub-alice"
APPROVER = "sub-approver"
SECURITY_REVIEWER = "sub-security"
RUNTIME = "orders_bot"
RUNTIME_ID = "orders_bot_1a2b3c4d-AbCdEfGhIj"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/{RUNTIME_ID}"
NOW = datetime.now(UTC).isoformat()
SHA = "a" * 64

#: ``high`` needs security AND the approvers; ``medium`` needs the approvers alone. Trusting, like the
#: shared ``_policy`` helper, so labelled tools classify as written unless a test says otherwise.
HIGH_POLICY = _policy(approval={"medium": ["g-approvers"], "high": ["g-security", "g-approvers"]})
#: What a store snapshot carries beside the policy: the exact item for the row write's condition.
FENCE = PolicyFence(
    table_name="org-config",
    key={"org_id": "org", "sk": "WORKSPACE#finance#POLICY"},
    document={"workspaceId": "finance", "budgets": {"maxAgentMonthlyUsd": Decimal("100")}},
    version=PolicyVersion(incarnation="a" * 16, revision=1),
)

READ_ONLY = _spec(tools=[_tool("lookup", "read")])
WRITER = _spec(tools=[_tool("jira", "write", kind="connector", ref="jira")])
PAYER = _spec(tools=[_tool("stripe", "pay")])
#: A Lambda nobody classified: the classifier cannot show it is harmless.
MYSTERY = _spec(tools=[_tool("mystery")])
NO_TOOLS = _spec()
#: Executable custom code and no declared tools.
CODER = _spec(
    code={"customNodes": {"transform": {"language": "python", "sourceRef": "inline:transform", "sha256": SHA}}}
)
#: Confidential knowledge and only a verified read tool: high by data class, not by tools.
CONFIDENTIAL = _spec(
    tools=[_tool("lookup", "read")], knowledge=[{"id": "kb", "type": "managedKb", "dataClass": "confidential"}]
)


def _admission(spec: AgentSpecV2, policy: WorkspacePolicy = HIGH_POLICY) -> dict[str, Any]:
    decision = admit(spec, policy)
    assert decision.outcome == "admitted"
    return decision.model_dump(by_alias=True)


def _lookup_of(policy: WorkspacePolicy | None = HIGH_POLICY) -> MagicMock:
    return MagicMock(return_value=None if policy is None else (policy, FENCE))


def _judge(trigger_type: str, admission, approvals=None, lookup=None) -> ta.TriggerAdmission:
    return ta.judge_trigger_attachment(
        trigger_type,
        admission,
        approvals,
        owner_sub=OWNER,
        policy_lookup=lookup if lookup is not None else _lookup_of(),
    )


def _codes(result: ta.TriggerAdmission) -> list[str]:
    return [violation.code for violation in result.violations]


# --------------------------------------------------------------------------- pure decision


def test_platform_trigger_types_map_onto_the_spec_vocabulary():
    assert {ta.spec_trigger_type(t) for t in (TYPE_CRON, TYPE_EVENTBRIDGE, TYPE_S3, TYPE_WEBHOOK)} == {
        "schedule",
        "event",
        "file",
        "webhook",
    }
    assert ta.is_public_ingress(TYPE_WEBHOOK)
    assert not any(ta.is_public_ingress(t) for t in (TYPE_CRON, TYPE_EVENTBRIDGE, TYPE_S3))


@pytest.mark.parametrize("trigger_type", [TYPE_CRON, TYPE_EVENTBRIDGE, TYPE_S3])
@pytest.mark.parametrize("admission", [None, {"not": "a decision"}, "garbage"])
def test_a_non_ingress_trigger_is_allowed_without_reading_anything(trigger_type, admission):
    lookup = _lookup_of()
    result = _judge(trigger_type, admission, lookup=lookup)
    assert result.outcome == "allowed"
    assert result.workspace_id is None and result.admitted_tier is None and result.resulting_tier is None
    assert result.policy_fence is None
    lookup.assert_not_called()


def test_a_webhook_on_a_version_without_admission_is_refused():
    lookup = _lookup_of()
    result = _judge(TYPE_WEBHOOK, None, lookup=lookup)
    assert result.outcome == "denied"
    assert _codes(result) == [ta.VERSION_NOT_ADMITTED]
    assert result.violations[0].path == "/triggers/0/type"
    assert "never classified" in result.violations[0].message
    lookup.assert_not_called()


def test_an_admission_that_predates_the_inventory_is_refused_not_guessed_at():
    """A version-1 decision records nothing to re-admit from."""
    legacy = _admission(WRITER)
    legacy["admissionVersion"] = 1
    legacy.pop("ingressSensitiveCapabilities")
    legacy.pop("inventory")
    lookup = _lookup_of()
    result = _judge(TYPE_WEBHOOK, legacy, lookup=lookup)
    assert result.outcome == "denied"
    assert _codes(result) == [ta.ADMISSION_PREDATES_INGRESS_RULE]
    assert "version 1" in result.violations[0].message
    lookup.assert_not_called()


@pytest.mark.parametrize("spec", [READ_ONLY, NO_TOOLS], ids=["read-only tool", "no tools"])
def test_a_webhook_on_an_agent_whose_tools_are_all_verified_read_only_keeps_its_tier_and_is_fenced(spec):
    lookup = _lookup_of()
    result = _judge(TYPE_WEBHOOK, _admission(spec), lookup=lookup)
    assert result.outcome == "allowed"
    assert (result.workspace_id, result.admitted_tier, result.resulting_tier) == ("finance", "low", "low")
    assert result.ingress_sensitive_capabilities == []
    assert result.policy_fence == FENCE, "every public-ingress judgement reads the policy and is fenced"
    lookup.assert_called_once_with("finance")


@pytest.mark.parametrize(
    ("spec", "capability", "admitted"),
    [
        (WRITER, "jira", "medium"),
        (MYSTERY, "mystery", "medium"),
        (PAYER, "stripe", "high"),
        (CODER, "code:transform", "medium"),
    ],
    ids=["write connector", "unclassified lambda", "pay tool", "custom code"],
)
def test_a_webhook_on_an_agent_with_an_ingress_sensitive_capability_needs_the_current_high_tier_approvers(
    spec, capability, admitted
):
    admission = _admission(spec)
    assert admission["tier"] == admitted
    assert admission["ingressSensitiveCapabilities"] == [capability]
    lookup = _lookup_of()
    result = _judge(TYPE_WEBHOOK, admission, [approval_record("g-approvers", APPROVER, NOW)], lookup)
    assert result.outcome == "denied"
    assert _codes(result) == [ta.TRIGGER_RAISES_RISK_TIER if admitted != "high" else ta.TRIGGER_REQUIRES_APPROVAL]
    assert (result.workspace_id, result.admitted_tier, result.resulting_tier) == ("finance", admitted, "high")
    assert result.ingress_sensitive_capabilities == [capability]
    assert "g-security" in result.violations[0].message
    assert "g-approvers" not in result.violations[0].message, "a group that already approved is not pending"
    lookup.assert_called_once_with("finance")


def test_an_unclassified_lambda_approved_as_medium_cannot_be_exposed_without_security():
    """Codex round-1 finding 1: omitting a classification must not make a mutating Lambda read-only."""
    admission = _admission(MYSTERY)
    assert admission["tier"] == "medium" and admission["approvers"] == ["g-approvers"]
    result = _judge(TYPE_WEBHOOK, admission, [approval_record("g-approvers", APPROVER, NOW)])
    assert _codes(result) == [ta.TRIGGER_RAISES_RISK_TIER]


def test_moving_the_mutation_into_custom_code_is_not_a_way_around_the_gate():
    """Codex round-2 finding 8: executable custom code is an ingress-sensitive capability."""
    admission = _admission(CODER)
    assert admission["tier"] == "medium", "custom code alone is medium"
    result = _judge(TYPE_WEBHOOK, admission, [approval_record("g-approvers", APPROVER, NOW)])
    assert _codes(result) == [ta.TRIGGER_RAISES_RISK_TIER]
    assert result.ingress_sensitive_capabilities == ["code:transform"]


def test_once_every_high_tier_group_has_approved_the_webhook_is_allowed_and_carries_the_fence():
    approvals = [
        approval_record("g-approvers", APPROVER, NOW),
        approval_record("g-security", SECURITY_REVIEWER, NOW),
    ]
    result = _judge(TYPE_WEBHOOK, _admission(WRITER), approvals)
    assert result.outcome == "allowed"
    assert (result.admitted_tier, result.resulting_tier) == ("medium", "high")
    assert result.ingress_sensitive_capabilities == ["jira"]
    assert result.policy_fence == FENCE


def test_a_workspace_that_auto_approves_high_allows_the_webhook():
    auto_high = _policy(approval={"medium": ["g-approvers"]})
    result = _judge(TYPE_WEBHOOK, _admission(WRITER, auto_high), None, _lookup_of(auto_high))
    assert result.outcome == "allowed"
    assert result.resulting_tier == "high" and result.policy_fence == FENCE


def test_an_agent_admitted_high_is_still_judged_by_the_current_policy():
    """Codex round-1 finding 2: a tier of high is not a licence; the policy of today decides."""
    auto_high = _policy(approval={"medium": ["g-approvers"]})
    admission = _admission(PAYER, auto_high)
    assert admission["tier"] == "high" and admission["approvers"] == []
    allowed = _judge(TYPE_WEBHOOK, admission, None, _lookup_of(auto_high))
    assert allowed.outcome == "allowed" and allowed.policy_fence == FENCE
    tightened = _policy(approval={"medium": ["g-approvers"], "high": ["g-security"]})
    refused = _judge(TYPE_WEBHOOK, admission, None, _lookup_of(tightened))
    assert _codes(refused) == [ta.TRIGGER_REQUIRES_APPROVAL], "the tier does not rise, the requirement does"
    assert "g-security" in refused.violations[0].message
    with pytest.raises(WorkspacePolicyUnavailableError):
        _judge(TYPE_WEBHOOK, admission, None, _lookup_of(None))


def test_a_high_agent_by_data_class_alone_is_judged_by_the_current_policy_too():
    """Codex round-2 finding 2: no ingress-sensitive tool, still high; the shortcut is gone."""
    auto_high = _policy(approval={"medium": ["g-approvers"]})
    admission = _admission(CONFIDENTIAL, auto_high)
    assert admission["tier"] == "high" and admission["ingressSensitiveCapabilities"] == []
    tightened = _policy(approval={"medium": ["g-approvers"], "high": ["g-security"]})
    refused = _judge(TYPE_WEBHOOK, admission, None, _lookup_of(tightened))
    assert _codes(refused) == [ta.TRIGGER_REQUIRES_APPROVAL]
    with pytest.raises(WorkspacePolicyUnavailableError):
        _judge(TYPE_WEBHOOK, admission, None, _lookup_of(None))


def test_revoking_trust_in_builder_classifications_revokes_the_exemption_it_granted():
    """Codex round-2 finding 7: a ``read`` label accepted under Developer is unclassified under Standard."""
    developer = _policy(defaults={"trustBuilderClassifications": True, "unclassifiedRiskTier": "low"})
    admission = _admission(_spec(tools=[_tool("lambda-x", "read")]), developer)
    assert admission["tier"] == "low" and admission["ingressSensitiveCapabilities"] == []
    standard = _policy(
        defaults={"trustBuilderClassifications": False},
        approval={"medium": ["g-approvers"], "high": ["g-security", "g-approvers"]},
    )
    refused = _judge(TYPE_WEBHOOK, admission, None, _lookup_of(standard))
    assert _codes(refused) == [ta.TRIGGER_RAISES_RISK_TIER]
    assert refused.ingress_sensitive_capabilities == ["lambda-x"], "the label no longer counts"
    assert refused.resulting_tier == "high"


def test_a_connector_the_current_policy_no_longer_allows_refuses_the_webhook_with_the_allow_lists_code():
    admission = _admission(WRITER)
    locked = _policy(allowed={"connectors": ["slack"]}, approval={"high": []})
    refused = _judge(TYPE_WEBHOOK, admission, None, _lookup_of(locked))
    assert refused.outcome == "denied"
    assert "connector_not_allowed" in _codes(refused)
    assert ta.trigger_denial_detail(refused)["message"].startswith(
        "This trigger cannot be attached: with it the agent uses"
    )


def test_the_judgement_reads_the_current_policy_not_the_one_admission_saw():
    admitted_under = _policy(approval={"medium": ["g-approvers"]})
    tightened = _policy(approval={"medium": ["g-approvers"], "high": ["g-security"]})
    result = _judge(TYPE_WEBHOOK, _admission(WRITER, admitted_under), None, _lookup_of(tightened))
    assert _codes(result) == [ta.TRIGGER_RAISES_RISK_TIER]
    assert "g-security" in result.violations[0].message


def test_an_owner_authored_approval_is_tampering_and_blocks():
    approvals = [approval_record("g-security", OWNER, NOW), approval_record("g-approvers", OWNER, NOW)]
    with pytest.raises(MalformedAdmissionError):
        _judge(TYPE_WEBHOOK, _admission(WRITER), approvals)


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda doc: doc.pop("admissionVersion"),
        lambda doc: doc.pop("classifierVersion"),
        lambda doc: doc.__setitem__("outcome", "denied"),
        lambda doc: doc.__setitem__("approvers", ["", "g-ok"]),
        lambda doc: doc.__setitem__("admissionVersion", 99),
        lambda doc: doc.__setitem__("admissionVersion", 2),  # development-only version, never readable
        lambda doc: doc.__setitem__("ingressSensitiveCapabilities", "jira"),
        lambda doc: doc.pop("ingressSensitiveCapabilities"),  # Codex round-2 finding 6
        lambda doc: doc.pop("inventory"),
        lambda doc: doc.__setitem__("inventory", None),
        lambda doc: doc.clear(),
    ],
)
def test_a_malformed_admission_blocks_instead_of_being_read_as_no_requirement(corrupt):
    admission = _admission(WRITER)
    corrupt(admission)
    with pytest.raises(MalformedAdmissionError):
        _judge(TYPE_WEBHOOK, admission)


#: Every key the inventory writer produces. The reader requires each to be PRESENT (an explicit empty
#: list or null is fine), so a record that lost one cannot re-admit as an agent with fewer capabilities.
INVENTORY_KEYS = [
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
]


def test_the_recorded_inventory_carries_every_key_the_reader_requires():
    assert set(_admission(WRITER)["inventory"]) == set(INVENTORY_KEYS)


@pytest.mark.parametrize("key", INVENTORY_KEYS)
def test_an_inventory_missing_any_field_is_malformed_not_an_agent_with_fewer_capabilities(key):
    """Codex round-3 finding 10: a version-3 record that lost ``inventory.tools`` kept its medium tier
    and its ``ingressSensitiveCapabilities`` but rebuilt as a toolless agent, which a webhook made LOW
    risk and the policy auto-approved. Absence is corruption, so the judgement is refused."""
    admission = _admission(WRITER)
    del admission["inventory"][key]
    with pytest.raises(MalformedAdmissionError):
        _judge(TYPE_WEBHOOK, admission)


@pytest.mark.parametrize(
    ("spec", "path"),
    [
        (WRITER, ("tools", 0, "riskClass")),
        (WRITER, ("tools", 0, "ref")),
        (CODER, ("customCode", 0, "sha256")),
        (CONFIDENTIAL, ("knowledge", 0, "dataClass")),
    ],
    ids=["tool label", "tool ref", "code digest", "knowledge class"],
)
def test_an_inventory_entry_missing_a_field_is_malformed(spec, path):
    admission = _admission(spec)
    *parents, leaf = path
    node = admission["inventory"]
    for step in parents:
        node = node[step]
    del node[leaf]
    with pytest.raises(MalformedAdmissionError):
        _judge(TYPE_WEBHOOK, admission)


def test_an_inventory_with_an_unknown_key_is_malformed():
    admission = _admission(WRITER)
    admission["inventory"]["toolsVerified"] = True
    with pytest.raises(MalformedAdmissionError):
        _judge(TYPE_WEBHOOK, admission)


def test_a_missing_policy_for_the_admitted_workspace_is_unavailable_not_the_default():
    with pytest.raises(WorkspacePolicyUnavailableError):
        _judge(TYPE_WEBHOOK, _admission(WRITER), None, _lookup_of(None))


def test_the_denial_body_is_the_deploy_refusals_envelope():
    result = _judge(TYPE_WEBHOOK, _admission(WRITER))
    assert ta.trigger_denial_detail(result) == {
        "code": POLICY_DENIED,
        "workspaceId": "finance",
        "tier": "medium",
        "violations": [
            {"code": ta.TRIGGER_RAISES_RISK_TIER, "path": "/triggers/0/type", "message": result.violations[0].message}
        ],
        "message": f"This trigger cannot be attached: {result.violations[0].message}. Nothing was created.",
    }
    with pytest.raises(ValueError):
        ta.trigger_denial_detail(_judge(TYPE_CRON, None))


def test_the_decision_serializes_camel_case_for_logs_and_evidence():
    result = _judge(TYPE_WEBHOOK, _admission(WRITER))
    document = result.model_dump(by_alias=True)
    assert set(document) == {
        "outcome",
        "triggerType",
        "workspaceId",
        "admittedTier",
        "resultingTier",
        "ingressSensitiveCapabilities",
        "violations",
        "policyFence",
    }


# --------------------------------------------------------------------------- inventory vs classifier


def _real_specs() -> list[AgentSpecV2]:
    """Every gallery template as the canvas persists it, plus hand-built specs covering each class."""
    specs = [migrate_v1_flow(json.loads(path.read_text())) for path in sorted(FIXTURES.glob("*.flow.json"))]
    assert len(specs) >= 6, "the gallery fixtures are missing"
    specs += [
        READ_ONLY,
        WRITER,
        PAYER,
        MYSTERY,
        NO_TOOLS,
        CODER,
        CONFIDENTIAL,
        _spec(tools=[_tool("jira", "read", kind="connector", ref="jira")]),  # catalog floors it to write
        _spec(tools=[_tool("lookup", "read"), _tool("sender", "write")]),
        _spec(
            model={"provider": "openai", "modelId": "gpt-5"},
            tools=[_tool("lookup", "read")],
            channels=[{"id": "ch", "type": "slack"}],
            deployment={"region": "eu-west-1"},
            policy={"budget": {"monthlyUsd": 42.5}},
        ),
    ]
    return specs


@pytest.mark.parametrize("trusting", [True, False])
def test_the_recorded_capabilities_agree_with_the_classifier_on_real_specs(trusting):
    policy = _policy(defaults={"trustBuilderClassifications": trusting})
    seen = set()
    for spec in _real_specs():
        decision = admit(spec, policy)
        assert decision.admission_version == ADMISSION_VERSION == 3
        expected = [tool.id for tool in spec.tools if effective_risk_class(tool, policy) != "read"]
        expected += [f"code:{node}" for node in spec.code.custom_nodes]
        assert decision.ingress_sensitive_capabilities == expected, spec.id
        with_webhook = spec.model_copy(update={"triggers": [TriggerSpec(id="hook", type="webhook")]})
        fires = any(reason.code == PUBLIC_INGRESS_WITH_WRITE_CODE for reason in classify(with_webhook, policy).reasons)
        assert fires is bool(expected), spec.id
        seen.add(bool(expected))
    assert seen == {True, False}, "the sample must exercise both outcomes"


@pytest.mark.parametrize("trusting", [True, False])
@pytest.mark.parametrize(
    "policy_overrides",
    [
        {},
        {"allowed": {"connectors": ["slack"], "providers": ["bedrock"], "channels": ["web"], "regions": ["us-east-1"]}},
        {"budgets": {"maxAgentMonthlyUsd": 10}},
    ],
    ids=["open", "constrained", "capped"],
)
def test_the_recorded_inventory_re_admits_exactly_like_the_original_spec(trusting, policy_overrides):
    """What the attachment gate judges is what admission judged: same tier, reasons, violations and
    capabilities, with and without the webhook, under permissive and constraining policies."""
    policy = _policy(defaults={"trustBuilderClassifications": trusting}, **policy_overrides)
    for spec in _real_specs():
        rebuilt = AdmissionInventory.from_spec(spec).to_spec()
        original, again = admit(spec, policy), admit(rebuilt, policy)
        assert (again.outcome, again.tier, again.reasons, again.violations) == (
            original.outcome,
            original.tier,
            original.reasons,
            original.violations,
        ), spec.id
        assert again.ingress_sensitive_capabilities == original.ingress_sensitive_capabilities, spec.id
        hook = TriggerSpec(id="trigger-webhook", type="webhook")
        with_hook = spec.model_copy(update={"triggers": [*spec.triggers, hook]})
        rebuilt_with_hook = AdmissionInventory.from_spec(spec).to_spec(extra_triggers=[hook])
        assert classify(rebuilt_with_hook, policy) == classify(with_hook, policy), spec.id


def test_the_inventory_keeps_labels_not_effective_classes():
    inventory = AdmissionInventory.from_spec(_spec(tools=[_tool("jira", "read", kind="connector", ref="jira")]))
    assert inventory.tools[0].risk_class == "read", "the builder's word, verbatim; the policy of the day decides"
    assert inventory.to_spec().tools[0].risk_class == "read"


def test_an_unclassified_tool_is_ingress_sensitive_unless_the_workspace_trusts_a_read_label():
    untrusting, trusting = _policy(defaults={"trustBuilderClassifications": False}), _policy()
    labelled_read = _spec(tools=[_tool("lambda-x", "read")])
    assert admit(MYSTERY, untrusting).ingress_sensitive_capabilities == ["mystery"]
    assert admit(MYSTERY, trusting).ingress_sensitive_capabilities == ["mystery"]
    assert admit(labelled_read, untrusting).ingress_sensitive_capabilities == ["lambda-x"]
    assert admit(labelled_read, trusting).ingress_sensitive_capabilities == []
    assert admit(CODER, trusting).ingress_sensitive_capabilities == ["code:transform"], "code is never verified"


# --------------------------------------------------------------------------- the route


class _Stop(Exception):
    """Raised by a stubbed side effect: the request got exactly this far."""


def _flags(monkeypatch, flags: dict[str, bool]) -> None:
    from app.services import feature_flags as ff

    monkeypatch.setenv(ff.LOCAL_ENV_VAR, json.dumps(flags))
    monkeypatch.setenv("ENVIRONMENT", "local")
    ff.reset_feature_flag_cache()


@pytest.fixture(autouse=True)
def _fresh_flag_cache():
    from app.services import feature_flags as ff

    ff.reset_feature_flag_cache()
    yield
    ff.reset_feature_flag_cache()


def _claim(*, admission=None, approvals=None, owner: str = OWNER) -> RuntimeClaim:
    return RuntimeClaim(
        slot=RuntimeSlots(runtime_name=RUNTIME, owner_sub=owner, production_version_id="v1"),
        version=AgentVersion(
            runtime_name=RUNTIME,
            version_id="v1",
            owner_sub=owner,
            created_at=NOW,
            deployment_id="dep-1",
            agentcore_runtime_name=RUNTIME,
            runtime_id=RUNTIME_ID,
            runtime_arn=RUNTIME_ARN,
            status="succeeded",
            admission=admission,
            admission_approvals=approvals,
        ),
        target_runtime_arn=RUNTIME_ARN,
    )


@pytest.fixture
def route(monkeypatch):
    """The real router: ownership resolves to an owned HTTP runtime, the workspace policy store is a
    fake, and every side effect (the webhook secret, the trigger row) is a sentinel."""
    state = SimpleNamespace(claim=_claim(), policy=HIGH_POLICY, store_error=None)
    monkeypatch.setattr(triggers, "_resolve_owned_runtime_claim", lambda name, sub: state.claim)
    monkeypatch.setattr(
        triggers, "resolve_owned_deployment_runtime_target", lambda *a, **k: SimpleNamespace(protocol="HTTP")
    )
    monkeypatch.setattr(triggers, "_region", lambda: "us-east-1")
    monkeypatch.setattr(triggers, "boto3", MagicMock())
    state.secret = MagicMock(side_effect=_Stop("webhook secret"))
    monkeypatch.setattr(triggers, "_store_webhook_secret", state.secret)
    state.trigger_store = MagicMock()
    state.trigger_store.create_trigger.side_effect = _Stop("trigger row")
    monkeypatch.setattr(triggers, "get_trigger_store", lambda: state.trigger_store)

    def _get_snapshot(org_id: str, workspace_id: str):
        if state.store_error is not None:
            raise state.store_error
        return None if state.policy is None else (state.policy, FENCE)

    state.policy_store = MagicMock()
    state.policy_store.get_snapshot.side_effect = _get_snapshot
    monkeypatch.setattr(wps, "get_workspace_policy_store", lambda: state.policy_store)

    app = FastAPI()
    app.include_router(triggers.router)
    app.dependency_overrides[get_caller_sub] = lambda: OWNER
    state.client = TestClient(app, raise_server_exceptions=False)
    return state


WEBHOOK = {"type": "webhook"}
CRON = {"type": "cron", "schedule": "cron(0 12 * * ? *)"}
FULLY_APPROVED = [approval_record("g-approvers", APPROVER, NOW), approval_record("g-security", SECURITY_REVIEWER, NOW)]


def _post(route, body: dict) -> Any:
    return route.client.post(f"/api/runtimes/{RUNTIME}/triggers", json=body)


def _nothing_happened(route) -> None:
    assert not route.secret.called
    assert not route.trigger_store.create_trigger.called


def test_flag_off_leaves_the_route_exactly_as_it_was(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": False})
    route.claim = _claim(admission=_admission(WRITER))
    response = _post(route, WEBHOOK)
    assert response.status_code == 500, response.text  # the sentinel at the secret mint: the gate did not run
    assert route.secret.called
    route.policy_store.get_snapshot.assert_not_called()


@pytest.mark.parametrize(
    "spec", [WRITER, MYSTERY, CODER], ids=["write connector", "unclassified lambda", "custom code"]
)
def test_a_business_persona_attaching_a_webhook_to_a_risky_agent_is_the_exact_403_with_zero_side_effects(
    route, monkeypatch, spec
):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=_admission(spec), approvals=[approval_record("g-approvers", APPROVER, NOW)])
    response = _post(route, WEBHOOK)
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert (detail["code"], detail["workspaceId"], detail["tier"]) == (POLICY_DENIED, "finance", "medium")
    assert [violation["code"] for violation in detail["violations"]] == [ta.TRIGGER_RAISES_RISK_TIER]
    assert detail["violations"][0]["path"] == "/triggers/0/type"
    assert "g-security" in detail["violations"][0]["message"]
    assert detail["message"].endswith("Nothing was created.")
    _nothing_happened(route)
    route.policy_store.get_snapshot.assert_called_once_with(wps.DEFAULT_ORG_ID, "finance")


def test_a_schedule_trigger_is_not_judged_and_proceeds_without_a_fence(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=_admission(WRITER))
    response = _post(route, CRON)
    assert response.status_code == 500, response.text
    assert response.json()["detail"] == "Could not register trigger"  # the sentinel at the row write
    assert route.trigger_store.create_trigger.call_args.kwargs["policy_fence"] is None
    route.policy_store.get_snapshot.assert_not_called()


def test_a_webhook_on_a_read_only_agent_proceeds_after_reading_the_current_policy(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=_admission(READ_ONLY))
    response = _post(route, WEBHOOK)
    assert response.status_code == 500, response.text
    assert route.secret.called
    route.policy_store.get_snapshot.assert_called_once_with(wps.DEFAULT_ORG_ID, "finance")


def test_an_approved_risky_agents_webhook_write_is_bound_to_the_policy_it_was_judged_against(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=_admission(WRITER), approvals=FULLY_APPROVED)
    route.secret.side_effect = None
    route.secret.return_value = ("arn:aws:secretsmanager:us-east-1:123456789012:secret:x", "minted")
    response = _post(route, WEBHOOK)
    assert response.status_code == 500, response.text  # the sentinel at the row write
    assert route.trigger_store.create_trigger.call_args.kwargs["policy_fence"] == FENCE


def test_a_policy_changed_between_judgement_and_write_is_409_and_the_secret_is_compensated(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=_admission(WRITER), approvals=FULLY_APPROVED)
    route.secret.side_effect = None
    route.secret.return_value = ("arn:aws:secretsmanager:us-east-1:123456789012:secret:x", "minted")
    route.trigger_store.create_trigger.side_effect = TriggerPolicyConflict("policy moved")
    response = _post(route, WEBHOOK)
    assert response.status_code == 409, response.text
    assert "workspace policy changed" in response.json()["detail"]
    assert triggers.boto3.client.return_value.delete_secret.called, "the minted secret is deleted again"


def test_a_stale_claim_is_still_the_runtime_changed_409(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=_admission(READ_ONLY))
    route.trigger_store.create_trigger.side_effect = TriggerClaimConflict("slot moved")
    response = _post(route, CRON)
    assert response.status_code == 409, response.text
    assert "runtime changed" in response.json()["detail"]


def test_a_legacy_version_without_admission_cannot_get_a_webhook(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=None)
    response = _post(route, WEBHOOK)
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert [violation["code"] for violation in detail["violations"]] == [ta.VERSION_NOT_ADMITTED]
    assert detail["workspaceId"] is None and detail["tier"] is None
    _nothing_happened(route)
    route.policy_store.get_snapshot.assert_not_called()


def test_a_legacy_version_still_gets_a_schedule(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=None)
    response = _post(route, CRON)
    assert response.status_code == 500, response.text
    assert route.trigger_store.create_trigger.called


def test_a_malformed_admission_record_blocks_with_409_and_zero_side_effects(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission={"tier": "medium"})
    response = _post(route, WEBHOOK)
    assert response.status_code == 409, response.text
    assert "cannot be read" in response.json()["detail"]
    _nothing_happened(route)


@pytest.mark.parametrize(
    ("spec", "key"),
    [(WRITER, "tools"), (CODER, "customCode"), (CONFIDENTIAL, "knowledge")],
    ids=["write connector", "custom code", "confidential knowledge"],
)
def test_a_version_3_record_whose_inventory_lost_a_list_blocks_with_409_not_a_lower_tier(route, monkeypatch, spec, key):
    """Codex round-3 finding 10, end to end. The record still says medium/high and still names its
    ingress-sensitive capabilities; only the inventory list that made it so is gone. Re-admitting what is
    left would be a harmless-looking agent that a webhook makes LOW risk, which this policy auto-approves:
    the attachment would succeed without g-security. It is a 409 with nothing created and no policy read."""
    _flags(monkeypatch, {"workspacePolicy": True})
    admission = _admission(spec)
    assert admission["tier"] in ("medium", "high")
    del admission["inventory"][key]
    route.claim = _claim(admission=admission)
    response = _post(route, WEBHOOK)
    assert response.status_code == 409, response.text
    assert "cannot be read" in response.json()["detail"]
    _nothing_happened(route)
    route.policy_store.get_snapshot.assert_not_called()


@pytest.mark.parametrize("failure", ["missing", "error"])
def test_an_unreadable_policy_is_503_with_zero_side_effects(route, monkeypatch, failure):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=_admission(WRITER))
    if failure == "missing":
        route.policy = None
    else:
        route.store_error = RuntimeError("secret-bearing botocore message")
    response = _post(route, WEBHOOK)
    assert response.status_code == 503, response.text
    assert "refused rather than attached unjudged" in response.json()["detail"]
    assert "botocore" not in response.text
    _nothing_happened(route)


def test_a_non_owner_is_still_404_before_any_policy_answer(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})

    def _not_found(name, sub):
        raise HTTPException(status_code=404, detail="Not found")

    monkeypatch.setattr(triggers, "_resolve_owned_runtime_claim", _not_found)
    response = _post(route, WEBHOOK)
    assert response.status_code == 404
    route.policy_store.get_snapshot.assert_not_called()
    _nothing_happened(route)


def test_a_malformed_request_is_400_before_any_policy_answer(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=None)  # would be a policy refusal for a webhook
    response = _post(route, {"type": "cron"})  # no schedule
    assert response.status_code == 400
    route.policy_store.get_snapshot.assert_not_called()
    _nothing_happened(route)
