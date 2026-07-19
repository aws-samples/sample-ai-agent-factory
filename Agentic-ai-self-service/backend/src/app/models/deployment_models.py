"""Pydantic models for deployment state, runtime configuration, and API request/response types.

These models support the serverless deployment orchestration via Step Functions,
deployment state persistence in DynamoDB, and the Deployment Lambda API surface.
"""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .components import ConnectorConfig

# Bedrock models published between October 2025 and May 2026 — the policy
# window enforced by this platform. Anything matching one of these substrings
# passes; anything else is rejected at /api/deploy with HTTP 422 instead of
# being deployed and failing at first invocation.
#
# Pre-Q4-2025 models (Claude Sonnet 4 / Opus 4.1, Claude 3.x, Nova v1
# Pro/Lite/Micro, Mistral Large 2407, Cohere Command R/R+, Llama 3.x) are
# intentionally excluded — Bedrock flags them Legacy and returns
# `ResourceNotFoundException: Access denied. This Model is marked by
# provider as Legacy and you have not been actively using the model in the
# last 30 days.` See tasks/lessons.md Bug 113. Update when new generations
# ship within the policy window.
_BEDROCK_ACTIVE_MODEL_SUBSTRINGS = (
    # Anthropic current generation (date-less IDs, no -v1:0 suffix)
    "anthropic.claude-sonnet-5",
    "anthropic.claude-sonnet-4-6",
    "anthropic.claude-opus-4-8",
    # Anthropic Claude Haiku 4.5 (Bedrock GA Oct 2025; dated ID with -v1:0)
    "anthropic.claude-haiku-4-5",
    # Amazon Nova 2 (Bedrock GA Q4 2025)
    "amazon.nova-2-",
    "amazon.nova-premier",
    # Meta Llama 4 (Bedrock GA Oct 2025)
    "meta.llama4-",
    # AI21 Jamba 1.5 (current Bedrock-supported)
    "ai21.jamba-1-5",
    # OpenAI OSS (Bedrock GA Q4 2025)
    "openai.gpt-oss-",
    # DeepSeek R1 / V3.1 (Bedrock GA Q4 2025 / Q1 2026)
    "deepseek.r1",
    "deepseek.v3",
)


def _validate_bedrock_model_id(model_id: str) -> None:
    """Reject obviously-invalid or known-Legacy Bedrock model IDs.

    Catches the most common foot-guns:
      - Empty / structurally malformed IDs (no dots)
      - Claude 3.x (Bedrock now flags Legacy on many accounts)
      - Random strings that look nothing like a Bedrock model

    For non-Bedrock providers (OpenAI/Anthropic-direct/etc), we don't have a
    catalog handy so we accept any non-empty string and let the provider
    surface the error at invocation. See tasks/lessons.md Bug 26 + 34.
    """
    if not model_id or not isinstance(model_id, str):
        raise ValueError("model.modelId is required")
    if "." not in model_id:
        raise ValueError(f"Bedrock model ID '{model_id}' is malformed (expected provider.model-name format)")
    # Explicit Legacy guard for the most common foot-guns. The substring list
    # below catches the IDs that ship in older sample/blueprint code and that
    # Bedrock now responds to with `ResourceNotFoundException: ... marked by
    # provider as Legacy ...`. Surfacing a clear error here is much better
    # than letting the deploy succeed and the runtime explode at first
    # invocation. Policy: only Bedrock models published Oct 2025 – May 2026.
    _LEGACY_SUBSTRINGS = (
        "claude-3-",  # Claude 3.x (early 2024)
        "claude-sonnet-4-2",  # Claude Sonnet 4 dated IDs (May 2025) — pre-cutoff
        "claude-opus-4-1",  # Claude Opus 4.1 (Aug 2025) — pre-cutoff
        "amazon.nova-pro-v1",  # Nova v1 (Dec 2024) — pre-cutoff
        "amazon.nova-lite-v1",
        "amazon.nova-micro-v1",
        "amazon.titan-",  # Titan family — pre-cutoff
        "meta.llama3-",  # Llama 3.x — pre-cutoff
        "mistral.mistral-large-2407",  # Mistral Large 2407 — pre-cutoff
        "mistral.mistral-small-2402",  # Mistral Small 2402 — pre-cutoff
        "cohere.command-r",  # Cohere Command R/R+ — pre-cutoff
    )
    for legacy in _LEGACY_SUBSTRINGS:
        if legacy in model_id:
            raise ValueError(
                f"Bedrock model '{model_id}' is outside the supported window "
                f"(Oct 2025 – May 2026) and Bedrock flags it Legacy. "
                f"Use a current ID such as "
                f"us.anthropic.claude-sonnet-5, "
                f"us.anthropic.claude-opus-4-8, "
                f"or us.amazon.nova-2-lite-v1:0. "
                f"See tasks/lessons.md Bug 113."
            )
    # Validator only runs when the caller sets model_provider="bedrock", so we
    # always require a known-active substring. Previously the regex gate let
    # non-prefixed bogus Bedrock-shaped IDs through (Bug 51).
    bedrock_like = any(s in model_id for s in _BEDROCK_ACTIVE_MODEL_SUBSTRINGS)
    if not bedrock_like:
        raise ValueError(
            f"Bedrock model '{model_id}' is not in the known-active list "
            f"and may have been decommissioned. If this is a new model, "
            f"add its substring to _BEDROCK_ACTIVE_MODEL_SUBSTRINGS."
        )


