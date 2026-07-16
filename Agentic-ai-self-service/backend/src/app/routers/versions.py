"""Agent versions API.

Phase 1 Gap 1A endpoints — list versions of a runtime, promote a version to
production, roll back to the previous production version.

Tenant isolation per Critic Finding 3: every read/write checks
``assert_owner`` against the caller's Cognito sub. Cross-tenant access
returns 404 (existence-non-disclosure).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.services.agent_versions_store import (
    AgentVersion,
    RuntimeSlots,
    get_slots_store,
    get_versions_store,
)
from app.services.auth import assert_owner, get_caller_sub
from app.services.rbac import require_scopes

logger = logging.getLogger(__name__)


def _validate_runtime_name(runtime_name: str) -> str:
    if not runtime_name or len(runtime_name) > 64:
        raise HTTPException(status_code=400, detail="Invalid runtime_name")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", runtime_name):
        raise HTTPException(
            status_code=400,
            detail=(
                "runtime_name must match [a-zA-Z][a-zA-Z0-9_]* "
                "(AgentCore naming rules)"
            ),
        )
    return runtime_name


def _validate_version_id(version_id: str) -> str:
    # Our minted ids are 32-char lowercase hex. Allow a slightly looser charset
    # so future schemes (ULIDs in Crockford base32, etc) don't require a router
    # change.
    if not version_id or len(version_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid version_id")
    if not re.match(r"^[a-zA-Z0-9_-]+$", version_id):
        raise HTTPException(status_code=400, detail="Invalid version_id format")
    return version_id


router = APIRouter(prefix="/api/runtimes", tags=["versions"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class VersionResponse(BaseModel):
    runtime_name: str
    version_id: str
    created_at: str
    deployment_id: str
    agentcore_runtime_name: str
    runtime_id: Optional[str] = None
    runtime_arn: Optional[str] = None
    runtime_endpoint: Optional[str] = None
    parent_version_id: Optional[str] = None
    status: str
    description: Optional[str] = None

    @classmethod
    def from_model(cls, v: AgentVersion) -> "VersionResponse":
        return cls(
            runtime_name=v.runtime_name,
            version_id=v.version_id,
            created_at=v.created_at,
            deployment_id=v.deployment_id,
            agentcore_runtime_name=v.agentcore_runtime_name,
            runtime_id=v.runtime_id,
            runtime_arn=v.runtime_arn,
            runtime_endpoint=v.runtime_endpoint,
            parent_version_id=v.parent_version_id,
            status=v.status,
            description=v.description,
        )


class SlotsResponse(BaseModel):
    runtime_name: str
    production_version_id: Optional[str] = None
    staging_version_id: Optional[str] = None
    previous_production_version_id: Optional[str] = None
    last_promoted_at: Optional[str] = None


class PromoteResponse(BaseModel):
    success: bool
    runtime_name: str
    promoted_version_id: str
    slot: str
    previous_version_id: Optional[str] = None
    message: str


class PromoteRequest(BaseModel):
    slot: Literal["staging", "production"] = Field(default="production")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/{runtime_name}/versions", response_model=list[VersionResponse], dependencies=[Depends(require_scopes("agent:read"))])
async def list_versions(
    runtime_name: str,
    caller_sub: str = Depends(get_caller_sub),
) -> list[VersionResponse]:
    """List every version of *runtime_name* owned by the caller, newest first."""
    runtime_name = _validate_runtime_name(runtime_name)
    versions = get_versions_store().list_for_runtime(runtime_name)
    if not versions:
        # Nothing exists — return empty list (don't 404, the runtime name
        # may simply be new). For non-empty results, ownership is checked
        # below; if the runtime exists but the caller doesn't own it, the
        # filter yields an empty list which is indistinguishable from
        # "no versions" — that's intentional (existence-non-disclosure).
        return []
    return [
        VersionResponse.from_model(v)
        for v in versions
        if v.owner_sub == caller_sub
    ]


@router.get("/{runtime_name}/slots", response_model=SlotsResponse, dependencies=[Depends(require_scopes("agent:read"))])
async def get_slots(
    runtime_name: str,
    caller_sub: str = Depends(get_caller_sub),
) -> SlotsResponse:
    """Return the production + staging slot pointers for a runtime."""
    runtime_name = _validate_runtime_name(runtime_name)
    slots = get_slots_store().get(runtime_name)
    if slots is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(slots.owner_sub, caller_sub)
    return SlotsResponse(
        runtime_name=slots.runtime_name,
        production_version_id=slots.production_version_id,
        staging_version_id=slots.staging_version_id,
        previous_production_version_id=slots.previous_production_version_id,
        last_promoted_at=slots.last_promoted_at,
    )


@router.post(
    "/{runtime_name}/versions/{version_id}/promote",
    response_model=PromoteResponse,
    dependencies=[Depends(require_scopes("agent:write"))],
)
async def promote_version(
    runtime_name: str,
    version_id: str,
    body: PromoteRequest = PromoteRequest(),
    caller_sub: str = Depends(get_caller_sub),
) -> PromoteResponse:
    """Move *version_id* into the requested slot (default: production)."""
    runtime_name = _validate_runtime_name(runtime_name)
    version_id = _validate_version_id(version_id)

    versions_store = get_versions_store()
    target = versions_store.get(runtime_name, version_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(target.owner_sub, caller_sub)
    if target.status != "succeeded":
        # Refuse to point a slot at a version whose runtime never came up.
        # The frontend can still display these but they're not promotable.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot promote version {version_id}: status is "
                f"'{target.status}'. Only succeeded deploys can be promoted."
            ),
        )

    slots_store = get_slots_store()
    existing = slots_store.get(runtime_name)
    if existing is not None:
        assert_owner(existing.owner_sub, caller_sub)
    else:
        existing = RuntimeSlots(runtime_name=runtime_name, owner_sub=caller_sub)

    previous = (
        existing.production_version_id
        if body.slot == "production"
        else existing.staging_version_id
    )

    if body.slot == "production":
        existing.previous_production_version_id = existing.production_version_id
        existing.production_version_id = version_id
        existing.last_promoted_at = datetime.now(timezone.utc).isoformat()
    else:
        existing.staging_version_id = version_id

    slots_store.upsert(existing)

    logger.info(
        "Promoted %s/%s to %s slot (caller=%s, previous=%s)",
        runtime_name,
        version_id,
        body.slot,
        caller_sub,
        previous,
    )
    return PromoteResponse(
        success=True,
        runtime_name=runtime_name,
        promoted_version_id=version_id,
        slot=body.slot,
        previous_version_id=previous,
        message=f"Promoted version {version_id} to {body.slot}",
    )


@router.post("/{runtime_name}/rollback", response_model=PromoteResponse, dependencies=[Depends(require_scopes("agent:write"))])
async def rollback_runtime(
    runtime_name: str,
    caller_sub: str = Depends(get_caller_sub),
) -> PromoteResponse:
    """Roll the production slot back to the previous version.

    Implementation: swap ``production_version_id`` ↔
    ``previous_production_version_id``. Subsequent rollbacks therefore
    oscillate between the two most recent productions — explicit
    ``promote()`` calls are required for further history navigation.
    """
    runtime_name = _validate_runtime_name(runtime_name)
    slots_store = get_slots_store()
    slots = slots_store.get(runtime_name)
    if slots is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(slots.owner_sub, caller_sub)

    if not slots.previous_production_version_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "No previous production version to roll back to — this is "
                "the first deploy of this runtime."
            ),
        )

    target_version = slots.previous_production_version_id
    # Confirm the target is a real, succeeded version in the table.
    target = get_versions_store().get(runtime_name, target_version)
    if target is None or target.status != "succeeded":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Previous production version {target_version} is missing or "
                f"not in succeeded state; cannot roll back."
            ),
        )

    rolled_from = slots.production_version_id
    slots.previous_production_version_id = slots.production_version_id
    slots.production_version_id = target_version
    slots.last_promoted_at = datetime.now(timezone.utc).isoformat()
    slots_store.upsert(slots)

    logger.info(
        "Rolled back %s production: %s -> %s (caller=%s)",
        runtime_name,
        rolled_from,
        target_version,
        caller_sub,
    )
    return PromoteResponse(
        success=True,
        runtime_name=runtime_name,
        promoted_version_id=target_version,
        slot="production",
        previous_version_id=rolled_from,
        message=f"Rolled back production to version {target_version}",
    )
