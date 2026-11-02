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
    from app.services.region_capabilities import SUPPORTED_SINGLE_REGION_RELEASES

    def _admissible(region: object) -> bool:
        # Only offer what the deploy route would admit. A legacy allowlist row outside
        # the release envelope is filtered here AND refused at deploy time; the panel is
        # not the enforcement point, it is just never shown an inadmissible choice.
        return isinstance(region, str) and region in SUPPORTED_SINGLE_REGION_RELEASES

    enabled = dt.targets_enabled()
    accounts = []
    regions = []
    region_targets = []
    if enabled:
        registered_regions = [target for target in dt.list_region_targets() if _admissible(target.get("region"))]
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
                if target.get("account_id") and _admissible(target.get("region") or dt.home_region())
            ],
            key=lambda target: (target["account_id"], target["region"]),
        )
    return {
        "enabled": enabled,
        "home_region": dt.home_region(),
        "home_region_admissible": _admissible(dt.home_region()),
        "release_regions": sorted(SUPPORTED_SINGLE_REGION_RELEASES),
        "regions": regions,
        "region_targets": region_targets,
        "accounts": accounts,
    }
