"""Pydantic models for AgentCore component configurations."""

import re
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from .enums import (
    A2ACommunicationPattern,
    AgentFramework,
    AgentServerProtocol,
    DeploymentType,
    EvaluatorType,
    ExtractionStrategy,
    GatewayTargetType,
    MultiAgentPattern,
    OAuth2Provider,
    PolicyEffect,
    PythonRuntime,
    StrandsModelProvider,
)


# ============================================================================
# Runtime Configuration Models
# ============================================================================


class ModelConfiguration(BaseModel):
    """Model configuration for AI agents."""

    provider: StrandsModelProvider = StrandsModelProvider.BEDROCK
    model_id: str = Field(min_length=1, max_length=200)
    temperature: float = Field(ge=0.0, le=2.0, default=0.7)
    top_p: float = Field(ge=0.0, le=1.0, default=0.9)


class VPCConfiguration(BaseModel):
    """VPC configuration for AgentCore resources."""

    subnet_ids: list[str] = Field(min_length=1)
    security_group_ids: list[str] = Field(min_length=1)


class RuntimeConfiguration(BaseModel):
    """Configuration for AgentCore Runtime component.

    Aligns with agentcore CLI configure command options.
    """

    component_type: Literal["runtime"] = "runtime"
    name: str = Field(min_length=1, max_length=100)
    entrypoint: str = Field(min_length=1, max_length=255, default="agent.py")
    framework: AgentFramework = AgentFramework.STRANDS_AGENTS
    model: ModelConfiguration
    system_prompt: str = Field(max_length=100000)
    deployment_type: DeploymentType = DeploymentType.DIRECT_CODE_DEPLOY
    python_runtime: PythonRuntime = PythonRuntime.PYTHON_3_13
    protocol: AgentServerProtocol = AgentServerProtocol.HTTP

    # Model provider (Strands)
    model_provider: StrandsModelProvider = StrandsModelProvider.BEDROCK
    provider_api_key_ref: Optional[str] = None  # Secrets Manager ARN for non-Bedrock

    # Multi-agent pattern
    multi_agent_pattern: MultiAgentPattern = MultiAgentPattern.NONE
    multi_agent_config: Optional[dict] = None

    # Timeouts
    idle_timeout: int = Field(ge=60, le=28800, default=900)
    max_lifetime: int = Field(ge=60, le=28800, default=28800)

    # VPC (optional)
    vpc_config: Optional[VPCConfiguration] = None

    # Observability
    enable_otel: bool = False

    # IAM (optional - auto-created if not provided)
    execution_role_arn: Optional[str] = None


# ============================================================================
# Gateway Configuration Models
# ============================================================================


class OpenAPITargetConfig(BaseModel):
    """OpenAPI target configuration."""

    type: Literal["openapi"] = "openapi"
    spec_url: Optional[str] = None
    spec_content: Optional[str] = None

    @model_validator(mode="after")
    def validate_spec_source(self) -> "OpenAPITargetConfig":
        """Ensure at least one spec source is provided."""
        if not self.spec_url and not self.spec_content:
            raise ValueError("Either spec_url or spec_content must be provided")
        return self


class LambdaTargetConfig(BaseModel):
    """Lambda target configuration."""

    type: Literal["lambda"] = "lambda"
    function_arn: Optional[str] = None  # Optional - auto-created if not provided

    @field_validator("function_arn")
    @classmethod
    def validate_lambda_arn(cls, v: Optional[str]) -> Optional[str]:
        """Validate Lambda ARN format if provided."""
        if v is None:
            return v
        pattern = r"^arn:aws:lambda:[a-z]{2}-[a-z]+-\d:\d{12}:function:[a-zA-Z0-9_-]+$"
        if not re.match(pattern, v):
            raise ValueError("Invalid Lambda ARN format. Expected: arn:aws:lambda:<region>:<account>:function:<name>")
        return v


class SmithyTargetConfig(BaseModel):
    """Smithy model target configuration."""

    type: Literal["smithy"] = "smithy"
    model_name: str = Field(default="dynamodb")


class MCPServerTargetConfig(BaseModel):
    """MCP Server target configuration."""

    type: Literal["mcp_server"] = "mcp_server"
    server_url: str = Field(min_length=1)


GatewayTargetConfig = Annotated[
    Union[
        OpenAPITargetConfig,
        LambdaTargetConfig,
        SmithyTargetConfig,
        MCPServerTargetConfig,
    ],
    Field(discriminator="type"),
]


class APIKeyCredentials(BaseModel):
    """API key credentials for gateway targets."""

    api_key: str = Field(min_length=1)
    credential_location: Literal["header", "query"] = "header"
    credential_parameter_name: str = Field(default="X-API-Key")


