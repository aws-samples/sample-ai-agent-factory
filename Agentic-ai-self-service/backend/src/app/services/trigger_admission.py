"""Workspace policy for triggers attached after deployment (plan §3.B; closes the attachment gap).

Admission (``deployment_handler._reject_workspace_policy_violation``) judged the agent as it was
DEPLOYED. A trigger is ingress attached later, through ``POST /api/runtimes/{name}/triggers``, so
the version row's admission decision never saw it. Left unjudged, a webhook on an agent with a
capability that can change the world would turn a medium-risk agent into the combination the
classifier floors to ``high`` (``public_ingress_with_write``) without anyone approving it.

This module RE-ADMITS the agent. The decision recorded at deploy time carries an inventory of what was
judged, as the builder declared it (``services/admission_inventory``); the attachment rebuilds that
spec with the trigger added and runs the real admission against the workspace's CURRENT policy. So:

* Only PUBLIC INGRESS changes risk (the classifier's ``PUBLIC_INGRESS_TRIGGERS``; of the platform's
  trigger types that is ``webhook``). A schedule or an EventBridge/S3 source is the platform's own
  plumbing, so those attachments are allowed WITHOUT reading anything: for them the flag-on path is the
  flag-off path.
* A webhook on a version with NO admission (deployed while the flag was off), or with an admission that
  predates the inventory (version below ``MIN_ADMISSION_VERSION_FOR_INGRESS``), is refused: there is
  nothing to re-admit from. Redeploying under the policy records it.
* Otherwise the CURRENT policy is read (missing or unreadable: ``WorkspacePolicyUnavailableError``,
  never "no requirement") and the agent plus the trigger is admitted against it in full: the allow-lists
  (a connector the policy no longer permits refuses the webhook), the tier (public ingress with any tool
  not verified read-only or any custom code is ``high``; a ``read`` label counts only if the policy of
  TODAY trusts builders), and that tier's approver groups, every one of which must have approved this
  version. A tightened policy therefore applies to an agent admitted ``high`` under a looser one, and
  revoking trust in builder classifications revokes the exemption it granted.
* The judgement is a snapshot, so an allowed result carries the ``PolicyFence`` of the item it read and
  the route binds the trigger row's transaction to it: a policy changed between the judgement and the
  write cancels the write (``TriggerPolicyConflict``).

The admission and approval documents are validated by ``workspace_approval`` first (a version-3
decision missing its inventory or capabilities is corruption); a malformed record raises
``MalformedAdmissionError`` and the route blocks (409) instead of reading it as "no requirement".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas.agent_spec_v2 import TriggerSpec
from app.schemas.workspace_policy import RiskTier, WorkspacePolicy
from app.services.risk_classifier import PUBLIC_INGRESS_TRIGGERS
from app.services.trigger_store import TYPE_CRON, TYPE_EVENTBRIDGE, TYPE_S3, TYPE_WEBHOOK
from app.services.workspace_admission import (
    POLICY_DENIED,
    AdmissionDecision,
    PolicyViolation,
    admit,
    approvers_for,
)
from app.services.workspace_approval import approved_groups, required_approver_groups
from app.services.workspace_policy_service import PolicyFence, WorkspacePolicyUnavailableError

#: Platform trigger type -> Spec v2 ``TriggerType`` (``schemas/agent_spec_v2``), so the classifier's
#: vocabulary is the one judged here. A type missing here maps to itself.
PLATFORM_TRIGGER_TO_SPEC: dict[str, str] = {
    TYPE_CRON: "schedule",
    TYPE_EVENTBRIDGE: "event",
    TYPE_S3: "file",
    TYPE_WEBHOOK: "webhook",
}

#: Violation codes (``PolicyViolation.code``) this module produces; tests assert the exact values. The
#: allow-list codes of a refused re-admission are ``workspace_admission``'s own.
VERSION_NOT_ADMITTED = "version_not_admitted"
ADMISSION_PREDATES_INGRESS_RULE = "admission_predates_ingress_rule"
TRIGGER_RAISES_RISK_TIER = "trigger_raises_risk_tier"
TRIGGER_REQUIRES_APPROVAL = "trigger_requires_approval"

#: JSON pointer of the offending value: the one trigger the request attaches.
TRIGGER_PATH = "/triggers/0/type"

#: The first admission document version that records the inventory a re-admission needs.
MIN_ADMISSION_VERSION_FOR_INGRESS = 3

_TIER_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

#: Reads the CURRENT policy of a workspace by id together with the fence on the item it came from;
#: ``None`` when none is stored. A store failure must surface as ``WorkspacePolicyUnavailableError``
#: (the route's lookup wraps it).
PolicyLookup = Callable[[str], tuple[WorkspacePolicy, PolicyFence] | None]


class TriggerAdmission(BaseModel):
    """The decision for one attachment. Serialises camelCase like ``AdmissionDecision``."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    outcome: Literal["allowed", "denied"]
    trigger_type: str
    #: The workspace whose policy governs the version; ``None`` when nothing was read.
    workspace_id: str | None = None
    admitted_tier: RiskTier | None = None
    #: The agent's tier with the trigger attached, under the current policy.
    resulting_tier: RiskTier | None = None
    #: The capabilities that make public ingress high risk for this agent, under the current policy.
    ingress_sensitive_capabilities: list[str] = Field(default_factory=list)
    violations: list[PolicyViolation] = Field(default_factory=list)
    #: The policy item the judgement read, for the route to condition the trigger row's write on;
    #: ``None`` when no policy was read (non-ingress trigger).
    policy_fence: PolicyFence | None = None


