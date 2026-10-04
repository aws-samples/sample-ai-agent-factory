"""Admin analytics API (Phase 5 — Loom-inspired audit dashboard).

Exposes the action-audit log for super-admins: action counts, per-actor
activity, and a recent-events timeline. Requires the ``admin`` scope (Phase 1),
so only super-admins (g-admins-super / org-admin) can read the org's audit trail.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.auth import get_caller_sub
from app.services.rbac import require_scopes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/audit", dependencies=[Depends(require_scopes("admin"))])
async def get_audit(
    limit: int = Query(default=200, ge=1, le=1000),
    _caller_sub: str = Depends(get_caller_sub),
) -> dict:
    """Return {total, by_action, by_actor, events[]} over recent audit events.

    Best-effort: if the audit table is unavailable (fresh stack) return an empty
    summary rather than 500 — the dashboard renders an empty state.
    """
    try:
        from app.services.audit_store import get_audit_store

        return get_audit_store().summarize("default", limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit summarize failed (returning empty): %s", exc)
        return {"total": 0, "by_action": {}, "by_actor": {}, "events": []}


# ---------------------------------------------------------------------------
# Phase 7 (opt-in) — multi-region / multi-account deployment targets. Admin
# only; OFF by default. Enabling is an explicit, audited admin action.
# ---------------------------------------------------------------------------


class EnableTargetsRequest(BaseModel):
    enabled: bool


class RegionTargetRequest(BaseModel):
    region: str = Field(min_length=2, max_length=32, pattern=r"^[a-z]{2}-[a-z]+-\d$")
    artifact_bucket: str | None = Field(
        default=None,
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$",
    )


class AccountTargetRequest(BaseModel):
    account_id: str = Field(pattern=r"^\d{12}$")
    role_arn: str = Field(
        pattern=(
            r"^arn:aws(?:-[a-z0-9-]+)?:iam::\d{12}:"
            r"role/AgentCoreFlowsDeploymentRole$"
        )
    )
    runtime_role_arn: str | None = Field(
        default=None,
        pattern=r"^arn:aws(?:-[a-z0-9-]+)?:iam::\d{12}:role/.+$",
    )
    mcp_runtime_role_arn: str | None = Field(
        default=None,
        pattern=r"^arn:aws(?:-[a-z0-9-]+)?:iam::\d{12}:role/.+$",
    )
    harness_role_arn: str | None = Field(
        default=None,
        pattern=r"^arn:aws(?:-[a-z0-9-]+)?:iam::\d{12}:role/.+$",
    )
    artifact_bucket: str | None = Field(
        default=None,
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$",
    )
    region: str = Field(min_length=2, max_length=32, pattern=r"^[a-z]{2}-[a-z]+-\d$")


@router.get("/deploy-targets", dependencies=[Depends(require_scopes("admin"))])
async def get_deploy_targets(_caller_sub: str = Depends(get_caller_sub)) -> dict:
    """Current deployment-targets config (feature flag + region/account allowlists)."""
    from app.services import deploy_target as dt

    return {
        "enabled": dt.targets_enabled(),
        "regions": dt.list_regions(),
        "region_targets": [
            {
                "region": target.get("region"),
                "account_id": target.get("account_id"),
                "artifact_bucket": target.get("artifact_bucket"),
            }
            for target in dt.list_region_targets()
        ],
        "accounts": [
            {
                "account_id": a.get("account_id"),
                "role_arn": a.get("role_arn"),
                "runtime_role_arn": a.get("runtime_role_arn") or dt.default_runtime_role_arn(a.get("account_id")),
                "mcp_runtime_role_arn": a.get("mcp_runtime_role_arn")
                or dt.default_mcp_runtime_role_arn(a.get("account_id")),
                "harness_role_arn": a.get("harness_role_arn") or dt.default_harness_role_arn(a.get("account_id")),
                "artifact_bucket": a.get("artifact_bucket")
                or dt.default_artifact_bucket_name(
                    a.get("account_id"),
                    a.get("region") or dt.home_region(),
                ),
                "region": a.get("region"),
            }
            for a in dt.list_accounts()
        ],
    }


@router.post("/deploy-targets/enable", dependencies=[Depends(require_scopes("admin"))])
async def enable_deploy_targets(body: EnableTargetsRequest, _caller_sub: str = Depends(get_caller_sub)) -> dict:
    """Explicitly enable/disable multi-region/account deployment (default off)."""
    from app.services import deploy_target as dt

    dt.set_targets_enabled(body.enabled)
    return {"enabled": body.enabled}


@router.post("/deploy-targets/regions", dependencies=[Depends(require_scopes("admin"))])
async def add_region_target(body: RegionTargetRequest, _caller_sub: str = Depends(get_caller_sub)) -> dict:
    from app.services import deploy_target as dt

    if not dt.targets_enabled():
        raise HTTPException(status_code=400, detail="Enable deployment targets first")
    try:
        session = dt.session_for_target(
            account_id=None,
            region=body.region,
            require_gate=False,
        )
        account_id = str(session.client("sts").get_caller_identity()["Account"])
        dt.require_platform_bucket_namespace(account_id, body.region, body.artifact_bucket)
        artifact_bucket = dt.validate_artifact_bucket(
            session,
            account_id=account_id,
            region=body.region,
            artifact_bucket=body.artifact_bucket,
            same_account=True,
        )
    except dt.TargetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail="The platform account or regional artifacts bucket could not be validated",
        ) from exc

    dt.add_region(
        body.region,
        account_id=account_id,
        artifact_bucket=artifact_bucket,
    )
    return {
        "region": body.region,
        "account_id": account_id,
        "artifact_bucket": artifact_bucket,
        "validated": True,
        "regions": dt.list_regions(),
        "region_targets": dt.list_region_targets(),
    }


@router.post("/deploy-targets/accounts", dependencies=[Depends(require_scopes("admin"))])
async def add_account_target(body: AccountTargetRequest, _caller_sub: str = Depends(get_caller_sub)) -> dict:
    """Register a cross-account deployment target. Validates the role is
    assumable + lands in the expected account BEFORE persisting (so a bad role
    ARN fails loudly here, not mid-deploy)."""
    from app.services import deploy_target as dt

    if not dt.targets_enabled():
        raise HTTPException(status_code=400, detail="Enable deployment targets first")
    try:
        session = dt.session_for_target(
            account_id=body.account_id,
            region=body.region,
            role_arn=body.role_arn,
        )
        runtime_role_arn, mcp_runtime_role_arn, harness_role_arn = dt.validate_execution_roles(
            session,
            account_id=body.account_id,
            runtime_role_arn=body.runtime_role_arn,
            mcp_runtime_role_arn=body.mcp_runtime_role_arn,
            harness_role_arn=body.harness_role_arn,
        )
        artifact_bucket = dt.validate_artifact_bucket(
            session,
            account_id=body.account_id,
            region=body.region,
            artifact_bucket=body.artifact_bucket,
        )
    except dt.TargetError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    dt.add_account(
        body.account_id,
        body.role_arn,
        body.region,
        runtime_role_arn=runtime_role_arn,
        mcp_runtime_role_arn=mcp_runtime_role_arn,
        harness_role_arn=harness_role_arn,
        artifact_bucket=artifact_bucket,
    )
    return {
        "account_id": body.account_id,
        "runtime_role_arn": runtime_role_arn,
        "mcp_runtime_role_arn": mcp_runtime_role_arn,
        "harness_role_arn": harness_role_arn,
        "artifact_bucket": artifact_bucket,
        "validated": True,
    }