# ============================================================================
# Deployment Enums
# ============================================================================


class DeploymentStatusEnum(str, Enum):
    """Status of a deployment execution tracked in the Deployment_State_Table."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DeploymentStepName(str, Enum):
    """Individual steps in the Step Functions deployment state machine."""

    VALIDATE = "validate"
    MCP_SERVER = "mcp_server"
    CODEGEN = "codegen"
    IAM = "iam"
    GATEWAY = "gateway"
    KNOWLEDGE_BASE = "knowledge_base"
    MEMORY = "memory"
    GUARDRAILS = "guardrails"
    POLICY = "policy"
    RUNTIME_CONFIGURE = "runtime_configure"
    RUNTIME_LAUNCH = "runtime_launch"
    HARNESS = "harness"
    EVALUATION = "evaluation"
    AUTH = "auth"
    STATUS_UPDATE = "status_update"


# ============================================================================
# Deployment State Model (DynamoDB persistence)
# ============================================================================


class DeploymentState(BaseModel):
    """Deployment execution state persisted in the Deployment_State_Table.

    Each record tracks a single deployment from initiation through completion,
    including the current step, runtime outputs, and error details.
    """

    deployment_id: str
    workflow_id: str
    user_id: str | None = None
    execution_arn: str | None = None
    status: DeploymentStatusEnum = DeploymentStatusEnum.PENDING
    current_step: DeploymentStepName | None = None
    started_at: datetime
    completed_at: datetime | None = None
    runtime_endpoint: str | None = None
    runtime_id: str | None = None
    gateway_url: str | None = None
    gateway_result: dict | None = None  # Full gateway deployment result for cleanup
    policy_result: dict | None = None  # Policy engine result for cleanup
    knowledge_base_result: dict | None = None  # KB result for cleanup
    guardrails_result: dict | None = None  # Guardrails result for cleanup
    mcp_server_runtime_id: str | None = None
    memory_result: dict | None = None  # Memory deployment result for cleanup
    runtime_arn: str | None = None  # Full ARN of the deployed runtime
    # Phase B — AgentCore Harness (parallel authoring path). When
    # ``deployment_mode == "harness"`` the runtime_*/codegen fields are unused
    # and the harness id/arn below identify the deployed managed harness so the
    # delete/test paths can route to harness_deployer instead of runtime ops.
    harness_id: str | None = None
    harness_arn: str | None = None
    deployment_mode: str | None = None  # "runtime" (default) | "harness"
    error_details: str | None = None
    ttl: int | None = None  # Unix epoch for DynamoDB TTL (30 days from started_at)
    # Phase 1 Gap 1A — versioning. Every deploy mints a sortable version_id.
    # ``parent_version_id`` is the version this deploy supersedes (None for the
    # first deploy of a friendly runtime name). ``deployment_slot`` is the
    # slot the user requested for this version; the actual production slot is
    # the source-of-truth in RuntimeSlotsTable, mutable via /promote /rollback.
    version_id: str | None = None
    parent_version_id: str | None = None
    deployment_slot: Literal["staging", "production"] | None = None
    # The AgentCore-side runtime name (friendly + version suffix). Distinct
    # from ``runtime_id`` (the AgentCore-assigned id) and ``RuntimeConfig.name``
    # (the user-facing friendly name). Stored explicitly so the delete path
    # can resolve it without reconstructing the suffix.
    agentcore_runtime_name: str | None = None
    # Generic teardown manifest: every deploy step appends the sub-resources it
    # creates here as {"type","id","region",...optional}. The delete path iterates
    # this to tear down EVERY created resource generically, instead of relying on
    # per-component *_result fields that a success-only step may never persist
    # (the root cause of orphan Bugs 154/158). Additive + idempotent: each entry
    # carries enough to delete it; the type-dispatched deleter no-ops on unknown
    # types so older records (no manifest) still fall back to *_result cleanup.
    created_resources: list[dict] | None = None
    # Phase 7 (opt-in) — the account/region this deploy targeted (None → home).
    # Recorded so the SEPARATE delete request can assume the same cross-account
    # role to tear down, without the original SFN event.
    target_account_id: str | None = None
    target_region: str | None = None
    # Async (slow-class) teardown tracking. KB-backed deletes exceed API
    # Gateway's 29s integration cap, so DELETE /api/runtime/{id} dispatches
    # them to a background self-invoke and the caller polls
    # GET /api/deploy/{deployment_id} for these fields.
    # "deleting" → "deleted" | "delete_failed"; delete_message carries the
    # final cleanup summary (truncated to ~1KB).
    delete_status: str | None = None
    delete_message: str | None = None


# ============================================================================
# Runtime Configuration Model (moved from routers/deployment.py)
# ============================================================================


class RuntimeConfig(BaseModel):
    """Runtime configuration received from the frontend.

    Uses camelCase aliases to match the frontend JSON payload while exposing
    snake_case attributes in Python. ``ConfigDict(populate_by_name=True)``
    allows construction with either naming convention.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=100)
    entrypoint: str = Field(default="agent.py")
    framework: Literal["strands_agents"] = Field(default="strands_agents")
    model: dict
    system_prompt: str = Field(
        alias="systemPrompt",
        default="You are a helpful AI assistant.",
        max_length=10000,
    )
    deployment_type: str = Field(alias="deploymentType", default="S3_CODE_DEPLOY")
    python_runtime: str = Field(alias="pythonRuntime", default="PYTHON_3_13")
    protocol: Literal["HTTP", "MCP", "A2A"] = Field(default="HTTP")
    idle_timeout: int = Field(alias="idleTimeout", ge=60, le=28800, default=900)
    max_lifetime: int = Field(alias="maxLifetime", ge=60, le=28800, default=28800)
    enable_otel: bool = Field(alias="enableOtel", default=False)
    # Observability (OTLP) — superset of enable_otel
    observability: Optional["ObservabilityConfig"] = Field(default=None)
    # Strands model provider
    model_provider: Literal[
        "bedrock",
        "openai",
        "anthropic",
        "gemini",
        "litellm",
        "mistral",
        "ollama",
        "sagemaker",
        "writer",
        "llamaapi",
        "deepseek",
        "groq",
        "together",
    ] = Field(alias="modelProvider", default="bedrock")
    provider_api_key_ref: str | None = Field(alias="providerApiKeyRef", default=None)
    # Optional base URL for OpenAI-compatible providers / a self-hosted LiteLLM
    # proxy. Injected as PROVIDER_BASE_URL and read by the generated model init.
    provider_base_url: str | None = Field(alias="providerBaseUrl", default=None, max_length=512)
    # VPC egress (Loom-study 0.1). When set, the runtime is created in VPC network
    # mode with these subnets/SGs so it can reach VPC-private resources. Accepts a
    # {subnet_ids, security_group_ids} dict; None → PUBLIC network mode.
    vpc_config: dict | None = Field(alias="vpcConfig", default=None)
    # Loom-study 4.2 — a named VPC profile (subnets/SGs defined once, picked here).
    # Resolved to vpc_config at the deploy boundary; explicit vpc_config wins.
    vpc_profile: str | None = Field(alias="vpcProfile", default=None, max_length=64)
    # Multi-agent pattern
    multi_agent_pattern: str = Field(alias="multiAgentPattern", default="none")
    multi_agent_config: dict | None = Field(alias="multiAgentConfig", default=None)

    @field_validator("name")
    @classmethod
    def _normalize_runtime_name(cls, v: str) -> str:
        """Shift-left the AgentCore runtime-name regex to the API boundary.

        The runtime name is later fed to ``sanitize_runtime_name`` (underscore
        style ``[a-zA-Z][a-zA-Z0-9_]{0,47}``) before CreateAgentRuntime. We
        NORMALIZE here (preferred over a hard 422) so a fixable name like
        "My Agent" never blocks a deploy, while a name that sanitizes to empty
        is rejected with a clear error. Matches the ConnectorConfig validator
        style (normalize-or-422 at the boundary).
        """
        from app.services.naming import is_valid_agentcore_name, sanitize_agentcore_name

        if v is None or not str(v).strip():
            raise ValueError("runtime name must not be empty")
        if is_valid_agentcore_name(v, style="underscore"):
            return v
        normalized = sanitize_agentcore_name(v, style="underscore", prefix="agent")
        if not normalized:
            raise ValueError(f"runtime name '{v}' cannot be normalized to a valid AgentCore name")
        return normalized

    @model_validator(mode="after")
    def _check_model_id(self) -> "RuntimeConfig":
        """Reject obviously-invalid / Legacy Bedrock model IDs at the API
        boundary instead of letting the deploy succeed and fail at invoke."""
        if self.model_provider == "bedrock":
            model_id = ""
            if isinstance(self.model, dict):
                model_id = self.model.get("modelId") or self.model.get("model_id") or ""
            _validate_bedrock_model_id(model_id)
        # Multi-agent sub-agents are also Bedrock-default — validate each.
        if self.multi_agent_config and isinstance(self.multi_agent_config, dict):
            for ag in self.multi_agent_config.get("agents", []):
                ag_provider = ag.get("modelProvider", self.model_provider)
                if ag_provider == "bedrock":
                    _validate_bedrock_model_id(ag.get("modelId", ""))
        return self

    @model_validator(mode="after")
    def _check_multi_agent_schema(self) -> "RuntimeConfig":
        """Validate multi_agent_config keys at the API boundary so a typo
        like `id`/`from`/`to` doesn't crash mid-SFN with KeyError. The codegen
        in services/code_generator.py expects `agentId` on each agent and
        `source`/`target` on each edge. See tasks/lessons.md Bug 59.
        """
        cfg = self.multi_agent_config
        if not cfg or not isinstance(cfg, dict):
            return self
        agents = cfg.get("agents") or []
        if not isinstance(agents, list):
            raise ValueError("multiAgentConfig.agents must be a list")
        for i, ag in enumerate(agents):
            if not isinstance(ag, dict):
                raise ValueError(f"multiAgentConfig.agents[{i}] must be an object")
            if not ag.get("agentId"):
                raise ValueError(f"multiAgentConfig.agents[{i}].agentId is required (got keys: {sorted(ag.keys())})")
        edges = cfg.get("edges") or []
        if not isinstance(edges, list):
            raise ValueError("multiAgentConfig.edges must be a list")
        for i, e in enumerate(edges):
            if not isinstance(e, dict):
                raise ValueError(f"multiAgentConfig.edges[{i}] must be an object")
            if not e.get("source") or not e.get("target"):
                raise ValueError(
                    f"multiAgentConfig.edges[{i}] requires source and target (got keys: {sorted(e.keys())})"
                )
        return self


