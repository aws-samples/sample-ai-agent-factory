"""GitOps API — Gap 3D (CI/CD + GitOps).

Two tenant-isolated endpoints, mounted on the WORKFLOW Lambda (they read/write
the workflows table + Secrets Manager):

  POST /api/workflows/{id}/git-token   store a git PAT in Secrets Manager and
                                        record its ARN on the workflow's
                                        git_source.token_ref (owner can_edit).
  POST /api/workflows/{id}/git-sync     pull the workflow JSON spec from the
                                        configured git_source and update the
                                        workflow's nodes/edges/metadata/version.

ROUTING (critical): this router carries its own ``/api/workflows`` prefix and is
mounted on the workflow Lambda — NOT the deployment Lambda — because it shares
the workflows table. The existing API GW route ``/api/workflows/{proxy+}`` (POST)
already forwards both endpoints to the workflow integration, so NO new CDK route
is required.

Tenant model (mirrors routers/workflows.py + routers/workspaces.py, Critic
Finding 3 / Bug 37 / Bug 126):
  * ``get_caller_sub`` identifies the caller.
  * Both endpoints require ``can_edit`` on the EXISTING workflow (owner or
    shared editor). Viewers and cross-tenant callers get **404, not 403**
    (existence-non-disclosure).
  * git-sync NEVER lets a repo-supplied spec overwrite ownership/ACL/identity
    fields (id, owner_sub, created_at, git_source, workspace_id, acl are
    preserved from the stored row) — Bug 122 escalation class. The fetched
    nodes/edges are trusted only at the same level as the user editing the
    canvas directly; deployment-time validation still runs at /api/deploy. This
    endpoint is NOT a privilege boundary beyond can_edit.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.models import WorkflowDefinition
from app.services import git_sync
from app.services.auth import get_caller_sub
from app.services.storage import get_workflow_storage
from app.services.workspace_acl import Acl, can_edit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workflows", tags=["git-sync"])


def _validate_workflow_id(workflow_id: str) -> str:
    """Mirror routers/workflows.py::_validate_workflow_id (path-traversal guard)."""
    if not workflow_id or len(workflow_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid workflow_id")
    if not re.match(r"^[a-zA-Z0-9_-]+$", workflow_id):
        raise HTTPException(status_code=400, detail="Invalid workflow_id format")
    return workflow_id


def _load_editable_workflow(workflow_id: str, caller_sub: str):
    """Load a workflow and enforce can_edit (owner or shared editor).

    Returns the workflow on success. Raises 404 for both missing AND
    cross-tenant/view-only callers (existence-non-disclosure).
    """
    workflow = get_workflow_storage().get(workflow_id)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    owner_sub = getattr(workflow, "owner_sub", None)
    if not can_edit(
        Acl.normalize(getattr(workflow, "acl", None), owner_sub=owner_sub),
        caller_sub,
        owner_sub=owner_sub,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    return workflow


class GitTokenRequest(BaseModel):
    """Body for POST /api/workflows/{id}/git-token."""

    token: str = Field(min_length=1, max_length=4096)
    repo_url: str = Field(min_length=1, max_length=512)
    branch: str = Field(default="main", min_length=1, max_length=255)
    path: str = Field(min_length=1, max_length=512)


@router.post("/{workflow_id}/git-token", response_model=WorkflowDefinition)
async def set_git_token(
    workflow_id: str,
    request: GitTokenRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> WorkflowDefinition:
    """Store a git PAT in Secrets Manager and attach the git_source to the workflow.

    Owner/editor only. The raw PAT goes to the owner-scoped agentcore-git/
    namespace and is NEVER persisted on the workflow row — only the returned ARN
    (token_ref) is. Validates the git_source (SSRF + structure) before storing
    anything so a bad repo URL fails fast.
    """
    workflow_id = _validate_workflow_id(workflow_id)
    workflow = _load_editable_workflow(workflow_id, caller_sub)

    try:
        normalized = git_sync.validate_git_source(
            request.repo_url, request.branch, request.path
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        token_ref = git_sync.store_git_token(caller_sub, request.token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ClientError as e:
        logger.exception("Failed to store git token in Secrets Manager")
        raise HTTPException(
            status_code=502,
            detail=f"Could not store git token: "
            f"{e.response.get('Error', {}).get('Message', str(e))}",
        ) from e

    normalized["token_ref"] = token_ref
    updated = workflow.model_copy(update={"git_source": normalized})
    result = get_workflow_storage().update(workflow_id, updated)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    return result


@router.post("/{workflow_id}/git-sync", response_model=WorkflowDefinition)
async def git_sync_workflow(
    workflow_id: str,
    caller_sub: str = Depends(get_caller_sub),
) -> WorkflowDefinition:
    """Pull the workflow spec from the configured git_source and update the canvas.

    Owner/editor only. Updates nodes/edges/metadata/version/name/description from
    the fetched spec while PRESERVING id/owner_sub/created_at/git_source/
    workspace_id/acl from the stored row — a repo-supplied spec can never change
    ownership or ACL (Bug 122 class).
    """
    workflow_id = _validate_workflow_id(workflow_id)
    workflow = _load_editable_workflow(workflow_id, caller_sub)

    git_source = getattr(workflow, "git_source", None)
    if not git_source or not isinstance(git_source, dict):
        raise HTTPException(
            status_code=400,
            detail="Workflow has no git_source configured. POST /git-token first.",
        )

    try:
        spec = git_sync.fetch_workflow_spec(
            git_source, git_source.get("token_ref")
        )
    except ValueError as e:
        # Invalid/blocked URL, bad path/branch, oversized or malformed spec.
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ClientError as e:
        logger.exception("Failed to resolve git token from Secrets Manager")
        raise HTTPException(
            status_code=502,
            detail=f"Could not resolve git token: "
            f"{e.response.get('Error', {}).get('Message', str(e))}",
        ) from e

    # Build the updated workflow by starting from the stored row's serialized
    # form and overlaying ONLY the trusted canvas/content fields from the spec.
    # Re-validating through WorkflowDefinition guarantees nodes/edges/metadata/
    # viewport are properly coerced to model instances (fetch_workflow_spec has
    # already proven the spec is a valid WorkflowDefinition). Identity/ACL fields
    # (id, owner_sub, created_at, git_source, workspace_id, acl) are taken from
    # the stored row and overwrite anything the repo spec tried to set — a
    # repo-supplied spec can never seize ownership or re-share (Bug 122 class).
    base = workflow.model_dump(mode="json")
    for field in ("name", "description", "version", "nodes", "edges"):
        base[field] = spec.get(field, base.get(field))
    if "viewport" in spec:
        base["viewport"] = spec["viewport"]
    if "metadata" in spec:
        base["metadata"] = spec["metadata"]
    base["updated_at"] = datetime.now(timezone.utc).isoformat()
    # Force identity/ACL fields back to the stored values (last write wins).
    base["id"] = workflow.id
    base["owner_sub"] = getattr(workflow, "owner_sub", None)
    base["created_at"] = workflow.created_at.isoformat() if hasattr(
        workflow.created_at, "isoformat"
    ) else workflow.created_at

    try:
        updated = WorkflowDefinition.model_validate(base)
    except Exception as e:
        # The spec validated standalone but failed to merge (e.g. edge refers to
        # a node only present in the stored row). Surface as a 400.
        raise HTTPException(status_code=400, detail=f"git spec merge failed: {e}") from e

    # Carry forward the loose extra fields the model doesn't (yet) declare —
    # git_source / acl / workspace_id — from the stored row, never the spec.
    carry = {}
    for field in ("git_source", "acl", "workspace_id"):
        if hasattr(workflow, field):
            carry[field] = getattr(workflow, field)
    if carry:
        updated = updated.model_copy(update=carry)

    result = get_workflow_storage().update(workflow_id, updated)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    return result
