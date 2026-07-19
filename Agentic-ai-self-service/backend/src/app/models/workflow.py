"""Pydantic models for workflow structure and definitions."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def to_camel(string: str) -> str:
    """Convert snake_case to camelCase."""
    components = string.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


from .components import (
    A2AConfiguration,
    BrowserConfiguration,
    CodeInterpreterConfiguration,
    ComponentConfiguration,
    EvaluationConfiguration,
    GatewayConfiguration,
    GuardrailsConfiguration,
    IdentityConfiguration,
    MemoryConfiguration,
    ObservabilityConfiguration,
    PolicyConfiguration,
    RuntimeConfiguration,
    ToolConfiguration,
)
from .enums import (
    AgentCoreComponentType,
    ConnectionType,
    DeploymentStatus,
    ValidationStatus,
)

# ============================================================================
# Position and Viewport Models
# ============================================================================


class Position(BaseModel):
    """Position on the canvas."""

    x: float
    y: float


class Viewport(BaseModel):
    """Canvas viewport state."""

    x: float
    y: float
    zoom: float = Field(ge=0.1, le=4.0, default=1.0)


# ============================================================================
# Edge Models
# ============================================================================


class EdgeData(BaseModel):
    """Additional data for connection edges."""

    label: str | None = None
    validation_status: ValidationStatus = ValidationStatus.PENDING


class ConnectionEdge(BaseModel):
    """Connection between two components."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    source_handle: str = Field(min_length=1)
    target_handle: str = Field(min_length=1)
    type: ConnectionType
    animated: bool = False
    data: EdgeData = Field(default_factory=EdgeData)

    @model_validator(mode="after")
    def validate_source_target_different(self) -> "ConnectionEdge":
        """Ensure source and target are different nodes."""
        if self.source == self.target:
            raise ValueError("Source and target must be different nodes")
        return self


# ============================================================================
# Node Models
# ============================================================================


# Discriminated union for component configurations based on component type
RuntimeConfigData = Annotated[RuntimeConfiguration, Field(discriminator=None)]
GatewayConfigData = Annotated[GatewayConfiguration, Field(discriminator=None)]
MemoryConfigData = Annotated[MemoryConfiguration, Field(discriminator=None)]
CodeInterpreterConfigData = Annotated[CodeInterpreterConfiguration, Field(discriminator=None)]
BrowserConfigData = Annotated[BrowserConfiguration, Field(discriminator=None)]
ObservabilityConfigData = Annotated[ObservabilityConfiguration, Field(discriminator=None)]
IdentityConfigData = Annotated[IdentityConfiguration, Field(discriminator=None)]


class ComponentNode(BaseModel):
    """A component node on the workflow canvas."""

    id: str = Field(min_length=1)
    type: AgentCoreComponentType
    position: Position
    data: ComponentConfiguration
    selected: bool = False
    validation_status: ValidationStatus = ValidationStatus.PENDING

    @model_validator(mode="after")
    def validate_data_matches_type(self) -> "ComponentNode":
        """Ensure the data configuration matches the component type."""
        type_to_config = {
            AgentCoreComponentType.RUNTIME: RuntimeConfiguration,
            AgentCoreComponentType.GATEWAY: GatewayConfiguration,
            AgentCoreComponentType.MEMORY: MemoryConfiguration,
            AgentCoreComponentType.CODE_INTERPRETER: CodeInterpreterConfiguration,
            AgentCoreComponentType.BROWSER: BrowserConfiguration,
            AgentCoreComponentType.OBSERVABILITY: ObservabilityConfiguration,
            AgentCoreComponentType.IDENTITY: IdentityConfiguration,
            AgentCoreComponentType.EVALUATION: EvaluationConfiguration,
            AgentCoreComponentType.POLICY: PolicyConfiguration,
            AgentCoreComponentType.A2A: A2AConfiguration,
            AgentCoreComponentType.GUARDRAILS: GuardrailsConfiguration,
            AgentCoreComponentType.TOOL: ToolConfiguration,
        }
        expected_config_type = type_to_config.get(self.type)
        if expected_config_type and not isinstance(self.data, expected_config_type):
            raise ValueError(
                f"Component type '{self.type.value}' requires "
                f"{expected_config_type.__name__} configuration, "
                f"got {type(self.data).__name__}"
            )
        return self


# ============================================================================
# Workflow Metadata Models
# ============================================================================


