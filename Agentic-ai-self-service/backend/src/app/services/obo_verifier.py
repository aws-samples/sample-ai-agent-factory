"""OBO (on-behalf-of) token-exchange dry-run + JWT claim decoding.

Loom-study Phase-1 (1.2). Configuring OBO (delegation_mode=obo) is not the same as
PROVING it works: an enterprise needs evidence that the downstream call actually
runs AS THE END USER with their scopes, not as a shared machine identity. This
module performs the AgentCore Identity RFC 8693 exchange as a dry-run and returns
the decoded claims of both the user token and the exchanged token, so the UI can
show the delegation chain (feeds the token-info card, 1.3).

Uses the bedrock-agentcore data-plane identity APIs:
  GetWorkloadAccessTokenForJWT(workloadName, userToken) -> workloadIdentityToken
  GetResourceOauth2Token(workloadIdentityToken, resourceCredentialProviderName,
      scopes, oauth2Flow=ON_BEHALF_OF_TOKEN_EXCHANGE, audiences=[...]) -> access token

No secrets are logged or returned — only DECODED CLAIMS (which are not credential
material) and the raw token is dropped after decode.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def decode_jwt_claims(token: str) -> dict:
    """Decode a JWT's payload claims WITHOUT signature verification.

    For display/inspection only — never used for an authorization decision. The
    caller obtained the token from a trusted AWS API, so we only need to render
    its claims, not re-verify it. Returns {} on any malformed input.
    """
    if not token or not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    # base64url decode with padding fix.
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        return json.loads(raw)
    except (binascii.Error, ValueError, json.JSONDecodeError):
        return {}


# Claims worth surfacing in the token-info card, with human annotations.
_ANNOTATED_CLAIMS = {
    "iss": "issuer (the IdP that minted this token)",
    "sub": "subject (the identity this token represents)",
    "aud": "audience (who this token is for)",
    "azp": "authorized party (the client the token was issued to)",
    "client_id": "client id",
    "scp": "scopes (space/array-delimited permissions)",
    "scope": "scopes",
    "roles": "roles",
    "groups": "groups",
    "exp": "expiry (unix seconds)",
    "iat": "issued-at (unix seconds)",
}


def annotate_claims(claims: dict) -> list[dict]:
    """Return an ordered list of {claim, value, note} for the interesting claims."""
    out: list[dict] = []
    for key, note in _ANNOTATED_CLAIMS.items():
        if key in claims:
            out.append({"claim": key, "value": claims[key], "note": note})
    return out


def dry_run_obo_exchange(
    identity_client,
    *,
    workload_name: str,
    user_token: str,
    resource_provider_name: str,
    scopes: Optional[list] = None,
    audience: Optional[str] = None,
) -> dict:
    """Perform the OBO exchange as a dry-run and return decoded before/after claims.

    Returns:
        {
          "ok": bool,
          "user_claims": [...annotated...],
          "exchanged_claims": [...annotated...],   # empty if exchange failed
          "error": str | None,
        }
    """
    result: dict = {
        "ok": False,
        "user_claims": annotate_claims(decode_jwt_claims(user_token)),
        "exchanged_claims": [],
        "error": None,
    }
    try:
        wat = identity_client.get_workload_access_token_for_jwt(
            workloadName=workload_name, userToken=user_token
        )
        workload_token = wat.get("workloadAccessToken") or wat.get("workloadIdentityToken") or ""
        params = {
            "workloadIdentityToken": workload_token,
            "resourceCredentialProviderName": resource_provider_name,
            "scopes": scopes or [],
            "oauth2Flow": "ON_BEHALF_OF_TOKEN_EXCHANGE",
        }
        if audience:
            params["audiences"] = [audience]
        resp = identity_client.get_resource_oauth2_token(**params)
        access_token = resp.get("accessToken") or resp.get("access_token") or ""
        result["exchanged_claims"] = annotate_claims(decode_jwt_claims(access_token))
        result["ok"] = True
    except Exception as e:  # noqa: BLE001
        # Return a CONTROLLED error to the caller — never the raw exception string
        # (CodeQL py/stack-trace-exposure). For botocore ClientErrors we surface the
        # AWS error CODE (a safe enum-like field, e.g. "ValidationException"), which
        # keeps the dry-run diagnostic without leaking internal detail; the full
        # message is logged server-side only.
        aws_code = ""
        resp = getattr(e, "response", None)
        if isinstance(resp, dict):
            aws_code = (resp.get("Error") or {}).get("Code") or ""
        result["error"] = f"{type(e).__name__}{': ' + aws_code if aws_code else ''}"
        logger.info("OBO dry-run exchange failed: %s (%s)", result["error"], str(e)[:200])
    return result
