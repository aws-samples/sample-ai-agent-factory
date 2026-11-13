"""Agent Spec v2: the single document every editor (canvas, form, copilot, code view) projects onto.

Shape (plan §3.A), strict: every object forbids unknown keys, so a malformed document fails
validation with the exact field path instead of being accepted with a silently ignored field.
Keys are camelCase on the wire and snake_case in Python, as in every other model here.

Authority inside the document, in this release
----------------------------------------------
``logic`` carries the canvas graph in the exact shape the platform persists today
(``buildFlowSavePayload``: snake_case node ``data`` with ``component_type``), and the deploy
compiler (``services/spec_compiler.py``) reads **only** ``logic`` plus ``deployment`` -- the same
inputs the frontend derives a deploy request from. The sections ``model``, ``instructions``,
``tools``, ``knowledge`` and ``policy`` are *projections* of that graph: the migration fills them,
``spec_service.normalize`` re-derives them, and ``spec_service.validate`` refuses a document whose
projections disagree with its graph (field path named). Nothing can therefore drift unnoticed, and a
Phase 2 writer that edits a projection must re-project the graph through ``normalize`` rather than
leave two truths behind. Making the projections authoritative and the canvas derived is a Phase 2
decision, recorded in the plan.

Classification fields introduced here (``riskClass`` on tools, ``dataClass`` on knowledge) default to
``unclassified`` when migrated from v1, never to a permissive value: a v1 canvas carries no risk or
data classification, and inventing one would be a safety judgement made by a migration. Component B
(workspaces, risk-tiered admission) treats ``unclassified`` as "needs classification".
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

SPEC_VERSION = 2
SPEC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

Origin = Literal["copilot", "form", "canvas", "code", "import", "template"]
KnowledgeType = Literal["managedKb", "customKb", "webSearch", "webAnswer"]
DataClass = Literal["public", "internal", "confidential", "pii", "unclassified"]
ToolType = Literal["lambda", "mcp", "openapi", "smithy", "connector", "agent", "knowledgeBase"]
RiskClass = Literal["read", "write", "pay", "unclassified"]
TriggerType = Literal["schedule", "webhook", "event", "email", "file"]
ChannelType = Literal["web", "slack", "teams", "email", "api", "mcp", "a2a"]
IdentityMode = Literal["obo", "service"]
DeploymentMode = Literal["runtime", "harness"]
PlatformVersion = Literal["V1", "V2"]
GuardrailProfile = Literal["off", "standard", "strict"]
ApprovalTier = Literal["low", "medium", "high", "derived"]
DerivedFromType = Literal["recommendation", "template", "import"]


class _Strict(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", str_strip_whitespace=False
    )


# --------------------------------------------------------------------------- sections


class ModelSpec(_Strict):
    """Which model the agent runs on. ``provider`` is the serving provider (bedrock, openai, ...);
    ``vendor`` is the model family the v1 canvas recorded beside the id (e.g. ``anthropic`` for a
    Claude model served by Bedrock) and is kept so a round trip loses nothing."""

    provider: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=256)
    params: dict[str, Any] = Field(default_factory=dict)
    vendor: str | None = Field(default=None, max_length=64)
    fallback_model_id: str | None = Field(default=None, max_length=256)


class InstructionsSpec(_Strict):
    system: str = Field(default="", max_length=100_000)
    style: str | None = Field(default=None, max_length=10_000)
    languages: list[str] = Field(default_factory=list, max_length=32)
    guardrail_profile: GuardrailProfile = "off"


class KnowledgeSpec(_Strict):
    id: str = Field(pattern=SPEC_ID_PATTERN)
    type: KnowledgeType
    config: dict[str, Any] = Field(default_factory=dict)
    data_class: DataClass = "unclassified"


class ToolSpec(_Strict):
    id: str = Field(pattern=SPEC_ID_PATTERN)
    type: ToolType
    ref: str = Field(min_length=1, max_length=512)
    auth: dict[str, Any] = Field(default_factory=dict)
    scopes: list[str] = Field(default_factory=list, max_length=64)
    risk_class: RiskClass = "unclassified"


class LogicNode(_Strict):
    """A canvas node, in the persisted v1 shape (``buildFlowSavePayload``), carried verbatim.

    ``data`` is the component configuration in snake_case with ``component_type``; it is kept as a
    mapping (not re-modelled) so that a configuration the frontend writes today round-trips byte
    for byte. Unknown node-level properties a future canvas adds are not forbidden silently: the
    migration moves them under ``x_legacy`` and a validation error names anything else."""

    id: str = Field(min_length=1, max_length=256)
    type: str = Field(min_length=1, max_length=64)
    position: dict[str, int | float] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    selected: bool = False
    validation_status: str = Field(default="pending", max_length=32)
    x_legacy: dict[str, Any] = Field(default_factory=dict)

    @field_validator("position")
    @classmethod
    def _position_keys(cls, value: dict[str, int | float]) -> dict[str, int | float]:
        unknown = sorted(set(value) - {"x", "y"})
        if unknown:
            raise ValueError(f"position accepts x and y only, got {unknown}")
        return value


class LogicEdge(_Strict):
    id: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=256)
    target: str = Field(min_length=1, max_length=256)
    source_handle: str = Field(default="output", max_length=64)
    target_handle: str = Field(default="input", max_length=64)
    type: str = Field(default="data", max_length=32)
    animated: bool = False
    x_legacy: dict[str, Any] = Field(default_factory=dict)


class LogicSpec(_Strict):
    nodes: list[LogicNode] = Field(default_factory=list, max_length=200)
    edges: list[LogicEdge] = Field(default_factory=list, max_length=400)
    viewport: dict[str, int | float] = Field(default_factory=lambda: {"x": 0, "y": 0, "zoom": 1})

    @field_validator("viewport")
    @classmethod
    def _viewport_keys(cls, value: dict[str, int | float]) -> dict[str, int | float]:
        unknown = sorted(set(value) - {"x", "y", "zoom"})
        if unknown:
            raise ValueError(f"viewport accepts x, y and zoom only, got {unknown}")
        return value


class TriggerSpec(_Strict):
    id: str = Field(pattern=SPEC_ID_PATTERN)
    type: TriggerType
    config: dict[str, Any] = Field(default_factory=dict)


class ChannelSpec(_Strict):
    id: str = Field(pattern=SPEC_ID_PATTERN)
    type: ChannelType
    config: dict[str, Any] = Field(default_factory=dict)
    identity_mode: IdentityMode = "service"


class BudgetSpec(_Strict):
    monthly_usd: float | None = Field(default=None, ge=0)
    per_run_usd: float | None = Field(default=None, ge=0)


class PolicySpec(_Strict):
    cedar_policy_refs: list[str] = Field(default_factory=list, max_length=64)
    gateway_guardrail_profile: GuardrailProfile = "off"
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    approval_tier: ApprovalTier = "derived"


class DeploymentSpec(_Strict):
    mode: DeploymentMode = "runtime"
    platform_version: PlatformVersion | None = None
    region: str | None = Field(default=None, max_length=32)
    vpc_egress: dict[str, Any] | None = None
    memory: dict[str, Any] | None = None
    identity: dict[str, Any] | None = None
    #: The gallery template the canvas was created from, when it was; the compiler passes it through
    #: because code generation and admission read it (template_composition).
    template_id: str | None = Field(default=None, max_length=128)
    #: The canvas node the deploy targets. None means "the default deployable runtime", resolved the
    #: way the canvas resolves it (first HTTP runtime that is not an MCP-server target).
    deployable_node_id: str | None = Field(default=None, max_length=256)
    #: The platform's deployment governance block (naming profile, tag profile), carried verbatim.
    governance: dict[str, Any] = Field(default_factory=dict)


class ABTestSpec(_Strict):
    weights: dict[str, float] = Field(default_factory=dict)
    success_metric: str = Field(default="", max_length=128)


class EvaluationSpec(_Strict):
    datasets: list[str] = Field(default_factory=list, max_length=64)
    evaluators: list[str] = Field(default_factory=list, max_length=64)
    thresholds: dict[str, float] = Field(default_factory=dict)
    ab_test: ABTestSpec | None = None


class CustomNodeCode(_Strict):
    language: Literal["python", "typescript"] = "python"
    source_ref: str = Field(min_length=1, max_length=1024)
    sha256: str
    inline: str | None = Field(default=None, max_length=102_400)

    @field_validator("sha256")
    @classmethod
    def _hex(cls, value: str) -> str:
        if not SHA256_HEX.match(value):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return value


class CodeSpec(_Strict):
    custom_nodes: dict[str, CustomNodeCode] = Field(default_factory=dict)
    hooks: dict[str, Any] | None = None


class DerivedFrom(_Strict):
    type: DerivedFromType
    ref: str = Field(min_length=1, max_length=512)


class MetaSpec(_Strict):
    revision: int = Field(default=0, ge=0)
    content_hash: str | None = None
    created_by: str = Field(default="", max_length=256)
    created_at: str = Field(default="", max_length=64)
    derived_from: DerivedFrom | None = None
    #: Document-level v1 fields that have no v2 home (``name: "auto-save"``, ``version: "1.0.0"``,
    #: the metadata block); kept so the migration is lossless.
    x_legacy: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content_hash")
    @classmethod
    def _hex(cls, value: str | None) -> str | None:
        if value is not None and not SHA256_HEX.match(value):
            raise ValueError("contentHash must be 64 lowercase hex characters")
        return value


# --------------------------------------------------------------------------- document


class AgentSpecV2(_Strict):
    spec_version: Literal[2] = Field(default=SPEC_VERSION)
    id: str = Field(pattern=SPEC_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    workspace_id: str | None = Field(default=None, max_length=128)
    owner_sub: str | None = Field(default=None, max_length=256)
    origin: Origin = "canvas"
    model: ModelSpec | None = None
    instructions: InstructionsSpec = Field(default_factory=InstructionsSpec)
    knowledge: list[KnowledgeSpec] = Field(default_factory=list, max_length=64)
    tools: list[ToolSpec] = Field(default_factory=list, max_length=128)
    logic: LogicSpec = Field(default_factory=LogicSpec)
    triggers: list[TriggerSpec] = Field(default_factory=list, max_length=32)
    channels: list[ChannelSpec] = Field(default_factory=list, max_length=32)
    policy: PolicySpec = Field(default_factory=PolicySpec)
    deployment: DeploymentSpec = Field(default_factory=DeploymentSpec)
    evaluation: EvaluationSpec = Field(default_factory=EvaluationSpec)
    code: CodeSpec = Field(default_factory=CodeSpec)
    meta: MetaSpec = Field(default_factory=MetaSpec)

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        for tag in value:
            if not tag or len(tag) > 64:
                raise ValueError("each tag must be 1..64 characters")
        return value


def json_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12, camelCase, by alias) exported to schemas/agent-spec-v2.schema.json."""
    schema = AgentSpecV2.model_json_schema(by_alias=True, mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://github.com/aws-samples/sample-ai-agent-factory/schemas/agent-spec-v2.schema.json"
    return schema
