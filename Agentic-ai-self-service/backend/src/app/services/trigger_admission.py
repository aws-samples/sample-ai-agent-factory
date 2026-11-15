"""Workspace policy for triggers attached after deployment (plan §3.B; closes the attachment gap).

Admission (``deployment_handler._reject_workspace_policy_violation``) judged the agent as it was
DEPLOYED. A trigger is ingress attached later, through ``POST /api/runtimes/{name}/triggers``, so
the version row's admission decision never saw it. Left unjudged, a webhook on an agent with a write
tool would turn a medium-risk agent into the combination the classifier floors to ``high``
(``public_ingress_with_write``) without anyone approving it. This module decides, from the version
row's recorded admission and the workspace's CURRENT policy, whether an attachment may proceed. It
is pure: the route supplies the two persisted documents and a policy lookup.

What is judged:

* Only PUBLIC INGRESS changes risk (the classifier's ``PUBLIC_INGRESS_TRIGGERS``; of the platform's
  trigger types that is ``webhook``). A schedule or an EventBridge/S3 source is the platform's own
  plumbing and never changes the tier, so those attachments are allowed WITHOUT reading anything: for
  them the flag-on path is the flag-off path.
* A webhook on a version that carries NO admission (deployed while the flag was off, or before it
  existed) is refused: the agent's tools were never classified, so the combination cannot be judged.
  Redeploying under the policy classifies it.
* A webhook on an admitted agent WITHOUT a write or pay tool keeps its tier: allowed.
* A webhook on an admitted agent WITH a write or pay tool is the ``high`` combination. A version
  admitted ``high`` already changes nothing: allowed. Otherwise the attachment RAISES the tier, which
  is allowed only when every approver group the workspace names for ``high`` has already approved
  this version (an empty list auto-approves); else it is the exact ``403 policy_denied`` naming the
  pending groups. Nothing is created either way.

The write-or-pay predicate is read from the recorded decision's reasons: ``write_tool`` / ``pay_tool``
fire exactly when a tool's effective class is write / pay, which is the predicate
``_rule_public_ingress_with_write`` evaluates; a test pins the two against each other on real specs.
The admission document is validated by ``workspace_approval.required_approver_groups`` first, so a
present-but-malformed record raises ``MalformedAdmissionError`` and the route blocks (409) instead of
reading it as "no requirement". A workspace whose policy cannot be read is
``WorkspacePolicyUnavailableError`` (503 at the route): never attach unjudged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.workspace_policy import RiskTier, WorkspacePolicy
from app.services.risk_classifier import PAY_TOOL_CODE, PUBLIC_INGRESS_TRIGGERS, WRITE_TOOL_CODE
from app.services.trigger_store import TYPE_CRON, TYPE_EVENTBRIDGE, TYPE_S3, TYPE_WEBHOOK
from app.services.workspace_admission import POLICY_DENIED, AdmissionDecision, PolicyViolation, approvers_for
from app.services.workspace_approval import approved_groups, required_approver_groups
from app.services.workspace_policy_service import WorkspacePolicyUnavailableError

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
TRIGGER_RAISES_RISK_TIER = "trigger_raises_risk_tier"

#: JSON pointer of the offending value: the one trigger the request attaches.
TRIGGER_PATH = "/triggers/0/type"

#: Reads the CURRENT policy of a workspace by id; ``None`` when none is stored. A store failure must
#: surface as ``WorkspacePolicyUnavailableError`` (the route's lookup wraps it).
PolicyLookup = Callable[[str], WorkspacePolicy | None]


class TriggerAdmission(BaseModel):
    """The decision for one attachment. Serialises camelCase like ``AdmissionDecision``."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    outcome: Literal["allowed", "denied"]
    trigger_type: str
    #: The workspace whose policy governs the version; ``None`` when nothing was read.
    workspace_id: str | None = None
    admitted_tier: RiskTier | None = None
    #: The agent's tier once the trigger is attached: ``admitted_tier`` unless public ingress meets a
    #: write tool, which makes it ``high``.
    resulting_tier: RiskTier | None = None
    violation: PolicyViolation | None = None


def spec_trigger_type(platform_type: str) -> str:
    """The Spec v2 trigger type of a platform trigger type."""
    return PLATFORM_TRIGGER_TO_SPEC.get(platform_type, platform_type)


def is_public_ingress(platform_type: str) -> bool:
    """Whether attaching this trigger type exposes the agent to the public internet."""
    return spec_trigger_type(platform_type) in PUBLIC_INGRESS_TRIGGERS


def admitted_has_write_or_pay(decision: AdmissionDecision) -> bool:
    """Whether the admitted agent carries a write or pay tool, read from the decision's reasons."""
    codes = {reason.code for reason in decision.reasons}
    return bool(codes & {WRITE_TOOL_CODE, PAY_TOOL_CODE})


def _allowed(
    trigger_type: str,
    decision: AdmissionDecision | None = None,
    *,
    resulting: RiskTier | None = None,
) -> TriggerAdmission:
    admitted = decision.tier if decision is not None else None
    return TriggerAdmission(
        outcome="allowed",
        trigger_type=trigger_type,
        workspace_id=decision.workspace_id if decision is not None else None,
        admitted_tier=admitted,
        resulting_tier=resulting if resulting is not None else admitted,
    )


def _denied(
    trigger_type: str,
    decision: AdmissionDecision | None,
    *,
    code: str,
    message: str,
    resulting: RiskTier | None,
) -> TriggerAdmission:
    return TriggerAdmission(
        outcome="denied",
        trigger_type=trigger_type,
        workspace_id=decision.workspace_id if decision is not None else None,
        admitted_tier=decision.tier if decision is not None else None,
        resulting_tier=resulting,
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

    if not admitted_has_write_or_pay(decision):
        return _allowed(trigger_type, decision)
    if decision.tier == "high":
        return _allowed(trigger_type, decision, resulting="high")

    policy = policy_lookup(decision.workspace_id)
    if policy is None:
        raise WorkspacePolicyUnavailableError(f"no policy is stored for workspace {decision.workspace_id!r}")
    required = approvers_for("high", policy)
    done = approved_groups(approvals, owner_sub=owner_sub)
    pending = [group for group in required if group not in done]
    if not pending:
        return _allowed(trigger_type, decision, resulting="high")
    return _denied(
        trigger_type,
        decision,
        code=TRIGGER_RAISES_RISK_TIER,
        message=(
            f"a {ingress} trigger is public ingress; combined with this agent's write tools it makes the agent "
            f"high risk, but the version was admitted {decision.tier} and approval from {', '.join(pending)} is "
            "missing; in this workspace a high-risk agent needs their approval before it can be reached from "
            "the internet"
        ),
        resulting="high",
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
