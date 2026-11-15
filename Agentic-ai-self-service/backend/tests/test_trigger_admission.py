"""Triggers attached after deployment are judged by the workspace policy (plan §3.B, attachment gap).

Three layers, each at its own boundary:

* ``trigger_admission.judge_trigger_attachment`` -- pure and table-driven: a non-ingress trigger is
  allowed without a read; a webhook on an un-admitted version is refused; a webhook keeps the tier
  of a read-only agent; a webhook on a write agent is refused until every ``high`` approver group has
  approved, allowed once they have (or when ``high`` auto-approves), and unchanged for an agent that
  is ``high`` already; malformed records raise; a missing policy is unavailable, never the default.
* The write-or-pay proxy read from a stored decision is pinned against the classifier ITSELF on real
  specs (every gallery template plus hand-built ones): it must equal the predicate
  ``_rule_public_ingress_with_write`` evaluates, and adding a webhook to the spec must fire that rule
  exactly when the proxy says so.
* ``POST /api/runtimes/{name}/triggers`` through the real router, with ownership resolved to an owned
  HTTP runtime and every side effect replaced by a sentinel, so each test can say exactly how far a
  request got: flag off is the old route; the business persona's webhook on a write agent is the
  exact ``403 policy_denied`` with ZERO side effects; a schedule is not judged; an approved agent
  proceeds; a legacy version cannot get a webhook; a malformed record is 409; an unreadable policy is
  503; a non-owner is still 404 before any policy answer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from app.routers import triggers
from app.schemas.agent_spec_v2 import AgentSpecV2, TriggerSpec
from app.schemas.workspace_policy import WorkspacePolicy
from app.services import trigger_admission as ta
from app.services import workspace_policy_service as wps
from app.services.agent_versions_store import AgentVersion, RuntimeSlots
from app.services.auth import get_caller_sub
from app.services.risk_classifier import PUBLIC_INGRESS_WITH_WRITE_CODE, classify, effective_risk_class
from app.services.spec_migration import migrate_v1_flow
from app.services.trigger_store import TYPE_CRON, TYPE_EVENTBRIDGE, TYPE_S3, TYPE_WEBHOOK, RuntimeClaim
from app.services.workspace_admission import POLICY_DENIED, admit
from app.services.workspace_approval import MalformedAdmissionError, approval_record
from app.services.workspace_policy_service import WorkspacePolicyUnavailableError
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests.test_workspace_admission import FIXTURES, _policy, _spec, _tool

OWNER = "sub-alice"
STRANGER = "sub-mallory"
APPROVER = "sub-approver"
SECURITY_REVIEWER = "sub-security"
RUNTIME = "orders_bot"
RUNTIME_ID = "orders_bot_1a2b3c4d-AbCdEfGhIj"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/{RUNTIME_ID}"
NOW = datetime.now(UTC).isoformat()

#: ``high`` needs security AND the approvers; ``medium`` needs the approvers alone.
HIGH_POLICY = _policy(approval={"medium": ["g-approvers"], "high": ["g-security", "g-approvers"]})

READ_ONLY = _spec(tools=[_tool("lookup", "read")])
WRITER = _spec(tools=[_tool("jira", "write", kind="connector", ref="jira")])
PAYER = _spec(tools=[_tool("stripe", "pay")])


def _admission(spec: AgentSpecV2, policy: WorkspacePolicy = HIGH_POLICY) -> dict[str, Any]:
    decision = admit(spec, policy)
    assert decision.outcome == "admitted"
    return decision.model_dump(by_alias=True)


def _judge(trigger_type: str, admission, approvals=None, lookup=None) -> ta.TriggerAdmission:
    return ta.judge_trigger_attachment(
        trigger_type,
        admission,
        approvals,
        owner_sub=OWNER,
        policy_lookup=lookup if lookup is not None else MagicMock(return_value=HIGH_POLICY),
    )


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
    lookup = MagicMock()
    result = _judge(trigger_type, admission, lookup=lookup)
    assert result.outcome == "allowed"
    assert result.workspace_id is None and result.admitted_tier is None and result.resulting_tier is None
    lookup.assert_not_called()


def test_a_webhook_on_a_version_without_admission_is_refused():
    lookup = MagicMock()
    result = _judge(TYPE_WEBHOOK, None, lookup=lookup)
    assert result.outcome == "denied"
    assert result.violation is not None
    assert result.violation.code == ta.VERSION_NOT_ADMITTED
    assert result.violation.path == "/triggers/0/type"
    assert "never classified" in result.violation.message
    lookup.assert_not_called()


def test_a_webhook_on_a_read_only_agent_keeps_its_tier():
    lookup = MagicMock()
    result = _judge(TYPE_WEBHOOK, _admission(READ_ONLY), lookup=lookup)
    assert result.outcome == "allowed"
    assert (result.workspace_id, result.admitted_tier, result.resulting_tier) == ("finance", "low", "low")
    lookup.assert_not_called()


def test_a_webhook_on_an_agent_that_is_already_high_changes_nothing():
    lookup = MagicMock()
    result = _judge(TYPE_WEBHOOK, _admission(PAYER), lookup=lookup)
    assert result.outcome == "allowed"
    assert (result.admitted_tier, result.resulting_tier) == ("high", "high")
    lookup.assert_not_called()


def test_a_webhook_on_a_write_agent_is_refused_until_the_high_tier_approvers_have_approved():
    admission = _admission(WRITER)
    assert admission["tier"] == "medium"
    lookup = MagicMock(return_value=HIGH_POLICY)
    result = _judge(TYPE_WEBHOOK, admission, [approval_record("g-approvers", APPROVER, NOW)], lookup)
    assert result.outcome == "denied"
    assert result.violation is not None
    assert result.violation.code == ta.TRIGGER_RAISES_RISK_TIER
    assert (result.workspace_id, result.admitted_tier, result.resulting_tier) == ("finance", "medium", "high")
    assert "g-security" in result.violation.message
    assert "g-approvers" not in result.violation.message, "a group that already approved is not pending"
    lookup.assert_called_once_with("finance")


def test_once_every_high_tier_group_has_approved_the_webhook_is_allowed():
    approvals = [
        approval_record("g-approvers", APPROVER, NOW),
        approval_record("g-security", SECURITY_REVIEWER, NOW),
    ]
    result = _judge(TYPE_WEBHOOK, _admission(WRITER), approvals)
    assert result.outcome == "allowed"
    assert (result.admitted_tier, result.resulting_tier) == ("medium", "high")


def test_a_workspace_that_auto_approves_high_allows_the_webhook():
    auto_high = _policy(approval={"medium": ["g-approvers"]})
    result = _judge(TYPE_WEBHOOK, _admission(WRITER, auto_high), None, MagicMock(return_value=auto_high))
    assert result.outcome == "allowed"
    assert result.resulting_tier == "high"


def test_the_judgement_reads_the_current_policy_not_the_one_admission_saw():
    """An admin who tightens ``approval.high`` after the deploy changes what the webhook needs."""
    admitted_under = _policy(approval={"medium": ["g-approvers"]})  # high auto-approved at deploy time
    tightened = _policy(approval={"medium": ["g-approvers"], "high": ["g-security"]})
    result = _judge(TYPE_WEBHOOK, _admission(WRITER, admitted_under), None, MagicMock(return_value=tightened))
    assert result.outcome == "denied"
    assert result.violation is not None and "g-security" in result.violation.message


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
        lambda doc: doc.clear(),
    ],
)
def test_a_malformed_admission_blocks_instead_of_being_read_as_no_requirement(corrupt):
    admission = _admission(WRITER)
    corrupt(admission)
    with pytest.raises(MalformedAdmissionError):
        _judge(TYPE_WEBHOOK, admission)


def test_a_missing_policy_for_the_admitted_workspace_is_unavailable_not_the_default():
    with pytest.raises(WorkspacePolicyUnavailableError):
        _judge(TYPE_WEBHOOK, _admission(WRITER), None, lambda workspace_id: None)


def test_the_denial_body_is_the_deploy_refusals_envelope():
    result = _judge(TYPE_WEBHOOK, _admission(WRITER))
    assert result.violation is not None
    assert ta.trigger_denial_detail(result) == {
        "code": POLICY_DENIED,
        "workspaceId": "finance",
        "tier": "medium",
        "violations": [
            {"code": ta.TRIGGER_RAISES_RISK_TIER, "path": "/triggers/0/type", "message": result.violation.message}
        ],
        "message": f"This trigger cannot be attached: {result.violation.message}. Nothing was created.",
    }
    with pytest.raises(ValueError):
        ta.trigger_denial_detail(_judge(TYPE_CRON, None))


def test_the_decision_serializes_camel_case_for_logs_and_evidence():
    result = _judge(TYPE_WEBHOOK, _admission(WRITER))
    document = result.model_dump(by_alias=True)
    assert set(document) == {"outcome", "triggerType", "workspaceId", "admittedTier", "resultingTier", "violation"}


# --------------------------------------------------------------------------- proxy vs classifier


def _real_specs() -> list[AgentSpecV2]:
    """Every gallery template as the canvas persists it, plus hand-built specs covering each class."""
    specs = [migrate_v1_flow(json.loads(path.read_text())) for path in sorted(FIXTURES.glob("*.flow.json"))]
    assert len(specs) >= 6, "the gallery fixtures are missing"
    specs += [
        READ_ONLY,
        WRITER,
        PAYER,
        _spec(tools=[_tool("jira", "read", kind="connector", ref="jira")]),  # catalog floors it to write
        _spec(tools=[_tool("mystery")]),  # unclassified Lambda: never a write for the ingress rule
        _spec(tools=[_tool("lookup", "read"), _tool("sender", "write")]),
    ]
    return specs


@pytest.mark.parametrize("trusting", [True, False])
def test_the_write_or_pay_proxy_agrees_with_the_classifier_on_real_specs(trusting):
    policy = _policy(defaults={"trustBuilderClassifications": trusting})
    seen = set()
    for spec in _real_specs():
        decision = admit(spec, policy)
        proxy = ta.admitted_has_write_or_pay(decision)
        predicate = any(effective_risk_class(tool, policy) in ("write", "pay") for tool in spec.tools)
        assert proxy is predicate, spec.id
        with_webhook = spec.model_copy(update={"triggers": [TriggerSpec(id="hook", type="webhook")]})
        fires = any(reason.code == PUBLIC_INGRESS_WITH_WRITE_CODE for reason in classify(with_webhook, policy).reasons)
        assert fires is proxy, spec.id
        seen.add(proxy)
    assert seen == {True, False}, "the sample must exercise both outcomes"


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

    def _get(org_id: str, workspace_id: str):
        if state.store_error is not None:
            raise state.store_error
        return state.policy

    state.policy_store = MagicMock()
    state.policy_store.get.side_effect = _get
    monkeypatch.setattr(wps, "get_workspace_policy_store", lambda: state.policy_store)

    app = FastAPI()
    app.include_router(triggers.router)
    app.dependency_overrides[get_caller_sub] = lambda: OWNER
    state.client = TestClient(app, raise_server_exceptions=False)
    return state


WEBHOOK = {"type": "webhook"}
CRON = {"type": "cron", "schedule": "cron(0 12 * * ? *)"}


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
    route.policy_store.get.assert_not_called()


def test_a_business_persona_attaching_a_webhook_to_a_write_agent_is_the_exact_403_with_zero_side_effects(
    route, monkeypatch
):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=_admission(WRITER), approvals=[approval_record("g-approvers", APPROVER, NOW)])
    response = _post(route, WEBHOOK)
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert (detail["code"], detail["workspaceId"], detail["tier"]) == (POLICY_DENIED, "finance", "medium")
    assert [violation["code"] for violation in detail["violations"]] == [ta.TRIGGER_RAISES_RISK_TIER]
    assert detail["violations"][0]["path"] == "/triggers/0/type"
    assert "g-security" in detail["violations"][0]["message"]
    assert detail["message"].endswith("Nothing was created.")
    _nothing_happened(route)
    route.policy_store.get.assert_called_once_with(wps.DEFAULT_ORG_ID, "finance")


def test_a_schedule_trigger_is_not_judged_and_proceeds(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=_admission(WRITER))
    response = _post(route, CRON)
    assert response.status_code == 500, response.text
    assert response.json()["detail"] == "Could not register trigger"  # the sentinel at the row write
    assert route.trigger_store.create_trigger.called
    route.policy_store.get.assert_not_called()


def test_a_webhook_on_a_read_only_agent_proceeds_without_reading_the_policy(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=_admission(READ_ONLY))
    response = _post(route, WEBHOOK)
    assert response.status_code == 500, response.text
    assert route.secret.called
    route.policy_store.get.assert_not_called()


def test_an_approved_write_agent_may_be_reached_from_the_internet(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    approvals = [approval_record("g-approvers", APPROVER, NOW), approval_record("g-security", SECURITY_REVIEWER, NOW)]
    route.claim = _claim(admission=_admission(WRITER), approvals=approvals)
    response = _post(route, WEBHOOK)
    assert response.status_code == 500, response.text
    assert route.secret.called


def test_a_legacy_version_without_admission_cannot_get_a_webhook(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=None)
    response = _post(route, WEBHOOK)
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert [violation["code"] for violation in detail["violations"]] == [ta.VERSION_NOT_ADMITTED]
    assert detail["workspaceId"] is None and detail["tier"] is None
    _nothing_happened(route)
    route.policy_store.get.assert_not_called()


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
    route.policy_store.get.assert_not_called()
    _nothing_happened(route)


def test_a_malformed_request_is_400_before_any_policy_answer(route, monkeypatch):
    _flags(monkeypatch, {"workspacePolicy": True})
    route.claim = _claim(admission=None)  # would be a policy refusal for a webhook
    response = _post(route, {"type": "cron"})  # no schedule
    assert response.status_code == 400
    route.policy_store.get.assert_not_called()
    _nothing_happened(route)
