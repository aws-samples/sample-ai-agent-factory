"""Pydantic models for flow management.

Requirements: 1.1, 1.2, 1.3, 6.4, 7.1
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import DeploymentStatus
from .workflow import DeploymentGovernanceV1, to_camel

# ============================================================================
# Flow Models
# ============================================================================


class Flow(BaseModel):
    """A named flow containing a workflow definition.

    The workflow field stores raw JSON (dict) to allow flexible node/edge
    data without strict Pydantic validation. Strict validation is only
    applied during deployment, not during save.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
    )

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    workflow: dict[str, Any]
    deployment_status: DeploymentStatus = DeploymentStatus.NOT_DEPLOYED
    created_at: datetime
    updated_at: datetime
    # Cognito sub of the user who created this flow. None for pre-tenancy
    # records. See services/auth.py + tasks/lessons.md Bug 37.
    owner_sub: str | None = None
    # F-15 optimistic-concurrency fence. Every write through the store advances it by one and is
    # conditioned on the value the writer read. Rows written before the field existed have no
    # attribute and read as 0, so their first save succeeds and stamps 1.
    version: int = Field(default=0, ge=0)

    @field_validator("workflow", mode="before")
    @classmethod
    def validate_nested_governance(cls, value):
        """Keep the canvas loose while strictly validating its governance envelope.

        Flow saves intentionally accept evolving node/edge shapes, but governance is
        consumed as deployment authority and cannot be a free-form dictionary. Legacy
        workflows with no field migrate to an explicit empty V1 object on read.
        """
        if not isinstance(value, dict):
            return value
        # Raw write-only credentials (LiteLLM virtual key, MCP apiKey, OAuth clientSecret, connector
        # secretValue...) never persist: only their references may. See services.credential_scrub.
        from app.services.credential_scrub import scrub_write_only_credentials

        normalized = dict(scrub_write_only_credentials(value))
        if "governance" in normalized:
            governance = DeploymentGovernanceV1.model_validate(normalized["governance"])
        else:
            governance = DeploymentGovernanceV1()
        normalized["governance"] = governance.model_dump(mode="json", by_alias=True)
        return normalized


class FlowCreateRequest(BaseModel):
    """Request body for creating a flow."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
    )

    name: str = Field(min_length=1, max_length=200)


class FlowUpdateRequest(BaseModel):
    """Request body for updating a flow (partial updates)."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
    )

    name: str | None = Field(default=None, min_length=1, max_length=200)
    workflow: dict[str, Any] | None = None
    # The ``Flow.version`` this request was built on (camelCase ``expectedVersion`` on the wire).
    # When it no longer matches the row, the router answers 409 with the server's current version
    # and writes nothing (F-15). ``None`` is a pre-fence client: the store still fences the row it
    # read, but cannot detect staleness across that client's session.
    expected_version: int | None = Field(default=None, ge=0)

    @field_validator("workflow", mode="before")
    @classmethod
    def validate_workflow_governance(cls, value):
        if value is None:
            return None
        # Reuse Flow's exact migration/validation semantics without tightening the
        # deliberately loose node/edge portion of this request.
        return Flow.validate_nested_governance(value)


class FlowSummary(BaseModel):
    """Lightweight flow info for list responses."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
    )

    id: str
    name: str
    deployment_status: DeploymentStatus
    created_at: datetime
    updated_at: datetime
    version: int = 0


# ============================================================================
# Flow Response Models
# ============================================================================


class FlowListResponse(BaseModel):
    """Response for listing flows."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
    )

    flows: list[FlowSummary]


class FlowResponse(BaseModel):
    """Response for single flow operations."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
    )

    flow: Flow
    message: str
