"""Offline tests for the inference-Gateway guardrail interceptor.

Run: scripts/live-agentcore-generated-agent-spike/.venv/bin/python -m pytest \
       packages/platform-inference-gateway/lambda/guardrail-interceptor -q
"""
import base64
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parent))
import index  # noqa: E402


def _b64(obj) -> str:
    raw = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj).encode()
    return base64.b64encode(raw).decode()


def _event(body, path="/inference/v1/chat/completions", method="POST"):
    return {
        "interceptorInputVersion": "1.0",
        "http": {"gatewayRequest": {"path": path, "httpMethod": method, "body": body}},
    }


def _response(result):
    body = json.loads(base64.b64decode(result["http"]["transformedGatewayResponse"]["body"]))
    return result["http"]["transformedGatewayResponse"]["statusCode"], body


class FakeBedrock:
    def __init__(self, action="NONE", assessments=None, error=None):
        self.action = action
        self.assessments = assessments or []
        self.error = error
        self.calls = []

    def apply_guardrail(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"action": self.action, "assessments": self.assessments}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GUARDRAIL_IDENTIFIER", "abc123guard")
    monkeypatch.setenv("GUARDRAIL_VERSION", "DRAFT")
    monkeypatch.setenv("MAX_GUARDED_CHARACTERS", "60000")
    monkeypatch.delenv("BLOCKED_MESSAGE", raising=False)


@pytest.fixture
def bedrock(monkeypatch):
    fake = FakeBedrock()
    monkeypatch.setattr(index, "_bedrock_runtime", lambda: fake)
    return fake


BLOCKED_VIOLENCE = [
    {"contentPolicy": {"filters": [
        {"type": "VIOLENCE", "action": "BLOCKED", "detected": True, "confidence": "HIGH"},
        {"type": "HATE", "action": "NONE", "detected": False},
    ]}},
]
ANONYMIZED_ONLY = [
    {"sensitiveInformationPolicy": {"piiEntities": [
        {"type": "EMAIL", "action": "ANONYMIZED", "detected": True, "match": "a@b.c"},
    ]}},
]


# ----------------------------------------------------------------- extraction
def test_extracts_openai_string_and_part_content():
    payload = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "image_url", "image_url": {"url": "x"}}]},
        ],
    }
    assert index.extract_texts(payload) == ["sys", "hello"]


def test_extracts_anthropic_system_and_responses_input():
    assert index.extract_texts({"system": "s", "messages": [{"role": "user", "content": "u"}]}) == ["u", "s"]
    assert index.extract_texts({"input": "plain"}) == ["plain"]
    assert index.extract_texts({"input": [{"role": "user", "content": [{"type": "input_text", "text": "t"}]}]}) == ["t"]
    assert index.extract_texts({"prompt": "p", "instructions": "i"}) == ["p", "i"]


def test_split_and_batch_respect_limits():
    blocks = index.split_blocks(["a" * 45_000, "b"])
    assert [len(b) for b in blocks] == [20_000, 20_000, 5_000, 1]
    grouped = list(index.batches(blocks))
    assert all(sum(len(b) for b in g) <= index.BATCH_CHARACTERS for g in grouped)
    assert sum(len(g) for g in grouped) == len(blocks)


def test_blocked_types_only_reports_blocked_actions():
    assert index.blocked_types(BLOCKED_VIOLENCE) == ["contentPolicy.VIOLENCE"]
    assert index.blocked_types(ANONYMIZED_ONLY) == []
    topics = [{"topicPolicy": {"topics": [{"name": "CredentialExposure", "action": "BLOCKED"}]}}]
    assert index.blocked_types(topics) == ["topicPolicy.CredentialExposure"]


# ------------------------------------------------------------------ decisions
def test_benign_request_passes_through_unchanged(bedrock):
    result = index.handler(_event(_b64({"model": "m", "messages": [{"role": "user", "content": "hi"}]})), None)
    assert result == {"interceptorOutputVersion": "1.0", "http": {}}
    assert bedrock.calls[0]["source"] == "INPUT"
    assert bedrock.calls[0]["guardrailIdentifier"] == "abc123guard"
    assert bedrock.calls[0]["guardrailVersion"] == "DRAFT"
    assert bedrock.calls[0]["content"] == [{"text": {"text": "hi"}}]


