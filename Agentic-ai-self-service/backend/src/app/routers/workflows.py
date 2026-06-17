"""Workflow CRUD API endpoints.

This module provides REST API endpoints for workflow management:
- POST /api/workflows - Create workflow
- GET /api/workflows/{id} - Get workflow
- PUT /api/workflows/{id} - Update workflow
- DELETE /api/workflows/{id} - Delete workflow
- POST /api/workflows/{id}/deploy - Deploy workflow

Requirements: 9.1, 9.5, 11.1, 11.5, 11.6, 11.7
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ValidationError

from app.services.auth import assert_owner, get_caller_sub


def _validate_workflow_id(workflow_id: str) -> str:
    """Validate workflow_id format to prevent injection attacks.

    SECURITY: Workflow IDs should be UUIDs. This rejects any ID containing
    characters that could be used for path traversal or injection.
    """
    if not workflow_id or len(workflow_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid workflow_id")
    if not re.match(r"^[a-zA-Z0-9_-]+$", workflow_id):
        raise HTTPException(status_code=400, detail="Invalid workflow_id format")
    return workflow_id


from app.models import (
    WorkflowDefinition,
    ValidationResult,
    Viewport,
    WorkflowMetadata,
    DeploymentStatus,
    DeploymentConfig,
    DeploymentResult,
)
from app.services.storage import get_workflow_storage
from app.services.validation import ValidationEngine
from app.services.deployment import WorkflowExecutor


router = APIRouter(prefix="/api/workflows", tags=["workflows"])

# Validation engine instance
validation_engine = ValidationEngine()


class WorkflowCreateRequest(BaseModel):
    """Request body for creating a workflow."""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=2000, default="")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", default="1.0.0")
    nodes: list = Field(default_factory=list)
    edges: list = Field(default_factory=list)
    viewport: Optional[Viewport] = None
    metadata: WorkflowMetadata


class WorkflowUpdateRequest(BaseModel):
    """Request body for updating a workflow."""

    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    version: Optional[str] = Field(None, pattern=r"^\d+\.\d+\.\d+$")
    nodes: Optional[list] = None
    edges: Optional[list] = None
    viewport: Optional[Viewport] = None
    metadata: Optional[WorkflowMetadata] = None


class WorkflowResponse(BaseModel):
    """Response for workflow operations."""

    workflow: WorkflowDefinition
    message: str


class DeleteResponse(BaseModel):
    """Response for delete operation."""

    success: bool
    message: str


@router.post("", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    request: WorkflowCreateRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> WorkflowResponse:
    """Create a new workflow.

    Requirements: 9.1
    """
    import uuid

    now = datetime.now(timezone.utc)

    workflow = WorkflowDefinition(
        id=str(uuid.uuid4()),
        name=request.name,
        description=request.description,
        version=request.version,
        nodes=request.nodes,
        edges=request.edges,
        viewport=request.viewport or Viewport(x=0, y=0, zoom=1.0),
        metadata=request.metadata,
        created_at=now,
        updated_at=now,
        owner_sub=caller_sub,
    )

    try:
        created = get_workflow_storage().create(workflow)
        return WorkflowResponse(
            workflow=created,
            message="Workflow created successfully",
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workflow already exists",
        )


@router.get("", response_model=list[WorkflowDefinition])
async def list_workflows(
    caller_sub: str = Depends(get_caller_sub),
) -> list[WorkflowDefinition]:
    """List workflows owned by the caller.

    Tenant-isolation (Critic Finding 3): strict ``owner_sub == caller_sub``.
    Pre-tenancy records (``owner_sub=None``) are excluded for every caller;
    making them visible to "local-dev only" via a wildcard is the same trap
    as the legacy-row bypass — fix is an explicit backfill, not a wildcard.
    Filtering happens in Python because the underlying storage may be
    DynamoDB (no GSI on owner_sub yet) or in-memory; flip to a query when
    scale demands it.
    """
    from app.services.workspace_acl import Acl, can_view

    storage = get_workflow_storage()
    # Gap 2E: include workflows shared with the caller (editor/viewer) in
    # addition to owned ones. list_by_owner (if present) only returns owned
    # rows, so when it exists we still scan list_all for shared rows and union.
    list_by_owner = getattr(storage, "list_by_owner", None)
    owned: list = list(list_by_owner(caller_sub)) if callable(list_by_owner) else []
    owned_ids = {wf.id for wf in owned}

    result = list(owned)
    for wf in storage.list_all():
        if wf.id in owned_ids:
            continue
        owner_sub = getattr(wf, "owner_sub", None)
        if owner_sub == caller_sub or can_view(
            Acl.normalize(getattr(wf, "acl", None), owner_sub=owner_sub),
            caller_sub,
            owner_sub=owner_sub,
        ):
            result.append(wf)
    return result


@router.get("/{workflow_id}", response_model=WorkflowDefinition)
async def get_workflow(
    workflow_id: str,
    caller_sub: str = Depends(get_caller_sub),
) -> WorkflowDefinition:
    """Get a workflow by ID. Caller must own it OR be a shared viewer/editor.

    Gap 2E (Bug M-1 fix): a workflow shared with the caller (acl.viewers /
    acl.editors) is viewable. Owner-only used to 404 shared editors who could
    see the row in the LIST endpoint — an inconsistency the security review
    flagged. Denial still returns 404 (existence-non-disclosure), never 403.

    Requirements: 9.5
    """
    from app.services.workspace_acl import Acl, can_view

    workflow_id = _validate_workflow_id(workflow_id)
    workflow = get_workflow_storage().get(workflow_id)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    owner_sub = getattr(workflow, "owner_sub", None)
    if not can_view(
        Acl.normalize(getattr(workflow, "acl", None), owner_sub=owner_sub),
        caller_sub,
        owner_sub=owner_sub,
    ):
        # 404 (not 403) — don't disclose existence to unauthorized callers.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    return workflow


@router.put("/{workflow_id}", response_model=WorkflowResponse)
async def update_workflow(
    workflow_id: str,
    request: WorkflowUpdateRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> WorkflowResponse:
    """Update an existing workflow. Caller must own it OR be a shared editor.

    Gap 2E (Bug M-1 fix): editors granted via acl.editors can update the
    workflow's nodes/edges/etc. Viewers cannot (can_edit is False for them).
    Denial returns 404 (existence-non-disclosure), never 403. The acl + owner
    fields themselves are NOT updatable here — sharing goes through the
    dedicated /share endpoint (routers/workspaces.py) which is owner-only — so
    an editor cannot escalate themselves to owner or re-share.

    Requirements: 9.1
    """
    from app.services.workspace_acl import Acl, can_edit

    workflow_id = _validate_workflow_id(workflow_id)
    existing = get_workflow_storage().get(workflow_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    owner_sub = getattr(existing, "owner_sub", None)
    if not can_edit(
        Acl.normalize(getattr(existing, "acl", None), owner_sub=owner_sub),
        caller_sub,
        owner_sub=owner_sub,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )

    # Build updated workflow with only provided fields
    update_data = {}
    if request.name is not None:
        update_data["name"] = request.name
    if request.description is not None:
        update_data["description"] = request.description
    if request.version is not None:
        update_data["version"] = request.version
    if request.nodes is not None:
        update_data["nodes"] = request.nodes
    if request.edges is not None:
        update_data["edges"] = request.edges
    if request.viewport is not None:
        update_data["viewport"] = request.viewport
    if request.metadata is not None:
        update_data["metadata"] = request.metadata

    updated_workflow = existing.model_copy(update=update_data)

    result = get_workflow_storage().update(workflow_id, updated_workflow)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )

    return WorkflowResponse(
        workflow=result,
        message="Workflow updated successfully",
    )


@router.delete("/{workflow_id}", response_model=DeleteResponse)
async def delete_workflow(
    workflow_id: str,
    caller_sub: str = Depends(get_caller_sub),
) -> DeleteResponse:
    """Delete a workflow by ID. Caller must own it.

    Requirements: 9.1
    """
    workflow_id = _validate_workflow_id(workflow_id)
    storage = get_workflow_storage()
    existing = storage.get(workflow_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    assert_owner(getattr(existing, "owner_sub", None), caller_sub)

    deleted = storage.delete(workflow_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )

    return DeleteResponse(
        success=True,
        message=f"Workflow '{workflow_id}' deleted successfully",
    )


@router.post("/{workflow_id}/validate", response_model=ValidationResult)
async def validate_workflow(workflow_id: str) -> ValidationResult:
    """Validate a workflow configuration.

    Requirements: 8.1, 8.2, 8.3
    """
    workflow_id = _validate_workflow_id(workflow_id)
    workflow = get_workflow_storage().get(workflow_id)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )

    result = validation_engine.validate_workflow(workflow)
    return result


# ============================================================================
# Import/Export Endpoints
# ============================================================================


class ImportRequest(BaseModel):
    """Request body for importing a workflow from JSON."""

    workflow_json: dict


class ImportResponse(BaseModel):
    """Response for import operation."""

    workflow: WorkflowDefinition
    message: str
    validation_errors: list[str] = Field(default_factory=list)


class ImportErrorResponse(BaseModel):
    """Response for failed import operation."""

    success: bool = False
    errors: list[str]


class ExportResponse(BaseModel):
    """Response for export operation."""

    workflow_json: dict
    message: str


@router.post("/import", response_model=ImportResponse)
async def import_workflow(request: ImportRequest) -> ImportResponse:
    """Import a workflow from JSON.

    Validates the JSON against the workflow schema before importing.

    Requirements: 14.1, 14.2, 14.3
    """
    import uuid
    from pydantic import ValidationError as PydanticValidationError

    validation_errors: list[str] = []

    try:
        # Validate and parse the workflow JSON
        workflow_data = request.workflow_json

        # Generate new ID if not provided or if it conflicts
        if "id" not in workflow_data or not workflow_data["id"]:
            workflow_data["id"] = str(uuid.uuid4())

        # Set timestamps if not provided
        now = datetime.now(timezone.utc)
        if "created_at" not in workflow_data:
            workflow_data["created_at"] = now.isoformat()
        if "updated_at" not in workflow_data:
            workflow_data["updated_at"] = now.isoformat()

        # Parse and validate the workflow
        workflow = WorkflowDefinition.model_validate(workflow_data)

        # Check if workflow with same ID exists
        existing = get_workflow_storage().get(workflow.id)
        if existing:
            # Generate new ID to avoid conflict
            workflow = workflow.model_copy(update={"id": str(uuid.uuid4())})

        # Store the workflow
        created = get_workflow_storage().create(workflow)

        return ImportResponse(
            workflow=created,
            message="Workflow imported successfully",
            validation_errors=validation_errors,
        )

    except PydanticValidationError as e:
        # Extract validation error messages
        errors = []
        for error in e.errors():
            loc = ".".join(str(x) for x in error["loc"])
            msg = error["msg"]
            errors.append(f"{loc}: {msg}")

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "errors": errors,
                "message": "Invalid workflow JSON schema",
            },
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "errors": [str(e)],
                "message": "Failed to import workflow",
            },
        )


@router.get("/{workflow_id}/export", response_model=ExportResponse)
async def export_workflow(workflow_id: str) -> ExportResponse:
    """Export a workflow as JSON.

    Requirements: 14.1, 14.2
    """
    workflow_id = _validate_workflow_id(workflow_id)
    workflow = get_workflow_storage().get(workflow_id)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )

    # Convert to JSON-serializable dict
    workflow_json = workflow.model_dump(mode="json")

    return ExportResponse(
        workflow_json=workflow_json,
        message="Workflow exported successfully",
    )


# ============================================================================
# Deployment Endpoint
# ============================================================================


class DeployRequest(BaseModel):
    """Request body for deploying a workflow."""

    aws_region: str = Field(pattern=r"^[a-z]{2}(-[a-z]+-\d+)?$", max_length=30)
    vpc_config: Optional[dict] = None
    enable_cloudwatch: bool = True
    enable_cloudtrail: bool = True


@router.post("/{workflow_id}/deploy", response_model=DeploymentResult)
async def deploy_workflow(workflow_id: str, request: DeployRequest) -> DeploymentResult:
    """Deploy a workflow to AWS.

    This endpoint validates the workflow before deployment and initiates
    the deployment process using the WorkflowExecutor.

    Requirements: 11.1, 11.5, 11.6, 11.7
    """
    workflow_id = _validate_workflow_id(workflow_id)
    # Get the workflow
    workflow = get_workflow_storage().get(workflow_id)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )

    # Validate the workflow before deployment
    validation_result = validation_engine.validate_workflow(workflow)
    if not validation_result.is_valid:
        error_messages = [e.message for e in validation_result.errors]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Workflow validation failed",
                "errors": error_messages,
            },
        )

    # Create deployment config
    try:
        deployment_config = DeploymentConfig(
            aws_region=request.aws_region,
            vpc_config=request.vpc_config,
            enable_cloudwatch=request.enable_cloudwatch,
            enable_cloudtrail=request.enable_cloudtrail,
        )
    except ValidationError as e:
        logger.exception("Invalid deployment configuration")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid deployment configuration",
        )

    # Create executor and deploy
    try:
        executor = WorkflowExecutor(region=request.aws_region)
        result = await executor.deploy(workflow, deployment_config)

        # Update workflow metadata with deployment status
        if result.status == "success":
            updated_metadata = workflow.metadata.model_copy(
                update={
                    "deployment_status": DeploymentStatus.DEPLOYED,
                    "endpoint_url": result.endpoint_url,
                    "last_deployed_at": datetime.now(timezone.utc),
                }
            )
            get_workflow_storage().update(
                workflow_id,
                workflow.model_copy(update={"metadata": updated_metadata}),
            )
        elif result.status == "failed":
            updated_metadata = workflow.metadata.model_copy(
                update={
                    "deployment_status": DeploymentStatus.FAILED,
                }
            )
            get_workflow_storage().update(
                workflow_id,
                workflow.model_copy(update={"metadata": updated_metadata}),
            )

        return result

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.exception("Deployment failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Deployment failed",
        )
