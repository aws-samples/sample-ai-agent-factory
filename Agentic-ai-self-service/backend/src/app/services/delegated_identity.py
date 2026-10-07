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


def workforce_gateway_identity(region: str) -> dict[str, Any]:
    """Build a Gateway authorizer from trusted stack-provided workforce IdP values.

    These values are Lambda environment variables synthesized from the same Cognito
    pool/client protecting the API.  They are not accepted from a deploy request.
    """

    pool_id = os.environ.get("WORKFORCE_USER_POOL_ID", "").strip()
    client_id = os.environ.get("WORKFORCE_CLIENT_ID", "").strip()
    workforce_region = os.environ.get("WORKFORCE_REGION", "").strip() or region
    if not pool_id or not client_id:
        raise DelegatedIdentityError(
            "OBO delegation requires the platform workforce user pool and client to be configured"
        )
    if not pool_id.startswith(f"{workforce_region}_"):
        raise DelegatedIdentityError("The workforce user pool does not belong to the configured region")
    discovery_url = (
        f"https://cognito-idp.{workforce_region}.amazonaws.com/"
        f"{pool_id}/.well-known/openid-configuration"
    )
    return {
        "authorizer_config": {
            "customJWTAuthorizer": {
                "discoveryUrl": discovery_url,
                "allowedClients": [client_id],
            }
        },
        # Deliberately contains no OAuth client secret or token endpoint.  An OBO
        # caller must present its own workforce token on every invocation.
        "client_info": {
            "provider": "workforce_jwt",
            "delegation_mode": "obo",
        },
        "resource_server_created": False,
    }


def runtime_obo_authorizer(region: str) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Return the supported Runtime JWT authorizer and header allowlist shapes."""

    identity = workforce_gateway_identity(region)
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


def extract_user_bearer(headers: Mapping[str, Any], *, now: float | None = None) -> str:
    """Return one fresh JWT from an Authorization header; reject every other scheme.

    API Gateway already verifies signature, issuer, client and scopes.  This local
    structural/expiry check prevents SigV4, Basic, junk, or a stale token from being
    repurposed as the OBO subject if the route is called outside that authorizer.
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
    if not claims.get("sub") or not claims.get("iss"):
        raise DelegatedIdentityError("The delegated bearer token lacks subject or issuer claims")
    return token
