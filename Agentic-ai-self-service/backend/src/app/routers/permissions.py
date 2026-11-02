"""JIT IAM permission-request API (Loom-study 1.6).

Auditable escalation path instead of over-provisioning roles up front:
  POST   /api/permissions/requests            create a request (any authed builder)
  GET    /api/permissions/requests/pending    admin pending queue
  POST   /api/permissions/requests/{id}/approve   approve + widen the role (admin)
  POST   /api/permissions/requests/{id}/reject    reject (admin)

Approve conditionally claims ``PENDING → APPLYING`` before IAM. Rejection can
transition only from ``PENDING``; an IAM write that may have landed can therefore
never be recorded as rejected. The deterministic `JIT-{request_id}` policy is safe
to reapply after an expired lease, then the same token finalizes ``APPROVED``.
Approval widens ONLY the platform's own AgentCore* managed roles (the deployment
role's iam:PutRolePolicy is scoped to arn:...:role/AgentCore*), never a SHARED
runtime role (every tenant's identity; infra denies mutating it outright), and the
requested actions are validated against an allowlist so a request can't grant
iam:* / *. Resources must be ARNs; a bare "*" is accepted only for a read-only
action set (F-05).
"""

from __future__ import annotations

import logging
import os
import re
from fnmatch import fnmatchcase

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.routers.registry import _caller_org_id, caller_is_admin
from app.services.auth import get_caller_sub
from app.services.iam_boundary import is_shared_runtime_role
from app.services.permission_request_store import (
    PermissionRequestNotPending,
    PermissionRequestStore,
)
from app.services.rbac import require_scopes
from app.services.runtime_policy_safety import FORBIDDEN_RUNTIME_CONTROL_ACTIONS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/permissions", tags=["permissions"])

_ROLE_RE = re.compile(r"^AgentCore[A-Za-z0-9_+=,.@-]{0,120}$")
# Deny obviously dangerous escalations regardless of the PutRolePolicy ARN scope.
_FORBIDDEN_ACTION_PREFIXES = ("iam:", "sts:", "organizations:", "account:")
# A resource is either an ARN (wildcards inside it are fine: "arn:aws:s3:::bucket/*") or the
# bare "*", and the bare form is accepted only for a read-only action set (F-05). The account
# field may be empty (S3) or "aws" (managed policies); the region field may be empty.
_ARN_RE = re.compile(r"^arn:aws[a-zA-Z-]*:[a-z0-9-]+:[a-z0-9-]*:(\d{12}|aws)?:.+$")
_DEFINITIVE_IAM_REFUSALS = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "MalformedPolicyDocument",
        "NoSuchEntity",
        "UnmodifiableEntity",
        "ValidationError",
    }
)
# Verbs that only read. A wildcard anywhere in the action disqualifies it: "s3:Get*" also
# matches nothing today and something tomorrow, and "s3:*" is every verb.
_READ_ONLY_VERB_PREFIXES = ("get", "list", "describe", "batchget", "query", "scan", "head")
_APPROVAL_LEASE_SECONDS = 300
# The shared runtime roles are every tenant's execution identity (Bug 60 / Bug 62): widening one
# widens every deployed agent in the stack, and infra now denies every role-mutating verb on
# them, so an approve would 502 and strand the request. The name set is the one the deploy path's
# adopt branches refuse too (services/iam_boundary.is_shared_runtime_role), so the two cannot drift.


def _is_read_only_action(action: str) -> bool:
    service, sep, verb = action.partition(":")
    if not sep or not service or not verb or "*" in action or "?" in action:
        return False
    return verb.lower().startswith(_READ_ONLY_VERB_PREFIXES)


def _definitive_iam_refusal(exc: BaseException) -> bool:
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in _DEFINITIVE_IAM_REFUSALS


def _policy_violation(role_name: str, actions: list[str], resources: list[str]) -> str | None:
    """Why this (role, actions, resources) triple must not become an inline policy, or None.

    One function for both the create path (400 to the requester) and the approve path (400 on a
    row that was tampered with or written by an older build), so the two cannot drift.
    """
    if not _ROLE_RE.match(role_name or ""):
        return "role_name must be a platform AgentCore* role"
    if is_shared_runtime_role(role_name):
        return (
            "role_name is a shared runtime role, which is every tenant's execution identity; "
            "request the widening on the agent's own per-agent role instead"
        )
    if not actions:
        return "at least one action is required"
    for action in actions:
        low = action.lower()
        if any(fnmatchcase(forbidden.lower(), low) for forbidden in FORBIDDEN_RUNTIME_CONTROL_ACTIONS):
            return f"Gateway/Policy control-plane action not permitted via JIT request: {action}"
        if low == "*" or any(low.startswith(p) for p in _FORBIDDEN_ACTION_PREFIXES):
            return f"Action not permitted via JIT request: {action}"
    if not resources:
        return "at least one resource is required"
    for resource in resources:
        if resource == "*":
            if not all(_is_read_only_action(a) for a in actions):
                return (
                    'Resource "*" is only permitted when every action is read-only '
                    "(Get*/List*/Describe*/BatchGet*/Query/Scan/Head*, no wildcards); name the resource ARNs"
                )
        elif not _ARN_RE.match(resource):
            return f"Resource is not an ARN: {resource[:120]}"
    return None


def _get_store() -> PermissionRequestStore:
    return PermissionRequestStore(
        table_name=os.environ.get("PERMISSION_REQUESTS_TABLE_NAME", "PermissionRequests"),
        region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
    )


