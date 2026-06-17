"""Workspaces + ACL API — Phase 2 Gap 2E (team collaboration).

Shared workspaces and viewer/editor access-control on individual workflows.
The ACL is a dict embedded on each ``WorkflowDefinition`` row (see
``services/workspace_acl.py``); this router never touches its own DDB table —
it reads/writes through the EXISTING ``get_workflow_storage()`` get/update API.

Endpoints (router prefix ``/api``):
  POST   /api/workflows/{id}/share         owner grants a sub viewer|editor
  DELETE /api/workflows/{id}/share/{sub}    owner revokes a sub's access
  GET    /api/workspaces                    list workflows the caller can view

ROUTING (critical, see design risks): this router is mounted on the WORKFLOW
Lambda (``app/main.py``) — NOT the deployment Lambda — because it shares the
workflows table and the ``GET /api/workflows`` list filter. The deployment
Lambda has no workflows-table grant and would 500 at runtime.

Tenant model (mirrors registry.py / versions.py, Critic Finding 3):
  * ``get_caller_sub`` dependency identifies the caller.
  * **Only the workflow owner may mutate the ACL** — every /share write is
    gated on ``assert_owner(workflow.owner_sub, caller_sub)``. A shared editor
    can change nodes/edges via PUT but can NOT add members or escalate
    (escalation guard, Bug 122 class — ``can_manage`` is owner-only).
  * Cross-tenant / missing workflows return **404, not 403**
    (existence-non-disclosure).
  * No new per-runtime AWS resource is created, so ``destroy_runtime`` needs no
    cleanup — the ACL is a column on the workflow row, removed when the
    workflow is deleted.
"""

from __future__ import annotations

import logging
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services.auth import assert_owner, get_caller_sub
from app.services.storage import get_workflow_storage
from app.services.workspace_acl import (
    Acl,
    add_member,
    can_view,
    remove_member,
)

logger = logging.getLogger(__name__)


