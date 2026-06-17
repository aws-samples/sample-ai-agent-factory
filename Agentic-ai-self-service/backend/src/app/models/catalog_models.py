"""Pydantic models for the Tool Catalog and Flow Submission governance system.

Supports the dual-persona (Developer / Admin) approval workflow where developers
create tools and submit them for admin review before they enter the shared palette.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ============================================================================
# Enums
# ============================================================================


class ToolStatus(str, Enum):
    """Lifecycle status of a catalog tool or flow submission."""

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


# ============================================================================
# Core Domain Models (DynamoDB persistence)
# ============================================================================


class CatalogTool(BaseModel):
    """A tool in the Tool Catalog, persisted in DynamoDB.

    Tracks the full lifecycle from draft creation through admin review
    to approved status (visible in the component palette).
    """

    model_config = ConfigDict(populate_by_name=True)

    tool_id: str = Field(alias="toolId")
    tool_name: str = Field(alias="toolName")
    display_name: str = Field(alias="displayName")
    description: str
    lambda_code: str = Field(alias="lambdaCode")
    input_schema: dict = Field(alias="inputSchema", default_factory=dict)
    env_vars: dict = Field(alias="envVars", default_factory=dict)
    status: ToolStatus = ToolStatus.DRAFT
    created_by: str = Field(alias="createdBy")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    submitted_at: Optional[datetime] = Field(alias="submittedAt", default=None)
    reviewed_by: Optional[str] = Field(alias="reviewedBy", default=None)
    reviewed_at: Optional[datetime] = Field(alias="reviewedAt", default=None)
    review_comments: Optional[str] = Field(alias="reviewComments", default=None)
    version: int = 1
    icon: str = "wrench"
    category: str = "custom"


class FlowSubmission(BaseModel):
    """A workflow submitted as a reusable template, persisted in DynamoDB.

    Developers submit working flows for admin review; approved flows
    appear in the Template Gallery for all users.
    """

    model_config = ConfigDict(populate_by_name=True)

    submission_id: str = Field(alias="submissionId")
    name: str
    description: str
    long_description: str = Field(alias="longDescription", default="")
    icon: str = "puzzle"
    difficulty: str = "intermediate"
    tags: list[str] = Field(default_factory=list)
    nodes: list[dict] = Field(default_factory=list)
    edges: list[dict] = Field(default_factory=list)
    component_types: list[str] = Field(alias="componentTypes", default_factory=list)
    built_in_tools: list[dict] = Field(alias="builtInTools", default_factory=list)
    status: ToolStatus = ToolStatus.DRAFT
    created_by: str = Field(alias="createdBy")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    submitted_at: Optional[datetime] = Field(alias="submittedAt", default=None)
    reviewed_by: Optional[str] = Field(alias="reviewedBy", default=None)
    reviewed_at: Optional[datetime] = Field(alias="reviewedAt", default=None)
    review_comments: Optional[str] = Field(alias="reviewComments", default=None)


# ============================================================================
# Tool Catalog Request / Response Models
# ============================================================================


class ToolCreateRequest(BaseModel):
    """Request body for POST /api/tools."""

    model_config = ConfigDict(populate_by_name=True)

    tool_name: str = Field(alias="toolName")
    display_name: str = Field(alias="displayName")
    description: str
    lambda_code: str = Field(alias="lambdaCode")
    input_schema: dict = Field(alias="inputSchema", default_factory=dict)
    env_vars: dict = Field(alias="envVars", default_factory=dict)
    icon: str = "wrench"
    category: str = "custom"


class ToolUpdateRequest(BaseModel):
    """Request body for PUT /api/tools/{tool_id}."""

    model_config = ConfigDict(populate_by_name=True)

    display_name: Optional[str] = Field(alias="displayName", default=None)
    description: Optional[str] = None
    lambda_code: Optional[str] = Field(alias="lambdaCode", default=None)
    input_schema: Optional[dict] = Field(alias="inputSchema", default=None)
    env_vars: Optional[dict] = Field(alias="envVars", default=None)
    icon: Optional[str] = None
    category: Optional[str] = None


class ToolRejectRequest(BaseModel):
    """Request body for POST /api/tools/{tool_id}/reject."""

    model_config = ConfigDict(populate_by_name=True)

    comments: str = Field(min_length=10)


class ToolTestRequest(BaseModel):
    """Request body for POST /api/tools/{tool_id}/test."""

    model_config = ConfigDict(populate_by_name=True)

    test_input: dict = Field(alias="testInput", default_factory=dict)
    env_vars: dict = Field(alias="envVars", default_factory=dict)


class ToolTestResponse(BaseModel):
    """Response body for POST /api/tools/{tool_id}/test."""

    model_config = ConfigDict(populate_by_name=True)

    success: bool
    output: Optional[dict] = None
    error: Optional[str] = None


# ============================================================================
# Flow Submission Request / Response Models
# ============================================================================


class FlowCreateRequest(BaseModel):
    """Request body for POST /api/flow-submissions."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=100)
    description: str
    long_description: str = Field(alias="longDescription", default="")
    icon: str = "puzzle"
    difficulty: str = "intermediate"
    tags: list[str] = Field(default_factory=list)
    nodes: list[dict]
    edges: list[dict]
    component_types: list[str] = Field(alias="componentTypes", default_factory=list)
    built_in_tools: list[dict] = Field(alias="builtInTools", default_factory=list)


class FlowRejectRequest(BaseModel):
    """Request body for POST /api/flow-submissions/{id}/reject."""

    model_config = ConfigDict(populate_by_name=True)

    comments: str = Field(min_length=10)
