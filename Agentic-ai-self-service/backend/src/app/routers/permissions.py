"""JIT IAM permission-request API (Loom-study 1.6).

Auditable escalation path instead of over-provisioning roles up front:
  POST   /api/permissions/requests            create a request (any authed builder)
  GET    /api/permissions/requests/pending    admin pending queue
  POST   /api/permissions/requests/{id}/approve   approve + widen the role (admin)
  POST   /api/permissions/requests/{id}/reject    reject (admin)

Approve widens ONLY the platform's own AgentCore* managed roles (the deployment
role's iam:PutRolePolicy is scoped to arn:...:role/AgentCore*), and the requested
actions are validated against an allowlist so a request can't grant iam:* / *.
"""

from __future__ import annotations

import logging
import os
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.routers.registry import caller_is_admin, _caller_org_id
from app.services.auth import get_caller_sub
from app.services.permission_request_store import (
    PermissionRequestNotPending,
    PermissionRequestStore,
)
from app.services.rbac import require_scopes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/permissions", tags=["permissions"])

_ROLE_RE = re.compile(r"^AgentCore[A-Za-z0-9_+=,.@-]{0,120}$")
# Deny obviously dangerous escalations regardless of the PutRolePolicy ARN scope.
_FORBIDDEN_ACTION_PREFIXES = ("iam:", "sts:", "organizations:", "account:")


def _get_store() -> PermissionRequestStore:
    return PermissionRequestStore(
        table_name=os.environ.get("PERMISSION_REQUESTS_TABLE_NAME", "PermissionRequests"),
        region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
    )


class CreateRequest(BaseModel):
    role_name: str = Field(alias="roleName", min_length=1, max_length=128)
    actions: list[str] = Field(min_length=1, max_length=50)
    resources: list[str] = Field(default_factory=lambda: ["*"], max_length=50)
    justification: str = Field(min_length=1, max_length=2000)
    model_config = {"populate_by_name": True}


class DecideRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)


def _validate_request(body: CreateRequest) -> None:
    if not _ROLE_RE.match(body.role_name):
        raise HTTPException(status_code=400, detail="role_name must be a platform AgentCore* role")
    for a in body.actions:
        low = a.lower()
        if low == "*" or any(low.startswith(p) for p in _FORBIDDEN_ACTION_PREFIXES):
            raise HTTPException(status_code=400, detail=f"Action not permitted via JIT request: {a}")


@router.post("/requests", dependencies=[Depends(require_scopes("settings:read"))])
async def create_request(body: CreateRequest, caller_sub: str = Depends(get_caller_sub)) -> dict:
    """Create a PENDING permission request (any authenticated builder)."""
    _validate_request(body)
    req = _get_store().create(
        org_id=_caller_org_id(caller_sub),
        requester_sub=caller_sub,
        role_name=body.role_name,
        actions=body.actions,
        resources=body.resources,
        justification=body.justification,
    )
    return {"request_id": req.request_id, "status": req.status}


@router.get("/requests/pending", dependencies=[Depends(require_scopes("settings:write"))])
async def list_pending(
    caller_sub: str = Depends(get_caller_sub), is_admin: bool = Depends(caller_is_admin)
) -> list[dict]:
    """Admin pending-review queue."""
    if not is_admin:
        raise HTTPException(status_code=403, detail="Requires an admin persona")
    return [r.to_item() for r in _get_store().list_pending()]


@router.post("/requests/{request_id}/approve", dependencies=[Depends(require_scopes("settings:write"))])
async def approve(
    request_id: str, body: DecideRequest,
    caller_sub: str = Depends(get_caller_sub), is_admin: bool = Depends(caller_is_admin),
) -> dict:
    """Approve a request AND widen the target role's inline policy."""
    if not is_admin:
        raise HTTPException(status_code=403, detail="Requires an admin persona")
    store = _get_store()
    org_id = _caller_org_id(caller_sub)
    req = store.get(org_id, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Not found")
    # Re-validate at approval time (defense-in-depth against a tampered row).
    if not _ROLE_RE.match(req.role_name) or any(
        a.lower() == "*" or a.lower().startswith(_FORBIDDEN_ACTION_PREFIXES) for a in req.actions
    ):
        raise HTTPException(status_code=400, detail="Request fails policy validation")

    # Apply the widening BEFORE recording APPROVED, so a failed apply leaves the
    # request PENDING (retryable) rather than APPROVED-but-not-applied.
    from app.services.iam_manager import _create_iam_client, _put_role_inline_policy

    policy_doc = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": req.actions, "Resource": req.resources}],
    }
    try:
        _put_role_inline_policy(
            _create_iam_client(), req.role_name, f"JIT-{request_id}", policy_doc
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("JIT approve: PutRolePolicy failed for %s: %s", req.role_name, exc)
        raise HTTPException(status_code=502, detail="Could not apply the permission to the role")

    try:
        decided = store.decide(org_id, request_id, status="APPROVED", decided_by=caller_sub, reason=body.reason)
    except PermissionRequestNotPending as e:
        raise HTTPException(status_code=409, detail=f"Request already {e}")
    return {"request_id": request_id, "status": decided.status}


@router.post("/requests/{request_id}/reject", dependencies=[Depends(require_scopes("settings:write"))])
async def reject(
    request_id: str, body: DecideRequest,
    caller_sub: str = Depends(get_caller_sub), is_admin: bool = Depends(caller_is_admin),
) -> dict:
    if not is_admin:
        raise HTTPException(status_code=403, detail="Requires an admin persona")
    try:
        decided = _get_store().decide(
            _caller_org_id(caller_sub), request_id, status="REJECTED", decided_by=caller_sub, reason=body.reason,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Not found")
    except PermissionRequestNotPending as e:
        raise HTTPException(status_code=409, detail=f"Request already {e}")
    return {"request_id": request_id, "status": decided.status}
