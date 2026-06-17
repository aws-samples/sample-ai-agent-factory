"""Enumeration types for AgentCore workflow components."""

from enum import Enum


class AgentFramework(str, Enum):
    """Supported AI agent frameworks — Strands only."""

    STRANDS_AGENTS = "strands_agents"


class StrandsModelProvider(str, Enum):
    """Strands-supported model providers."""

    BEDROCK = "bedrock"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    LITELLM = "litellm"
    MISTRAL = "mistral"
    OLLAMA = "ollama"
    SAGEMAKER = "sagemaker"
    WRITER = "writer"
    LLAMAAPI = "llamaapi"
    DEEPSEEK = "deepseek"
    GROQ = "groq"
    TOGETHER = "together"


# Backward-compat alias
ModelProvider = StrandsModelProvider


class MultiAgentPattern(str, Enum):
    """Strands multi-agent orchestration patterns."""

    NONE = "none"
    GRAPH = "graph"
    SWARM = "swarm"
    WORKFLOW = "workflow"


class GatewayTargetType(str, Enum):
    """Gateway target types."""

    OPENAPI = "openapi"
    LAMBDA = "lambda"
    SMITHY = "smithy"
    API_GATEWAY = "api_gateway"
    PREBUILT = "prebuilt"


class PrebuiltIntegration(str, Enum):
    """Pre-built integration types."""

    SALESFORCE = "salesforce"
    SLACK = "slack"
    JIRA = "jira"
    GITHUB = "github"
    CONFLUENCE = "confluence"
    ZENDESK = "zendesk"


class OAuth2Provider(str, Enum):
    """OAuth2 provider types."""

    GOOGLE = "google"
    MICROSOFT = "microsoft"
    GITHUB = "github"
    SALESFORCE = "salesforce"
    SLACK = "slack"
    COGNITO = "cognito"
    CUSTOM = "custom"


class FederatedIdentityProvider(str, Enum):
    """Federated identity provider types for workflow authentication."""

    COGNITO = "cognito"
    OKTA = "okta"
    AUTH0 = "auth0"
    AZURE_AD = "azure_ad"
    CUSTOM = "custom"


class AgentCoreComponentType(str, Enum):
    """AgentCore component types (primitives)."""

    RUNTIME = "runtime"
    GATEWAY = "gateway"
    MEMORY = "memory"
    CODE_INTERPRETER = "code_interpreter"
    BROWSER = "browser"
    OBSERVABILITY = "observability"
    IDENTITY = "identity"
    EVALUATION = "evaluation"
    POLICY = "policy"
    A2A = "a2a"  # Agent-to-Agent communication
    GUARDRAILS = "guardrails"
    TOOL = "tool"  # Tool nodes (DuckDuckGo, Weather, custom tools, etc.)


class A2ACommunicationPattern(str, Enum):
    """Agent-to-Agent communication patterns."""

    HIERARCHICAL = "hierarchical"  # Manager-worker pattern
    PEER_TO_PEER = "peer_to_peer"  # Direct agent communication
    BROADCAST = "broadcast"  # One-to-many communication
    HANDOFF = "handoff"  # Sequential delegation
    ORCHESTRATOR = "orchestrator"  # Central coordinator pattern


class EvaluatorType(str, Enum):
    """Built-in evaluator types from AgentCore Evaluations."""

    CORRECTNESS = "correctness"
    HELPFULNESS = "helpfulness"
    FAITHFULNESS = "faithfulness"
    ANSWER_RELEVANCE = "answer_relevance"
    CONTEXT_RELEVANCE = "context_relevance"
    HARMFULNESS = "harmfulness"
    MALICIOUSNESS = "maliciousness"
    COHERENCE = "coherence"
    CONCISENESS = "conciseness"
    TOOL_SELECTION = "tool_selection"
    TOOL_CALL_QUALITY = "tool_call_quality"
    SQL_CORRECTNESS = "sql_correctness"
    SUMMARIZATION_QUALITY = "summarization_quality"
    CUSTOM = "custom"


class ExtractionStrategy(str, Enum):
    """Memory extraction strategies."""

    SEMANTIC = "semantic"
    SUMMARY = "summary"
    EPISODIC = "episodic"
    USER_PREFERENCES = "user_preferences"
    CUSTOM = "custom"


class PolicyEffect(str, Enum):
    """Cedar policy effects."""

    PERMIT = "permit"
    FORBID = "forbid"


class AgentServerProtocol(str, Enum):
    """Agent server protocol types."""

    HTTP = "HTTP"
    MCP = "MCP"
    A2A = "A2A"


class PythonRuntime(str, Enum):
    """Python runtime versions for AgentCore."""

    PYTHON_3_10 = "PYTHON_3_10"
    PYTHON_3_11 = "PYTHON_3_11"
    PYTHON_3_12 = "PYTHON_3_12"
    PYTHON_3_13 = "PYTHON_3_13"


class DeploymentType(str, Enum):
    """Deployment types for AgentCore Runtime."""

    DIRECT_CODE_DEPLOY = "direct_code_deploy"
    CONTAINER = "container"


class ConnectionType(str, Enum):
    """Connection types between components."""

    DATA = "data"
    AUTHENTICATION = "authentication"
    POLICY = "policy"


class ValidationStatus(str, Enum):
    """Validation status for components and edges."""

    VALID = "valid"
    WARNING = "warning"
    ERROR = "error"
    PENDING = "pending"


class DeploymentStatus(str, Enum):
    """Deployment status for workflows."""

    NOT_DEPLOYED = "not_deployed"
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    FAILED = "failed"
