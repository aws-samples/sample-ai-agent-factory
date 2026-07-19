"""HITL approval-policy CRUD API (Loom-study 2.2).

Manage config-driven approval policies (which tools require human approval, and
whether to block or just notify). The deploy hook serializes matching policies
into the agent so the in-agent BeforeToolInvocation hook enforces them.

  GET    /api/settings/approval-policies         list
  POST   /api/settings/approval-policies         create/update
  DELETE /api/settings/approval-policies/{name}  delete

Reuses the tag-policy DDB table (org-config single table, SK APPROVAL#<name>).
Gated on hitl:read / hitl:write.
"""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, Depends, HTTPException

from app.services.approval_policy_store import ApprovalPolicy, ApprovalPolicyStore
from app.services.auth import get_caller_sub
from app.services.rbac import require_scopes
from app.services.tag_policy_store import DEFAULT_ORG_ID

router = APIRouter(prefix="/api/settings", tags=["approvals"])

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _org(_caller_sub: str) -> str:
    return DEFAULT_ORG_ID


def _store() -> ApprovalPolicyStore:
    return ApprovalPolicyStore(
        table_name=os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy"),
        region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
    )


@router.get(
    "/approval-policies", response_model=list[ApprovalPolicy], dependencies=[Depends(require_scopes("hitl:read"))]
)
def list_policies(caller_sub: str = Depends(get_caller_sub)) -> list[ApprovalPolicy]:
    return _store().list(_org(caller_sub))


@router.post("/approval-policies", response_model=ApprovalPolicy, dependencies=[Depends(require_scopes("hitl:write"))])
def upsert_policy(body: ApprovalPolicy, caller_sub: str = Depends(get_caller_sub)) -> ApprovalPolicy:
    if not _NAME_RE.match(body.name):
        raise HTTPException(status_code=400, detail="Invalid policy name")
    if body.mode not in ("require", "notify"):
        raise HTTPException(status_code=400, detail="mode must be 'require' or 'notify'")
    return _store().put(_org(caller_sub), body)


@router.delete("/approval-policies/{name}", dependencies=[Depends(require_scopes("hitl:write"))])
def delete_policy(name: str, caller_sub: str = Depends(get_caller_sub)) -> dict:
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid policy name")
    _store().delete(_org(caller_sub), name)
    return {"deleted": name}