def test_blocked_request_short_circuits_with_403(bedrock):
    bedrock.action, bedrock.assessments = "GUARDRAIL_INTERVENED", BLOCKED_VIOLENCE
    result = index.handler(_event(_b64({"model": "m", "messages": [{"role": "user", "content": "x"}]})), None)
    status, body = _response(result)
    assert status == 403
    assert body["error"]["code"] == "guardrail_intervened"
    assert body["error"]["tripped"] == ["contentPolicy.VIOLENCE"]
    assert body["error"]["guardrail"] == {"id": "abc123guard", "version": "DRAFT"}
    assert result["http"]["transformedGatewayResponse"]["headers"]["x-agenticai-guardrail"] == "guardrail_intervened"
    assert "x" not in json.dumps(body)  # request text is never echoed


def test_anonymize_only_intervention_is_not_a_block(bedrock):
    bedrock.action, bedrock.assessments = "GUARDRAIL_INTERVENED", ANONYMIZED_ONLY
    result = index.handler(_event(_b64({"messages": [{"role": "user", "content": "mail a@b.c"}]})), None)
    assert result == {"interceptorOutputVersion": "1.0", "http": {}}


def test_streaming_flag_does_not_bypass_evaluation(bedrock):
    bedrock.action, bedrock.assessments = "GUARDRAIL_INTERVENED", BLOCKED_VIOLENCE
    result = index.handler(_event(_b64({"stream": True, "messages": [{"role": "user", "content": "x"}]})), None)
    assert _response(result)[0] == 403


# ----------------------------------------------------------------- fail closed
def test_guardrail_api_error_fails_closed_503(bedrock):
    bedrock.error = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "ApplyGuardrail")
    result = index.handler(_event(_b64({"messages": [{"role": "user", "content": "x"}]})), None)
    status, body = _response(result)
    assert status == 503 and body["error"]["code"] == "guardrail_unavailable"


def test_missing_configuration_fails_closed_503(bedrock, monkeypatch):
    monkeypatch.delenv("GUARDRAIL_VERSION")
    result = index.handler(_event(_b64({"messages": [{"role": "user", "content": "x"}]})), None)
    assert _response(result)[0] == 503
    assert bedrock.calls == []


def test_oversized_text_fails_closed_413(bedrock):
    result = index.handler(_event(_b64({"messages": [{"role": "user", "content": "z" * 60_001}]})), None)
    assert _response(result)[0] == 413
    assert bedrock.calls == []


def test_non_json_and_non_object_bodies_are_rejected_400(bedrock):
    assert _response(index.handler(_event(_b64(b"not json")), None))[0] == 400
    assert _response(index.handler(_event(_b64([1, 2])), None))[0] == 400
    assert _response(index.handler(_event("%%%not-base64%%%"), None))[0] == 400
    assert bedrock.calls == []


# ------------------------------------------------------------------ passthrough
def test_requests_without_text_pass_through(bedrock):
    assert index.handler(_event(None, path="/inference/v1/models", method="GET"), None) == index.passthrough()
    assert index.handler(_event(_b64({"model": "m"})), None) == index.passthrough()
    assert bedrock.calls == []


def test_mcp_payload_passes_through_with_original_body(bedrock):
    event = {"interceptorInputVersion": "1.0", "mcp": {"gatewayRequest": {"body": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}}}}
    result = index.handler(event, None)
    assert result["mcp"]["transformedGatewayRequest"]["body"]["method"] == "tools/list"
    assert bedrock.calls == []


def test_large_prompt_is_evaluated_in_multiple_calls(bedrock):
    text = "q" * 55_000
    result = index.handler(_event(_b64({"messages": [{"role": "user", "content": text}]})), None)
    assert result == index.passthrough()
    assert len(bedrock.calls) >= 3
    assert sum(len(c["text"]["text"]) for call in bedrock.calls for c in call["content"]) == 55_000
