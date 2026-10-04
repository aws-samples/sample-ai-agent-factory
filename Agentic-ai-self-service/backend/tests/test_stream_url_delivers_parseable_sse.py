"""The stream Lambda's return shape and its Function URL's InvokeMode must agree.

Measured live 2026-09-20, the first time anything drove ``TestRuntimeStreamUrl``:

    $ signed POST -> https://...lambda-url.us-east-1.on.aws/
      HTTP 200, Content-Type: text/event-stream, Transfer-Encoding: chunked
      body: {"statusCode": 200, "headers": {...}, "body": "data: {\\"type\\": \\"token\\"...}"}

Zero token frames for any client. Three facts produced that, and each one was
individually reasonable:

  1. The function is a **managed python3.12 runtime**. Lambda response streaming is a
     Node.js managed-runtime feature, so the runtime never passes a writable
     ``response_stream`` and ``lambda_handler`` always takes its buffered branch.
     Proven with ``invoke_with_response_stream``: exactly ONE PayloadChunk, arriving
     at completion, containing the envelope.
  2. That buffered branch returns the API-Gateway ``{statusCode, headers, body}``
     envelope — correct, and unit-tested as correct since 2026-06-25.
  3. The Function URL was ``InvokeMode=RESPONSE_STREAM``, which does **not** unwrap
     that envelope. It parses the leading JSON for ``headers`` (so the response
     carried ``Content-Type: text/event-stream`` and HTTP 200 and looked perfectly
     healthy) and then emitted the whole envelope JSON as the body.

Reproduced in a 20-line throwaway function to prove the fault was the invoke mode
and not the product's code, then measured under BUFFERED with the same code: clean
``data:`` bytes, ``Content-Type`` applied, HTTP 200 after 45.4s and again after
240.6s — so BUFFERED keeps the entire reason this Lambda exists, which is outliving
API Gateway's hard 30s integration cap.

**Why this test is shaped as a pairing and not as an assertion about one side.**
Neither half is wrong on its own. An envelope return is right for BUFFERED; raw SSE
writes are right for RESPONSE_STREAM. The defect is the *combination*, and it lives
in two files in two languages' worth of tooling — a backend handler and a CDK
stack — which is exactly why 23 green tests on ``stream_handler`` and a green infra
suite both held while the URL delivered nothing. So this test reads the real
InvokeMode out of the CDK source with ``ast`` and asserts it against the shape the
handler actually returns. Flip either side alone and it fails; flip both
consistently and it passes, which is the correct behaviour if the function ever
moves to a Node.js handler.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys

import pytest

sys.path.insert(0, "src")

from app import stream_handler as sh  # noqa: E402

LAMBDAS_PY = pathlib.Path(__file__).resolve().parents[2] / "infra" / "stacks" / "platform" / "lambdas.py"
BUILDER = "build_stream_lambda"


class _FakeContext:
    """Mimics a LambdaContext: NO ``.write``, which is what the real runtime passes."""

    function_name = "stream"


# ---------------------------------------------------------------------------
# Read the deployed InvokeMode out of the CDK source.
# ---------------------------------------------------------------------------


def _function_url_kwargs() -> dict[str, str]:
    """Return {kwarg: enum member} for the ``add_function_url`` call in build_stream_lambda.

    Uses ``ast`` rather than a regex so a reformat, a line break or a comment
    mentioning RESPONSE_STREAM cannot change the answer. Values are returned as the
    trailing attribute name, e.g. ``_lambda.InvokeMode.BUFFERED`` -> "BUFFERED".
    """
    tree = ast.parse(LAMBDAS_PY.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != BUILDER:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not (isinstance(func, ast.Attribute) and func.attr == "add_function_url"):
                continue
            out: dict[str, str] = {}
            for kw in call.keywords:
                if kw.arg and isinstance(kw.value, ast.Attribute):
                    out[kw.arg] = kw.value.attr
            return out
    return {}


@pytest.fixture(scope="module")
def url_kwargs() -> dict[str, str]:
    return _function_url_kwargs()


# ---------------------------------------------------------------------------
# The premise: what does the production path actually return?
# ---------------------------------------------------------------------------


def _envelope_for_a_real_answer(monkeypatch) -> dict:
    """Drive lambda_handler's production branch on a SUCCESSFUL invoke.

    A refusal would satisfy every "the body is SSE" assertion below while telling us
    nothing about the shape a real answer arrives in, so ``_handle`` is stubbed to
    write the token/done frames a real agent produces. The auth path has its own 23
    tests in test_stream_handler_auth.py; the subject here is the wire shape.
    """
    monkeypatch.setattr(
        sh,
        "_handle",
        lambda _event, write: (
            write(sh._sse({"type": "token", "token": "OK"})),
            write(sh._sse({"type": "done", "full_response": "OK"})),
        ),
    )
    return sh.lambda_handler({"headers": {}}, _FakeContext())


def test_the_production_path_returns_an_api_gateway_envelope(monkeypatch):
    """The managed Python runtime passes the context, not a stream, so this is THE path."""
    result = _envelope_for_a_real_answer(monkeypatch)
    assert isinstance(result, dict), "the buffered branch must return a dict envelope"
    assert set(result) >= {"statusCode", "headers", "body"}
    assert result["statusCode"] == 200
    assert result["headers"]["Content-Type"] == "text/event-stream"


def test_the_envelope_body_is_sse_a_client_can_parse(monkeypatch):
    """`body` must be raw SSE text — the bytes a client is meant to receive."""
    body = _envelope_for_a_real_answer(monkeypatch)["body"]
    assert body.startswith("data: "), body[:80]
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["token", "done"]
    assert events[0]["token"] == "OK"


def test_a_refusal_is_framed_the_same_way():
    """The unauthenticated path must also produce SSE, not a bare string or None."""
    result = sh.lambda_handler({"headers": {}}, _FakeContext())
    assert result["statusCode"] == 200
    assert result["body"].startswith("data: ")
    assert "Unauthorized" in result["body"]


# ---------------------------------------------------------------------------
# The other half: the deployed InvokeMode.
# ---------------------------------------------------------------------------


def test_the_cdk_construct_is_actually_found(url_kwargs):
    """Vacuity guard. If ``add_function_url`` is renamed or moved out of
    ``build_stream_lambda``, the AST search returns {} and every assertion below
    would pass on nothing. Fail loudly instead, so a human re-points this test."""
    assert LAMBDAS_PY.exists(), LAMBDAS_PY
    assert url_kwargs, f"no add_function_url(...) call with enum kwargs found in {BUILDER}()"
    assert "invoke_mode" in url_kwargs, url_kwargs
    assert "auth_type" in url_kwargs, url_kwargs


def test_the_function_url_is_buffered(url_kwargs):
    """RESPONSE_STREAM leaks the envelope as the body. Measured; see the module docstring."""
    assert url_kwargs["invoke_mode"] == "BUFFERED", (
        "the Function URL's InvokeMode is "
        f"{url_kwargs['invoke_mode']}, but the handler returns a {{statusCode, headers, body}} "
        "envelope. Only BUFFERED unwraps that envelope; under RESPONSE_STREAM the client "
        "receives the envelope's own JSON and an SSE parser finds zero frames."
    )


def test_the_function_url_stays_iam_authed(url_kwargs):
    """A public NONE URL is a world-accessible Lambda (Palisade/Epoxy 19a210be) and is
    also the tempting way to 'fix' a streaming problem. Verified live: an unsigned POST
    and a POST bearing a valid Cognito token but no signature both get HTTP 403."""
    assert url_kwargs["auth_type"] == "AWS_IAM", url_kwargs


# ---------------------------------------------------------------------------
# The pairing itself.
# ---------------------------------------------------------------------------


def test_the_return_shape_and_the_invoke_mode_agree(monkeypatch, url_kwargs):
    """The invariant neither file could state alone.

    BUFFERED  <-> the handler returns an ``{statusCode, headers, body}`` envelope.
    RESPONSE_STREAM <-> the handler writes raw SSE bytes and returns None.

    Both pairings are legitimate. Only the mismatch ships a URL that answers HTTP
    200 with a body no client can parse, which is what shipped for three months.
    """
    returns_envelope = isinstance(_envelope_for_a_real_answer(monkeypatch), dict)
    mode = url_kwargs["invoke_mode"]
    expected = "BUFFERED" if returns_envelope else "RESPONSE_STREAM"
    assert mode == expected, (
        f"lambda_handler's production branch returns "
        f"{'an envelope' if returns_envelope else 'raw bytes'}, which pairs with "
        f"InvokeMode={expected}, but the CDK stack sets InvokeMode={mode}. "
        "Change both sides or neither."
    )
