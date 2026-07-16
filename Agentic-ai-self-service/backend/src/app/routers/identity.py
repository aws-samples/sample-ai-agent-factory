"""Identity inspection + OBO verification API (Loom-study Phase 1: 1.2 + 1.3).

Two read-oriented endpoints that make the platform's identity/delegation story
inspectable — the "prove the user identity actually propagates" evidence
enterprises ask for:

  GET  /api/identity/token-info  → the signed-in caller's decoded claims +
       resolved group→scope mapping (the token-info card, 1.3).
  POST /api/identity/test-obo    → dry-run the AgentCore RFC 8693 on-behalf-of
       exchange for a connector and return decoded before/after claims (1.2),
       so an admin can confirm delegation runs AS THE USER before shipping.

No secrets are returned — only decoded CLAIMS (not credential material).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.services.auth import get_caller_claims, get_caller_sub
from app.services.obo_verifier import annotate_claims, dry_run_obo_exchange
from app.services.rbac import caller_scopes, require_scopes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/identity", tags=["identity"])


class TokenInfoResponse(BaseModel):
    sub: str
    claims: list[dict]  # [{claim, value, note}]
    groups: list[str]
    scopes: list[str]


@router.get("/token-info", response_model=TokenInfoResponse)
async def token_info(request: Request, caller_sub: str = Depends(get_caller_sub)) -> TokenInfoResponse:
    """Return the caller's decoded identity: claims + resolved groups + scopes.

    Auth-gated (any authenticated caller can inspect THEIR OWN token) — no scope
    requirement beyond being signed in.
    """
    claims = get_caller_claims(request)
    groups = claims.get("cognito:groups")
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.strip("[]").replace(",", " ").split() if g.strip()]
    elif not isinstance(groups, list):
        groups = []
    return TokenInfoResponse(
        sub=caller_sub,
        claims=annotate_claims(claims),
        groups=[str(g) for g in groups],
        scopes=sorted(caller_scopes(request)),
    )


class TestOboRequest(BaseModel):
    """Dry-run an OBO exchange. The user_token is the caller's own bearer token
    (the frontend supplies it); it is used ONCE for the exchange and never stored."""

    workload_name: str = Field(alias="workloadName", min_length=1, max_length=256)
    resource_provider_name: str = Field(alias="resourceProviderName", min_length=1, max_length=256)
    user_token: str = Field(alias="userToken", min_length=1)
    scopes: list[str] = Field(default_factory=list)
    audience: str | None = Field(alias="audience", default=None, max_length=512)

    model_config = {"populate_by_name": True}


@router.post("/test-obo", dependencies=[Depends(require_scopes("settings:read"))])
async def test_obo(body: TestOboRequest, caller_sub: str = Depends(get_caller_sub)) -> dict:
    """Dry-run the on-behalf-of token exchange and return decoded before/after claims.

    Gated on settings:read (an admin-ish inspection capability). Best-effort — a
    failed exchange returns ok=false with the error (e.g. Okta 'audience required'),
    which is itself useful diagnostic output.
    """
    import boto3

    identity_client = boto3.client("bedrock-agentcore")
    return dry_run_obo_exchange(
        identity_client,
        workload_name=body.workload_name,
        user_token=body.user_token,
        resource_provider_name=body.resource_provider_name,
        scopes=body.scopes,
        audience=body.audience,
    )
