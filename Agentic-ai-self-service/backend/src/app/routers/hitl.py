"""Human-in-the-loop (HITL) approval API — Phase 2 Gap 2D.

A generated agent's injected ``human_approval`` @tool writes a PENDING row to
the HitlRequests table and returns a "waiting for approval" sentinel. This
router lets the deployer (the human approver) view their pending queue and
APPROVE / REJECT each request.

Endpoints:
  GET  /api/hitl/pending                     owner-scoped PENDING queue (GSI)
  POST /api/hitl/{request_id}/decision       approve|reject a PENDING request

Tenant isolation (Critic Finding 3, Bug 37): every endpoint depends on
``get_caller_sub``; the decide path resolves the row, calls
``assert_owner(row.owner_sub, caller_sub)`` (404 on cross-tenant — existence-
non-disclosure), and rejects a non-PENDING request with 409.

The PK is ``runtime_id`` — an opaque, server-supplied string. The injected
@tool stamps it with the AgentCore runtime *name* (``agentcore_runtime_name``,
injected as the ``HITL_RUNTIME_ID`` env var), which is stable + owner-scoped
and known at configure time (the canonical agentRuntimeId does not exist until
the runtime is created, after env vars are already fixed). It is not
tenant-typed, so the Bug 122 PK-collision class does not apply; the isolation
guarantee is the owner_sub GSI for the queue plus assert_owner on decide().
"""

from __future__ import annotations

import logging
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services.auth import assert_owner, get_caller_sub
from app.services.hitl_store import (
    STATUS_APPROVED,
    STATUS_REJECTED,
    HitlNotPending,
    HitlRequest,
    get_hitl_store,
)

logger = logging.getLogger(__name__)


# Minted request ids are 32-char lowercase hex. Allow a slightly looser charset
# so a future id scheme doesn't require a router change, but still constrain it
# to a safe key charset and bounded length.
_REQUEST_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
# runtime_id is AgentCore-shaped or a stable per-deploy identifier.
_RUNTIME_ID_RE = re.compile(r"^[a-zA-Z0-9_.:/-]+$")


def _validate_request_id(request_id: str) -> str:
    if not request_id or len(request_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid request_id")
    if not _REQUEST_ID_RE.match(request_id):
        raise HTTPException(status_code=400, detail="Invalid request_id format")
    return request_id


def _validate_runtime_id(runtime_id: str) -> str:
    if not runtime_id or len(runtime_id) > 256:
        raise HTTPException(status_code=400, detail="Invalid runtime_id")
    if not _RUNTIME_ID_RE.match(runtime_id):
        raise HTTPException(status_code=400, detail="Invalid runtime_id format")
    return runtime_id


router = APIRouter(prefix="/api/hitl", tags=["hitl"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=2000)
    # runtime_id is part of the table PK; the @tool stamps it on the row and
    # the frontend echoes it back from the pending-queue payload so the router
    # can locate the exact row to decide.
    runtime_id: str = Field(min_length=1, max_length=256)


class HitlRequestResponse(BaseModel):
    runtime_id: str
    request_id: str
    status: str
    action: str
    reason: str
    created_at: int
    comment: Optional[str] = None
    decided_at: Optional[str] = None

    @classmethod
    def from_model(cls, r: HitlRequest) -> "HitlRequestResponse":
        return cls(
            runtime_id=r.runtime_id,
            request_id=r.request_id,
            status=r.status,
            action=r.action,
            reason=r.reason,
            created_at=r.created_at,
            comment=r.comment,
            decided_at=r.decided_at,
        )


class DecisionResponse(BaseModel):
    success: bool
    request_id: str
    status: str
    message: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/pending", response_model=list[HitlRequestResponse])
async def list_pending(
    caller_sub: str = Depends(get_caller_sub),
) -> list[HitlRequestResponse]:
    """Return the caller's PENDING approval requests, newest first.

    Owner-scoped via the owner_sub GSI — a caller only ever sees rows stamped
    with their own sub, so there's no cross-tenant leakage.
    """
    requests = get_hitl_store().list_pending_for_owner(caller_sub)
    return [HitlRequestResponse.from_model(r) for r in requests]


@router.post("/{request_id}/decision", response_model=DecisionResponse)
async def decide(
    request_id: str,
    body: DecisionRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> DecisionResponse:
    """Approve or reject a PENDING request the caller owns."""
    request_id = _validate_request_id(request_id)
    runtime_id = _validate_runtime_id(body.runtime_id)

    store = get_hitl_store()
    row = store.get(runtime_id, request_id)
    if row is None:
        # 404 (not 403) — don't disclose existence of rows the caller can't
        # see. Same rule as services.auth.assert_owner.
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(row.owner_sub, caller_sub)  # 404 on cross-tenant

    new_status = STATUS_APPROVED if body.decision == "approve" else STATUS_REJECTED
    comment = body.comment or None
    try:
        updated = store.decide(
            runtime_id=runtime_id,
            request_id=request_id,
            decision=new_status,
            decided_by=caller_sub,
            comment=comment,
        )
    except HitlNotPending as e:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Request {request_id} is not PENDING (status={e.status}); "
                f"it has already been decided."
            ),
        ) from e

    logger.info(
        "HITL request %s/%s decided %s by %s",
        runtime_id,
        request_id,
        updated.status,
        caller_sub,
    )
    return DecisionResponse(
        success=True,
        request_id=request_id,
        status=updated.status,
        message=f"Request {request_id} {updated.status.lower()}.",
    )
