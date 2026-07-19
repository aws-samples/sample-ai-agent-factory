"""Flow CRUD API endpoints.

This module provides REST API endpoints for flow management:
- POST /flows - Create flow
- GET /flows - List all flows
- GET /flows/{flow_id} - Get flow
- PUT /flows/{flow_id} - Update flow
- DELETE /flows/{flow_id} - Delete flow

Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 3.2, 3.3, 4.2, 4.3, 4.5, 6.2, 6.4, 7.4
"""

import logging
import re

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError

from app.models import (
    Flow,
    FlowCreateRequest,
    FlowListResponse,
    FlowResponse,
    FlowSummary,
    FlowUpdateRequest,
)
from app.services.auth import assert_owner, get_caller_sub
from app.services.flow_storage import get_flow_storage

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/flows", tags=["flows"])


# ============================================================================
# Helpers
# ============================================================================


def _get_flow_storage():
    """Get the active flow storage instance."""
    return get_flow_storage()


def _validate_flow_id(flow_id: str) -> str:
    """Validate flow_id format to prevent injection attacks."""
    if not flow_id or len(flow_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid flow_id")
    if not re.match(r"^[a-zA-Z0-9_-]+$", flow_id):
        raise HTTPException(status_code=400, detail="Invalid flow_id format")
    return flow_id


# ============================================================================
# Endpoints
# ============================================================================


@router.post("", response_model=FlowResponse, status_code=status.HTTP_201_CREATED)
async def create_flow(
    request: FlowCreateRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> FlowResponse:
    """Create a new flow with an empty workflow.

    Requirements: 1.1, 1.2, 1.3, 1.4
    """
    try:
        storage = _get_flow_storage()
        flow = storage.create(request.name, owner_sub=caller_sub)
        return FlowResponse(
            flow=flow,
            message="Flow created successfully",
        )
    except ClientError as exc:
        logger.exception("DynamoDB error in create_flow")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service unavailable",
        ) from exc
    except ValidationError as exc:
        logger.exception("Validation error in create_flow")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process flow data",
        ) from exc


@router.get("", response_model=FlowListResponse)
async def list_flows(
    caller_sub: str = Depends(get_caller_sub),
) -> FlowListResponse:
    """List flows owned by the caller, sorted by updated_at descending.

    Requirements: 2.1

    Tenant-isolation (Critic Finding 3): strict ``owner_sub == caller_sub``.
    Legacy / un-owned (None) rows are excluded for every caller; back-compat
    with pre-isolation data requires an explicit backfill, not a wildcard
    that grants every authenticated user cross-tenant read.
    """
    storage = _get_flow_storage()
    try:
        # Prefer a server-side owner-filtered listing when storage supports it
        # (DynamoDBFlowStorage exposes ``list_by_owner``); fall back to
        # in-Python filter on ``list_all`` for the in-memory store / tests.
        list_by_owner = getattr(storage, "list_by_owner", None)
        if callable(list_by_owner):
            flows = list_by_owner(caller_sub)
        else:
            flows = [c for c in storage.list_all() if getattr(c, "owner_sub", None) == caller_sub]
        summaries = [
            FlowSummary(
                id=c.id,
                name=c.name,
                deployment_status=c.deployment_status,
                created_at=c.created_at,
                updated_at=c.updated_at,
            )
            for c in flows
        ]
        return FlowListResponse(flows=summaries)
    except ClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service unavailable",
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process flow data",
        ) from exc


@router.get("/{flow_id}", response_model=Flow)
async def get_flow(
    flow_id: str,
    caller_sub: str = Depends(get_caller_sub),
) -> Flow:
    """Get a flow by ID with full workflow. Caller must own it.

    Requirements: 3.2, 3.3
    """
    flow_id = _validate_flow_id(flow_id)
    try:
        flow = _get_flow_storage().get(flow_id)
        if flow is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Flow '{flow_id}' not found",
            )
        assert_owner(getattr(flow, "owner_sub", None), caller_sub)
        return flow
    except HTTPException:
        raise
    except ClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service unavailable",
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process flow data",
        ) from exc


@router.put("/{flow_id}", response_model=FlowResponse)
async def update_flow(
    flow_id: str,
    request: FlowUpdateRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> FlowResponse:
    """Update an existing flow name and/or workflow. Caller must own it.

    Requirements: 6.2, 6.4
    """
    flow_id = _validate_flow_id(flow_id)
    try:
        existing = _get_flow_storage().get(flow_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Flow '{flow_id}' not found",
            )
        assert_owner(getattr(existing, "owner_sub", None), caller_sub)
        updated = _get_flow_storage().update(
            flow_id,
            name=request.name,
            workflow=request.workflow,
        )
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Flow '{flow_id}' not found",
            )
        return FlowResponse(
            flow=updated,
            message="Flow updated successfully",
        )
    except HTTPException:
        raise
    except ClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service unavailable",
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process flow data",
        ) from exc


@router.delete("/{flow_id}")
async def delete_flow(
    flow_id: str,
    caller_sub: str = Depends(get_caller_sub),
) -> dict:
    """Delete a flow by ID. Caller must own it.

    Requirements: 4.2, 4.3, 4.5
    """
    flow_id = _validate_flow_id(flow_id)
    try:
        existing = _get_flow_storage().get(flow_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Flow '{flow_id}' not found",
            )
        assert_owner(getattr(existing, "owner_sub", None), caller_sub)
        deleted = _get_flow_storage().delete(flow_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Flow '{flow_id}' not found",
            )
        return {"message": f"Flow '{flow_id}' deleted successfully"}
    except HTTPException:
        raise
    except ClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service unavailable",
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process flow data",
        ) from exc