class WorkflowMetadata(BaseModel):
    """Metadata for a workflow."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,  # Allow both snake_case and camelCase
    )

    author: str = Field(min_length=1, max_length=100)
    tags: list[str] = Field(default_factory=list)
    aws_region: str = Field(min_length=1, max_length=50)
    deployment_status: DeploymentStatus = DeploymentStatus.NOT_DEPLOYED
    last_deployed_at: datetime | None = None
    endpoint_url: str | None = None

    @field_validator("aws_region")
    @classmethod
    def validate_aws_region(cls, v: str) -> str:
        """Validate AWS region format."""
        import re

        pattern = r"^[a-z]{2}-[a-z]+-\d$"
        if not re.match(pattern, v):
            raise ValueError(f"Invalid AWS region format: '{v}'. Expected format: us-east-1, eu-west-2, etc.")
        return v


# ============================================================================
# Workflow Definition Model
# ============================================================================


class WorkflowDefinition(BaseModel):
    """Complete workflow definition."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=2000, default="")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    nodes: list[ComponentNode] = Field(default_factory=list)
    edges: list[ConnectionEdge] = Field(default_factory=list)
    viewport: Viewport = Field(default_factory=Viewport)
    metadata: WorkflowMetadata
    created_at: datetime
    updated_at: datetime
    # Cognito sub of the user who created this workflow. None for pre-tenancy
    # records (legacy data). See services/auth.py + tasks/lessons.md Bug 37.
    owner_sub: str | None = None
    # Gap 2E team collaboration: optional shared-workspace id + ACL.
    # acl shape: {"owner_sub": str, "editors": [sub], "viewers": [sub]}.
    # None == legacy/owner-only. Parsed via services.workspace_acl.Acl.
    workspace_id: str | None = None
    acl: dict | None = None
    # Gap 3D GitOps: {repo_url, branch, path, token_ref}. token_ref is a Secrets
    # Manager ARN in the owner-scoped agentcore-git/ namespace; the raw PAT is
    # NEVER stored here. None == not git-backed. Kept a loose dict (like acl) so
    # model_validate of legacy rows never breaks; git_sync.validate_git_source
    # does the structural validation server-side before any fetch.
    git_source: dict | None = None

    @model_validator(mode="after")
    def validate_edge_references(self) -> "WorkflowDefinition":
        """Ensure all edge source/target references exist in nodes."""
        node_ids = {node.id for node in self.nodes}
        for edge in self.edges:
            if edge.source not in node_ids:
                raise ValueError(f"Edge '{edge.id}' references non-existent source node '{edge.source}'")
            if edge.target not in node_ids:
                raise ValueError(f"Edge '{edge.id}' references non-existent target node '{edge.target}'")
        return self

    @model_validator(mode="after")
    def validate_unique_node_ids(self) -> "WorkflowDefinition":
        """Ensure all node IDs are unique."""
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            duplicates = [id for id in node_ids if node_ids.count(id) > 1]
            raise ValueError(f"Duplicate node IDs found: {set(duplicates)}")
        return self

    @model_validator(mode="after")
    def validate_unique_edge_ids(self) -> "WorkflowDefinition":
        """Ensure all edge IDs are unique."""
        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            duplicates = [id for id in edge_ids if edge_ids.count(id) > 1]
            raise ValueError(f"Duplicate edge IDs found: {set(duplicates)}")
        return self


# ============================================================================
# Validation Result Models
# ============================================================================


class ValidationError(BaseModel):
    """Validation error for a component or edge."""

    component_id: str | None = None
    field: str
    message: str
    severity: str = Field(pattern=r"^(error|warning)$")


class ValidationResult(BaseModel):
    """Result of workflow validation."""

    is_valid: bool
    errors: list[ValidationError] = Field(default_factory=list)
    warnings: list[ValidationError] = Field(default_factory=list)


# ============================================================================
# Deployment Models
# ============================================================================


class DeploymentConfig(BaseModel):
    """Configuration for workflow deployment."""

    aws_region: str
    vpc_config: dict | None = None
    enable_cloudwatch: bool = True
    enable_cloudtrail: bool = True

    @field_validator("aws_region")
    @classmethod
    def validate_aws_region(cls, v: str) -> str:
        """Validate AWS region format."""
        import re

        pattern = r"^[a-z]{2}-[a-z]+-\d$"
        if not re.match(pattern, v):
            raise ValueError(f"Invalid AWS region format: '{v}'. Expected format: us-east-1, eu-west-2, etc.")
        return v


class DeploymentResult(BaseModel):
    """Result of workflow deployment."""

    deployment_id: str
    status: str = Field(pattern=r"^(success|failed|in_progress)$")
    endpoint_url: str | None = None
    error_message: str | None = None
    created_resources: list[str] = Field(default_factory=list)
    runtime_id: str | None = None


class RollbackError(BaseModel):
    """Error during rollback operation."""

    resource_arn: str
    error_message: str


class RollbackResult(BaseModel):
    """Result of deployment rollback."""

    success: bool
    errors: list[RollbackError] = Field(default_factory=list)
