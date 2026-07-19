"""Named VPC config profile CRUD API (Loom-study 4.2).

  GET    /api/settings/vpc-profiles         list
  POST   /api/settings/vpc-profiles         create/update
  DELETE /api/settings/vpc-profiles/{name}  delete

Networking config is admin territory → gated on settings:read / settings:write.
Reuses the org-config tag-policy DDB table (SK VPCPROFILE#<name>).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.services.auth import get_caller_sub
from app.services.rbac import require_scopes
from app.services.tag_policy_store import DEFAULT_ORG_ID
from app.services.vpc_profile_store import (
    VpcProfile,
    get_vpc_profile_store,
    validate_profile,
)

router = APIRouter(prefix="/api/settings", tags=["vpc-profiles"])


def _org(_caller_sub: str) -> str:
    return DEFAULT_ORG_ID


@router.get("/vpc-profiles", response_model=list[VpcProfile], dependencies=[Depends(require_scopes("settings:read"))])
def list_profiles(caller_sub: str = Depends(get_caller_sub)) -> list[VpcProfile]:
    return get_vpc_profile_store().list(_org(caller_sub))


@router.post("/vpc-profiles", response_model=VpcProfile, dependencies=[Depends(require_scopes("settings:write"))])
def upsert_profile(body: VpcProfile, caller_sub: str = Depends(get_caller_sub)) -> VpcProfile:
    try:
        validate_profile(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return get_vpc_profile_store().put(_org(caller_sub), body)


@router.delete("/vpc-profiles/{name}", dependencies=[Depends(require_scopes("settings:write"))])
def delete_profile(name: str, caller_sub: str = Depends(get_caller_sub)) -> dict:
    get_vpc_profile_store().delete(_org(caller_sub), name)
    return {"deleted": name}
