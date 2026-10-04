"""Pydantic models for deployment state, runtime configuration, and API request/response types.

These models support the serverless deployment orchestration via Step Functions,
deployment state persistence in DynamoDB, and the Deployment Lambda API surface.
"""

import ipaddress
import urllib.parse
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .components import ConnectorConfig
from .template_composition import template_composition_refusal, template_implied_capabilities

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
            # Suggest IDs with THIS region's cross-region prefix — a `us.`
            # inference profile does not exist in eu-central-1, so a Frankfurt
            # deployment telling the user to type `us.…` sends them in circles.
            # Imported lazily: app.services.__init__ imports app.models, so a
            # module-level import here would be a cycle.
            from app.services.region_models import region_inference_prefix

            p = region_inference_prefix()
            raise ValueError(
                f"Bedrock model '{model_id}' is outside the supported window "
                f"(Oct 2025 – May 2026) and Bedrock flags it Legacy. "
                f"Use a current ID such as "
                f"{p}.anthropic.claude-sonnet-5, "
                f"{p}.anthropic.claude-opus-4-8, "
                f"or {p}.amazon.nova-2-lite-v1:0."
            )
    # Validator only runs when the caller sets model_provider="bedrock", so we
    # always require a known-active substring. Previously the regex gate let
    # non-prefixed bogus Bedrock-shaped IDs through (Bug 51).
    bedrock_like = any(s in model_id for s in _BEDROCK_ACTIVE_MODEL_SUBSTRINGS)
    if not bedrock_like:
        # Imported lazily for the same cycle reason as the Legacy branch above.
        from app.services.region_models import region_inference_prefix

        p = region_inference_prefix()
        # This string reaches the browser (error_details -> GET /api/deploy/{id} ->
        # useDeployment.ts). It used to end "add its substring to
        # _BEDROCK_ACTIVE_MODEL_SUBSTRINGS", naming a private module constant -- an
        # internal system component per ARCC cnt_94E30Xo4RZHtSJ, and useless to the
        # person reading it, who cannot edit our source. Suggest models they can pick
        # instead; the constant is discoverable from this file for the developer who
        # genuinely needs to extend it.
        raise ValueError(
            f"Bedrock model '{model_id}' is not one of the models this platform "
            f"supports. Choose one from the model list in the runtime configuration "
            f"panel, such as {p}.anthropic.claude-sonnet-5, "
            f"{p}.anthropic.claude-haiku-4-5-20251001-v1:0, "
            f"or {p}.amazon.nova-2-lite-v1:0."
        )


