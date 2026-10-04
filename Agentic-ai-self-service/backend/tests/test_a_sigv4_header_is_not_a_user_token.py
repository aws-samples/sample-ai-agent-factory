"""F-28: a SigV4 ``Authorization`` header is not a user token and is never forwarded as one.

``_extract_bearer`` returned any non-bearer header value whole, so on the SigV4 path the
``AWS4-HMAC-SHA256 Credential=..., Signature=...`` string was carried into the invoke payload as
``user_access_token``. Latent (the Function URL is unwired) and consumer-less, but a signature
in an agent's input is exactly the kind of leak the OBO field was not meant to carry.
"""

from __future__ import annotations

from app import stream_handler as sh

JWT = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImsxIn0.eyJzdWIiOiJ1c2VyLTEifQ.FAKESIGNATURE"
SIGV4 = (
    "AWS4-HMAC-SHA256 Credential=AKIAFAKEFAKEFAKEFAKE/20260928/us-east-1/lambda/aws4_request, "
    "SignedHeaders=host;x-amz-date, Signature=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)


def test_a_bearer_token_is_extracted():
    assert sh._extract_bearer({"headers": {"authorization": f"Bearer {JWT}"}}) == JWT
    assert sh._extract_bearer({"headers": {"Authorization": f"bearer {JWT}"}}) == JWT


def test_a_bare_jwt_without_the_scheme_is_still_accepted():
    assert sh._extract_bearer({"headers": {"authorization": JWT}}) == JWT


def test_a_sigv4_header_yields_no_token():
    assert sh._extract_bearer({"headers": {"authorization": SIGV4}}) == ""


def test_other_schemes_and_junk_yield_no_token():
    for value in ("Basic dXNlcjpwYXNz", "Digest username=x", "not a token", "", "a.b"):
        assert sh._extract_bearer({"headers": {"authorization": value}}) == "", value
    assert sh._extract_bearer({}) == ""


def test_the_sigv4_path_still_resolves_the_iam_caller(monkeypatch):
    """The other direction: the SigV4 caller comes from the IAM authorizer context, not the
    header, and that path is untouched."""
    event = {
        "headers": {"authorization": SIGV4},
        "requestContext": {"authorizer": {"iam": {"userId": "AROAEXAMPLE:session"}}},
    }
    assert sh._resolve_caller(event).startswith("iam:")
