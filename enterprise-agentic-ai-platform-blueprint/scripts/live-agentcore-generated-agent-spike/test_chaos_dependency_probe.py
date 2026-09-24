"""Offline contract tests for ``chaos_dependency_probe.py`` (no AWS calls)."""
from __future__ import annotations

import base64
import json

import chaos_dependency_probe as p


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status_code = status
        self.text = body

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