# ============================================================================
# Observability Configuration (OTLP)
# ============================================================================


class ObservabilityConfig(BaseModel):
    """OTLP observability configuration from the Observability node.

    Aliases use camelCase to match the frontend payload.
    """

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = True
    provider: Literal[
        "langfuse",
        "custom",
    ] = "langfuse"
    otlp_endpoint: str | None = Field(alias="otlpEndpoint", default=None)
    otlp_protocol: Literal["http/protobuf", "grpc"] = Field(alias="otlpProtocol", default="http/protobuf")
    service_name: str | None = Field(alias="serviceName", default=None)
    sample_rate: float = Field(alias="sampleRate", ge=0.0, le=1.0, default=1.0)
    resource_attributes: dict[str, str] = Field(alias="resourceAttributes", default_factory=dict)
    auth_header_secret_arn: str | None = Field(alias="authHeaderSecretArn", default=None)
    extra_headers: dict[str, str] = Field(alias="extraHeaders", default_factory=dict)


# Forward-ref resolution for RuntimeConfig.observability
RuntimeConfig.model_rebuild()


# ============================================================================
# API Request / Response Models
# ============================================================================


class IdentityConfig(BaseModel):
    """Identity provider configuration from the frontend Identity node."""

    model_config = ConfigDict(populate_by_name=True)

    provider: str = "cognito"
    client_id: str = Field(alias="clientId", default="")
    # Gap P3.3B — per-agent identity. mode == 'per_agent' opts this runtime into
    # a least-privilege per-runtime IAM execution role (minted by iam_step);
    # mode == 'shared' (the default) keeps the Bug-60 stack shared role. The
    # 'shared' default guarantees absent/legacy callers are unaffected.
    mode: Literal["shared", "per_agent"] = "shared"
    scope: str | None = None
    client_secret_ref: str = Field(alias="clientSecretRef", default="")
    discovery_url: str = Field(alias="discoveryUrl", default="")
    scopes: list[str] = Field(default_factory=list)
    audience: str | None = None


