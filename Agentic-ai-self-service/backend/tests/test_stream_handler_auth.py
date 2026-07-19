"""Security tests for the streaming test endpoint's hand-rolled RS256 JWT verify.

stream_handler.py verifies Cognito access tokens WITHOUT a vetted JWT library
(none are bundled in the Lambda asset) — it reconstructs EMSA-PKCS1-v1_5 padding
and does RSA modexp by hand (_rsa_pkcs1v15_verify) plus claim checks
(_verify_cognito_token). That is the single most security-critical routine in the
streaming path, so it gets positive + adversarial coverage here: a correctly
signed token is accepted, and every tampered/forged/wrong-claim variant is
rejected. Uses a locally generated RSA keypair (cryptography) to mint tokens and a
monkeypatched JWKS so no network/AWS is touched.
"""

from __future__ import annotations

import base64
import json
import sys
import time

import pytest

sys.path.insert(0, "src")

cryptography = pytest.importorskip("cryptography")
from app import stream_handler as sh  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

# --- helpers ---------------------------------------------------------------


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _int_to_b64url(i: int) -> str:
    return _b64url(i.to_bytes((i.bit_length() + 7) // 8, "big"))


@pytest.fixture(scope="module")
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key().public_numbers()
    return priv, pub


@pytest.fixture
def wired(monkeypatch, keypair):
    """Wire the module: env (pool/client), issuer, and a JWKS with our public key."""
    _priv, pub = keypair
    monkeypatch.setattr(sh, "COGNITO_USER_POOL_ID", "us-east-1_TESTPOOL", raising=False)
    monkeypatch.setattr(sh, "COGNITO_CLIENT_ID", "test-client-id", raising=False)
    monkeypatch.setattr(sh, "_issuer", lambda: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL")
    jwk = {"kid": "test-kid", "kty": "RSA", "alg": "RS256", "n": _int_to_b64url(pub.n), "e": _int_to_b64url(pub.e)}
    monkeypatch.setattr(sh, "_fetch_jwks", lambda: [jwk])
    return jwk


def _make_token(priv, *, kid="test-kid", alg="RS256", claims=None, tamper=False, bad_sig=False):
    header = {"alg": alg, "kid": kid, "typ": "JWT"}
    base_claims = {
        "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL",
        "token_use": "access",
        "client_id": "test-client-id",
        "exp": int(time.time()) + 3600,
        "sub": "user-sub-123",
    }
    if claims:
        base_claims.update(claims)
    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps(base_claims).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    if alg == "none":
        return f"{h}.{p}."
    sig = priv.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    if bad_sig:
        sig = bytes((sig[0] ^ 0xFF,)) + sig[1:]
    token = f"{h}.{p}.{_b64url(sig)}"
    if tamper:
        # Flip a byte in the payload AFTER signing.
        bad_claims = dict(base_claims, sub="attacker")
        token = f"{h}.{_b64url(json.dumps(bad_claims).encode())}.{_b64url(sig)}"
    return token


# --- positive --------------------------------------------------------------


def test_valid_token_accepted(wired, keypair):
    priv, _ = keypair
    assert sh._verify_cognito_token(_make_token(priv)) == "user-sub-123"


# --- adversarial: signature / algorithm ------------------------------------


def test_tampered_payload_rejected(wired, keypair):
    priv, _ = keypair
    with pytest.raises(sh._AuthError):
        sh._verify_cognito_token(_make_token(priv, tamper=True))


def test_bad_signature_rejected(wired, keypair):
    priv, _ = keypair
    with pytest.raises(sh._AuthError):
        sh._verify_cognito_token(_make_token(priv, bad_sig=True))


def test_alg_none_rejected(wired, keypair):
    priv, _ = keypair
    with pytest.raises(sh._AuthError):
        sh._verify_cognito_token(_make_token(priv, alg="none"))


def test_wrong_key_rejected(wired, monkeypatch):
    # Sign with a DIFFERENT key than the JWKS advertises.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(sh._AuthError):
        sh._verify_cognito_token(_make_token(other))


def test_unknown_kid_rejected(wired, keypair):
    priv, _ = keypair
    with pytest.raises(sh._AuthError):
        sh._verify_cognito_token(_make_token(priv, kid="rotated-away"))


# --- adversarial: claims ---------------------------------------------------


@pytest.mark.parametrize(
    "claims,_why",
    [
        ({"iss": "https://evil.example.com/pool"}, "wrong issuer"),
        ({"client_id": "someone-elses-client"}, "wrong client_id"),
        ({"token_use": "id"}, "id token, not access"),
        ({"exp": int(time.time()) - 10}, "expired"),
        ({"sub": ""}, "missing sub"),
    ],
)
def test_bad_claims_rejected(wired, keypair, claims, _why):
    priv, _ = keypair
    with pytest.raises(sh._AuthError):
        sh._verify_cognito_token(_make_token(priv, claims=claims))


# --- adversarial: structural ----------------------------------------------


def test_missing_token_rejected(wired):
    with pytest.raises(sh._AuthError):
        sh._verify_cognito_token("")


def test_malformed_token_rejected(wired):
    with pytest.raises(sh._AuthError):
        sh._verify_cognito_token("not.a.jwt.at.all")


def test_fails_closed_when_pool_unconfigured(monkeypatch, keypair):
    # If the stack never wired the pool, verification must DENY (never allow).
    priv, _ = keypair
    monkeypatch.setattr(sh, "COGNITO_USER_POOL_ID", "", raising=False)
    monkeypatch.setattr(sh, "COGNITO_CLIENT_ID", "", raising=False)
    with pytest.raises(sh._AuthError):
        sh._verify_cognito_token(_make_token(priv))


# --- low-level primitive ---------------------------------------------------


def test_rsa_primitive_rejects_oversized_signature(keypair):
    """sig_int >= n must be rejected (prevents a class of forgery)."""
    priv, pub = keypair
    k = (pub.n.bit_length() + 7) // 8
    # A signature equal to n (as bytes) -> sig_int == n -> must reject.
    assert sh._rsa_pkcs1v15_verify(pub.n, pub.e, b"msg", pub.n.to_bytes(k, "big")) is False


def test_rsa_primitive_accepts_valid(keypair):
    priv, pub = keypair
    msg = b"hello-agentcore"
    sig = priv.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    assert sh._rsa_pkcs1v15_verify(pub.n, pub.e, msg, sig) is True


# --- Bug 147: AWS_IAM SigV4 caller resolution ------------------------------


def _iam_event(user_id="AROAEXAMPLE:session", arn="arn:aws:sts::1:assumed-role/r/s"):
    return {"requestContext": {"authorizer": {"iam": {"userId": user_id, "userArn": arn, "accountId": "1"}}}}


def test_sigv4_caller_extracted_from_iam_context():
    assert sh._sigv4_caller(_iam_event()) == "AROAEXAMPLE:session"


def test_sigv4_caller_falls_back_to_arn_then_account():
    ev = {"requestContext": {"authorizer": {"iam": {"userArn": "arn:aws:iam::1:user/u", "accountId": "1"}}}}
    assert sh._sigv4_caller(ev) == "arn:aws:iam::1:user/u"
    ev2 = {"requestContext": {"authorizer": {"iam": {"accountId": "1"}}}}
    assert sh._sigv4_caller(ev2) == "1"


def test_sigv4_caller_empty_without_iam_context():
    assert sh._sigv4_caller({}) == ""
    assert sh._sigv4_caller({"requestContext": {}}) == ""


def test_resolve_caller_prefers_sigv4_over_bearer(wired):
    # IAM context present → caller is the IAM principal; no Cognito token needed.
    caller = sh._resolve_caller(_iam_event())
    assert caller == "iam:AROAEXAMPLE:session"


def test_resolve_caller_falls_back_to_cognito_when_no_iam(wired, keypair):
    # No IAM context (e.g. NONE URL / local) → must verify the Cognito bearer.
    priv, _ = keypair
    ev = {"headers": {"authorization": f"Bearer {_make_token(priv)}"}}
    assert sh._resolve_caller(ev) == "user-sub-123"


def test_resolve_caller_rejects_when_neither_present(wired):
    # No IAM identity AND no valid bearer → AuthError (fail closed).
    with pytest.raises(sh._AuthError):
        sh._resolve_caller({"headers": {}})


# --- lambda_handler arg detection (caught live: 2nd arg was the context) -----


class _FakeContext:
    """Mimics a LambdaContext: NO .write attribute."""

    function_name = "stream"


def test_lambda_handler_falls_back_to_buffered_when_second_arg_is_context(wired):
    """If the runtime passes the context (no .write) as the 2nd positional arg,
    lambda_handler must NOT AttributeError — it falls back to a buffered Function
    URL response so the caller still gets a complete SSE body."""
    # Unauthenticated event → handler returns a buffered 200 with an SSE error
    # frame (proves it took the buffered path, not the stream-write path).
    result = sh.lambda_handler({"headers": {}}, _FakeContext())
    assert isinstance(result, dict)
    assert result["statusCode"] == 200
    assert "text/event-stream" in result["headers"]["Content-Type"]
    assert "Unauthorized" in result["body"]


def test_lambda_handler_buffered_when_second_arg_missing(wired):
    result = sh.lambda_handler({"headers": {}})
    assert isinstance(result, dict)
    assert result["statusCode"] == 200


def test_iam_caller_bypasses_cognito_sub_ownership(wired, monkeypatch):
    """A SigV4 (iam:) caller must NOT be rejected by the per-Cognito-sub owner
    check — the AWS_IAM Function URL is the trust boundary. We drive _handle with
    an IAM-authed event against a deployment owned by a different Cognito sub and
    assert it proceeds PAST the ownership gate (i.e. does NOT emit the tenant
    'Runtime not found')."""
    import app.stream_handler as shm

    # Deployment owned by some Cognito sub, runtime mode.
    monkeypatch.setattr(
        shm,
        "_scan_for_runtime",
        lambda *a, **k: {
            "user_id": "someone-else-sub",
            "deployment_mode": "runtime",
            "runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1",
        },
    )
    # Make the data-plane invoke a no-op success so we get past the gate cleanly.
    monkeypatch.setattr(
        shm, "_resolve_runtime_arn", lambda *a, **k: "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1"
    )
    monkeypatch.setattr(shm, "_stream_invoke", lambda write, body, caller: write(_sse_ok()))
    out = []
    ev = {
        "requestContext": {"authorizer": {"iam": {"userId": "AROAX:sess"}}},
        "body": json.dumps({"runtimeId": "r-1", "input": "hi"}),
    }
    shm._handle(ev, lambda b: out.append(b))
    joined = b"".join(out).decode()
    assert "Runtime not found" not in joined  # IAM caller passed the ownership gate


def _sse_ok():
    return b'data: {"type":"done"}\n\n'


def test_cognito_caller_still_owner_scoped(wired, monkeypatch, keypair):
    """A Cognito-bearer caller is STILL rejected for a deployment owned by a
    different sub (isolation preserved for non-IAM callers)."""
    import app.stream_handler as shm

    priv, _ = keypair
    monkeypatch.setattr(
        shm, "_scan_for_runtime", lambda *a, **k: {"user_id": "someone-else-sub", "deployment_mode": "runtime"}
    )
    out = []
    ev = {
        "headers": {"authorization": f"Bearer {_make_token(priv)}"},  # sub=user-sub-123
        "body": json.dumps({"runtimeId": "r-1", "input": "hi"}),
    }
    shm._handle(ev, lambda b: out.append(b))
    assert "Runtime not found" in b"".join(out).decode()


def test_lambda_handler_uses_stream_when_writable(wired):
    """When a real writable stream IS provided, it writes to it (not buffered)."""
    written = []

    class _Stream:
        def write(self, b):
            written.append(b)

        def end(self):
            pass

    out = sh.lambda_handler({"headers": {}}, _Stream())
    assert out is None  # streaming path returns nothing
    assert written and b"Unauthorized" in b"".join(written)
