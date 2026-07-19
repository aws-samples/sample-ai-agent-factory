"""Pydantic models for flow management.

Requirements: 1.1, 1.2, 1.3, 6.4, 7.1
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .enums import DeploymentStatus
from .workflow import to_camel

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
