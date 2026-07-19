"""Tests for OBO dry-run + JWT claim decoding (Loom-study 1.2/1.3)."""

from __future__ import annotations

import base64
import json
import sys

sys.path.insert(0, "src")

from app.services.obo_verifier import (  # noqa: E402
    annotate_claims,
    decode_jwt_claims,
    dry_run_obo_exchange,
)


def _make_jwt(claims: dict) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.signature"


def test_decode_jwt_claims_valid():
    tok = _make_jwt({"sub": "u1", "iss": "https://idp", "scp": "read write"})
    claims = decode_jwt_claims(tok)
    assert claims["sub"] == "u1"
    assert claims["iss"] == "https://idp"


def test_decode_jwt_claims_malformed_returns_empty():
    assert decode_jwt_claims("") == {}
    assert decode_jwt_claims("not-a-jwt") == {}
    assert decode_jwt_claims("a.b") == {}  # invalid base64 payload -> {}


def test_annotate_claims_orders_and_notes():
    ann = annotate_claims({"sub": "u1", "aud": "api", "unknown": "x"})
    keys = [a["claim"] for a in ann]
    assert "sub" in keys and "aud" in keys
    assert "unknown" not in keys  # only annotated claims surfaced
    for a in ann:
        assert a["note"]  # every surfaced claim carries a human note


class _FakeIdentity:
    def __init__(self, exchanged_claims=None, fail=False):
        self._exchanged = exchanged_claims
        self._fail = fail
        self.exchange_params = None

    def get_workload_access_token_for_jwt(self, **kw):  # noqa: ARG002
        return {"workloadAccessToken": "workload.token.x"}

    def get_resource_oauth2_token(self, **kw):
        self.exchange_params = kw
        if self._fail:
            raise RuntimeError("audience required")
        return {"accessToken": _make_jwt(self._exchanged or {})}


def test_dry_run_success_returns_before_after_claims():
    user_tok = _make_jwt({"sub": "alice", "iss": "https://corp-idp"})
    idc = _FakeIdentity(exchanged_claims={"sub": "alice", "aud": "downstream-api", "scp": "orders:read"})
    out = dry_run_obo_exchange(
        idc,
        workload_name="wl",
        user_token=user_tok,
        resource_provider_name="prov",
        scopes=["orders:read"],
        audience="downstream-api",
    )
    assert out["ok"] is True
    assert any(c["claim"] == "sub" for c in out["user_claims"])
    assert any(c["value"] == "downstream-api" for c in out["exchanged_claims"])
    # audience threaded into the exchange call (the Okta requirement).
    assert idc.exchange_params["audiences"] == ["downstream-api"]
    assert idc.exchange_params["oauth2Flow"] == "ON_BEHALF_OF_TOKEN_EXCHANGE"


def test_dry_run_failure_is_captured_not_raised():
    user_tok = _make_jwt({"sub": "bob"})
    idc = _FakeIdentity(fail=True)
    out = dry_run_obo_exchange(
        idc,
        workload_name="wl",
        user_token=user_tok,
        resource_provider_name="prov",
    )
    assert out["ok"] is False
    # Sanitized error contract (CodeQL py/stack-trace-exposure): the exception
    # TYPE is surfaced for diagnostics, but the raw exception MESSAGE must never
    # reach the caller — it's logged server-side only.
    assert out["error"] == "RuntimeError"
    assert "audience required" not in out["error"]
    # user claims still decoded even when the exchange fails.
    assert any(c["claim"] == "sub" for c in out["user_claims"])


def test_dry_run_botocore_error_surfaces_safe_code_only():
    """A botocore ClientError surfaces the AWS error CODE (safe), not str(e)."""

    class _ClientErr(Exception):
        def __init__(self):
            super().__init__("An error occurred (ValidationException): secret internal detail 12345")
            self.response = {"Error": {"Code": "ValidationException", "Message": "secret internal detail 12345"}}

    class _BotoIdc(_FakeIdentity):
        def get_resource_oauth2_token(self, **kw):  # noqa: ARG002
            raise _ClientErr()

    out = dry_run_obo_exchange(
        _BotoIdc(fail=True),
        workload_name="wl",
        user_token=_make_jwt({"sub": "bob"}),
        resource_provider_name="prov",
    )
    assert out["ok"] is False
    assert out["error"] == "_ClientErr: ValidationException"
    assert "secret internal detail" not in out["error"]  # raw message not leaked
