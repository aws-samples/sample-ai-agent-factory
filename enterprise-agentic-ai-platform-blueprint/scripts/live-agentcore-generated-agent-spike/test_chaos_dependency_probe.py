"""Offline contract tests for ``chaos_dependency_probe.py`` (no AWS calls)."""
from __future__ import annotations

import base64
import json

import chaos_dependency_probe as p


class FakeResponse:
    def __init__(self, status: int, body: str, headers: dict | None = None) -> None:
        self.status_code = status
        self.text = body
        self.headers = headers or {}

    def json(self):  # noqa: ANN201 - httpx shape
        return json.loads(self.text)


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002 - httpx shape
        return self._response


def test_completion_classifies_unauthorized_without_leaking_body() -> None:
    result = p.post_completion(FakeClient(FakeResponse(401, '{"message":"Unauthorized bearer abc.def.ghi"}')), "https://gw/inference/v1", {}, {})
    assert result["status"] == 401
    assert result["errorClass"] == "unauthorized"
    assert result["gotModelContent"] is False
    assert "abc.def.ghi" not in json.dumps(result)


def test_completion_detects_model_content_only_on_real_choice() -> None:
    ok = p.post_completion(FakeClient(FakeResponse(200, json.dumps({"choices": [{"message": {"content": "ok"}}]}))), "https://gw/inference/v1", {}, {})
    assert ok["errorClass"] == "ok" and ok["gotModelContent"] is True
    empty = p.post_completion(FakeClient(FakeResponse(200, json.dumps({"choices": [{"message": {"content": None, "reasoning": "..."}}]}))), "https://gw/inference/v1", {}, {})
    assert empty["gotModelContent"] is False


def test_completion_flags_guardrail_error_bodies() -> None:
    result = p.post_completion(FakeClient(FakeResponse(400, '{"error":"guardrail gr-x not found"}')), "https://gw/inference/v1", {}, {})
    assert result["errorClass"] == "guardrail"


def test_forged_jwt_has_three_segments_and_a_plausible_scope() -> None:
    token = p.forged_jwt()
    header, payload, signature = token.split(".")
    assert json.loads(base64.urlsafe_b64decode(header + "==").decode())["alg"] == "RS256"
    claims = json.loads(base64.urlsafe_b64decode(payload + "==").decode())
    assert claims["scope"] == "inference/invoke" and claims["exp"] > 0
    assert signature  # present but not a valid signature by construction


def test_fingerprint_never_echoes_input() -> None:
    assert "secret" not in p.fingerprint("secret")


INTERCEPTOR_BLOCK = FakeResponse(
    403,
    json.dumps({"error": {"code": "guardrail_intervened", "type": "guardrail_intervened", "message": "blocked",
                          "guardrail": {"id": "g1", "version": "DRAFT"}, "tripped": ["contentPolicy.PROMPT_ATTACK"]}}),
    # No marker header: the Gateway strips custom headers on short-circuit.
)


def test_completion_records_interceptor_code_and_tripped_types() -> None:
    result = p.post_completion(FakeClient(INTERCEPTOR_BLOCK), "https://gw/inference/v1", {}, {})
    assert result["status"] == 403
    assert result["interceptorCode"] == "guardrail_intervened"
    assert result["tripped"] == ["contentPolicy.PROMPT_ATTACK"]
    assert result["gotModelContent"] is False
    assert p.is_guardrail_block(result) is True


def test_plain_403_without_interceptor_marker_is_not_a_guardrail_block() -> None:
    result = p.post_completion(FakeClient(FakeResponse(403, '{"message":"Forbidden"}')), "https://gw/inference/v1", {}, {})
    assert result["interceptorCode"] is None
    assert p.is_guardrail_block(result) is False
    ok = p.post_completion(FakeClient(FakeResponse(200, json.dumps({"choices": [{"message": {"content": "ok"}}]}))), "https://gw/inference/v1", {}, {})
    assert p.is_guardrail_block(ok) is False


class RoutingClient:
    """Returns the interceptor block for tripping prompts and a model answer otherwise."""

    def __init__(self, block_all_tripping: bool = True) -> None:
        self.block_all_tripping = block_all_tripping
        self.calls = 0

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002 - httpx shape
        self.calls += 1
        content = json["messages"][0]["content"]
        if content in p.TRIPPING_PROMPTS.values() and self.block_all_tripping:
            return INTERCEPTOR_BLOCK
        import json as _json
        return FakeResponse(200, _json.dumps({"choices": [{"message": {"content": "ok"}}]}))


def test_guardrail_mode_pass_gate_requires_every_tripping_prompt_blocked(monkeypatch) -> None:
    import types
    monkeypatch.setattr(p, "mint_bearer", lambda secret_arn, region: "tok")
    args = types.SimpleNamespace(secret_arn="arn", region="us-west-2", inference_gateway_url="https://gw/mcp",
                                 model_id="m", guardrail_id="g1")
    strict = RoutingClient(block_all_tripping=True)
    monkeypatch.setattr(p.httpx, "Client", lambda: _ctx(strict))
    result = p.mode_inference_guardrail(args)
    assert result["passed"] is True
    assert result["guardrailEnforcedAtGateway"] is True
    assert result["parameterIgnoredByConnector"] is True  # benign bogus/absent -> 200
    assert set(result["trippingBlocked"]) == set(p.TRIPPING_PROMPTS)
    assert strict.calls == 3 + 2 * len(p.TRIPPING_PROMPTS)

    lenient = RoutingClient(block_all_tripping=False)
    monkeypatch.setattr(p.httpx, "Client", lambda: _ctx(lenient))
    result = p.mode_inference_guardrail(args)
    assert result["passed"] is False
    assert result["guardrailEnforcedAtGateway"] is False


class _ctx:
    def __init__(self, client) -> None:
        self._client = client

    def __enter__(self):
        return self._client

    def __exit__(self, *exc):  # noqa: ANN002
        return False