def _validate_provider_base_url(url: str) -> str:
    """Validate a customer-supplied model-provider base URL.

    ``provider_base_url`` is injected as ``PROVIDER_BASE_URL`` (see
    ``step_handlers/runtime_configure_step.py``) and is read only by the OpenAI
    and LiteLLM model inits — both of which send ``PROVIDER_API_KEY`` to it as a
    bearer credential. Until now the field had no validation beyond a 512-char
    cap, so a typo'd or hostile value silently became the destination of the
    customer's provider key.

    Deliberately NOT routed through ``gateway_deployer._validate_outbound_url``,
    even though that is this repo's SSRF guard for user-supplied URLs, because
    the two have different dialers. That guard resolves DNS and rejects every
    private CIDR, which is correct when the *control plane* fetches the URL. Here
    the fetcher is the AgentCore Runtime, which supports VPC egress via
    ``vpc_config`` — so a self-hosted LiteLLM or OpenAI-compatible proxy on a
    private address is the intended configuration for this field, and a
    private-CIDR denylist would reject the very setup it exists to serve. The
    checks below are therefore pure string validation with no network I/O, which
    also keeps them safe to run on every /api/deploy request.
    """
    candidate = url.strip()
    if not candidate:
        raise ValueError("providerBaseUrl was provided but is empty")
    # Control characters (notably \n) would be injected verbatim into the
    # runtime's environment, where a newline can forge a second variable.
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in candidate):
        raise ValueError("providerBaseUrl must not contain whitespace or control characters")

    parsed = urllib.parse.urlparse(candidate)
    # https only: the provider API key is transmitted to this host, so plaintext
    # http would leak it on the wire. Neither the OpenAI nor the LiteLLM init has
    # any http-only use case — `ollama` is the one provider that would want a
    # plaintext local host, and its init does not read PROVIDER_BASE_URL at all.
    if parsed.scheme != "https":
        raise ValueError(
            f"providerBaseUrl must use https (got '{parsed.scheme or 'no scheme'}') — the provider API key is sent to it"
        )
    if not parsed.hostname:
        raise ValueError(f"providerBaseUrl '{candidate}' has no host")
    # Credentials in the URL end up in logs and deployment records.
    if parsed.username or parsed.password:
        raise ValueError("providerBaseUrl must not embed credentials (user:pass@)")
    # Link-local covers the 169.254.169.254 instance-metadata endpoint. Only
    # literal IPs are checked — resolving names here is the DNS lookup this
    # validator deliberately avoids (see the docstring).
    try:
        ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        ip = None
    if ip is not None and ip.is_link_local:
        raise ValueError(f"providerBaseUrl must not point at a link-local address ({parsed.hostname})")

    return candidate


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
    #: F-55 item A. The SAVED FLOW this deployment belongs to, or absent when it belongs to none
    #: (a harness deploy, an unsaved canvas, or an adopted runtime -- which writes the explicit
    #: ``imported-<id>`` marker instead and is detected by that prefix).
    #:
    #: Optional where it used to be required, because it used to be populated with
    #: ``request.node_id`` -- a canvas NODE id -- which made every row claim a flow membership it
    #: could not have. Writing nothing is the honest representation of "no flow", and
    #: ``serialize_deployment_state`` uses ``exclude_none=True``, so such a row simply carries no
    #: ``workflow_id`` attribute and is therefore absent from ``workflow_id-index`` rather than
    #: indexed under a wrong key. No UI caller queries that GSI -- ``ActiveDeploymentBanner``
    #: calls ``/api/deployments?status=succeeded``, which goes down the ``user_id`` branch -- so
    #: correcting the semantic removes no working lookup.
    workflow_id: str | None = None
    #: Which canvas node was deployed. Recorded separately so that neither id has to stand in for
    #: the other, and so the two remain distinguishable in the persisted record.
    node_id: str | None = None
    user_id: str | None = None
    execution_arn: str | None = None
    status: DeploymentStatusEnum = DeploymentStatusEnum.PENDING
    current_step: DeploymentStepName | None = None
    started_at: datetime
    completed_at: datetime | None = None
    runtime_endpoint: str | None = None
    runtime_id: str | None = None
    # Public invocation contract. Legacy rows predate this field and therefore
    # default to HTTP; new deployments persist the validated protocol before
    # any asynchronous work begins.
    runtime_protocol: Literal["HTTP", "MCP", "A2A"] = "HTTP"
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
    # Full harness step result, for cleanup. Its `gateway_outbound_provider_name` is
    # what DELETE uses to tear down the harness->gateway OAuth2 credential provider.
    # destroy_harness also reconstructs that name deterministically, but only while the
    # harness itself still resolves; this persisted copy is the fallback when it does
    # not. It was previously never stored, which made that cleanup branch dead code.
    harness_result: dict | None = None
    deployment_mode: str | None = None  # "runtime" (default) | "harness"
    # True when /api/runtime/import ADOPTED an existing runtime instead of this
    # platform creating one. The delete path reads it to decide whether teardown may
    # destroy the AWS runtime at all: an adopted runtime is only destroyed when the
    # caller passes ?destroy=true. Records written before this field existed are
    # recognized by their ``imported-<id>`` workflow_id instead (see
    # deployment_handler._is_imported_record), so None here does not mean "ours".
    imported: bool | None = None
    error_details: str | None = None
    # Unix epoch used only for successfully deleted tombstones (30 days after
    # deletion). Live, failed, and retained records must remain durable because
    # they carry tenant authorization and safe-teardown authority.
    ttl: int | None = None
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
    # F-81: the sanitized user-facing name, which is the EXACT partition key of both
    # AgentVersionsTable and RuntimeSlotsTable -- the cross-tenant name lock (H-1).
    #
    # Two delete-path consumers already read ``friendly_runtime_name`` off the stored record,
    # and it was never a field, so both always got None and silently fell through to
    # ``node_id`` -- a raw canvas node id, which ``sanitize_runtime_name`` rewrites (a hyphen
    # becomes an underscore). The derived key therefore matched nothing, which made the name
    # release a no-op and, on the cross-account path, pre-empted the resolver that would have
    # got it right. Measured live 2026-09-24.
    #
    # Persisted explicitly rather than derived, because ``agentcore_runtime_name`` truncates the
    # friendly portion to 39 chars before appending the suffix: stripping the suffix recovers the
    # key only while the name is short enough, and nothing about the stored value says whether it
    # was cut.
    friendly_runtime_name: str | None = None
    # Generic teardown manifest: every deploy step appends the sub-resources it
    # creates here as {"type","id","region",...optional}. The delete path iterates
    # this to tear down EVERY created resource generically, instead of relying on
    # per-component *_result fields that a success-only step may never persist
    # (the root cause of orphan Bugs 154/158). Additive + idempotent: each entry
    # carries enough to delete it; the type-dispatched deleter no-ops on unknown
    # types so older records (no manifest) still fall back to *_result cleanup.
    created_resources: list[dict] | None = None
    # Manifest durability protocol. New deployments start incomplete, every
    # best-effort append marks manifest_error if DynamoDB rejects it, and only
    # the final status step may atomically mark the manifest complete alongside
    # SUCCEEDED. Legacy rows have None and therefore use both manifest and
    # live-ownership-gated fallback cleanup.
    resource_manifest_version: int | None = None
    resource_manifest_complete: bool | None = None
    resource_manifest_error: bool | None = None
    # Phase 7 (opt-in) — the account/region this deploy targeted (None → home).
    # Recorded so the SEPARATE delete request can assume the same cross-account
    # role to tear down, without the original SFN event.
    target_account_id: str | None = None
    target_region: str | None = None
    # The exact role used for this deployment. Kept internal because it exposes
    # an IAM principal, but persisted so a later delete does not depend on the
    # target registry still containing the same role mapping.
    target_role_arn: str | None = None
    # The exact target-account bucket validated at deploy admission. Persisted
    # for operator/audit continuity; codegen receives the same frozen value on
    # the SFN event rather than reconstructing a convention later.
    target_artifact_bucket: str | None = None
    # Async (slow-class) teardown tracking. KB-backed deletes exceed API
    # Gateway's 29s integration cap, so DELETE /api/runtime/{id} dispatches
    # them to a background self-invoke and the caller polls
    # GET /api/deploy/{deployment_id} for these fields.
    # "deleting" → "deleted" | "delete_retained" | "delete_failed";
    # delete_message carries the final cleanup summary (truncated to ~1KB).
    # delete_retained means the request completed safely but at least one
    # resource was deliberately kept because deletion authority was absent.
    delete_status: str | None = None
    delete_message: str | None = None
    # Epoch deadline on the atomic ``deleting`` claim. A Lambda can disappear
    # after acquiring the claim (timeout, process crash, async delivery
    # exhaustion); without a lease the row blocks every safe retry forever.
    # The lease exceeds the deployment Lambda's maximum runtime and is removed
    # on every terminal outcome.
    delete_claim_expires_at: int | None = None


