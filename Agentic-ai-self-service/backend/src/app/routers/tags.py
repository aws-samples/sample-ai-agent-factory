"""Tag policy + tag profile API (Phase 2 — governance tagging).

Endpoints (all under /api/settings), scope-gated via Phase-1 RBAC:
  GET    /api/settings/tags                 tag:read   list policies
  POST   /api/settings/tags                 tag:write  create/update a policy
  DELETE /api/settings/tags/{key}           tag:write  delete a custom policy
  GET    /api/settings/tag-profiles         tag:read   list profiles
  POST   /api/settings/tag-profiles         tag:write  create/update a profile
  DELETE /api/settings/tag-profiles/{name}  tag:write  delete a profile

Platform-required policies (``platform:*``) are seeded on first list and are
read-only: they cannot be deleted and their ``required`` flag can't be cleared.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services.auth import get_caller_sub
from app.services.rbac import require_scopes
from app.services.tag_policy_store import (
    DEFAULT_ORG_ID,
    TagPolicy,
    TagProfile,
    get_tag_policy_store,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["tags"])


def _org(_caller_sub: str) -> str:
    # Single-org today (mirrors registry/_caller_org_id). Kept as a seam.
    return DEFAULT_ORG_ID


class TagPolicyRequest(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    default_value: str | None = Field(default=None, max_length=256)
    required: bool = False
    show_on_card: bool = False


class TagProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    values: dict[str, str] = Field(default_factory=dict)


# -- policies ----------------------------------------------------------------


@router.get("/tags", response_model=list[TagPolicy],
            dependencies=[Depends(require_scopes("tag:read"))])
def list_policies(caller_sub: str = Depends(get_caller_sub)) -> list[TagPolicy]:
    store = get_tag_policy_store()
    org = _org(caller_sub)
    store.ensure_platform_policies(org)  # idempotent seed
    return store.list_policies(org)


@router.post("/tags", response_model=TagPolicy,
             dependencies=[Depends(require_scopes("tag:write"))])
def upsert_policy(
    body: TagPolicyRequest, caller_sub: str = Depends(get_caller_sub)
) -> TagPolicy:
    store = get_tag_policy_store()
    org = _org(caller_sub)
    if body.key.startswith("platform:"):
        # platform:* keys can't be CREATED by callers, but an admin MAY update an
        # existing platform policy (e.g. flip required=true to enforce it, or set
        # a default). Reject only creation of a brand-new platform: key.
        if store.get_policy(org, body.key) is None:
            raise HTTPException(
                status_code=400,
                detail="platform: tag keys are reserved (cannot create new ones)",
            )
    return store.put_policy(
        org,
        TagPolicy(
            key=body.key,
            default_value=body.default_value,
            required=body.required,
            show_on_card=body.show_on_card,
        ),
    )


@router.delete("/tags/{key}",
               dependencies=[Depends(require_scopes("tag:write"))])
def delete_policy(key: str, caller_sub: str = Depends(get_caller_sub)) -> dict:
    if key.startswith("platform:"):
        raise HTTPException(status_code=400, detail="Cannot delete a platform-required tag")
    store = get_tag_policy_store()
    store.delete_policy(_org(caller_sub), key)
    return {"deleted": key}


# -- profiles ----------------------------------------------------------------


@router.get("/tag-profiles", response_model=list[TagProfile],
            dependencies=[Depends(require_scopes("tag:read"))])
def list_profiles(caller_sub: str = Depends(get_caller_sub)) -> list[TagProfile]:
    return get_tag_policy_store().list_profiles(_org(caller_sub))


@router.post("/tag-profiles", response_model=TagProfile,
             dependencies=[Depends(require_scopes("tag:write"))])
def upsert_profile(
    body: TagProfileRequest, caller_sub: str = Depends(get_caller_sub)
) -> TagProfile:
    return get_tag_policy_store().put_profile(
        _org(caller_sub), TagProfile(name=body.name, values=body.values)
    )


@router.delete("/tag-profiles/{name}",
               dependencies=[Depends(require_scopes("tag:write"))])
def delete_profile(name: str, caller_sub: str = Depends(get_caller_sub)) -> dict:
    get_tag_policy_store().delete_profile(_org(caller_sub), name)
    return {"deleted": name}