class OAuth2Credentials(BaseModel):
    """OAuth2 credentials for gateway targets."""

    client_id: str = Field(min_length=1)
    client_secret_ref: str = Field(min_length=1)  # Reference to Secrets Manager
    discovery_url: str = Field(min_length=1)
    scopes: list[str] = Field(default_factory=list)


class GatewayConfiguration(BaseModel):
    """Configuration for AgentCore Gateway component.

    Aligns with agentcore gateway CLI commands.
    """

    component_type: Literal["gateway"] = "gateway"
    name: str = Field(min_length=1, max_length=100)
    target_type: GatewayTargetType
    target_config: GatewayTargetConfig
    enable_semantic_search: bool = True

    # Credentials (optional, for OpenAPI targets)
    api_key_credentials: Optional[APIKeyCredentials] = None
    oauth2_credentials: Optional[OAuth2Credentials] = None

    # IAM (optional - auto-created if not provided)
    role_arn: Optional[str] = None

    @model_validator(mode="after")
    def validate_target_config_type(self) -> "GatewayConfiguration":
        """Ensure target_config type matches target_type. Reject target types
        whose plumbing isn't implemented yet so callers get a clean 422
        instead of a silently-fallback deploy. See tasks/lessons.md Bug 106.
        """
        type_mapping = {
            GatewayTargetType.OPENAPI: "openapi",
            GatewayTargetType.LAMBDA: "lambda",
            GatewayTargetType.SMITHY: "smithy",
        }
        # Unsupported types — reject loudly at the API boundary.
        unsupported = {
            GatewayTargetType.API_GATEWAY: (
                "api_gateway target type is not yet implemented. "
                "Use 'lambda' or 'openapi' for now, or wire your API GW REST stage "
                "through an OpenAPI export."
            ),
            GatewayTargetType.PREBUILT: (
                "prebuilt target type is not yet implemented. "
                "Wire the underlying OpenAPI/Lambda manually until this lands."
            ),
        }
        if self.target_type in unsupported:
            raise ValueError(unsupported[self.target_type])
        expected_type = type_mapping.get(self.target_type)
        if expected_type and self.target_config.type != expected_type:
            raise ValueError(
                f"target_config type '{self.target_config.type}' does not match target_type '{self.target_type.value}'"
            )
        return self


# ============================================================================
# Memory Configuration Models
# ============================================================================


class MemoryConfiguration(BaseModel):
    """Configuration for AgentCore Memory component."""

    component_type: Literal["memory"] = "memory"
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    # Memory is typically configured at runtime level


# ============================================================================
# Code Interpreter Configuration Models
# ============================================================================


class CodeInterpreterConfiguration(BaseModel):
    """Configuration for AgentCore Code Interpreter component."""

    component_type: Literal["code_interpreter"] = "code_interpreter"
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    # Code interpreter is a built-in tool


# ============================================================================
# Browser Configuration Models
# ============================================================================


class BrowserConfiguration(BaseModel):
    """Configuration for AgentCore Browser component."""

    component_type: Literal["browser"] = "browser"
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    # Browser is a built-in tool


# ============================================================================
# Guardrails Configuration Models
# ============================================================================


class GuardrailsConfiguration(BaseModel):
    """Configuration for Amazon Bedrock Guardrails component."""

    component_type: Literal["guardrails"] = "guardrails"
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    mode: str = "existing"  # "existing" or "create_new"
    guardrail_id: Optional[str] = None
    guardrail_version: Optional[str] = None
    # Create-new mode fields
    content_filters: Optional[dict] = None  # {hate: "MEDIUM", violence: "HIGH", ...}
    pii_filters: Optional[list] = None  # [{type: "EMAIL", action: "ANONYMIZE"}, ...]
    denied_topics: Optional[list] = None  # [{name: str, definition: str}, ...]
    word_filters: Optional[list] = None  # ["word1", "word2"]


# ============================================================================
# Observability Configuration Models
# ============================================================================


class ObservabilityConfiguration(BaseModel):
    """Configuration for AgentCore Observability component.

    Supports the AgentCore-native CloudWatch sidecar, Langfuse, and any
    OTLP-compatible backend via the ``custom`` provider.
    """

    component_type: Literal["observability"] = "observability"
    name: str = Field(min_length=1, max_length=100)
    enable_otel: bool = False

    # OTLP backend selection
    provider: Literal[
        "langfuse",
        "custom",
    ] = "langfuse"
    otlp_endpoint: Optional[str] = Field(default=None, max_length=2048)
    otlp_protocol: Literal["http/protobuf", "grpc"] = "http/protobuf"
    service_name: Optional[str] = Field(default=None, max_length=200)
    sample_rate: float = Field(ge=0.0, le=1.0, default=1.0)
    resource_attributes: dict[str, str] = Field(default_factory=dict)

    # Auth: Secrets Manager ARN holding the OTEL_EXPORTER_OTLP_HEADERS string
    auth_header_secret_arn: Optional[str] = Field(default=None, max_length=2048)
    extra_headers: dict[str, str] = Field(default_factory=dict)