class CreateRequest(BaseModel):
    role_name: str = Field(alias="roleName", min_length=1, max_length=128)
    actions: list[str] = Field(min_length=1, max_length=50)
    resources: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=50)
    justification: str = Field(min_length=1, max_length=2000)
    model_config = {"populate_by_name": True}


class DecideRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)


def _validate_request(body: CreateRequest) -> None:
    violation = _policy_violation(body.role_name, body.actions, body.resources)
    if violation is not None:
        raise HTTPException(status_code=400, detail=violation)


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
    request_id: str,
    body: DecideRequest,
    caller_sub: str = Depends(get_caller_sub),
    is_admin: bool = Depends(caller_is_admin),
) -> dict:
    """Approve a request AND widen the target role's inline policy."""
    if not is_admin:
        raise HTTPException(status_code=403, detail="Requires an admin persona")
    store = _get_store()
    org_id = _caller_org_id(caller_sub)
    try:
        req = store.claim_approval(
            org_id,
            request_id,
            decided_by=caller_sub,
            reason=body.reason,
            lease_seconds=_APPROVAL_LEASE_SECONDS,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except PermissionRequestNotPending as exc:
        raise HTTPException(status_code=409, detail=f"Request already {exc}") from exc

    # Re-validate the row returned by the conditional claim. This is the exact
    # policy the IAM write will use, not a stale read from before the lease.
    violation = _policy_violation(req.role_name, list(req.actions or []), list(req.resources or []))
    if violation is not None:
        try:
            store.complete_approval(
                org_id,
                request_id,
                approval_token=req.approval_token or "",
                status="REJECTED",
                decided_by=caller_sub,
                reason=f"Approval validation failed: {violation}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Invalid JIT request could not be finalized (%s)", type(exc).__name__)
            raise HTTPException(
                status_code=503,
                detail=(
                    "Request failed approval validation, but rejection evidence is unconfirmed; "
                    "retry after the approval lease expires"
                ),
                headers={"Retry-After": str(_APPROVAL_LEASE_SECONDS)},
            ) from exc
        raise HTTPException(status_code=400, detail=f"Request fails policy validation: {violation}")
    if not req.approval_token:
        raise HTTPException(
            status_code=503,
            detail="Approval lease evidence is unavailable; no permission was applied",
        )

    from app.services.iam_manager import _create_iam_client, _put_role_inline_policy

    policy_doc = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": req.actions, "Resource": req.resources}],
    }
    try:
        _put_role_inline_policy(_create_iam_client(), req.role_name, f"JIT-{request_id}", policy_doc)
    except Exception as exc:  # noqa: BLE001
        if _definitive_iam_refusal(exc):
            try:
                store.release_approval(
                    org_id,
                    request_id,
                    approval_token=req.approval_token,
                )
            except Exception as release_exc:  # noqa: BLE001
                logger.error("Definitive JIT refusal lease release failed (%s)", type(release_exc).__name__)
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "IAM refused the permission, but request recovery is unconfirmed; "
                        "the approval cannot be rejected yet"
                    ),
                    headers={"Retry-After": str(_APPROVAL_LEASE_SECONDS)},
                ) from release_exc
            logger.warning("JIT approval was definitively refused by IAM (%s)", type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail="IAM refused the permission; the request returned to PENDING and no policy was applied",
            ) from exc
        # A timeout or transport error is ambiguous: IAM may have applied the
        # deterministic policy. Keep APPLYING so rejection cannot race it; an
        # admin may reclaim the expired lease and idempotently apply/finalize.
        logger.warning("JIT approval IAM write outcome is unconfirmed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail=(
                "Permission application is unconfirmed; the request remains APPLYING. "
                "Retry approval after the lease expires; do not reject it."
            ),
            headers={"Retry-After": str(_APPROVAL_LEASE_SECONDS)},
        ) from exc

    try:
        decided = store.complete_approval(
            org_id,
            request_id,
            approval_token=req.approval_token,
            status="APPROVED",
            decided_by=caller_sub,
            reason=body.reason,
        )
    except PermissionRequestNotPending as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "Permission was applied but another approval lease now owns finalization; retry after its lease expires"
            ),
            headers={"Retry-After": str(_APPROVAL_LEASE_SECONDS)},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("JIT approval evidence could not be finalized (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail=(
                "Permission was applied but approval evidence is unconfirmed; "
                "the request remains APPLYING and cannot be rejected"
            ),
            headers={"Retry-After": str(_APPROVAL_LEASE_SECONDS)},
        ) from exc
    return {"request_id": request_id, "status": decided.status}


@router.post("/requests/{request_id}/reject", dependencies=[Depends(require_scopes("settings:write"))])
async def reject(
    request_id: str,
    body: DecideRequest,
    caller_sub: str = Depends(get_caller_sub),
    is_admin: bool = Depends(caller_is_admin),
) -> dict:
    if not is_admin:
        raise HTTPException(status_code=403, detail="Requires an admin persona")
    try:
        decided = _get_store().decide(
            _caller_org_id(caller_sub),
            request_id,
            status="REJECTED",
            decided_by=caller_sub,
            reason=body.reason,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Not found") from e
    except PermissionRequestNotPending as e:
        raise HTTPException(status_code=409, detail=f"Request already {e}") from e
    return {"request_id": request_id, "status": decided.status}