def spec_trigger_type(platform_type: str) -> str:
    """The Spec v2 trigger type of a platform trigger type."""
    return PLATFORM_TRIGGER_TO_SPEC.get(platform_type, platform_type)


def is_public_ingress(platform_type: str) -> bool:
    """Whether attaching this trigger type exposes the agent to the public internet."""
    return spec_trigger_type(platform_type) in PUBLIC_INGRESS_TRIGGERS


def _allowed(
    trigger_type: str,
    decision: AdmissionDecision | None = None,
    *,
    resulting: RiskTier | None = None,
    sensitive: list[str] | None = None,
    fence: PolicyFence | None = None,
) -> TriggerAdmission:
    admitted = decision.tier if decision is not None else None
    return TriggerAdmission(
        outcome="allowed",
        trigger_type=trigger_type,
        workspace_id=decision.workspace_id if decision is not None else None,
        admitted_tier=admitted,
        resulting_tier=resulting if resulting is not None else admitted,
        ingress_sensitive_capabilities=list(sensitive or []),
        policy_fence=fence,
    )


def _denied(
    trigger_type: str,
    decision: AdmissionDecision | None,
    *,
    violations: list[PolicyViolation],
    resulting: RiskTier | None,
    sensitive: list[str] | None = None,
) -> TriggerAdmission:
    return TriggerAdmission(
        outcome="denied",
        trigger_type=trigger_type,
        workspace_id=decision.workspace_id if decision is not None else None,
        admitted_tier=decision.tier if decision is not None else None,
        resulting_tier=resulting,
        ingress_sensitive_capabilities=list(sensitive or []),
        violations=violations,
    )


def _violation(code: str, message: str) -> PolicyViolation:
    return PolicyViolation(code=code, path=TRIGGER_PATH, message=message)