# ============================================================================
# Identity Configuration Models
# ============================================================================


class CustomOAuth2Config(BaseModel):
    """Custom OAuth2 provider configuration."""

    authorization_url: str = Field(min_length=1)
    token_url: str = Field(min_length=1)
    user_info_url: Optional[str] = None


class OAuth2Configuration(BaseModel):
    """OAuth2 credential configuration."""

    provider: OAuth2Provider
    client_id: str = Field(min_length=1)
    client_secret_ref: str = Field(min_length=1)  # Reference to Secrets Manager
    scopes: list[str] = Field(default_factory=list)
    custom_config: Optional[CustomOAuth2Config] = None

    @model_validator(mode="after")
    def validate_custom_config(self) -> "OAuth2Configuration":
        """Ensure custom_config is provided when provider is 'custom'."""
        if self.provider == OAuth2Provider.CUSTOM and self.custom_config is None:
            raise ValueError("custom_config is required when provider is 'custom'")
        return self


class APIKeyConfiguration(BaseModel):
    """API key credential configuration."""

    key_name: str = Field(min_length=1, max_length=100)
    key_value_ref: str = Field(min_length=1)  # Reference to Secrets Manager
    header_name: str = Field(min_length=1, max_length=100)


class IdentityConfiguration(BaseModel):
    """Configuration for AgentCore Identity component."""

    component_type: Literal["identity"] = "identity"
    name: str = Field(min_length=1, max_length=100)
    credential_type: Literal["oauth2", "api_key"]
    oauth2_config: Optional[OAuth2Configuration] = None
    api_key_config: Optional[APIKeyConfiguration] = None

    @model_validator(mode="after")
    def validate_credential_config(self) -> "IdentityConfiguration":
        """Ensure appropriate config is provided based on credential_type."""
        if self.credential_type == "oauth2" and self.oauth2_config is None:
            raise ValueError("oauth2_config is required when credential_type is 'oauth2'")
        if self.credential_type == "api_key" and self.api_key_config is None:
            raise ValueError("api_key_config is required when credential_type is 'api_key'")
        return self


# ============================================================================
# Evaluation Configuration Models
# ============================================================================


class CustomEvaluatorConfig(BaseModel):
    """Custom evaluator configuration."""

    evaluator_name: str = Field(min_length=1, max_length=100)
    evaluator_code: str = Field(min_length=1)  # Python code for custom evaluator
    description: Optional[str] = None


class EvaluatorConfig(BaseModel):
    """Individual evaluator configuration."""

    evaluator_type: EvaluatorType
    enabled: bool = True
    threshold: float = Field(ge=0.0, le=1.0, default=0.7)
    custom_config: Optional[CustomEvaluatorConfig] = None

    @model_validator(mode="after")
    def validate_custom_evaluator(self) -> "EvaluatorConfig":
        """Ensure custom_config is provided when type is CUSTOM."""
        if self.evaluator_type == EvaluatorType.CUSTOM and self.custom_config is None:
            raise ValueError("custom_config is required when evaluator_type is 'custom'")
        return self


class EvaluationConfiguration(BaseModel):
    """Configuration for AgentCore Evaluation component.

    Supports both on-demand evaluation during development and
    continuous monitoring in production with automatic sampling.
    """

    component_type: Literal["evaluation"] = "evaluation"
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True

    # Evaluators to use
    evaluators: list[EvaluatorConfig] = Field(default_factory=list)

    # Evaluation mode
    mode: Literal["on_demand", "continuous"] = "on_demand"

    # Sampling rate for continuous mode (0.0 to 1.0)
    sampling_rate: float = Field(ge=0.0, le=1.0, default=0.1)

    # CloudWatch dashboard integration
    enable_dashboard: bool = True

    # Extraction strategy for evaluation data
    extraction_strategy: ExtractionStrategy = ExtractionStrategy.SEMANTIC


# ============================================================================
# Policy Configuration Models (Cedar-based)
# ============================================================================


class PolicyCondition(BaseModel):
    """Cedar policy condition."""

    attribute: str = Field(min_length=1)
    operator: Literal["==", "!=", "<", ">", "<=", ">=", "in", "contains"] = "=="
    value: str = Field(min_length=1)


