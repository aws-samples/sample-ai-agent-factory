"""Offline contract tests for ``registry_reader_trust_twins.py``.

No AWS calls: the Lambda client is faked. They pin (1) the classifier that
turns a validator invoke into evidence, (2) the pass gate, which needs the
positive leg, BOTH denial twins as exact STS 403 AccessDenied, a byte-identical
restored environment and a passing post-restore invoke, and (3) that the
ExternalId value never reaches the evidence.
"""
from __future__ import annotations

import io
import json

import registry_reader_trust_twins as twins


class FakePayload:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body


class FakeLambda:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)

    def invoke(self, FunctionName: str, Payload: bytes) -> dict:  # noqa: N803 - boto shape
        return self.responses.pop(0)


def denied(message: str) -> dict:
    return {
        "StatusCode": 200,
        "FunctionError": "Unhandled",
        "Payload": FakePayload(json.dumps({"errorType": "Error", "errorMessage": message})),
    }


STS_DENIAL = (
    "STS AssumeRole HTTP 403: <ErrorResponse><Error><Type>Sender</Type><Code>AccessDenied</Code>"
    "<Message>User: arn:aws:sts::111111111111:assumed-role/AgenticAI-D03-nonprod-x-y-RegistryValidator/fn "
    "is not authorized to perform: sts:AssumeRole on resource: arn:aws:iam::222222222222:role/Reader"
    "</Message></Error></ErrorResponse>"
)


def test_invoke_classifies_exact_sts_access_denied() -> None:
    result = twins.invoke(FakeLambda([denied(STS_DENIAL)]), "fn", {})
    assert result["functionError"] == "Unhandled"
    assert result["stsHttpStatus"] == 403
    assert result["accessDenied"] is True
    assert "AccessDenied" not in json.dumps(result)  # only a fingerprint leaves the probe
    assert result["messageFingerprint"] == twins.fingerprint(STS_DENIAL)


def test_invoke_does_not_accept_other_failures_as_denials() -> None:
    other = twins.invoke(FakeLambda([denied("AgentCore Registry record 'r' not found in registry 'g'.")]), "fn", {})
    assert other["stsHttpStatus"] is None
    assert other["accessDenied"] is False
    wrong_code = twins.invoke(FakeLambda([denied("STS AssumeRole HTTP 403: <Code>Throttling</Code>")]), "fn", {})
    assert wrong_code["stsHttpStatus"] == 403
    assert wrong_code["accessDenied"] is False


def test_invoke_positive_records_object_payload_only_as_fingerprint() -> None:
    ok = {"StatusCode": 200, "Payload": FakePayload(json.dumps({"Data": {"validated": True}}))}
    result = twins.invoke(FakeLambda([ok]), "fn", {})
    assert result["functionError"] is None
    assert result["payloadIsObject"] is True
    assert "validated" not in json.dumps(result)


def test_fingerprint_never_echoes_input() -> None:
    secret = "external-id-that-must-not-leak"
    assert secret not in twins.fingerprint(secret)
    assert len(twins.fingerprint(secret)) == 32
