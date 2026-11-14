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
  * an UNCLASSIFIED tool or knowledge item           -> the policy's ``defaults.unclassifiedRiskTier``
                                                        (medium unless the admin lowers it): a class
                                                        nobody gave cannot be shown to be harmless
  * a per-agent budget above the workspace default   -> one tier higher (modifier, applied last)

Trust model for classes. ``riskClass``/``dataClass`` are asserted by the builder on the spec, so the
classifier never lets an assertion LOWER what the platform knows: ``effective_risk_class`` floors a
connector's class at what its catalog operations imply (``services/connectors_catalog``: any
``create_*``/``send_*`` operation makes it ``write``; the Payments category makes it ``pay``). A builder
may raise a class, never reduce it below the catalog floor. Knowledge has no trusted source yet, so a
``dataClass`` assertion stands, and an admin who needs certainty constrains ``allowed.dataClasses``,
which refuses anything unclassified.

The order the reasons appear in the result is the order above, with the budget modifier last, so the
output is stable for a given input.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.agent_spec_v2 import AgentSpecV2, DataClass, KnowledgeSpec, RiskClass, ToolSpec
from app.schemas.workspace_policy import RiskTier, WorkspacePolicy

#: Bump when the rule set changes behaviour, so a stored classification can be compared against the
#: logic that produced it.
CLASSIFIER_VERSION = 2

_TIER_RANK: dict[RiskTier, int] = {"low": 0, "medium": 1, "high": 2}
_RANK_TIER: tuple[RiskTier, ...] = ("low", "medium", "high")

#: Trigger types that expose the agent to the public internet without a human in the loop.
_PUBLIC_INGRESS_TRIGGERS = frozenset({"webhook", "email"})

_CLASS_RANK: dict[str, int] = {"unclassified": -1, "read": 0, "write": 1, "pay": 2}
#: Catalog operation-name prefixes that mutate the connected system.
_WRITE_OPERATION_PREFIXES = ("create_", "send_", "update_", "delete_", "post_", "put_", "charge", "refund", "pay")
_PAY_CATEGORIES = frozenset({"payments"})


def catalog_risk_floor(connector_id: str) -> RiskClass | None:
    """The risk class the connector CATALOG implies for ``connector_id``, or None when the catalog
    does not know it. ``pay`` for the Payments category; ``write`` when any declared operation mutates
    the connected system; ``read`` for a connector whose operations are all reads. Trusted: the
    catalog is platform code, not builder input."""
    from app.services.connectors_catalog import get_connector

    entry = get_connector(connector_id)
    if entry is None:
        return None
    if str(entry.get("category", "")).strip().lower() in _PAY_CATEGORIES:
        return "pay"
    operations = [
        str(schema.get("name", "")).lower() for schema in entry.get("tool_schemas") or [] if isinstance(schema, dict)
    ]
    if any(name.startswith(_WRITE_OPERATION_PREFIXES) for name in operations):
        return "write"
    return "read"


def effective_risk_class(tool: ToolSpec, policy: WorkspacePolicy | None = None) -> RiskClass:
    """The class the classifier judges a tool by.

    Trusted first: a connector's class is floored at what its catalog operations imply, whatever the
    builder wrote. Then the builder's own assertion: in a workspace that trusts builder classifications
    (``defaults.trustBuilderClassifications``) it counts as written; elsewhere it may only make the agent
    STRICTER -- a ``write`` or ``pay`` label still counts (a builder declaring more risk is believed),
    while a ``read`` label is unverifiable and reads as ``unclassified`` (so it floors at the workspace's
    unclassified tier and is refused on a constrained axis). Nobody can lower a class below the catalog
    floor anywhere.
    """
    trusted = policy is not None and policy.defaults.trust_builder_classifications
    asserted: RiskClass = tool.risk_class
    if not trusted and asserted not in ("write", "pay"):
        asserted = "unclassified"
    if tool.type != "connector":
        return asserted
    floor = catalog_risk_floor(tool.ref)
    if floor is None:
        return asserted
    return floor if _CLASS_RANK[floor] > _CLASS_RANK[asserted] else asserted


def effective_data_class(item: KnowledgeSpec, policy: WorkspacePolicy | None = None) -> DataClass:
    """The data class the classifier judges knowledge by. There is no trusted source for data classes
    yet, so outside a workspace that trusts builder classifications only a label that RAISES risk
    counts (``confidential``, ``pii``); ``public`` and ``internal`` are unverifiable and read as
    ``unclassified``."""
    trusted = policy is not None and policy.defaults.trust_builder_classifications
    if trusted or item.data_class in ("confidential", "pii"):
        return item.data_class
    return "unclassified"


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
    offending = [tool.id for tool in spec.tools if effective_risk_class(tool, policy) == "pay"]
    if offending:
        return RiskReason(
            code="pay_tool",
            tier="high",
            message=f"payment tool(s) {offending} are always highest tier",
        )
    return None


def _rule_write_tool(spec: AgentSpecV2, policy: WorkspacePolicy) -> RiskReason | None:
    offending = [tool.id for tool in spec.tools if effective_risk_class(tool, policy) == "write"]
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
    offending = [item.id for item in spec.knowledge if effective_data_class(item, policy) in ("confidential", "pii")]
    if offending:
        return RiskReason(
            code="sensitive_data_class",
            tier="high",
            message=f"knowledge {offending} is confidential or PII",
        )
    return None


def _rule_public_ingress_with_write(spec: AgentSpecV2, policy: WorkspacePolicy) -> RiskReason | None:
    public = sorted({trig.type for trig in spec.triggers if trig.type in _PUBLIC_INGRESS_TRIGGERS})
    has_write = any(effective_risk_class(tool, policy) in ("write", "pay") for tool in spec.tools)
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


def _rule_unclassified(spec: AgentSpecV2, policy: WorkspacePolicy) -> RiskReason | None:
    """A tool or knowledge item nobody classified floors the agent to the policy's unclassified tier.
    ``low`` is an admin's explicit choice for a workspace (prototyping); it is never the default."""
    tools = [tool.id for tool in spec.tools if effective_risk_class(tool, policy) == "unclassified"]
    knowledge = [item.id for item in spec.knowledge if effective_data_class(item, policy) == "unclassified"]
    if not tools and not knowledge:
        return None
    tier = policy.defaults.unclassified_risk_tier
    if tier == "low":
        return None
    parts = []
    if tools:
        parts.append(f"tool(s) {tools}")
    if knowledge:
        parts.append(f"knowledge {knowledge}")
    return RiskReason(
        code="unclassified",
        tier=tier,
        message=f"unclassified {' and '.join(parts)}; this workspace floors unclassified items at {tier}",
    )


#: Evaluated in order; the result's ``reasons`` preserve this order.
_FLOOR_RULES = (
    _rule_pay_tool,
    _rule_write_tool,
    _rule_non_bedrock_provider,
    _rule_sensitive_data,
    _rule_public_ingress_with_write,
    _rule_custom_code,
    _rule_unclassified,
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