#: Fields on :class:`DeploymentState` that are persisted but must NOT be serialized to a
#: caller.
#:
#: This exists because the API response **is** the storage model: every route that serves a
#: deployment record does ``state.model_dump(mode="json")``, so any field added here for the
#: platform's own bookkeeping becomes public the moment it is added, with nothing to object.
#:
#: Measured live, on ``GET /api/deploy/{id}`` against ``acfe2e-p0920`` after a real failed
#: deployment. The body carried::
#:
#:     "execution_arn": "arn:aws:states:us-east-1:123456789012:execution:
#:                       acfe2e-p0920-deployment:deploy-70e488d0-..."
#:
#: which names the platform's account, its state machine, and its region -- "Internal system
#: components" in ARCC ``cnt_94E30Xo4RZHtSJ``'s list of what a response must not contain. It
#: was the only 12-digit account id anywhere in the document, and it reached the browser on
#: three surfaces: this route, ``GET /api/deployments``, and the ``POST /api/deploy`` 202.
#:
#: Nothing consumed it. Not the frontend (zero references), not the backend outside the two
#: lines that write it. The argument in ``error_sanitizer._redact_principals`` applies here
#: verbatim: the execution is the platform's, never the caller's, and no caller holds IAM
#: permission on it, so disclosing it cannot help them fix anything.
#:
#: Kept in the stored record deliberately -- it is the handle an operator needs to find the
#: execution in the console, and ``_update_execution_arn`` writes it for exactly that.
INTERNAL_ONLY_STATE_FIELDS: frozenset[str] = frozenset(
    {
        "execution_arn",
        "friendly_runtime_name",
        "resource_manifest_complete",
        "resource_manifest_error",
        "resource_manifest_version",
        "target_role_arn",
        "target_artifact_bucket",
        "delete_claim_expires_at",
    }
)


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
    # Optional at the type level so the standalone, model-free FastMCP runtime
    # (templateId 'mcp-server-runtime', protocol 'MCP') can be deployed with no
    # model at all — a protocol tool server has no model loop. Every OTHER
    # runtime still requires a model; DeployRequest._mcp_protocol_admission
    # enforces that, and rejects an EXPLICIT model on the MCP template rather
    # than accepting it and silently ignoring it.
    model: dict | None = None
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

    @field_validator("provider_base_url")
    @classmethod
    def _check_provider_base_url(cls, v: str | None) -> str | None:
        return None if v is None else _validate_provider_base_url(v)

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
        # A model-free runtime (the standalone FastMCP server) carries no model;
        # there is nothing to validate, and DeployRequest enforces that only the
        # dedicated MCP template may omit it.
        if self.model_provider == "bedrock" and self.model is not None:
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

    enabled: bool = Field(alias="enableOtel", default=True)
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


