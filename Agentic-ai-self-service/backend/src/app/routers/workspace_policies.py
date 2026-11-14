"""Workspace policy admin API (plan §3.B, implementation step 1).

Routes (router prefix ``/api/workspaces``), every one behind the ``workspacePolicy`` feature flag
FIRST (off: the exact 404 of a nonexistent route) and the ``admin`` scope SECOND:

  GET    /api/workspaces/policies                 list every workspace policy in the org
  GET    /api/workspaces/{workspaceId}/policy     read one policy (404 when none is stored)
  PUT    /api/workspaces/{workspaceId}/policy     create or replace a policy (strict schema)
  DELETE /api/workspaces/{workspaceId}/policy     delete a policy (204; idempotent)

ROUTING: this router is mounted on the DEPLOYMENT Lambda (``app/deployment_handler.py``), not the
workflow Lambda, because policies live in the shared org-config table that only the deployment
Lambda has a grant for (tag policies and HITL approval policies already live there). The existing
``routers/workspaces.py`` (workflow sharing ACLs, ``GET /api/workspaces``) is a different concept
on the workflow Lambda; the two share the ``/api/workspaces`` prefix and nothing else, and these
paths are disjoint from its one GET.

Authorization: ``admin`` scope only (``g-admins-super`` today). A policy says what OTHER people may
deploy, so it is an org-admin control, not a per-workspace one; ``adminGroups`` on the policy is
carried for the later UI and for component C, not consulted here. The ``workspaceId`` in the path
must equal the one in the body on PUT, so a policy cannot be filed under another workspace's key.

Fail closed: a store failure is 503 and nothing is written.
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.schemas.workspace_policy import WorkspacePolicy
from app.services.feature_flags import require_feature
from app.services.rbac import require_scopes
from app.services.workspace_policy_service import (
    DEFAULT_ORG_ID,
    InvalidWorkspaceIdError,
    WorkspacePolicyError,
    WorkspacePolicyStore,
    get_workspace_policy_store,
    validate_workspace_id,
)

logger = logging.getLogger(__name__)

FLAG = "workspacePolicy"
router = APIRouter(prefix="/api/workspaces", tags=["workspace-policies"])
_GATES = [Depends(require_feature(FLAG)), Depends(require_scopes("admin"))]

_STORE_UNAVAILABLE = "The workspace policy store is unavailable. Nothing was changed."


def _store() -> WorkspacePolicyStore:
    return get_workspace_policy_store()


def _workspace_id(workspace_id: str) -> str:
    try:
        return validate_workspace_id(workspace_id)
    except InvalidWorkspaceIdError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _store_failure(exc: Exception, operation: str) -> HTTPException:
    # Type only: a botocore message echoes request parameters.
    logger.error("Workspace policy %s failed (%s)", operation, type(exc).__name__)
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_STORE_UNAVAILABLE)


def _as_document(policy: WorkspacePolicy) -> dict[str, Any]:
    return policy.model_dump(by_alias=True, mode="json")


@router.get("/policies", dependencies=_GATES)
def list_policies() -> list[dict[str, Any]]:
    try:
        policies = _store().list(DEFAULT_ORG_ID)
    except (ClientError, BotoCoreError, WorkspacePolicyError) as exc:
        raise _store_failure(exc, "list") from exc
    return [_as_document(policy) for policy in policies]


@router.get("/{workspace_id}/policy", dependencies=_GATES)
def get_policy(workspace_id: str) -> dict[str, Any]:
    workspace_id = _workspace_id(workspace_id)
    try:
        policy = _store().get(DEFAULT_ORG_ID, workspace_id)
    except (ClientError, BotoCoreError, WorkspacePolicyError) as exc:
        raise _store_failure(exc, "read") from exc
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No policy is stored for workspace '{workspace_id}'"
        )
    return _as_document(policy)


@router.put("/{workspace_id}/policy", dependencies=_GATES)
def put_policy(workspace_id: str, body: WorkspacePolicy) -> dict[str, Any]:
    workspace_id = _workspace_id(workspace_id)
    if body.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"workspaceId in the body ({body.workspace_id!r}) must equal the one in the path ({workspace_id!r})"
            ),
        )
    try:
        stored = _store().put(DEFAULT_ORG_ID, body)
    except (ClientError, BotoCoreError) as exc:
        raise _store_failure(exc, "write") from exc
    logger.info("Workspace policy stored: workspace=%s", workspace_id)
    return _as_document(stored)


@router.delete("/{workspace_id}/policy", dependencies=_GATES, status_code=status.HTTP_204_NO_CONTENT)
def delete_policy(workspace_id: str) -> Response:
    workspace_id = _workspace_id(workspace_id)
    try:
        _store().delete(DEFAULT_ORG_ID, workspace_id)
    except (ClientError, BotoCoreError) as exc:
        raise _store_failure(exc, "delete") from exc
    logger.info("Workspace policy deleted: workspace=%s", workspace_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
