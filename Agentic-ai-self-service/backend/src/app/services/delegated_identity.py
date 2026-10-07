"""Fail-closed workforce identity handoff for AgentCore OBO connectors.

The control plane marks a deployment as delegated only when a connector explicitly
requests ``delegationMode=obo``.  Runtime invocations then carry the already-
authenticated workforce JWT ephemerally to the generated agent, which presents it
to the Gateway.  The Gateway validates the same issuer/client and performs the
configured OAuth token exchange for the selected target.

No token is logged, persisted, cached, or returned as evidence.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any


class DelegatedIdentityError(ValueError):
    """The requested delegated-identity contract cannot be established."""


def obo_requested(connectors: Sequence[Mapping[str, Any]] | None) -> bool:
    """Whether any connector explicitly requires on-behalf-of delegation."""

    return any(
        str(connector.get("delegation_mode") or connector.get("delegationMode") or "m2m").lower() == "obo"
        for connector in (connectors or ())
        if isinstance(connector, Mapping)
    )


def deployment_requires_obo(deployment: Mapping[str, Any] | None) -> bool:
    """Read the durable OBO marker produced by Gateway deployment."""

    gateway = (deployment or {}).get("gateway_result") or {}
    return isinstance(gateway, Mapping) and gateway.get("obo_required") is True


def _workforce_contract(region: str) -> tuple[str, str]:
    """Return the stack-bound workforce issuer and client identifier."""

    pool_id = os.environ.get("WORKFORCE_USER_POOL_ID", "").strip()
    client_id = os.environ.get("WORKFORCE_CLIENT_ID", "").strip()
    workforce_region = os.environ.get("WORKFORCE_REGION", "").strip() or region
    if not pool_id or not client_id:
        raise DelegatedIdentityError(
            "OBO delegation requires the platform workforce user pool and client to be configured"
        )
    if not pool_id.startswith(f"{workforce_region}_"):
        raise DelegatedIdentityError("The workforce user pool does not belong to the configured region")
    issuer = f"https://cognito-idp.{workforce_region}.amazonaws.com/{pool_id}"
    return issuer, client_id


def _owner_claim(owner_sub: str) -> dict[str, Any]:
    """Build an exact owner claim accepted by AgentCore's JWT authorizer model."""

    owner_sub = str(owner_sub or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", owner_sub):
        raise DelegatedIdentityError("OBO delegation requires a valid deployment owner subject")
    return {
        "inboundTokenClaimName": "sub",
        "inboundTokenClaimValueType": "STRING",
        "authorizingClaimMatchValue": {
            "claimMatchValue": {"matchValueString": owner_sub},
            "claimMatchOperator": "EQUALS",
        },
    }


def workforce_token_expectations(region: str) -> tuple[str, str]:
    """Return trusted issuer/client values used to bind the API bearer hop."""

    return _workforce_contract(region)


def workforce_gateway_identity(region: str, owner_sub: str) -> dict[str, Any]:
    """Build an owner-bound authorizer from trusted stack-provided IdP values.

    These values are Lambda environment variables synthesized from the same Cognito
    pool/client protecting the API. They are not accepted from a deploy request.
    The exact ``sub`` rule prevents another valid platform user from invoking this
    tenant's Runtime or Gateway directly with their own workforce token.
    """

    issuer, client_id = _workforce_contract(region)
    return {
        "authorizer_config": {
            "customJWTAuthorizer": {
                "discoveryUrl": f"{issuer}/.well-known/openid-configuration",
                "allowedClients": [client_id],
                "customClaims": [_owner_claim(owner_sub)],
            }
        },
        # Deliberately contains no OAuth client secret or token endpoint. An OBO
        # caller must present its own workforce token on every invocation.
        "client_info": {
            "provider": "workforce_jwt",
            "delegation_mode": "obo",
        },
        "resource_server_created": False,
    }


def runtime_obo_authorizer(
    region: str,
    owner_sub: str,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Return owner-bound Runtime JWT and Authorization-header shapes."""

    identity = workforce_gateway_identity(region, owner_sub)
    return (
        identity["authorizer_config"],
        {"requestHeaderAllowlist": ["Authorization"]},
    )


def _decode_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise DelegatedIdentityError("A delegated invocation requires a JWT bearer token")
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise DelegatedIdentityError("A delegated invocation requires a valid JWT bearer token") from exc
    if not isinstance(claims, dict):
        raise DelegatedIdentityError("A delegated invocation requires a valid JWT bearer token")
    return claims


def extract_user_bearer(
    headers: Mapping[str, Any],
    *,
    expected_subject: str,
    expected_issuer: str,
    expected_client_id: str,
    now: float | None = None,
) -> str:
    """Return one fresh, caller-bound workforce JWT from Authorization.

    API Gateway verifies the signature. This second boundary pins the exact token
    claims that will cross into Runtime/Gateway to the already-authorized caller and
    the stack-bound workforce issuer/client, preventing credential substitution if a
    route or test harness is ever invoked outside that authorizer.
    """

    raw = str(headers.get("authorization") or headers.get("Authorization") or "").strip()
    if not raw.lower().startswith("bearer "):
        raise DelegatedIdentityError("A delegated invocation requires a bearer token")
    token = raw[7:].strip()
    if not token or any(character.isspace() for character in token):
        raise DelegatedIdentityError("A delegated invocation requires a single bearer token")
    claims = _decode_claims(token)
    expiry = claims.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        raise DelegatedIdentityError("The delegated bearer token has no valid expiry")
    if float(expiry) <= (time.time() if now is None else now):
        raise DelegatedIdentityError("The delegated bearer token has expired")
    if claims.get("sub") != expected_subject:
        raise DelegatedIdentityError("The delegated bearer token does not belong to the authenticated caller")
    if claims.get("iss") != expected_issuer:
        raise DelegatedIdentityError("The delegated bearer token has the wrong issuer")
    if claims.get("client_id") != expected_client_id:
        raise DelegatedIdentityError("The delegated bearer token has the wrong client")
    if claims.get("token_use") != "access":
        raise DelegatedIdentityError("The delegated bearer token is not an access token")
    return token


def delegated_bearer_identity(token: str) -> tuple[str, str, float]:
    """Return only non-credential identity fields from a validated bearer token."""

    claims = _decode_claims(token)
    subject = claims.get("sub")
    issuer = claims.get("iss")
    expiry = claims.get("exp")
    if (
        not isinstance(subject, str)
        or not subject
        or not isinstance(issuer, str)
        or not issuer
        or isinstance(expiry, bool)
        or not isinstance(expiry, (int, float))
    ):
        raise DelegatedIdentityError("The delegated bearer token has invalid identity claims")
    return subject, issuer, float(expiry)
