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
        ({"stopReason": "protocol_violation"}, "stopReasonDone"),
        ({"stopReason": "max_iterations"}, "stopReasonDone"),
    ],
)
def test_positive_gate_fails_closed_per_leg(mutation: dict, failing_check: str) -> None:
    body = {**GOOD_BODY, **mutation}
    checks = probe.assess_positive(200, body)
    assert checks[failing_check] is False
    assert not all(v is True for v in checks.values())


def test_positive_gate_rejects_non_2xx_even_with_good_body() -> None:
    assert probe.assess_positive(500, GOOD_BODY)["http2xx"] is False


def test_unsubscribed_twin_accepts_either_refusal_layer_and_rejects_leaks() -> None:
    # Layer 2: the agent's PermissionError surfaces as a non-2xx without marker.
    refused = {"errorCode": "RuntimeClientError", "errorMessageFingerprint": "x"}
    checks = probe.assess_unsubscribed(424, refused)
    assert checks["refusedByModelAllowlistOrAgentGuard"] is True
    assert checks["refusalLayer"] == "agent-permission-error"
    assert checks["forbiddenToolNeverCalled"] is True
    assert checks["noToolCallsRecorded"] is True

    # Layer 1 (live-proven deterministic): the model declines the forbidden
    # directive itself -> 200, marker, no tool calls, ONE content block.
    declined = {**GOOD_BODY, "toolCalls": [], "contentBlocks": 1}
    checks = probe.assess_unsubscribed(200, declined)
    assert checks["refusedByModelAllowlistOrAgentGuard"] is True
    assert checks["refusalLayer"] == "model-allowlist"

    # A marker-bearing 200 that ALSO ran a tool loop is not a refusal.
    looped = {**GOOD_BODY, "toolCalls": [], "contentBlocks": 2}
    checks = probe.assess_unsubscribed(200, looped)
    assert checks["refusedByModelAllowlistOrAgentGuard"] is False
    assert checks["refusalLayer"] == "none"

    leaked = {**GOOD_BODY, "toolCalls": [probe.UNSUBSCRIBED_TOOL]}
    checks = probe.assess_unsubscribed(200, leaked)
    assert checks["refusedByModelAllowlistOrAgentGuard"] is False
    assert checks["forbiddenToolNeverCalled"] is False

    # 1.2.0+: a decline preceded by one counted protocol repair is still the
    # model's refusal; an extra block that is NOT a counted repair is not.
    repaired = {**GOOD_BODY, "toolCalls": [], "contentBlocks": 2, "protocolRepairs": 1}
    assert probe.assess_unsubscribed(200, repaired)["refusalLayer"] == "model-allowlist"
    uncounted = {**GOOD_BODY, "toolCalls": [], "contentBlocks": 3, "protocolRepairs": 1}
    assert probe.assess_unsubscribed(200, uncounted)["refusalLayer"] == "none"


def test_positive_gate_tolerates_revisions_without_stop_reason_only() -> None:
    import agent as agent_mod

    assert probe.STOP_DONE == agent_mod.STOP_DONE
    legacy = {k: v for k, v in GOOD_BODY.items() if k != "stopReason"}
    assert probe.assess_positive(200, legacy)["stopReasonDone"] is True
    assert probe.assess_positive(200, {**GOOD_BODY, "stopReason": "done"})["stopReasonDone"] is True


def test_prompt_names_the_exact_tool_and_arguments_the_agent_grammar_accepts() -> None:
    # The user turn names the tool and its JSON arguments in plain language
    # (the protocol lives in the agent's system prompt); the directive the model
    # composes from them must parse. If either side drifts, the live tool leg
    # silently degrades to "no tool call" -- catch it offline.
    assert probe.ECHO_TOOL in probe.POSITIVE_PROMPT
    assert probe.UNSUBSCRIBED_TOOL in probe.UNSUBSCRIBED_PROMPT
    assert json.loads(probe.PROMPT_ARGUMENTS) == {"message": "probe"}
    directive = f"TOOL {probe.ECHO_TOOL} {probe.PROMPT_ARGUMENTS}"
    parsed = ReferenceAgentCore._parse_tool_request(directive)
    assert parsed == (probe.ECHO_TOOL, {"message": "probe"})
    # No imperative "reply with exactly ... nothing else" wording: it scores as
    # a prompt attack under the baseline guardrail (live 2026-09-24).
    assert "nothing else" not in probe.POSITIVE_PROMPT.lower()


def test_fingerprint_never_echoes_input() -> None:
    secret = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/abc"
    fp = probe.fingerprint(secret)
    assert len(fp) == 32 and secret not in fp and "123456789012" not in fp