class CustomToolDefinition(BaseModel):
    """A custom AI-generated tool to deploy as a Lambda Gateway Target."""

    model_config = ConfigDict(populate_by_name=True)

    tool_name: str = Field(alias="toolName", min_length=1, max_length=64)
    display_name: str = Field(alias="displayName", default="", max_length=128)
    description: str = Field(default="", max_length=1000)
    lambda_code: str = Field(alias="lambdaCode", max_length=50000)
    input_schema: dict = Field(alias="inputSchema", default_factory=dict)


class ImportRuntimeRequest(BaseModel):
    """Adopt an already-deployed AgentCore Runtime by ARN (Loom-study 1.5).

    POST /api/runtime/import — records an externally-built runtime as a
    caller-owned SUCCEEDED deployment without any codegen/deploy.
    """

    model_config = ConfigDict(populate_by_name=True)

    runtime_arn: str = Field(alias="runtimeArn", min_length=20, max_length=2048)
    aws_region: str | None = Field(alias="awsRegion", default=None, max_length=30)


class DeployRequest(BaseModel):
    """Request body for POST /api/deploy."""

    model_config = ConfigDict(populate_by_name=True)

    node_id: str = Field(alias="nodeId", max_length=256, pattern=r"^[a-zA-Z0-9_-]+$")
    config: RuntimeConfig
    # Phase B — selects the authoring/deploy path. "runtime" (default) keeps the
    # existing visual-canvas code-generated AgentCore Runtime UNCHANGED; "harness"
    # declares a managed AgentCore Harness instead (no codegen / S3 / runtime).
    deployment_mode: Literal["runtime", "harness"] | None = Field(alias="deploymentMode", default="runtime")
    connected_tools: list | None = Field(alias="connectedTools", default=None, max_length=20)
    gateway_config: dict | None = Field(alias="gatewayConfig", default=None)
    gateway_tools: list | None = Field(alias="gatewayTools", default=None, max_length=20)
    template_id: str | None = Field(alias="templateId", default=None, max_length=128)
    identity_config: IdentityConfig | None = Field(alias="identityConfig", default=None)
    custom_tools: list[CustomToolDefinition] | None = Field(alias="customTools", default=None)
    # SaaS connectors (Phase A) — deployed as Gateway OpenAPI targets. Each
    # entry's secret_value is write-only (minted into Secrets Manager in the
    # gateway step, then dropped); only secret_arn is ever persisted.
    connectors: list[ConnectorConfig] | None = Field(default=None, max_length=20)
    # External MCP catalog servers wired as Gateway `mcpServer` targets (Loom
    # external-MCP path). Each entry: {server_id, endpoint_vars?, secret_value?
    # (write-only, minted then dropped), secret_arn?, oauth?}. Only direct-* tier
    # catalog entries are wireable; adapter-* are rejected server-side.
    external_mcp_servers: list[dict] | None = Field(alias="externalMcpServers", default=None, max_length=20)
    memory_config: dict | None = Field(alias="memoryConfig", default=None)
    evaluation_config: dict | None = Field(alias="evaluationConfig", default=None)
    policy_config: dict | None = Field(alias="policyConfig", default=None)
    mcp_server_config: dict | None = Field(alias="mcpServerConfig", default=None)
    knowledge_base_config: dict | None = Field(alias="knowledgeBaseConfig", default=None)
    guardrails_config: dict | None = Field(alias="guardrailsConfig", default=None)
    observability_config: dict | None = Field(alias="observabilityConfig", default=None)
    a2a_config: dict | None = Field(alias="a2aConfig", default=None)
    # Phase 1 Gap 1A — versioning. Caller can pin the slot this deploy lands
    # on; default is "production". Version_id is server-minted (we never trust
    # client-supplied ids) but ``description`` is captured for the version
    # history UI.
    deployment_slot: Literal["staging", "production"] | None = Field(alias="deploymentSlot", default="production")
    version_description: str | None = Field(alias="versionDescription", default=None, max_length=500)
    # Phase 2 (Loom) governance tagging. Caller supplies ad-hoc tag values
    # and/or selects a named tag profile; the deploy handler resolves them
    # against the org's tag policies (required-tag enforcement → HTTP 400) and
    # applies the resolved set to every AWS resource the deploy creates.
    resource_tags: dict | None = Field(alias="resourceTags", default=None)
    tag_profile: str | None = Field(alias="tagProfile", default=None, max_length=128)
    # Phase 7 (opt-in) deployment targets. Default None → deploy to the
    # platform's home account + region (unchanged). When multi-region/account is
    # enabled, targetAccountId routes the deploy through a cross-account
    # sts:AssumeRole and targetRegion selects an allowlisted region.
    target_account_id: str | None = Field(alias="targetAccountId", default=None, pattern=r"^\d{12}$")
    target_region: str | None = Field(alias="targetRegion", default=None, max_length=32)

    @model_validator(mode="after")
    def _check_kb_config(self) -> "DeployRequest":
        """Validate KB config at the API boundary so the user gets a 422
        instead of a deployment that goes 202 then dies mid-SFN with a Python
        ValueError. See tasks/lessons.md Bug 35.
        """
        kb = self.knowledge_base_config
        if not kb:
            return self
        kb_mode = (kb.get("kbMode") or kb.get("kb_mode") or "existing").lower()
        if kb_mode == "existing":
            kb_id = kb.get("knowledgeBaseId") or kb.get("knowledge_base_id") or ""
            if not kb_id.strip():
                raise ValueError(
                    "knowledgeBaseConfig.knowledgeBaseId is required when kbMode is 'existing'. "
                    "Either set kbMode='create_new' to create a new KB, or supply an existing KB ID."
                )
        elif kb_mode == "create_new":
            # Minimum viable create config: a data source pointer.
            ds_type = kb.get("dataSourceType") or kb.get("data_source_type") or ""
            if not ds_type:
                raise ValueError("knowledgeBaseConfig.dataSourceType is required when kbMode is 'create_new'.")
        else:
            raise ValueError(f"knowledgeBaseConfig.kbMode must be 'existing' or 'create_new', got '{kb_mode}'.")
        return self


