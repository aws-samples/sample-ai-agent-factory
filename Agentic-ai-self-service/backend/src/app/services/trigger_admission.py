"""Workspace policy for triggers attached after deployment (plan §3.B; closes the attachment gap).

Admission (``deployment_handler._reject_workspace_policy_violation``) judged the agent as it was
DEPLOYED. A trigger is ingress attached later, through ``POST /api/runtimes/{name}/triggers``, so
the version row's admission decision never saw it. Left unjudged, a webhook on an agent with a tool
that can change the world would turn a medium-risk agent into the combination the classifier floors
to ``high`` (``public_ingress_with_write``) without anyone approving it. This module decides, from the
version row's recorded admission and the workspace's CURRENT policy, whether an attachment may
proceed, and hands the route the exact policy item it judged so the row write can be conditioned on
it. It is pure: the route supplies the two persisted documents and a policy lookup.

What is judged:

* Only PUBLIC INGRESS changes risk (the classifier's ``PUBLIC_INGRESS_TRIGGERS``; of the platform's
  trigger types that is ``webhook``). A schedule or an EventBridge/S3 source is the platform's own
  plumbing and never changes the tier, so those attachments are allowed WITHOUT reading anything: for
  them the flag-on path is the flag-off path.
* A webhook on a version that carries NO admission (deployed while the flag was off) is refused: its
  tools were never classified, so the combination cannot be judged. So is a version whose admission
  predates the ingress rule (``admissionVersion`` below ``MIN_ADMISSION_VERSION_FOR_INGRESS``): it does
  not record which tools public ingress may not reach. Redeploying under the policy classifies it.
* A webhook on an admitted agent none of whose tools is ingress-sensitive keeps its tier: allowed. A
  tool is ingress-sensitive when it is NOT verified read-only: ``write``, ``pay`` or UNCLASSIFIED (a
  Lambda nobody classified may mutate anything; exposing it to the internet is not a read-only act).
  The list is recorded on the decision at deploy time by the same classifier helper the ingress rule
  uses (``risk_classifier.ingress_sensitive_tools``), so the two can never disagree.
* A webhook on an admitted agent WITH an ingress-sensitive tool is the ``high`` combination, whatever
  tier it was admitted at, ``high`` itself included. The CURRENT policy of the admitted workspace is
  read (missing or unreadable: ``WorkspacePolicyUnavailableError``, never "no requirement"), and the
  attachment is allowed only when every group ``approval.high`` names has already approved this version
  (an empty list auto-approves); else it is the exact ``403 policy_denied`` naming the pending groups.
  An admin who tightens ``approval.high`` after the deploy therefore changes what the webhook needs,
  and a deleted policy blocks it.

The judgement is a snapshot, so an allowed result carries the ``PolicyFence`` of the item it read and
the route binds the trigger row's transaction to it: a policy changed between the judgement and the
write cancels the write (``TriggerPolicyConflict``). The admission and approval documents are validated
by ``workspace_approval`` first; a present-but-malformed record raises ``MalformedAdmissionError`` and
the route blocks (409) instead of reading it as "no requirement".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas.workspace_policy import RiskTier, WorkspacePolicy
from app.services.risk_classifier import PUBLIC_INGRESS_TRIGGERS
from app.services.trigger_store import TYPE_CRON, TYPE_EVENTBRIDGE, TYPE_S3, TYPE_WEBHOOK
from app.services.workspace_admission import POLICY_DENIED, AdmissionDecision, PolicyViolation, approvers_for
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

#: Violation codes (``PolicyViolation.code``) this module produces; tests assert the exact values.
VERSION_NOT_ADMITTED = "version_not_admitted"
ADMISSION_PREDATES_INGRESS_RULE = "admission_predates_ingress_rule"
TRIGGER_RAISES_RISK_TIER = "trigger_raises_risk_tier"

#: JSON pointer of the offending value: the one trigger the request attaches.
TRIGGER_PATH = "/triggers/0/type"

#: The first admission document version that records ``ingressSensitiveTools``. An older decision
#: cannot say which tools public ingress may reach, so it is refused rather than guessed at.
MIN_ADMISSION_VERSION_FOR_INGRESS = 2

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
    #: The agent's tier once the trigger is attached: ``admitted_tier`` unless public ingress meets an
    #: ingress-sensitive tool, which makes it ``high``.
    resulting_tier: RiskTier | None = None
    #: The recorded tools that make public ingress high risk for this agent.
    ingress_sensitive_tools: list[str] = Field(default_factory=list)
    violation: PolicyViolation | None = None
    #: The policy item the judgement read, for the route to condition the trigger row's write on;
    #: ``None`` when no policy was read (non-ingress trigger, or no ingress-sensitive tool).
    policy_fence: PolicyFence | None = None


def spec_trigger_type(platform_type: str) -> str:
    """The Spec v2 trigger type of a platform trigger type."""
    return PLATFORM_TRIGGER_TO_SPEC.get(platform_type, platform_type)


def is_public_ingress(platform_type: str) -> bool:
    """Whether attaching this trigger type exposes the agent to the public internet."""
    return spec_trigger_type(platform_type) in PUBLIC_INGRESS_TRIGGERS


def ingress_sensitive(decision: AdmissionDecision) -> list[str]:
    """The recorded tools public ingress may not reach without the high-tier gate."""
    return list(decision.ingress_sensitive_tools)


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
        ingress_sensitive_tools=list(sensitive or []),
        policy_fence=fence,
    )


def _denied(
    trigger_type: str,
    decision: AdmissionDecision | None,
    *,
    code: str,
    message: str,
    resulting: RiskTier | None,
    sensitive: list[str] | None = None,
) -> TriggerAdmission:
    return TriggerAdmission(
        outcome="denied",
        trigger_type=trigger_type,
        workspace_id=decision.workspace_id if decision is not None else None,
        admitted_tier=decision.tier if decision is not None else None,
        resulting_tier=resulting,
        ingress_sensitive_tools=list(sensitive or []),
        violation=PolicyViolation(code=code, path=TRIGGER_PATH, message=message),
    )


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
            code=VERSION_NOT_ADMITTED,
            message=(
                f"a {ingress} trigger is public ingress, and this version was deployed without workspace "
                "admission, so its tools were never classified; redeploy the agent under the workspace "
                "policy, then attach the trigger"
            ),
            resulting=None,
        )

    # Validates the whole document (outcome, versions, approver names) before anything is read from it.
    required_approver_groups(admission)
    decision = AdmissionDecision.model_validate(admission)

    if decision.admission_version < MIN_ADMISSION_VERSION_FOR_INGRESS:
        return _denied(
            trigger_type,
            decision,
            code=ADMISSION_PREDATES_INGRESS_RULE,
            message=(
                f"a {ingress} trigger is public ingress, and this version's admission (version "
                f"{decision.admission_version}) predates the rule that records which tools public ingress may "
                "not reach, so the combination cannot be judged; redeploy the agent under the workspace policy, "
                "then attach the trigger"
            ),
            resulting=None,
        )

    sensitive = ingress_sensitive(decision)
    if not sensitive:
        return _allowed(trigger_type, decision)

    snapshot = policy_lookup(decision.workspace_id)
    if snapshot is None:
        raise WorkspacePolicyUnavailableError(f"no policy is stored for workspace {decision.workspace_id!r}")
    policy, fence = snapshot
    required = approvers_for("high", policy)
    done = approved_groups(approvals, owner_sub=owner_sub)
    pending = [group for group in required if group not in done]
    if not pending:
        return _allowed(trigger_type, decision, resulting="high", sensitive=sensitive, fence=fence)
    return _denied(
        trigger_type,
        decision,
        code=TRIGGER_RAISES_RISK_TIER,
        message=(
            f"a {ingress} trigger is public ingress; combined with tool(s) {sensitive} that are not verified "
            f"read-only it makes the agent high risk, but the version was admitted {decision.tier} and this "
            f"workspace's current policy still needs approval from {', '.join(pending)}; a high-risk agent needs "
            "their approval before it can be reached from the internet"
        ),
        resulting="high",
        sensitive=sensitive,
    )


def trigger_denial_detail(result: TriggerAdmission) -> dict[str, object]:
    """The exact ``detail`` body of the route's ``403 policy_denied``: the deploy refusal's envelope."""
    if result.violation is None:
        raise ValueError("trigger_denial_detail needs a denied result")
    return {
        "code": POLICY_DENIED,
        "workspaceId": result.workspace_id,
        "tier": result.admitted_tier,
        "violations": [result.violation.model_dump(by_alias=True)],
        "message": f"This trigger cannot be attached: {result.violation.message}. Nothing was created.",
    }
