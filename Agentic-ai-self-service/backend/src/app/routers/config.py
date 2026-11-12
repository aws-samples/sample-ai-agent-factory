"""Platform configuration the UI reads after sign-in: today, the feature flags.

``GET /api/config/features`` returns every KNOWN flag with its resolved value and how it was
resolved (``source``), so the frontend can hide flagged-off surfaces and an operator can see a
typo in the SSM value (``ignored``). ``GET /api/config/features/canary`` is the self-test
behind ``platformFlagsCanary``: 404 while the flag is off, 200 once it is on.

Both routes sit behind the API Gateway JWT authorizer (access tokens only) and the
``agent:read`` scope that every builder holds; see services/feature_flags.py for the rules.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.services.feature_flags import KNOWN_FEATURE_FLAGS, get_feature_flags, require_feature, ttl_seconds
from app.services.rbac import require_scopes

router = APIRouter(prefix="/api/config", tags=["config"])

CANARY_FLAG = "platformFlagsCanary"


class FeatureFlagsResponse(BaseModel):
    features: dict[str, bool] = Field(description="Every known flag and its resolved value (unset = false).")
    source: str = Field(description="ssm | env | missing | invalid | error; only ssm/env carry real values.")
    parameter: str = Field(description="The SSM parameter operators edit (aws ssm put-parameter --overwrite).")
    ignored: list[str] = Field(
        default_factory=list, description="Keys in the parameter that were unknown or not boolean."
    )
    ttlSeconds: int = Field(description="Per-container cache lifetime; an operator change lands within it.")
    refreshedAt: str
    descriptions: dict[str, str] = Field(description="What each known flag gates.")


class CanaryResponse(BaseModel):
    canary: str
    flag: str


@router.get("/features", response_model=FeatureFlagsResponse, dependencies=[Depends(require_scopes("agent:read"))])
async def read_feature_flags() -> FeatureFlagsResponse:
    flags = get_feature_flags()
    return FeatureFlagsResponse(**flags.as_response(ttl_seconds()), descriptions=dict(KNOWN_FEATURE_FLAGS))


@router.get(
    "/features/canary",
    response_model=CanaryResponse,
    # Flag first: a flagged-off route is a 404 for every caller, scope or not.
    dependencies=[Depends(require_feature(CANARY_FLAG)), Depends(require_scopes("agent:read"))],
)
async def feature_flags_canary() -> CanaryResponse:
    return CanaryResponse(canary="enabled", flag=CANARY_FLAG)