class DeployResponse(BaseModel):
    """Response body for POST /api/deploy (202 Accepted)."""

    model_config = ConfigDict(populate_by_name=True)

    deployment_id: str = Field(alias="deploymentId")
    execution_arn: str | None = Field(alias="executionArn", default=None)
    status: DeploymentStatusEnum = DeploymentStatusEnum.PENDING
    message: str = "Deployment started"


class TestRequest(BaseModel):
    """Request body for POST /api/test-runtime."""

    model_config = ConfigDict(populate_by_name=True)

    endpoint: str | None = None
    input: str = Field(max_length=10000)
    simulated: bool = False
    runtime_id: str | None = Field(alias="runtimeId", default=None, max_length=256)
    session_id: str | None = Field(alias="sessionId", default=None, max_length=256)
    history: list | None = Field(default=None, max_length=50)


class TestResponse(BaseModel):
    """Response body for POST /api/test-runtime."""

    model_config = ConfigDict(populate_by_name=True)

    success: bool
    response: str | None = None
    error: str | None = None
    session_id: str | None = Field(alias="sessionId", default=None)
    request_id: str | None = Field(alias="requestId", default=None)
    arn: str | None = None
    logs: str | None = None


class DeleteResponse(BaseModel):
    """Response body for DELETE /api/runtime/{runtime_id}."""

    success: bool
    message: str