def _validate_workflow_id(workflow_id: str) -> str:
    """Mirror the workflow_id validation in routers/workflows.py."""
    if not workflow_id or len(workflow_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid workflow_id")
    if not re.match(r"^[a-zA-Z0-9_-]+$", workflow_id):
        raise HTTPException(status_code=400, detail="Invalid workflow_id format")
    return workflow_id


def _validate_sub(sub: str) -> str:
    """Cognito subs are opaque; keep this permissive but bounded."""
    if not sub or len(sub) > 128:
        raise HTTPException(status_code=400, detail="Invalid sub")
    if not re.match(r"^[a-zA-Z0-9_:.@-]+$", sub):
        raise HTTPException(status_code=400, detail="Invalid sub format")
    return sub


def _workflow_acl(workflow) -> Optional[dict]:
    """Read the embedded acl dict (None on legacy rows or pre-shared-edit models)."""
    return getattr(workflow, "acl", None)


def _persist_acl(workflow_id: str, workflow, new_acl: dict):
    """Persist a mutated acl back onto the workflow row via storage.update().

    Uses ``model_copy(update={"acl": ...})`` so only the acl column changes;
    nodes/edges/metadata are preserved. Works both before and after the
    ``WorkflowDefinition.acl`` shared edit lands (model_copy carries the value
    either way), and against the in-memory fake storage used in tests.
    """
    updated = workflow.model_copy(update={"acl": new_acl})
    result = get_workflow_storage().update(workflow_id, updated)
    if result is None:
        raise HTTPException(status_code=404, detail="Not found")
    return result


router = APIRouter(prefix="/api", tags=["workspaces"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ShareRequest(BaseModel):
    sub: str = Field(min_length=1, max_length=128)
    role: Literal["viewer", "editor"] = "viewer"


class AclResponse(BaseModel):
    workflow_id: str
    owner_sub: Optional[str] = None
    editors: list[str] = Field(default_factory=list)
    viewers: list[str] = Field(default_factory=list)
    workspace_id: Optional[str] = None

    @classmethod
    def from_acl(cls, workflow_id: str, acl: Acl) -> "AclResponse":
        return cls(
            workflow_id=workflow_id,
            owner_sub=acl.owner_sub,
            editors=list(acl.editors),
            viewers=list(acl.viewers),
            workspace_id=acl.workspace_id,
        )


class WorkspaceWorkflowResponse(BaseModel):
    """A workflow the caller can view, with the caller's effective role."""

    workflow_id: str
    name: str
    workspace_id: Optional[str] = None
    role: str  # one of owner | editor | viewer
    owner_sub: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/workflows/{workflow_id}/share", response_model=AclResponse)
async def share_workflow(
    workflow_id: str,
    body: ShareRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> AclResponse:
    """Grant *sub* viewer|editor access to a workflow. Owner only.

    Re-running with a different role promotes/demotes the sub (idempotent).
    """
    workflow_id = _validate_workflow_id(workflow_id)
    sub = _validate_sub(body.sub)

    workflow = get_workflow_storage().get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Not found")
    owner_sub = getattr(workflow, "owner_sub", None)
    # Only the workflow owner manages the ACL. 404 (not 403) on mismatch.
    assert_owner(owner_sub, caller_sub)

    # Owner cannot add itself — its access is implicit.
    if sub == owner_sub:
        raise HTTPException(
            status_code=400, detail="Cannot share a workflow with its owner"
        )

    try:
        new_acl = add_member(
            _workflow_acl(workflow), sub, body.role, owner_sub=owner_sub
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    self_owner = owner_sub  # capture before persist for re-normalisation
    self_persisted = _persist_acl(workflow_id, workflow, new_acl)
    logger.info(
        "Shared workflow %s with %s as %s (owner=%s)",
        workflow_id,
        sub,
        body.role,
        caller_sub,
    )
    return AclResponse.from_acl(
        workflow_id,
        Acl.normalize(_workflow_acl(self_persisted) or new_acl, owner_sub=self_owner),
    )


@router.delete("/workflows/{workflow_id}/share/{sub}", response_model=AclResponse)
async def unshare_workflow(
    workflow_id: str,
    sub: str,
    caller_sub: str = Depends(get_caller_sub),
) -> AclResponse:
    """Revoke *sub*'s access to a workflow. Owner only. Idempotent."""
    workflow_id = _validate_workflow_id(workflow_id)
    sub = _validate_sub(sub)

    workflow = get_workflow_storage().get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Not found")
    owner_sub = getattr(workflow, "owner_sub", None)
    assert_owner(owner_sub, caller_sub)

    if sub == owner_sub:
        # The owner can never be removed from its own workflow.
        raise HTTPException(
            status_code=400, detail="Cannot remove the workflow owner"
        )

    new_acl = remove_member(_workflow_acl(workflow), sub, owner_sub=owner_sub)
    persisted = _persist_acl(workflow_id, workflow, new_acl)
    logger.info("Unshared workflow %s from %s (owner=%s)", workflow_id, sub, caller_sub)
    return AclResponse.from_acl(
        workflow_id,
        Acl.normalize(_workflow_acl(persisted) or new_acl, owner_sub=owner_sub),
    )


@router.get("/workspaces", response_model=list[WorkspaceWorkflowResponse])
async def list_workspaces(
    caller_sub: str = Depends(get_caller_sub),
) -> list[WorkspaceWorkflowResponse]:
    """List every workflow the caller can view — owned, shared-as-editor, or
    shared-as-viewer — with the caller's effective role.

    Mirrors the GET /api/workflows visibility rule: a workflow with
    ``owner_sub=None`` and no ACL match is invisible to every caller
    (legacy-row exclusion, Critic Finding 3).
    """
    storage = get_workflow_storage()
    out: list[WorkspaceWorkflowResponse] = []
    for wf in storage.list_all():
        owner_sub = getattr(wf, "owner_sub", None)
        acl = Acl.normalize(_workflow_acl(wf), owner_sub=owner_sub)
        if not can_view(acl, caller_sub, owner_sub=owner_sub):
            continue
        if caller_sub == owner_sub:
            role = "owner"
        elif caller_sub in acl.editors:
            role = "editor"
        else:
            role = "viewer"
        out.append(
            WorkspaceWorkflowResponse(
                workflow_id=wf.id,
                name=getattr(wf, "name", ""),
                workspace_id=acl.workspace_id,
                role=role,
                owner_sub=owner_sub,
            )
        )
    # Owned first, then shared; stable within each group by name.
    out.sort(key=lambda w: (0 if w.role == "owner" else 1, w.name.lower()))
    return out
