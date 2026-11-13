"""Risk-tiered classification of an Agent Spec v2 against its workspace policy (plan §3.B).

Pure and deterministic: ``classify(spec, policy)`` returns a tier (low/medium/high) and the ordered
reasons that produced it, with no I/O. Each rule is a small function so a reviewer can read exactly
why an agent is High, and so a mutation test can disable one rule and watch exactly one test fail.
The tier is advisory input to admission (component B): admission routes it through the workspace's
``approval`` groups. Nothing here grants or denies on its own.

Rules (plan §3.B). Each is a *floor* (it raises the tier to at least X) unless marked a modifier:
  * a ``pay`` tool                                   -> High   (payments are never auto-admitted)
  * a ``write`` tool                                 -> Medium
  * a non-Bedrock model provider                     -> Medium
  * ``confidential`` or ``pii`` knowledge            -> High
  * public ingress (webhook/email trigger) AND a write tool -> High
  * custom code (ejected nodes)                      -> Medium
  * a per-agent budget above the workspace default   -> one tier higher (modifier, applied last)

The order the reasons appear in the result is the order above, with the budget modifier last, so the
output is stable for a given input.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.agent_spec_v2 import AgentSpecV2
from app.schemas.workspace_policy import RiskTier, WorkspacePolicy

#: Bump when the rule set changes behaviour, so a stored classification can be compared against the
#: logic that produced it.
CLASSIFIER_VERSION = 1

_TIER_RANK: dict[RiskTier, int] = {"low": 0, "medium": 1, "high": 2}
_RANK_TIER: tuple[RiskTier, ...] = ("low", "medium", "high")

#: Trigger types that expose the agent to the public internet without a human in the loop.
_PUBLIC_INGRESS_TRIGGERS = frozenset({"webhook", "email"})


def _higher(current: RiskTier, candidate: RiskTier) -> RiskTier:
    """The more severe of two tiers."""
    return current if _TIER_RANK[current] >= _TIER_RANK[candidate] else candidate


def _bump(tier: RiskTier) -> RiskTier:
    """One tier higher, capped at High."""
    return _RANK_TIER[min(_TIER_RANK[tier] + 1, _TIER_RANK["high"])]


class RiskReason(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    #: Stable identifier of the rule that fired (asserted by tests and surfaced in the UI badge).
    code: str
    #: ``floor`` rules raise the tier to at least ``tier``; the single ``modifier`` records the tier
    #: after its one-step bump.
    kind: Literal["floor", "modifier"] = "floor"
    tier: RiskTier
    message: str


class RiskResult(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    tier: RiskTier
    reasons: list[RiskReason]
    classifier_version: int = CLASSIFIER_VERSION


# --------------------------------------------------------------------------- floor rules
# Each returns the RiskReason it imposes, or None when it does not apply. They never read each other.


def _rule_pay_tool(spec: AgentSpecV2, policy: WorkspacePolicy) -> RiskReason | None:
    offending = [tool.id for tool in spec.tools if tool.risk_class == "pay"]
    if offending:
        return RiskReason(
            code="pay_tool",
            tier="high",
            message=f"payment tool(s) {offending} are always highest tier",
        )
    return None


def _rule_write_tool(spec: AgentSpecV2, policy: WorkspacePolicy) -> RiskReason | None:
    offending = [tool.id for tool in spec.tools if tool.risk_class == "write"]
    if offending:
        return RiskReason(
            code="write_tool",
            tier="medium",
            message=f"write tool(s) {offending} can change external state",
        )
    return None


def _rule_non_bedrock_provider(spec: AgentSpecV2, policy: WorkspacePolicy) -> RiskReason | None:
    if spec.model is not None and spec.model.provider.strip().lower() != "bedrock":
        return RiskReason(
            code="non_bedrock_provider",
            tier="medium",
            message=f"model provider {spec.model.provider!r} is not Bedrock",
        )
    return None


def _rule_sensitive_data(spec: AgentSpecV2, policy: WorkspacePolicy) -> RiskReason | None:
    offending = [item.id for item in spec.knowledge if item.data_class in ("confidential", "pii")]
    if offending:
        return RiskReason(
            code="sensitive_data_class",
            tier="high",
            message=f"knowledge {offending} is confidential or PII",
        )
    return None


def _rule_public_ingress_with_write(spec: AgentSpecV2, policy: WorkspacePolicy) -> RiskReason | None:
    public = sorted({trig.type for trig in spec.triggers if trig.type in _PUBLIC_INGRESS_TRIGGERS})
    has_write = any(tool.risk_class == "write" for tool in spec.tools)
    if public and has_write:
        return RiskReason(
            code="public_ingress_with_write",
            tier="high",
            message=f"public ingress {public} combined with a write tool",
        )
    return None


def _rule_custom_code(spec: AgentSpecV2, policy: WorkspacePolicy) -> RiskReason | None:
    if spec.code.custom_nodes:
        return RiskReason(
            code="custom_code",
            tier="medium",
            message=f"custom code node(s) {sorted(spec.code.custom_nodes)}",
        )
    return None


#: Evaluated in order; the result's ``reasons`` preserve this order.
_FLOOR_RULES = (
    _rule_pay_tool,
    _rule_write_tool,
    _rule_non_bedrock_provider,
    _rule_sensitive_data,
    _rule_public_ingress_with_write,
    _rule_custom_code,
)


def _budget_over_default(spec: AgentSpecV2, policy: WorkspacePolicy) -> bool:
    """True when the agent asks for more monthly budget than the workspace's per-agent default. When
    either side is unset there is nothing to compare, so the modifier does not fire."""
    default = policy.budgets.per_agent_monthly_usd
    requested = spec.policy.budget.monthly_usd
    return default is not None and requested is not None and requested > default


def classify(spec: AgentSpecV2, policy: WorkspacePolicy) -> RiskResult:
    """Classify ``spec`` against ``policy`` into a risk tier with the ordered reasons behind it."""
    reasons: list[RiskReason] = []
    tier: RiskTier = "low"

    for rule in _FLOOR_RULES:
        reason = rule(spec, policy)
        if reason is not None:
            reasons.append(reason)
            tier = _higher(tier, reason.tier)

    if _budget_over_default(spec, policy):
        tier = _bump(tier)
        default = policy.budgets.per_agent_monthly_usd
        requested = spec.policy.budget.monthly_usd
        reasons.append(
            RiskReason(
                code="budget_above_default",
                kind="modifier",
                tier=tier,
                message=(
                    f"requested monthly budget ${requested:g} exceeds workspace default ${default:g}, raising one tier"
                ),
            )
        )

    return RiskResult(tier=tier, reasons=reasons)
