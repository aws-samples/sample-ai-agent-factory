"""Lambda *response-streaming* entrypoint for long-running runtime tests.

Bug 157: ``POST /api/test-runtime[-stream]`` routes through API Gateway HTTP
API, which imposes a hard 30s integration timeout. Tool-heavy agents that run
longer than 30s time out at the *transport* even though the agent completes
server-side. API Gateway + Lambda (Mangum) also cannot truly stream — the whole
response is buffered before delivery.

This module is a SEPARATE Lambda entry point fronted by a **Lambda Function URL**
with ``InvokeMode=RESPONSE_STREAM``. The AWS Lambda runtime invokes
``lambda_handler(event, response_stream, context)`` with a writable stream so we
can emit SSE chunks incrementally and keep the connection open well past 30s
(Function URLs allow up to ~15 min). The same SSE wire format the API-GW
``/api/test-runtime-stream`` path emits is reused so the existing frontend SSE
parser works unchanged: ``data: {"type":"token"|"done"|"error", ...}\\n\\n``.

SECURITY: the Function URL uses ``auth_type=NONE`` (Function URLs cannot use a
Cognito JWT authorizer the way API Gateway HTTP APIs can), so this handler
MUST authenticate every request itself. We replicate the API-GW Cognito JWT
authorizer minimally and WITHOUT third-party libraries:

  * fetch + cache the pool's JWKS,
  * verify the RS256 signature with a pure-stdlib RSA PKCS#1 v1.5 check,
  * verify ``iss`` (issuer), ``token_use == "access"``, ``client_id``
    (Cognito access tokens carry ``client_id``, not ``aud``), and ``exp``,
  * extract ``sub`` for tenant isolation.

This is NOT an unauthenticated invoke endpoint. A request without a valid
Cognito access token for our pool/client gets a 401 SSE error and no invoke.

Tenant isolation mirrors the delete/test handlers: the caller's ``sub`` must
match the deployment record's ``user_id`` (legacy ``user_id=None`` records stay
accessible), otherwise we return the same opaque "Runtime not found" the sync
path returns (404-equivalent over SSE).
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import base64
import hashlib
import json
import logging
import os
import re
import time
import urllib.request
from typing import Optional

import boto3

from app.services.config import load_config
from app.services.deployment_state_store import DeploymentStateStore
from app.services.harness_deployer import invoke_harness

# Reuse the exact resolver + parser the API-GW path uses so the two stay in
# lockstep (same ARN construction, same response-body parsing).
from app.deployment_handler import (
    _create_agentcore_client,
    _parse_response_body,
    _scan_for_runtime,
)

logger = logging.getLogger(__name__)

config = load_config()

DEPLOYMENT_TABLE_NAME = os.environ.get(
    "DEPLOYMENTS_TABLE_NAME",
    os.environ.get("DEPLOYMENT_TABLE_NAME", "AgentCoreDeployments"),
)

# Cognito JWT verification config (injected by the CDK stack from the same
# user pool / client the API-GW HttpJwtAuthorizer uses).
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
COGNITO_REGION = os.environ.get("COGNITO_REGION", config.aws_region)


# ---------------------------------------------------------------------------
# Deployment state store (lazy-initialised, mirrors deployment_handler)
# ---------------------------------------------------------------------------

_state_store: Optional[DeploymentStateStore] = None


def _get_state_store() -> DeploymentStateStore:
    global _state_store
    if _state_store is None:
        _state_store = DeploymentStateStore(
            table_name=DEPLOYMENT_TABLE_NAME,
            region=config.aws_region,
        )
    return _state_store


# ---------------------------------------------------------------------------
# Minimal, dependency-free Cognito access-token verification
# ---------------------------------------------------------------------------

_JWKS_CACHE: dict = {"keys": None, "fetched_at": 0.0}
_JWKS_TTL_SECONDS = 3600


def _issuer() -> str:
    return f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"


def _b64url_decode(data: str) -> bytes:
    """URL-safe base64 decode with padding restored."""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _b64url_to_int(data: str) -> int:
    return int.from_bytes(_b64url_decode(data), "big")


def _fetch_jwks() -> list:
    """Fetch + cache the pool JWKS (rotated rarely; 1h TTL is safe)."""
    now = time.time()
    if _JWKS_CACHE["keys"] is not None and (now - _JWKS_CACHE["fetched_at"]) < _JWKS_TTL_SECONDS:
        return _JWKS_CACHE["keys"]
    url = f"{_issuer()}/.well-known/jwks.json"
    with urllib.request.urlopen(url, timeout=5) as resp:  # nosec B310 — https Cognito endpoint
        body = json.loads(resp.read().decode("utf-8"))
    keys = body.get("keys", [])
    _JWKS_CACHE["keys"] = keys
    _JWKS_CACHE["fetched_at"] = now
    return keys


# ASN.1 DigestInfo prefix for SHA-256 (RFC 8017 / PKCS#1 v1.5 EMSA-PKCS1-v1_5).
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _rsa_pkcs1v15_verify(n: int, e: int, message: bytes, signature: bytes) -> bool:
    """Verify an RS256 signature using only stdlib (RSA modexp + manual padding).

    Avoids pulling in PyJWT / python-jose / cryptography (none are bundled in the
    Lambda asset). Reconstructs the expected EMSA-PKCS1-v1_5 encoded message and
    compares it to RSA^e(signature) mod n.
    """
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    sig_int = int.from_bytes(signature, "big")
    if sig_int >= n:
        return False
    em_int = pow(sig_int, e, n)
    em = em_int.to_bytes(k, "big")

    digest = hashlib.sha256(message).digest()
    t = _SHA256_DIGEST_INFO + digest
    # EM = 0x00 || 0x01 || PS (0xff...) || 0x00 || T  where PS >= 8 bytes.
    ps_len = k - len(t) - 3
    if ps_len < 8:
        return False
    expected = b"\x00\x01" + (b"\xff" * ps_len) + b"\x00" + t
    # Constant-time compare.
    return hashlib.sha256(em).digest() == hashlib.sha256(expected).digest()


class _AuthError(Exception):
    """Raised when the bearer token is missing or invalid."""


def _extract_bearer(event: dict) -> str:
    headers = event.get("headers") or {}
    # Function URL lowercases header names, but be defensive.
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth.strip()


def _verify_cognito_token(token: str) -> str:
    """Verify a Cognito ACCESS token and return its ``sub``.

    Raises ``_AuthError`` on any failure. Checks signature (RS256 against the
    pool JWKS), issuer, token_use, client_id, and expiry.
    """
    if not token:
        raise _AuthError("missing bearer token")
    if not COGNITO_USER_POOL_ID or not COGNITO_CLIENT_ID:
        # Fail closed: if the stack didn't wire the pool, never allow invoke.
        raise _AuthError("token verification not configured")

    parts = token.split(".")
    if len(parts) != 3:
        raise _AuthError("malformed token")
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001
        raise _AuthError(f"unparseable token: {exc}")

    if header.get("alg") != "RS256":
        raise _AuthError("unexpected alg")
    kid = header.get("kid")
    if not kid:
        raise _AuthError("missing kid")

    # Find the matching JWK (refresh cache once on miss in case of rotation).
    jwk = next((k for k in _fetch_jwks() if k.get("kid") == kid), None)
    if jwk is None:
        _JWKS_CACHE["keys"] = None
        jwk = next((k for k in _fetch_jwks() if k.get("kid") == kid), None)
    if jwk is None:
        raise _AuthError("unknown signing key")

    n = _b64url_to_int(jwk["n"])
    e = _b64url_to_int(jwk["e"])
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    if not _rsa_pkcs1v15_verify(n, e, signing_input, signature):
        raise _AuthError("bad signature")

    # Claim checks (Cognito access tokens use token_use=access + client_id).
    if claims.get("iss") != _issuer():
        raise _AuthError("bad issuer")
    if claims.get("token_use") != "access":
        raise _AuthError("not an access token")
    if claims.get("client_id") != COGNITO_CLIENT_ID:
        raise _AuthError("bad client_id")
    if float(claims.get("exp", 0)) < time.time():
        raise _AuthError("token expired")

    sub = claims.get("sub")
    if not sub:
        raise _AuthError("missing sub")
    return sub


def _sigv4_caller(event: dict) -> str:
    """Return the IAM caller identity when the Function URL is AWS_IAM-authed.

    Bug 147: the Function URL uses ``AuthType=AWS_IAM`` (a public ``NONE`` URL is
    forbidden in this org — Palisade/Epoxy auto-mitigate world-accessible
    Lambdas). With AWS_IAM the caller MUST SigV4-sign, and SigV4 occupies the
    ``Authorization`` header — so the request is ALREADY authenticated by IAM
    before the handler runs, and there is no room for a separate Cognito bearer
    in the same header. AWS injects the verified caller into
    ``requestContext.authorizer.iam`` (userId / arn). We use that as the caller
    identity. This makes the streaming path actually usable for SigV4 callers
    (e.g. a browser via a Cognito Identity Pool, or a signed backend/test client)
    instead of dead-on-arrival. Returns "" when no IAM context is present (e.g. a
    NONE URL or a local test), so the caller falls back to Cognito-bearer verify.
    """
    rc = event.get("requestContext") or {}
    authz = rc.get("authorizer") or {}
    iam = authz.get("iam") or {}
    # Prefer the unique principal id; fall back to the caller ARN / account.
    ident = iam.get("userId") or iam.get("userArn") or iam.get("accountId") or ""
    return str(ident).strip()


def _resolve_caller(event: dict) -> str:
    """Resolve the authenticated caller for the stream invoke.

    Two compliant auth modes are accepted (Bug 147):
      1. AWS_IAM (SigV4) — the Function URL already verified the signature; the
         caller identity comes from ``requestContext.authorizer.iam``.
      2. Cognito bearer — when there's no IAM context (NONE URL / local test) we
         verify the access JWT in the Authorization header (defence-in-depth and
         the path used once a Cognito Identity Pool fronts the SPA).
    Raises ``_AuthError`` only when NEITHER mode yields a caller.
    """
    iam_caller = _sigv4_caller(event)
    if iam_caller:
        return f"iam:{iam_caller}"
    # No SigV4 identity → require a valid Cognito access token.
    return _verify_cognito_token(_extract_bearer(event))


# ---------------------------------------------------------------------------
# SSE framing helpers
# ---------------------------------------------------------------------------


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode("utf-8")


def _emit_tokens(write, text: str) -> None:
    """Emit ``text`` as word-by-word token events (matches the API-GW path)."""
    words = text.split(" ")
    for i, word in enumerate(words):
        token = word + (" " if i < len(words) - 1 else "")
        write(_sse({"type": "token", "token": token}))


# ---------------------------------------------------------------------------
# Core invoke (reuses deployment_handler resolver + invoke_harness)
# ---------------------------------------------------------------------------


def _resolve_runtime_arn(deployment_state: Optional[dict], runtime_id: str, region: str) -> str:
    runtime_arn = (deployment_state or {}).get("runtime_arn", "")
    if runtime_arn:
        return runtime_arn
    sts = boto3.client("sts", region_name=region)
    account_id = sts.get_caller_identity()["Account"]
    return f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_id}"


def _stream_invoke(write, body: dict, caller_sub: Optional[str]) -> None:
    """Resolve the deployment, enforce tenant isolation, invoke, and emit SSE."""
    region = config.aws_region

    if body.get("simulated"):
        _emit_tokens(write, "[Simulated] Mock response - deploy a real agent to test.")
        write(_sse({"type": "done"}))
        return

    runtime_id = body.get("runtimeId") or body.get("runtime_id") or ""
    if not runtime_id or not re.match(r"^[a-zA-Z0-9_-]+$", runtime_id) or len(runtime_id) > 128:
        write(_sse({"type": "error", "error": "Invalid runtime_id"}))
        return

    # Build prompt with conversation history (same shaping as the sync path).
    prompt = body.get("input", "")
    history = body.get("history") or []
    if history:
        history_text = "\n".join(
            f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('content', '')}"
            for m in history[-6:]
        )
        prompt = f"Previous conversation:\n{history_text}\n\nUser: {body.get('input', '')}"

    session_id = body.get("sessionId") or body.get("session_id")

    store = _get_state_store()
    deployment_state = None
    try:
        deployment_state = _scan_for_runtime(store._table, runtime_id)
    except Exception:  # noqa: BLE001
        logger.warning("stream: deployment lookup failed for %s", runtime_id, exc_info=True)

    # Tenant isolation — identical rule to handle_test_runtime / delete, with
    # one carve-out: a SigV4 (AWS_IAM) caller is identified by its IAM principal
    # ("iam:<id>"), NOT the Cognito sub that owns the deployment, so it can never
    # match owner==sub. The AWS_IAM Function URL is itself a trusted boundary
    # (only signed AWS principals in this account reach the handler), so for an
    # IAM caller we DON'T enforce the per-Cognito-sub ownership check — otherwise
    # every SigV4-authed stream invoke 404s on a deployment it legitimately may
    # test. Cognito-bearer callers still get full owner-scoped isolation.
    is_iam_caller = isinstance(caller_sub, str) and caller_sub.startswith("iam:")
    if deployment_state and not is_iam_caller:
        owner = deployment_state.get("user_id")
        if owner and owner != caller_sub:
            write(_sse({"type": "error", "error": "Runtime not found"}))
            return

    # HARNESS mode → data-plane invoke_harness (Phase B).
    if deployment_state and deployment_state.get("deployment_mode") == "harness":
        harness_arn = deployment_state.get("harness_arn", "")
        if not harness_arn:
            write(_sse({"type": "error", "error": "Harness ARN not found for this deployment"}))
            return
        result = invoke_harness(region, harness_arn, prompt, session_id or runtime_id)
        if not result.get("success"):
            # SECURITY (CodeQL py/stack-trace-exposure): never surface
            # invoke_harness's raw exception text to the external caller.
            logger.warning("Harness stream invoke failed: %s", result.get("error"))
            write(_sse({"type": "error", "error": "Harness invocation failed"}))
            return
        output = result.get("output", "")
        _emit_tokens(write, output)
        write(_sse({"type": "done", "session_id": session_id or runtime_id, "full_response": output}))
        return

    # RUNTIME mode → data-plane invoke_agent_runtime.
    try:
        runtime_arn = _resolve_runtime_arn(deployment_state, runtime_id, region)
    except Exception:  # noqa: BLE001
        logger.exception("stream: cannot resolve runtime ARN for %s", runtime_id)
        write(_sse({"type": "error", "error": "Cannot resolve runtime ARN"}))
        return

    try:
        # Long read timeout: the whole point of the Function URL path is to let
        # tool-heavy agents run well past API Gateway's 30s cap.
        from botocore.config import Config as _BotoConfig

        agentcore_client = boto3.client(
            "bedrock-agentcore",
            region_name=region,
            config=_BotoConfig(read_timeout=870, connect_timeout=10, retries={"max_attempts": 0}),
        )
        payload_body: dict[str, str] = {"prompt": prompt}
        if session_id:
            payload_body["session_id"] = session_id
        # Phase 3 (Loom) OBO — pass the user's access token to the runtime so its
        # OAuth2 handler can perform the on-behalf-of exchange. Carried in the
        # invoke payload (not an AgentCore header, which the proxy strips).
        _uat = body.get("_user_access_token")
        if _uat:
            payload_body["user_access_token"] = _uat
        invoke_params: dict = {
            "agentRuntimeArn": runtime_arn,
            "payload": json.dumps(payload_body),
        }
        if session_id:
            invoke_params["runtimeSessionId"] = session_id

        resp = agentcore_client.invoke_agent_runtime(**invoke_params)
        out_session = resp.get("runtimeSessionId") or resp.get("sessionId") or session_id

        raw_response = resp.get("response", "") or resp.get("body", b"")
        if hasattr(raw_response, "read"):
            raw_response = raw_response.read()
        if isinstance(raw_response, bytes):
            raw_response = raw_response.decode("utf-8", errors="replace")

        parsed = _parse_response_body(str(raw_response))
        _emit_tokens(write, parsed)
        write(_sse({"type": "done", "session_id": out_session, "full_response": parsed}))
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "ResourceNotFound" in msg:
            write(_sse({"type": "error", "error": "Runtime not found. It may have been deleted."}))
        else:
            logger.exception("stream: runtime invocation failed")
            write(_sse({"type": "error", "error": "Internal error"}))


# ---------------------------------------------------------------------------
# Lambda response-streaming entry points
# ---------------------------------------------------------------------------


def _handle(event: dict, write) -> None:
    """Shared request handling: auth → parse body → stream invoke."""
    # CORS/preflight: Function URLs deliver OPTIONS too; just close cleanly.
    method = (
        (event.get("requestContext", {}) or {}).get("http", {}) or {}
    ).get("method", "POST")
    if method == "OPTIONS":
        return

    # Authenticate BEFORE any invoke. Accept either the AWS_IAM SigV4 caller
    # (Function URL already verified it) or a Cognito access token (Bug 147).
    try:
        caller_sub = _resolve_caller(event)
    except _AuthError as exc:
        logger.warning("stream: auth rejected: %s", exc)
        write(_sse({"type": "error", "error": "Unauthorized"}))
        return

    # Parse the JSON body (Function URL may base64-encode it).
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception:  # noqa: BLE001
            raw = "{}"
    try:
        body = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:  # noqa: BLE001
        write(_sse({"type": "error", "error": "Invalid JSON body"}))
        return

    # Phase 3 (Loom) OBO — forward the caller's bearer token so the runtime's
    # OAuth2 handler can use it as the SUBJECT token in an on-behalf-of exchange
    # (agent acts AS the user downstream). Only the user's own token is carried;
    # the SigV4 path has no bearer and simply omits it (M2M connectors ignore it).
    if isinstance(body, dict):
        _bearer = _extract_bearer(event)
        if _bearer:
            body["_user_access_token"] = _bearer

    _stream_invoke(write, body, caller_sub)


# AWS Lambda RESPONSE_STREAM contract: the runtime passes a writable
# ``response_stream`` as the second positional argument. We set the SSE content
# type via the awslambdaric HttpResponseStream metadata helper when available,
# and always emit ``data:`` framed chunks the frontend SSE parser understands.
def lambda_handler(event, response_stream=None, context=None):  # noqa: D401
    """Function URL streaming entry point (InvokeMode=RESPONSE_STREAM).

    The AWS Lambda streaming runtime injects a writable ``response_stream`` as
    the second positional arg. We attach the SSE content type via the
    ``HttpResponseStream`` helper when the runtime exposes it (so the browser
    sees ``text/event-stream``), then write ``data:`` framed chunks and close.
    If the helper isn't available we fall back to writing raw SSE bytes — the
    frontend SSE parser tolerates a missing explicit content type.

    Bug (caught live 2026-06-25): the second positional arg is NOT guaranteed to
    be the writable stream — depending on how the runtime/Function-URL invokes
    the function (and for buffered callers / some RIC paths) the second arg is
    the LambdaContext, which has no ``.write`` (every write then threw
    AttributeError and the client saw ``null``). So we DETECT a writable stream
    (``hasattr(arg, "write")``); when the second arg is not writable we fall back
    to the buffered path so the caller still gets a complete SSE response.
    """
    if response_stream is None or not hasattr(response_stream, "write"):
        # No real stream object (second arg was the context, or absent) — serve
        # the buffered response so the caller still gets the full SSE payload.
        ctx = response_stream if context is None else context
        return handler(event, ctx)

    stream = response_stream
    try:
        from awslambdaric.lambda_response_stream import HttpResponseStream  # type: ignore

        stream = HttpResponseStream.from_content_type(response_stream, "text/event-stream")
    except Exception:  # noqa: BLE001
        stream = response_stream

    def write(chunk: bytes) -> None:
        try:
            stream.write(chunk)
        except Exception:  # noqa: BLE001
            logger.exception("stream: write failed")

    try:
        if not isinstance(event, dict):
            write(_sse({"type": "error", "error": "Invalid event"}))
            return
        _handle(event, write)
    finally:
        try:
            stream.end()
        except Exception:  # noqa: BLE001
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass


# Buffered fallback (non-streaming). Some local/test harnesses invoke a Lambda
# with the classic 2-arg (event, context) signature. Detect that and return a
# normal Function-URL-shaped buffered SSE response so the same code path is
# testable without the streaming runtime.
def handler(event, context=None):
    """Classic (buffered) entry point — collects SSE then returns it once."""
    chunks: list[bytes] = []

    def write(chunk: bytes) -> None:
        chunks.append(chunk)

    if not isinstance(event, dict):
        write(_sse({"type": "error", "error": "Invalid event"}))
    else:
        _handle(event, write)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        "body": b"".join(chunks).decode("utf-8"),
    }
