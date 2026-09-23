"""Offline tests for ``live_invoke_probe`` gate logic.

No AWS calls: only the pure assessment functions and the prompt grammar are
exercised, so a regression in the gate definition (what counts as a passing
live round-trip) is caught before any live run.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import json

import pytest

import live_invoke_probe as probe
from agent import HANDSHAKE_MARKER, ReferenceAgentCore

GOOD_BODY = {
    "marker": HANDSHAKE_MARKER,
    "replyFingerprint": "abc",
    "toolCalls": [probe.ECHO_TOOL],
    "discoveredToolCount": 2,
    "memoryRoundTrip": True,
    "contentBlocks": 2,
    "memoryConfigured": True,
}


def test_positive_gate_requires_all_five_legs() -> None:
    checks = probe.assess_positive(200, GOOD_BODY)
    assert all(checks.values()), checks


@pytest.mark.parametrize(
    "mutation, failing_check",
    [
        ({"marker": "other"}, "markerExact"),
        ({"discoveredToolCount": 0}, "discoveredToolsPositive"),
        ({"toolCalls": []}, "echoToolCalled"),
        ({"toolCalls": [probe.ECHO_TOOL, "target-x___x"]}, "onlySubscribedToolsCalled"),
        ({"contentBlocks": 0}, "inferenceContentBlocksPositive"),
        ({"memoryRoundTrip": False}, "memoryRoundTrip"),
        ({"memoryConfigured": False}, "memoryConfigured"),
    ],
)
def test_positive_gate_fails_closed_per_leg(mutation: dict, failing_check: str) -> None:
    body = {**GOOD_BODY, **mutation}
    checks = probe.assess_positive(200, body)
    assert checks[failing_check] is False
    assert not all(v is True for v in checks.values())


def test_positive_gate_rejects_non_2xx_even_with_good_body() -> None:
    assert probe.assess_positive(500, GOOD_BODY)["http2xx"] is False


def test_unsubscribed_twin_rejects_any_success_marker_or_tool_call() -> None:
    refused = {"errorCode": "RuntimeClientError", "errorMessageFingerprint": "x"}
    checks = probe.assess_unsubscribed(424, refused)
    assert checks["runtimeDidNotReturnSuccessMarker"] is True
    assert checks["forbiddenToolNeverCalled"] is True
    assert checks["noToolCallsRecorded"] is True

    leaked = {**GOOD_BODY, "toolCalls": [probe.UNSUBSCRIBED_TOOL]}
    checks = probe.assess_unsubscribed(200, leaked)
    assert checks["runtimeDidNotReturnSuccessMarker"] is False
    assert checks["forbiddenToolNeverCalled"] is False


def test_prompt_directive_is_parseable_by_the_agent_grammar() -> None:
    # The probe's prompt embeds the exact directive line the agent's parser
    # accepts; if either side drifts, the live tool leg silently degrades to
    # "no tool call" -- catch it offline.
    directive = probe.POSITIVE_PROMPT.split("else: ", 1)[1].split(" . Step 2", 1)[0]
    parsed = ReferenceAgentCore._parse_tool_request(directive)
    assert parsed == (probe.ECHO_TOOL, {"message": "probe"})
    assert json.loads(directive.split(" ", 2)[2]) == {"message": "probe"}


def test_fingerprint_never_echoes_input() -> None:
    secret = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/abc"
    fp = probe.fingerprint(secret)
    assert len(fp) == 32 and secret not in fp and "123456789012" not in fp
