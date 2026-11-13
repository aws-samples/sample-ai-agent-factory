"""Workspace policy: what a workspace may use and how its agents get approved (plan §3.B).

An admin hands a business user a sandbox by declaring, per workspace, which model providers,
connectors, data classes, channels and Regions are allowed; the per-agent and per-workspace budget
ceilings; and which approver groups each risk tier routes to. The risk classifier
(``services/risk_classifier.py``) reads the budget default to decide a tier; the admission hook
(component B, a later slice) reads the allowed lists to permit or deny and the approval routing to
pick the human gate. Strict: unknown keys are refused, so a mis-typed policy fails loudly instead of
silently granting or denying. Keys are camelCase on the wire and snake_case in Python, as in
``agent_spec_v2``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas.agent_spec_v2 import SPEC_ID_PATTERN, ChannelType, GuardrailProfile, PlatformVersion

#: The three risk tiers admission routes on, ordered low < medium < high.
RiskTier = Literal["low", "medium", "high"]

#: The classified (non-``unclassified``) subsets an allow-list may name. A workspace lists what it
#: permits; ``unclassified`` is never a permitted value -- it means "not yet classified", which
#: admission treats as needing classification -- so it must not appear in an allow-list.
AllowedToolRiskClass = Literal["read", "write", "pay"]
AllowedDataClass = Literal["public", "internal", "confidential", "pii"]


class _StrictPolicyModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", str_strip_whitespace=False
    )


class AllowedResources(_StrictPolicyModel):
    """What a workspace permits. An EMPTY list means "unconstrained on this axis" (the admission hook
    reads it that way), so a locked-down workspace lists exactly what it allows and a developer
    workspace leaves lists empty. ``unclassified`` is deliberately not a member of the risk/data
    enums here: an agent carrying it is not yet classified and cannot be auto-admitted."""

    providers: list[str] = Field(default_factory=list, max_length=32)
    models: list[str] = Field(default_factory=list, max_length=128)
    connectors: list[str] = Field(default_factory=list, max_length=128)
    tool_risk_classes: list[AllowedToolRiskClass] = Field(default_factory=list, max_length=3)
    data_classes: list[AllowedDataClass] = Field(default_factory=list, max_length=4)
    channels: list[ChannelType] = Field(default_factory=list, max_length=7)
    regions: list[str] = Field(default_factory=list, max_length=32)


class WorkspaceBudgets(_StrictPolicyModel):
    per_agent_monthly_usd: float | None = Field(default=None, ge=0)
    per_workspace_monthly_usd: float | None = Field(default=None, ge=0)


class ApprovalRouting(_StrictPolicyModel):
    """Approver groups required to admit an agent at each risk tier. An EMPTY list means auto-approve
    (no human gate); a non-empty list means every named group must approve. The classifier assigns
    the tier; this maps the tier to a human gate. Defaults follow the plan: low auto-approves, medium
    and high require groups the admin names."""

    low: list[str] = Field(default_factory=list, max_length=16)
    medium: list[str] = Field(default_factory=list, max_length=16)
    high: list[str] = Field(default_factory=list, max_length=16)


class WorkspaceDefaults(_StrictPolicyModel):
    platform_version: PlatformVersion | None = None
    guardrail_profile: GuardrailProfile = "standard"
    memory: str = Field(default="shortTerm", max_length=32)


class WorkspacePolicy(_StrictPolicyModel):
    workspace_id: str = Field(pattern=SPEC_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    admin_groups: list[str] = Field(default_factory=list, max_length=32)
    allowed: AllowedResources = Field(default_factory=AllowedResources)
    budgets: WorkspaceBudgets = Field(default_factory=WorkspaceBudgets)
    approval: ApprovalRouting = Field(default_factory=ApprovalRouting)
    defaults: WorkspaceDefaults = Field(default_factory=WorkspaceDefaults)
