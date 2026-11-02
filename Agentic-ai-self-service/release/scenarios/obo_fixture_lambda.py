"""Synthetic RFC 8693 token-exchange and whoami fixture for OBO certification.

Deployed by the ``identity.delegated_obo`` live scenario as a private Lambda behind a Function URL.
The Function URL is public at the transport layer; only the OIDC discovery document is anonymous.
The token endpoint requires exact HTTP Basic client authentication plus a Cognito-validated
workforce access token; the whoami endpoint requires that same valid access token. Tokens and client
secrets are never logged or returned except for the OAuth ``access_token`` field required by the
protocol; the exchange returns the original short-lived workforce token rather than minting another
credential. All configuration arrives through environment variables, so this module is candidate- and
account-agnostic.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from typing import Any

import boto3

CLIENT_ID = os.environ["FIXTURE_CLIENT_ID"]
SECRET_PARAMETER = os.environ["FIXTURE_SECRET_PARAMETER"]
WORKFORCE_ISSUER = os.environ["WORKFORCE_ISSUER"]
WORKFORCE_CLIENT_ID = os.environ["WORKFORCE_CLIENT_ID"]
WORKFORCE_REGION = os.environ["WORKFORCE_REGION"]
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
_SECRET: str | None = None


def _ssm_client():
    return boto3.client("ssm", region_name=WORKFORCE_REGION)


def _cognito_client():
    return boto3.client("cognito-idp", region_name=WORKFORCE_REGION)


def _client_secret() -> str:
    global _SECRET
    if _SECRET is None:
        response = _ssm_client().get_parameter(Name=SECRET_PARAMETER, WithDecryption=True)
        value = str((response.get("Parameter") or {}).get("Value") or "")
        if not value:
            raise RuntimeError("fixture client secret is unavailable")
        _SECRET = value
    return _SECRET


def _json_response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        },
        "body": json.dumps(body, separators=(",", ":")),
    }


def _oauth_error(status: int, code: str, *, challenge: str = "Bearer") -> dict[str, Any]:
    """Build an RFC 6749 error response. ``code`` is always a literal at the call site.

    The rejection is logged HERE, from the literal ``status`` and ``code`` arguments, so the
    diagnostic never reads back from a response object (which carries headers and bodies).
    """
    response = _json_response(status, {"error": code})
    if status == 401:
        response["headers"]["www-authenticate"] = challenge
    _log_rejection(status, code)
    return response


def _request_body(event: dict[str, Any]) -> bytes:
    raw = str(event.get("body") or "")
    if event.get("isBase64Encoded"):
        return base64.b64decode(raw, validate=True)
    return raw.encode()


def _header(event: dict[str, Any], name: str) -> str:
    wanted = name.casefold()
    for key, value in (event.get("headers") or {}).items():
        if str(key).casefold() == wanted:
            return str(value or "").strip()
    return ""


def _base_url(event: dict[str, Any]) -> str:
    host = _header(event, "host")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
    if not host or any(character not in allowed for character in host):
        raise ValueError("invalid host")
    return f"https://{host}"


def _decode_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise ValueError("invalid token")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(payload.encode()))
    if not isinstance(decoded, dict):
        raise ValueError("invalid claims")
    return decoded


def _validate_user_token(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not token or any(character.isspace() for character in token):
        raise ValueError("invalid token")
    user = _cognito_client().get_user(AccessToken=token)
    claims = _decode_claims(token)
    expiry = claims.get("exp")
    if claims.get("iss") != WORKFORCE_ISSUER:
        raise ValueError("wrong issuer")
    if claims.get("client_id") != WORKFORCE_CLIENT_ID:
        raise ValueError("wrong client")
    if claims.get("token_use") != "access":
        raise ValueError("wrong token type")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or float(expiry) <= time.time():
        raise ValueError("expired token")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise ValueError("missing subject")
    return claims, user


def _basic_client(event: dict[str, Any]) -> bool:
    authorization = _header(event, "authorization")
    if not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:], validate=True).decode()
        client_id, client_secret = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(client_id, CLIENT_ID) and hmac.compare_digest(client_secret, _client_secret())


def _safe_subject_hash(subject: str) -> str:
    return hashlib.sha256(subject.encode()).hexdigest()[:16]


def _log_subject_rejection(token: str, error: Exception) -> None:
    """Live-gate diagnostic for a refused subject token: the validation error plus claim *metadata*.

    Never logs the token or any claim value that could identify the user: only match booleans
    against the fixture's expected issuer/client, the token type and scope, the expiry delta, the
    claim key names, and the hashed subject.
    """
    meta: dict[str, Any] = {
        "event": "subject_rejected",
        "reason": f"{type(error).__name__}: {str(error)[:140]}",
        "segments": len(token.split(".")),
    }
    try:
        claims = _decode_claims(token)
    except Exception:  # noqa: BLE001
        claims = {}
    expiry = claims.get("exp")
    subject = claims.get("sub")
    meta.update(
        {
            "iss_match": claims.get("iss") == WORKFORCE_ISSUER,
            "client_match": claims.get("client_id") == WORKFORCE_CLIENT_ID,
            "token_use": str(claims.get("token_use") or "")[:16],
            "scope": str(claims.get("scope") or "")[:160],
            "exp_delta_s": (
                int(float(expiry) - time.time())
                if isinstance(expiry, (int, float)) and not isinstance(expiry, bool)
                else None
            ),
            "subject_hash": _safe_subject_hash(subject) if isinstance(subject, str) and subject else None,
            "claim_keys": sorted(str(key) for key in claims)[:24],
        }
    )
    print(json.dumps(meta))


def _discovery(event: dict[str, Any]) -> dict[str, Any]:
    base = _base_url(event)
    return _json_response(
        200,
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/oauth2/token",
            "jwks_uri": f"{WORKFORCE_ISSUER}/.well-known/jwks.json",
            "grant_types_supported": [TOKEN_EXCHANGE_GRANT],
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            "scopes_supported": ["whoami.read"],
            "response_types_supported": ["token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        },
    )


def _token_exchange(event: dict[str, Any]) -> dict[str, Any]:
    if not _basic_client(event):
        return _oauth_error(401, "invalid_client", challenge='Basic realm="token-exchange"')
    try:
        form = urllib.parse.parse_qs(_request_body(event).decode(), strict_parsing=True)
    except (ValueError, UnicodeDecodeError):
        return _oauth_error(400, "invalid_request")
    if form.get("grant_type") != [TOKEN_EXCHANGE_GRANT]:
        return _oauth_error(400, "unsupported_grant_type")
    # RFC 8693 §3: the subject token may be presented as an access token or as a JWT. AgentCore
    # Identity's documented TOKEN_EXCHANGE default is ``urn:ietf:params:oauth:token-type:jwt``
    # (a Cognito access token *is* a JWT); any other type is still rejected, and the token is
    # still validated against Cognito below regardless of the declared type.
    if form.get("subject_token_type") not in (None, [ACCESS_TOKEN_TYPE], [JWT_TOKEN_TYPE]):
        return _oauth_error(400, "invalid_request")
    subject_tokens = form.get("subject_token") or []
    if len(subject_tokens) != 1:
        return _oauth_error(400, "invalid_request")
    try:
        claims, _user = _validate_user_token(subject_tokens[0])
    except Exception as error:  # noqa: BLE001
        _log_subject_rejection(subject_tokens[0], error)
        return _oauth_error(401, "invalid_grant")
    subject = str(claims["sub"])
    print(json.dumps({"event": "token_exchange", "subjectHash": _safe_subject_hash(subject)}))
    remaining = max(1, int(float(claims["exp"]) - time.time()))
    return _json_response(
        200,
        {
            "access_token": subject_tokens[0],
            "issued_token_type": ACCESS_TOKEN_TYPE,
            "token_type": "Bearer",
            "expires_in": remaining,
            "scope": " ".join((form.get("scope") or ["whoami.read"])[0].split()),
        },
    )


def _whoami(event: dict[str, Any]) -> dict[str, Any]:
    authorization = _header(event, "authorization")
    if not authorization.startswith("Bearer "):
        return _oauth_error(401, "invalid_token")
    try:
        claims, user = _validate_user_token(authorization[7:].strip())
    except Exception:
        return _oauth_error(401, "invalid_token")
    subject = str(claims["sub"])
    print(json.dumps({"event": "whoami", "subjectHash": _safe_subject_hash(subject)}))
    return _json_response(
        200,
        {
            "principal": subject,
            "sub": subject,
            "issuer": claims["iss"],
            "client_id": claims["client_id"],
            "username": str(user.get("Username") or ""),
        },
    )


def _route(event: dict[str, Any], method: str, path: str) -> dict[str, Any]:
    if method == "GET" and path == "/.well-known/openid-configuration":
        return _discovery(event)
    if method == "POST" and path == "/oauth2/token":
        return _token_exchange(event)
    if method == "GET" and path == "/whoami":
        return _whoami(event)
    return _oauth_error(404, "not_found")


#: Route of the request being served, set once per invocation by ``handler`` so the rejection log
#: can name the method and path without ever being handed the response.
_REQUEST_ROUTE: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "acfpr_obo_request_route", default=("", "")
)


def _log_rejection(status: int, code: str) -> None:
    """Live-gate diagnostic: route, status and OAuth error code only -- never tokens, headers or bodies.

    Called only by ``_oauth_error`` with its literal arguments. Nothing here is read from a request
    or a response object, so no credential-bearing value can reach the log.
    """
    method, path = _REQUEST_ROUTE.get()
    print(json.dumps({"event": "rejected", "method": method[:8], "path": path[:64], "status": status, "error": code}))


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    request = (event.get("requestContext") or {}).get("http") or {}
    method = str(request.get("method") or "").upper()
    path = str(event.get("rawPath") or "/")
    _REQUEST_ROUTE.set((method, path))
    try:
        return _route(event, method, path)
    except Exception:
        return _oauth_error(503, "temporarily_unavailable")