class PolicyRule(BaseModel):
    """Individual Cedar policy rule."""

    rule_id: str = Field(min_length=1, max_length=100)
    effect: PolicyEffect
    principal: Optional[str] = None  # e.g., "User::\"admin\"" or "*"
    action: Optional[str] = None  # e.g., "Action::\"invoke_tool\""
    resource: Optional[str] = None  # e.g., "Tool::\"database_query\""
    conditions: list[PolicyCondition] = Field(default_factory=list)
    description: Optional[str] = None


class PolicyConfiguration(BaseModel):
    """Configuration for AgentCore Policy component.

    Uses Cedar language for fine-grained access control policies.
    Evaluates requests in real-time to determine whether tool
    invocations should be allowed or denied.
    """

    component_type: Literal["policy"] = "policy"
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True

    # Policy rules
    rules: list[PolicyRule] = Field(default_factory=list)

    # Default behavior when no rules match
    default_effect: PolicyEffect = PolicyEffect.FORBID

    # Enable natural language policy authoring
    enable_nl_authoring: bool = False

    # Policy validation strictness
    strict_validation: bool = True

    # Audit logging for policy decisions
    enable_audit_log: bool = True


# ============================================================================
# Advanced Memory Configuration (Extended)
# ============================================================================


class AdvancedMemoryConfiguration(BaseModel):
    """Extended Memory configuration with extraction strategies.

    Supports dual-layer architecture (short-term + long-term),
    multiple extraction strategies, and vector-based semantic search.
    """

    component_type: Literal["advanced_memory"] = "advanced_memory"
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True

    # Extraction strategies
    extraction_strategies: list[ExtractionStrategy] = Field(default_factory=lambda: [ExtractionStrategy.SEMANTIC])

    # Short-term memory settings
    short_term_enabled: bool = True
    short_term_max_messages: int = Field(ge=1, le=1000, default=100)

    # Long-term memory settings
    long_term_enabled: bool = True
    vector_store_type: Optional[str] = None  # e.g., "pinecone", "opensearch"

    # Session management
    session_timeout_minutes: int = Field(ge=1, le=10080, default=60)  # max 7 days
    enable_branching: bool = False  # Allow conversation branching


# ============================================================================
# A2A (Agent-to-Agent) Configuration Models
# ============================================================================


class AgentEndpoint(BaseModel):
    """Configuration for a remote agent endpoint."""

    agent_id: str = Field(min_length=1, max_length=100)
    endpoint_url: str = Field(min_length=1)
    protocol: Literal["HTTP", "MCP", "A2A"] = "A2A"
    description: Optional[str] = None


class A2AConfiguration(BaseModel):
    """Configuration for Agent-to-Agent communication component.

    Enables multi-agent orchestration with various communication patterns
    including hierarchical, peer-to-peer, broadcast, and handoff patterns.
    """

    component_type: Literal["a2a"] = "a2a"
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True

    # Communication pattern
    pattern: A2ACommunicationPattern = A2ACommunicationPattern.PEER_TO_PEER

    # Connected agents (for multi-agent workflows)
    agent_endpoints: list[AgentEndpoint] = Field(default_factory=list)

    # Orchestration settings
    timeout_seconds: int = Field(ge=1, le=300, default=60)
    max_retries: int = Field(ge=0, le=10, default=3)
    enable_parallel_execution: bool = False

    # Message routing
    enable_message_routing: bool = True
    routing_strategy: Literal["round_robin", "capability_based", "load_balanced"] = "capability_based"

    # Context sharing between agents
    share_context: bool = True
    context_window_size: int = Field(ge=1, le=100, default=10)


class ToolConfiguration(BaseModel):
    """Configuration for tool nodes (DuckDuckGo, Weather, custom tools, etc.)."""

    component_type: Literal["tool"] = "tool"
    name: str = Field(min_length=1, max_length=200)
    tool_id: str = Field(default="")
    description: str = Field(default="", max_length=2000)
    enabled: bool = True
    is_custom: bool = False
    lambda_code: Optional[str] = None
    input_schema: Optional[dict] = None
    display_name: Optional[str] = None
    is_knowledge_base: Optional[bool] = None


# ============================================================================
# Union Type for All Component Configurations
# ============================================================================

ComponentConfiguration = Annotated[
    Union[
        RuntimeConfiguration,
        GatewayConfiguration,
        MemoryConfiguration,
        CodeInterpreterConfiguration,
        BrowserConfiguration,
        ObservabilityConfiguration,
        IdentityConfiguration,
        EvaluationConfiguration,
        PolicyConfiguration,
        AdvancedMemoryConfiguration,
        A2AConfiguration,
        GuardrailsConfiguration,
        ToolConfiguration,
    ],
    Field(discriminator="component_type"),
]