def judge_trigger_attachment(
    trigger_type: str,
    admission: dict[str, Any] | None,
    approvals: list[dict[str, Any]] | None,
    *,
    owner_sub: str | None,
    policy_lookup: PolicyLookup,
) -> TriggerAdmission:
    """Decide whether ``trigger_type`` may be attached to a version carrying ``admission``.

    Raises ``MalformedAdmissionError`` for a present-but-unreadable admission or approval record and
    ``WorkspacePolicyUnavailableError`` when the governing policy cannot be read; the route maps them
    to 409 and 503. Nothing here has side effects.
    """
    if not is_public_ingress(trigger_type):
        return _allowed(trigger_type)

    ingress = spec_trigger_type(trigger_type)
    if admission is None:
        return _denied(
            trigger_type,
            None,
            violations=[
                _violation(
                    VERSION_NOT_ADMITTED,
                    f"a {ingress} trigger is public ingress, and this version was deployed without workspace "
                    "admission, so its tools were never classified; redeploy the agent under the workspace "
                    "policy, then attach the trigger",
                )
            ],
            resulting=None,
        )

    # Validates the whole document (outcome, versions, required fields, approver names) before use.
    required_approver_groups(admission)
    decision = AdmissionDecision.model_validate(admission)

    if decision.admission_version < MIN_ADMISSION_VERSION_FOR_INGRESS or decision.inventory is None:
        return _denied(
            trigger_type,
            decision,
            violations=[
                _violation(
                    ADMISSION_PREDATES_INGRESS_RULE,
                    f"a {ingress} trigger is public ingress, and this version's admission (version "
                    f"{decision.admission_version}) does not record what was judged, so the agent cannot be "
                    "re-admitted with the trigger; redeploy the agent under the workspace policy, then attach it",
                )
            ],
            resulting=None,
        )

    # Every public-ingress attachment reads the CURRENT policy: what the agent was admitted under is history.
    snapshot = policy_lookup(decision.workspace_id)
    if snapshot is None:
        raise WorkspacePolicyUnavailableError(f"no policy is stored for workspace {decision.workspace_id!r}")
    policy, fence = snapshot

    # Re-admit the recorded agent WITH the trigger, under today's policy: allow-lists, tier and routing.
    spec = decision.inventory.to_spec(extra_triggers=[TriggerSpec(id=f"trigger-{ingress}", type=ingress)])
    readmission = admit(spec, policy)
    sensitive = list(readmission.ingress_sensitive_capabilities)
    if readmission.outcome == "denied":
        return _denied(
            trigger_type,
            decision,
            violations=list(readmission.violations),
            resulting=readmission.tier,
            sensitive=sensitive,
        )

    required = approvers_for(readmission.tier, policy)
    done = approved_groups(approvals, owner_sub=owner_sub)
    pending = [group for group in required if group not in done]
    if not pending:
        return _allowed(trigger_type, decision, resulting=readmission.tier, sensitive=sensitive, fence=fence)

    raises = _TIER_RANK[readmission.tier] > _TIER_RANK[decision.tier]
    if raises:
        message = (
            f"a {ingress} trigger is public ingress; combined with {sensitive} (tools not verified read-only, or "
            f"executable custom code) it makes the agent {readmission.tier} risk, but the version was admitted "
            f"{decision.tier} and this workspace's current policy needs approval from {', '.join(pending)} for "
            f"{readmission.tier} risk; the agent needs their approval before it can be reached from the internet"
        )
    else:
        message = (
            f"with a {ingress} trigger the agent stays {readmission.tier} risk, but this workspace's current "
            f"policy needs approval from {', '.join(pending)} for {readmission.tier} risk and this version does "
            "not have it; obtain it before the agent can be reached from the internet"
        )
    return _denied(
        trigger_type,
        decision,
        violations=[_violation(TRIGGER_RAISES_RISK_TIER if raises else TRIGGER_REQUIRES_APPROVAL, message)],
        resulting=readmission.tier,
        sensitive=sensitive,
    )


def trigger_denial_detail(result: TriggerAdmission) -> dict[str, object]:
    """The exact ``detail`` body of the route's ``403 policy_denied``: the deploy refusal's envelope."""
    if not result.violations:
        raise ValueError("trigger_denial_detail needs a denied result")
    own_codes = {
        VERSION_NOT_ADMITTED,
        ADMISSION_PREDATES_INGRESS_RULE,
        TRIGGER_RAISES_RISK_TIER,
        TRIGGER_REQUIRES_APPROVAL,
    }
    if len(result.violations) == 1 and result.violations[0].code in own_codes:
        message = f"This trigger cannot be attached: {result.violations[0].message}. Nothing was created."
    else:
        message = (
            f"This trigger cannot be attached: with it the agent uses {len(result.violations)} thing(s) workspace "
            f"{result.workspace_id!r} no longer allows under its current policy. Nothing was created."
        )
    return {
        "code": POLICY_DENIED,
        "workspaceId": result.workspace_id,
        "tier": result.admitted_tier,
        "violations": [violation.model_dump(by_alias=True) for violation in result.violations],
        "message": message,
    }
