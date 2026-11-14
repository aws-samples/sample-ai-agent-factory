"""Workspace admission decision (plan §3.B, implementation step 3). Pure.

``admit(spec, policy)`` answers, for one agent spec under one workspace policy:

* **denied** -- the spec uses something the workspace does not permit. The decision carries one
  ``PolicyViolation`` per offending item with a stable reason ``code``; the HTTP layer renders it
  as the exact ``403 policy_denied``.
* **admitted** -- every allow-list axis passes. The decision carries the risk tier the classifier
  assigned, the approver groups that tier routes to (``[]`` = auto-approve), and the reasons, so a
  caller can route the deployment (low: proceed; medium/high: the named groups must approve).

Rules, one small function each (so a mutation that disables one rule breaks exactly one test):

* ``providers``       -- ``spec.model.provider`` must be listed when the list is non-empty.
* ``models``          -- ``spec.model.modelId`` must be listed when the list is non-empty.
* ``connectors``      -- every tool of type ``connector`` must have its ``ref`` listed.
* ``toolRiskClasses`` -- every tool's ``riskClass`` must be listed; ``unclassified`` never is.
* ``dataClasses``     -- every knowledge item's ``dataClass`` must be listed; ``unclassified`` never is.
* ``channels``        -- every channel's ``type`` must be listed.
* ``regions``         -- ``spec.deployment.region`` must be listed when the spec names one.
* ``budget``          -- ``spec.policy.budget.monthlyUsd`` may not exceed ``perWorkspaceMonthlyUsd``.

An EMPTY allow-list is unconstrained on that axis (``schemas/workspace_policy.py``). The
``unclassified`` handling is the one asymmetry: on an axis the workspace constrains, an item that
was never classified cannot be shown to be permitted, so it is refused with its own code
(``tool_unclassified`` / ``knowledge_unclassified``) that tells the builder what to do -- classify
it -- instead of a misleading "not allowed". On an unconstrained axis it passes, and the
classifier (not this module) decides what tier an unclassified item implies.

Nothing here performs I/O; the deployment handler loads the spec and policy and calls ``admit``
before any persistence.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.agent_spec_v2 import AgentSpecV2
from app.schemas.workspace_policy import RiskTier, WorkspacePolicy
from app.services.risk_classifier import RiskReason, RiskResult, classify

#: Wire-level ``code`` of a denied decision; tests assert the exact value.
POLICY_DENIED = "policy_denied"

#: Bump when the rule set changes behaviour.
ADMISSION_VERSION = 1


class PolicyViolation(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    #: Stable reason code (``provider_not_allowed``, ``tool_unclassified``, ...).
    code: str
    #: JSON pointer into the spec document naming the offending value.
    path: str
    message: str


class AdmissionDecision(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    outcome: Literal["admitted", "denied"]
    workspace_id: str
    #: Set on both outcomes: a denied agent still has a tier, which the UI badge shows.
    tier: RiskTier
    reasons: list[RiskReason]
    #: Approver groups the tier routes to under this policy. Empty means auto-approve.
    approvers: list[str]
    violations: list[PolicyViolation]
    admission_version: int = ADMISSION_VERSION
    classifier_version: int

    @property
    def requires_approval(self) -> bool:
        return self.outcome == "admitted" and bool(self.approvers)


# --------------------------------------------------------------------------- allow-list rules


def _check_provider(spec: AgentSpecV2, policy: WorkspacePolicy) -> list[PolicyViolation]:
    allowed = policy.allowed.providers
    if not allowed or spec.model is None:
        return []
    provider = spec.model.provider
    if provider in allowed:
        return []
    return [
        PolicyViolation(
            code="provider_not_allowed",
            path="/model/provider",
            message=f"model provider {provider!r} is not allowed in this workspace (allowed: {sorted(allowed)})",
        )
    ]


def _check_model(spec: AgentSpecV2, policy: WorkspacePolicy) -> list[PolicyViolation]:
    allowed = policy.allowed.models
    if not allowed or spec.model is None:
        return []
    model_id = spec.model.model_id
    if model_id in allowed:
        return []
    return [
        PolicyViolation(
            code="model_not_allowed",
            path="/model/modelId",
            message=f"model {model_id!r} is not allowed in this workspace",
        )
    ]


def _check_connectors(spec: AgentSpecV2, policy: WorkspacePolicy) -> list[PolicyViolation]:
    allowed = policy.allowed.connectors
    if not allowed:
        return []
    violations = []
    for index, tool in enumerate(spec.tools):
        if tool.type != "connector" or tool.ref in allowed:
            continue
        violations.append(
            PolicyViolation(
                code="connector_not_allowed",
                path=f"/tools/{index}/ref",
                message=f"connector {tool.ref!r} (tool {tool.id!r}) is not allowed in this workspace",
            )
        )
    return violations


def _check_tool_risk_classes(spec: AgentSpecV2, policy: WorkspacePolicy) -> list[PolicyViolation]:
    allowed = policy.allowed.tool_risk_classes
    if not allowed:
        return []
    violations = []
    for index, tool in enumerate(spec.tools):
        if tool.risk_class == "unclassified":
            violations.append(
                PolicyViolation(
                    code="tool_unclassified",
                    path=f"/tools/{index}/riskClass",
                    message=(
                        f"tool {tool.id!r} has no risk class; this workspace only admits tools classified as "
                        f"{sorted(allowed)}, so classify it first"
                    ),
                )
            )
        elif tool.risk_class not in allowed:
            violations.append(
                PolicyViolation(
                    code="tool_risk_class_not_allowed",
                    path=f"/tools/{index}/riskClass",
                    message=f"tool {tool.id!r} is a {tool.risk_class!r} tool; this workspace allows {sorted(allowed)}",
                )
            )
    return violations


def _check_data_classes(spec: AgentSpecV2, policy: WorkspacePolicy) -> list[PolicyViolation]:
    allowed = policy.allowed.data_classes
    if not allowed:
        return []
    violations = []
    for index, item in enumerate(spec.knowledge):
        if item.data_class == "unclassified":
            violations.append(
                PolicyViolation(
                    code="knowledge_unclassified",
                    path=f"/knowledge/{index}/dataClass",
                    message=(
                        f"knowledge {item.id!r} has no data class; this workspace only admits data classified as "
                        f"{sorted(allowed)}, so classify it first"
                    ),
                )
            )
        elif item.data_class not in allowed:
            violations.append(
                PolicyViolation(
                    code="data_class_not_allowed",
                    path=f"/knowledge/{index}/dataClass",
                    message=f"knowledge {item.id!r} is {item.data_class!r} data; this workspace allows {sorted(allowed)}",
                )
            )
    return violations


def _check_channels(spec: AgentSpecV2, policy: WorkspacePolicy) -> list[PolicyViolation]:
    allowed = policy.allowed.channels
    if not allowed:
        return []
    violations = []
    for index, channel in enumerate(spec.channels):
        if channel.type in allowed:
            continue
        violations.append(
            PolicyViolation(
                code="channel_not_allowed",
                path=f"/channels/{index}/type",
                message=f"channel {channel.type!r} ({channel.id!r}) is not allowed in this workspace",
            )
        )
    return violations


def _check_region(spec: AgentSpecV2, policy: WorkspacePolicy) -> list[PolicyViolation]:
    allowed = policy.allowed.regions
    region = spec.deployment.region
    if not allowed or region is None or region in allowed:
        return []
    return [
        PolicyViolation(
            code="region_not_allowed",
            path="/deployment/region",
            message=f"Region {region!r} is not allowed in this workspace (allowed: {sorted(allowed)})",
        )
    ]


def _check_budget_ceiling(spec: AgentSpecV2, policy: WorkspacePolicy) -> list[PolicyViolation]:
    ceiling = policy.budgets.per_workspace_monthly_usd
    requested = spec.policy.budget.monthly_usd
    if ceiling is None or requested is None or requested <= ceiling:
        return []
    return [
        PolicyViolation(
            code="budget_exceeds_workspace",
            path="/policy/budget/monthlyUsd",
            message=f"monthly budget {requested} USD exceeds the workspace ceiling of {ceiling} USD",
        )
    ]


#: Evaluation order is the order violations are reported in; tests pin it.
_RULES = (
    _check_provider,
    _check_model,
    _check_connectors,
    _check_tool_risk_classes,
    _check_data_classes,
    _check_channels,
    _check_region,
    _check_budget_ceiling,
)


def violations_for(spec: AgentSpecV2, policy: WorkspacePolicy) -> list[PolicyViolation]:
    """Every allow-list violation of ``spec`` under ``policy``, in rule order."""
    found: list[PolicyViolation] = []
    for rule in _RULES:
        found.extend(rule(spec, policy))
    return found


# --------------------------------------------------------------------------- routing


def approvers_for(tier: RiskTier, policy: WorkspacePolicy) -> list[str]:
    """The approver groups ``tier`` routes to under ``policy`` (``[]`` = auto-approve)."""
    if tier == "low":
        return list(policy.approval.low)
    if tier == "medium":
        return list(policy.approval.medium)
    return list(policy.approval.high)


def admit(spec: AgentSpecV2, policy: WorkspacePolicy) -> AdmissionDecision:
    """Decide admission of ``spec`` under ``policy``: allow-lists first, then tier and routing."""
    risk: RiskResult = classify(spec, policy)
    violations = violations_for(spec, policy)
    return AdmissionDecision(
        outcome="denied" if violations else "admitted",
        workspace_id=policy.workspace_id,
        tier=risk.tier,
        reasons=risk.reasons,
        approvers=approvers_for(risk.tier, policy),
        violations=violations,
        classifier_version=risk.classifier_version,
    )


def denial_detail(decision: AdmissionDecision) -> dict[str, object]:
    """The exact ``detail`` body of a ``403 policy_denied`` response."""
    return {
        "code": POLICY_DENIED,
        "workspaceId": decision.workspace_id,
        "tier": decision.tier,
        "violations": [violation.model_dump(by_alias=True) for violation in decision.violations],
        "message": (
            f"This agent uses {len(decision.violations)} thing(s) workspace {decision.workspace_id!r} "
            "does not allow. Nothing was created."
        ),
    }
