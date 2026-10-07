"""Read-only deployment-target choices for users who can deploy agents.

Target registration and role details remain admin-only under ``/api/admin``.
The deploy panel only needs the allowlisted account/region choices, so this
route deliberately omits IAM role ARNs and requires the same ``agent:write``
scope as a deployment.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.services.auth import get_caller_sub
from app.services.rbac import require_scopes

router = APIRouter(prefix="/api/deploy-targets", tags=["deploy-targets"])


@router.get("", dependencies=[Depends(require_scopes("agent:write"))])
async def list_deploy_target_options(
    _caller_sub: str = Depends(get_caller_sub),
) -> dict:
    from app.services import deploy_target as dt

    enabled = dt.targets_enabled()
    accounts = []
    regions = []
    region_targets = []
    if enabled:
        registered_regions = dt.list_region_targets()
        regions = sorted({str(target.get("region")) for target in registered_regions if target.get("region")})
        region_targets = sorted(
            [
                {
                    "region": str(target.get("region")),
                    "production_promotion_supported": bool(target.get("evaluation_kms_key_arn")),
                }
                for target in registered_regions
                if target.get("region")
            ],
            key=lambda target: target["region"],
        )
        accounts = sorted(
            [
                {
                    "account_id": str(target.get("account_id", "")),
                    "region": str(target.get("region") or dt.home_region()),
                    "production_promotion_supported": bool(target.get("evaluation_kms_key_arn")),
                }
                for target in dt.list_accounts()
                if target.get("account_id")
            ],
            key=lambda target: (target["account_id"], target["region"]),
        )
    return {
        "enabled": enabled,
        "home_region": dt.home_region(),
        "regions": regions,
        "region_targets": region_targets,
        "accounts": accounts,
    }