class CfnNamingProfile(BaseModel):
    """Generation-time naming rules for a CloudFormation export.

    A Python callback cannot cross the JSON API boundary, so the public equivalent
    is a small declarative profile: a common prefix plus optional templates for
    individual resource families. Templates are validated and expanded by the CFN
    generator; unknown keys/placeholders are refused rather than ignored.

    ``prefix`` is deliberately lowercase alphanumeric. It is reused in Cognito
    hosted-domain names, whose grammar is stricter than IAM/Lambda naming, and one
    value that is legal everywhere is safer than silently normalising it differently
    for each service.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    prefix: str = Field(
        min_length=1,
        max_length=12,
        pattern=r"^[a-z][a-z0-9]{0,11}$",
        description="Customer naming prefix shared by exported resources, for example 'ecb'.",
    )
    resource_names: dict[str, str] = Field(
        alias="resourceNames",
        default_factory=dict,
        description=(
            "Optional per-family templates using {prefix}, {deployment}, "
            "{component}, and (for stack-unique names such as domains, vector "
            "buckets, and IAM roles) {suffix}."
        ),
    )

    @field_validator("resource_names")
    @classmethod
    def _validate_resource_name_templates(cls, value: dict[str, str]) -> dict[str, str]:
        # There are already 35 supported families. Keep room for a complete profile
        # and modest contract growth without making this an unbounded request surface.
        if len(value) > 64:
            raise ValueError("namingProfile.resourceNames accepts at most 64 overrides")
        for key, template in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ValueError("namingProfile.resourceNames keys must be 1-64 character strings")
            if not isinstance(template, str) or not template or len(template) > 160:
                raise ValueError(
                    f"namingProfile.resourceNames.{key} must be a non-empty string of at most 160 characters"
                )
            if any(ord(char) < 32 or ord(char) > 126 for char in template):
                raise ValueError(f"namingProfile.resourceNames.{key} must contain printable ASCII characters only")
        return value


class DeployRequest(BaseModel):
    """Request body for POST /api/deploy, /api/generate-cfn-template and /api/export-python.

    ``extra="forbid"`` because the default silently discarded the key. Found live: an export
    requested with ``deletionPolicy: "Delete"`` — the wrong spelling of
    ``dataRetentionPolicy`` — returned HTTP 200 and a bundle whose data-bearing resources
    were all ``Retain``, with nothing anywhere saying the request had been ignored. The
    caller's next move is to delete the stack and discover the Knowledge Base, the Cognito
    pool and the conversation Memory are still billing; there is no recovery step that
    tells them why. A 422 naming the key they got wrong is the whole remedy.

    Both callers send only declared aliases, with one exception the ``mode="before"``
    validator below absorbs: the deploy panel sends ``deployment_mode`` *and*
    ``deploymentMode``. ``populate_by_name`` accepts either, but not both at once under
    ``forbid`` — the second one is "extra" — so without that shim this change would have
    422'd every deploy from the UI. Verified against the payload the panel actually builds
    rather than assumed.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _collapse_duplicate_alias_spellings(cls, data):
        """Drop a snake_case key that duplicates its own camelCase alias, if they agree.

        ``populate_by_name`` invites a caller to send either spelling, and a caller hedging
        by sending both was harmless until ``extra="forbid"``. Dropping the redundant one
        keeps every existing client working; the point of ``forbid`` is to catch a key that
        means nothing to this model, and a second spelling of a field it has is not that.

        If the two DISAGREE the request is not hedging, it is contradictory, and guessing
        which one the caller meant is how a deploy lands in the wrong mode. That raises.
        """
        if not isinstance(data, dict):
            return data
        for name, field in cls.model_fields.items():
            alias = field.alias
            if not alias or alias == name or name not in data or alias not in data:
                continue
            if data[name] != data[alias]:
                raise ValueError(
                    f"'{name}' and '{alias}' are two spellings of the same field and were sent "
                    f"with different values ({data[name]!r} vs {data[alias]!r}). Send one."
                )
            data = {k: v for k, v in data.items() if k != name}
        return data

    node_id: str = Field(alias="nodeId", max_length=256, pattern=r"^[a-zA-Z0-9_-]+$")
    #: F-55 item A. Which SAVED FLOW this deploy belongs to, if any. Distinct from ``node_id``,
    #: which says which node of a canvas is being deployed and is not an identity for the canvas.
    #:
    #: Persisting ``node_id`` as the deployment's ``workflow_id`` conflated the two, and the
    #: conflation was not cosmetic: it was why the ValidateWorkflow lookup could never succeed
    #: (it searched the ``workflows`` table for a node id) and why two deploys of two different
    #: canvases that happen to share a node id are indistinguishable in the deployment table.
    #:
    #: Optional, and optional on purpose rather than for compatibility. A harness deployment has
    #: no canvas at all, and a canvas that has never been saved has no flow to reference; both
    #: must remain deployable. Absent means "this deployment belongs to no flow", which is a fact
    #: worth recording accurately -- NOT an invitation to substitute the node id. The length and
    #: charset match ``routers/flows.py::_validate_flow_id`` so a value accepted here is one the
    #: flows API would also accept.
    flow_id: str | None = Field(alias="flowId", default=None, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
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
    # P0-B. The governance state the caller resolved ITS values against: the
    # ``sha256:<hex>`` policy revision (frontend computeTagPolicyRevision, backend
    # tag_policy_store.compute_policy_revision -- one algorithm, two runtimes) and the
    # selected profile's ``updated_at``. Supplied → re-checked server-side against a fresh
    # read before any AWS side effect, and a mismatch refuses the deploy (HTTP 409) instead
    # of applying values an admin has since changed. Required whenever resource_tags or
    # tag_profile is present, so "governed" can never degrade to "whatever the client sent".
    policy_revision: str | None = Field(alias="policyRevision", default=None, max_length=128)
    tag_profile_updated_at: str | None = Field(alias="tagProfileUpdatedAt", default=None, max_length=64)
    # CloudFormation export only. A JSON-safe equivalent of a pluggable naming
    # function: one customer prefix plus optional per-resource-family templates.
    # The live Step Functions path keeps its existing service-managed names.
    naming_profile: CfnNamingProfile | None = Field(alias="namingProfile", default=None)
    # Phase 7 (opt-in) deployment targets. Default None → deploy to the
    # platform's home account + region (unchanged). When multi-region/account is
    # enabled, targetAccountId routes the deploy through a cross-account
    # sts:AssumeRole and targetRegion selects an allowlisted region.
    target_account_id: str | None = Field(alias="targetAccountId", default=None, pattern=r"^\d{12}$")
    target_region: str | None = Field(alias="targetRegion", default=None, max_length=32)
    # CloudFormation export only. Sets DeletionPolicy/UpdateReplacePolicy on the
    # data-bearing resources of the exported template (Cognito user pools, the
    # Knowledge Base and its data source, AgentCore Memory). "Retain" is the
    # default because the alternative is that one `cfn delete-stack` or
    # `terraform destroy` silently takes user identities and conversation history
    # with it. "Delete" is for throwaway demo stacks that should tear down clean.
    #
    # Deliberately a generation-time choice, not a template Parameter:
    # DeletionPolicy and UpdateReplacePolicy are CloudFormation *attributes* and
    # accept only literal values, so `{"Ref": ...}` is not valid there. The value
    # has to be baked into the YAML when it is generated.
    #
    # The default is None, NOT "Retain", even though "Retain" is the effective default. The
    # distinction is between "the caller did not ask" and "the caller asked for Retain", and it
    # matters because this field is CFN-only: the live-deploy and Python-export routes cannot
    # honour it, so they must refuse it rather than silently drop it (the same silent-drop defect
    # ``extra="forbid"`` exists to close, and the same reason ``namingProfile`` is refused there).
    # A guard keyed on ``is not None`` over a field defaulting to "Retain" would reject EVERY
    # request including the UI's, so the effective default is applied at the one place that can
    # act on it -- ``cfn_template_generator`` reads ``request.data_retention_policy or "Retain"``
    # -- and absence stays distinguishable here.
    data_retention_policy: Literal["Retain", "Delete"] | None = Field(alias="dataRetentionPolicy", default=None)

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

    @model_validator(mode="after")
    def _mcp_protocol_admission(self) -> "DeployRequest":
        """Admit ``protocol=MCP`` only for the dedicated FastMCP template.

        An MCP control-plane protocol cannot make an ordinary HTTP
        ``BedrockAgentCoreApp`` artifact speak MCP — the two generate different
        source. So ``protocol='MCP'`` is valid ONLY for templateId
        ``'mcp-server-runtime'`` (the model-free FastMCP tool server), and it is
        refused here, at the request boundary, before any deployment row,
        credential staging, resource grant, or Step Functions execution.

        That template is a tool server, not a conversational agent, so its
        model-only settings (``model``, ``modelProvider``, ``providerApiKeyRef``,
        ``providerBaseUrl``, ``systemPrompt``, ``framework``) must be ABSENT
        rather than accepted and silently ignored — an explicitly supplied one
        is a 422 naming exactly which field to remove. Every OTHER runtime still
        requires a ``model``; that requirement moved here when ``model`` became
        optional at the type level so the MCP template could omit it.

        Harness deployments have no generated runtime artifact and are skipped.
        """
        if (self.deployment_mode or "runtime") == "harness":
            return self

        is_mcp_template = self.template_id == "mcp-server-runtime"

        if self.config.protocol == "MCP" and not is_mcp_template:
            raise ValueError(
                "config.protocol='MCP' is only valid for templateId "
                f"'mcp-server-runtime' (the dedicated model-free FastMCP tool "
                f"server); got templateId {self.template_id!r}. No other template "
                "can be made to speak the MCP protocol, so this is refused before "
                "any deployment side effect rather than deployed as an HTTP agent."
            )

        if is_mcp_template:
            # Reject EXPLICITLY-supplied model-only fields. These all carry
            # defaults, so presence — not truthiness — is the test: model_fields_set
            # is exactly the set the caller sent. Naming the offending alias makes
            # the 422 actionable.
            explicit = self.config.model_fields_set
            offenders = [
                alias
                for pyname, alias in (
                    ("model", "model"),
                    ("model_provider", "modelProvider"),
                    ("provider_api_key_ref", "providerApiKeyRef"),
                    ("provider_base_url", "providerBaseUrl"),
                    ("system_prompt", "systemPrompt"),
                    ("framework", "framework"),
                )
                if pyname in explicit
            ]
            if offenders:
                raise ValueError(
                    "templateId 'mcp-server-runtime' is a standalone model-free "
                    "FastMCP tool server and has no model loop; it cannot honour "
                    f"explicit {', '.join(offenders)}. Remove "
                    f"{'these' if len(offenders) > 1 else 'this'} model-only "
                    "field; none will be silently ignored."
                )
        elif self.config.model is None:
            # Every conversational/HTTP/A2A runtime needs a model.
            raise ValueError(
                "config.model is required for this runtime; only the standalone "
                "model-free FastMCP template ('mcp-server-runtime') may omit it."
            )

        return self

    @model_validator(mode="after")
    def _check_codegen_provider_compatibility(self) -> "DeployRequest":
        """Fail at the API boundary instead of silently substituting Bedrock.

        Most generated Strands patterns are provider-aware. Two legacy
        templates still call Bedrock Converse directly, and Bedrock Guardrails
        cannot enforce policy on a third-party model. Those combinations must
        be explicit 422s before a deployment record or Step Functions execution
        is created.
        """
        if (self.deployment_mode or "runtime") == "harness":
            return self
        provider = self.config.model_provider or "bedrock"
        if self.template_id == "mcp-server-runtime":
            if provider != "bedrock":
                raise ValueError(
                    "templateId 'mcp-server-runtime' is a model-free MCP tool "
                    f"server and cannot honour modelProvider='{provider}'; remove "
                    "the provider configuration rather than having it silently ignored"
                )
            return self
        if provider == "bedrock":
            return self
        if self.template_id == "web-search-agent":
            raise ValueError(
                f"templateId '{self.template_id}' currently requires modelProvider='bedrock'; "
                f"it cannot honour modelProvider='{provider}' and will not silently substitute Bedrock"
            )
        tools = set(self.connected_tools or [])
        if self.guardrails_config or "guardrails" in tools:
            raise ValueError(
                "Bedrock Guardrails require modelProvider='bedrock'; remove the Guardrails "
                f"node or change the provider from '{provider}'"
            )
        return self

    @model_validator(mode="after")
    def _check_standalone_mcp_contract(self) -> "DeployRequest":
        """Keep the standalone MCP template honest before any deployment write.

        The generated artifact exposes three tools and has no model loop,
        Gateway consumer, evaluator, or HITL continuation. Every incompatible
        field is refused here rather than provisioned and silently dropped.
        Platform-level observability defaults are checked by the deploy handler
        because they are server state and are not present on this model.
        """

        if (self.deployment_mode or "runtime") == "harness" or self.template_id != "mcp-server-runtime":
            return self

        if self.config.protocol != "MCP":
            raise ValueError(
                "templateId 'mcp-server-runtime' requires config.protocol='MCP'; "
                "it cannot be deployed through the HTTP agent contract"
            )

        conflicts: list[str] = []
        tools = {
            {
                "knowledgeBase": "knowledge_base",
                "knowledge-base": "knowledge_base",
                "codeInterpreter": "code_interpreter",
                "code-interpreter": "code_interpreter",
            }.get(tool, tool)
            for tool in (self.connected_tools or [])
            if isinstance(tool, str)
        }
        display_tools = {
            "memory": "Memory",
            "gateway": "Gateway",
            "browser": "Browser",
            "code_interpreter": "Code Interpreter",
            "knowledge_base": "Knowledge Base",
            "guardrails": "Guardrails",
            "hitl": "human approval",
            "observability": "generic agent observability",
            "a2a": "A2A",
        }
        conflicts.extend(display_tools[tool] for tool in sorted(tools & display_tools.keys()))

        if self.memory_config and self.memory_config.get("enabled", True) is not False:
            conflicts.append("Memory")
        if self.gateway_config is not None or self.gateway_tools:
            conflicts.append("Gateway")
        if self.custom_tools:
            conflicts.append("custom Gateway tools")
        if self.connectors:
            conflicts.append("connectors")
        if self.external_mcp_servers:
            conflicts.append("external MCP servers")
        if self.knowledge_base_config is not None:
            conflicts.append("Knowledge Base")
        if self.guardrails_config is not None:
            conflicts.append("Guardrails")
        if self.evaluation_config is not None:
            conflicts.append("evaluationConfig")
        if self.policy_config is not None:
            conflicts.append("policyConfig")
        if self.mcp_server_config is not None:
            conflicts.append("mcpServerConfig")
        if self.a2a_config is not None:
            conflicts.append("A2A")
        if (self.config.multi_agent_pattern or "none") != "none":
            conflicts.append(f"multi-agent {self.config.multi_agent_pattern}")
        if self.config.provider_api_key_ref:
            conflicts.append("providerApiKeyRef")
        if self.config.provider_base_url:
            conflicts.append("providerBaseUrl")
        if self.config.enable_otel or self.config.observability is not None or self.observability_config is not None:
            conflicts.append("generic agent observability")

        if conflicts:
            names = ", ".join(dict.fromkeys(conflicts))
            raise ValueError(
                "templateId 'mcp-server-runtime' is a standalone MCP tool "
                f"server and cannot honour: {names}. Remove those settings or "
                "use a conversational/Gateway template; none will be silently omitted."
            )
        return self

    @model_validator(mode="after")
    def _check_codegen_component_composition(self) -> "DeployRequest":
        """Refuse combinations that code generation cannot faithfully compose.

        The deployment state machine creates Gateway, Knowledge Base, and Memory
        resources before its code-generation step. A late codegen refusal can
        therefore leave billable resources behind, while an early-returning
        generator can report success after silently omitting a connected
        capability. Keep the admission decision at the request boundary.

        Single-agent Memory/Gateway/Browser/Code Interpreter/Knowledge Base
        combinations are intentionally *not* rejected here: those are feasible
        compositions and are implemented by the code generator. A2A and the
        multi-agent graph/swarm/workflow generators are separate runtime shapes
        whose tool-assignment semantics are not defined yet, so mixing them with
        those capabilities must be explicit rather than lossy.
        """
        if (self.deployment_mode or "runtime") == "harness":
            return self

        aliases = {
            "knowledgeBase": "knowledge_base",
            "knowledge-base": "knowledge_base",
            "codeInterpreter": "code_interpreter",
            "code-interpreter": "code_interpreter",
        }
        capabilities = {aliases.get(tool, tool) for tool in (self.connected_tools or []) if isinstance(tool, str)}

        if self.memory_config and self.memory_config.get("enabled", True) is not False:
            capabilities.add("memory")
        if self.knowledge_base_config:
            capabilities.add("knowledge_base")
        if (
            self.gateway_config
            or self.gateway_tools
            or self.custom_tools
            or self.connectors
            or self.external_mcp_servers
        ):
            capabilities.add("gateway")

        template_implied = template_implied_capabilities(self.template_id)
        if "memory" in template_implied and self.memory_config and self.memory_config.get("enabled", True) is False:
            raise ValueError(
                f"Template {self.template_id!r} generates a Memory agent, so memoryConfig.enabled "
                "cannot be false: the runtime would ship with no Memory to read. Enable Memory "
                "or start from a different template."
            )
        capabilities |= template_implied

        a2a_enabled = self.config.protocol == "A2A" or bool(self.a2a_config) or "a2a" in capabilities
        if a2a_enabled:
            capabilities.add("a2a")

        multi_agent_pattern = self.config.multi_agent_pattern or "none"
        multi_agent_enabled = multi_agent_pattern != "none"

        def _display(values: set[str]) -> str:
            return ", ".join(sorted(value.replace("_", " ") for value in values))

        non_a2a = capabilities - {"a2a"}
        if a2a_enabled and (non_a2a or multi_agent_enabled):
            details = set(capabilities)
            if multi_agent_enabled:
                details.add(f"multi-agent {multi_agent_pattern}")
            raise ValueError(
                "A2A code generation cannot currently compose with the other "
                f"requested capabilities ({_display(details)}). Disconnect those "
                "capabilities or use a separate runtime; none will be silently omitted."
            )

        if multi_agent_enabled and capabilities:
            raise ValueError(
                f"The multi-agent {multi_agent_pattern} generator cannot currently "
                "assign the connected capabilities to individual agents "
                f"({_display(capabilities)}). Disconnect them or use a single-agent "
                "runtime; none will be silently omitted."
            )

        template_refusal = template_composition_refusal(self.template_id, capabilities)
        if template_refusal:
            raise ValueError(template_refusal)

        return self


class DeployResponse(BaseModel):
    """Response body for POST /api/deploy (202 Accepted)."""

    model_config = ConfigDict(populate_by_name=True)

    deployment_id: str = Field(alias="deploymentId")
    # No ``execution_arn`` here. It used to be returned on this 202 and it names the
    # platform's account, state machine and region -- see INTERNAL_ONLY_STATE_FIELDS above.
    # The caller polls GET /api/deploy/{deployment_id}; the deployment id is the only handle
    # they need and the only one they are authorized to use.
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
    # Set only by the deploy-time warmup ping. A generated Memory agent returns before
    # Memory and the model, so the ping is not recorded as a turn in the owner's stream.
    warmup: bool = False


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
    # W3C trace id sent on InvokeHarness (harness mode). Lets a caller jump from
    # a test turn to the harness + Memory spans in aws/spans.
    trace_id: str | None = Field(alias="traceId", default=None)
    # Tool calls the agent's own loop executed this turn (name, status, argument
    # digests); None when the runtime reported none. runtime_invocation.parse_tool_receipts.
    tool_receipts: list[dict[str, Any]] | None = Field(alias="toolReceipts", default=None)


class DeleteResponse(BaseModel):
    """Response body for DELETE /api/runtime/{runtime_id}."""

    success: bool
    message: str
    # True only when retention/protection was the sole reason success is false.
    # This lets the dispatcher persist delete_retained rather than conflating a
    # safety refusal with an operational cleanup failure.
    retained: bool = False
    # True when nothing was refused or failed, but a delete the service accepted had
    # not finished inside this invocation's confirmation budget (a Memory still
    # DELETING, and the role kept for it). The async teardown then confirms again in a
    # later invocation instead of recording a retention the user would retry by hand.
    confirmation_pending: bool = False
