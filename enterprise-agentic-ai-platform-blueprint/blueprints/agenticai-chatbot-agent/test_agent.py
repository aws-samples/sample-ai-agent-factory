"""Unit tests for the chatbot blueprint.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest

from chatbot_agent import ChatbotAgent, ChatbotConfig  # type: ignore[import-not-found]


class _FakeLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict] = []

    def invoke(self, messages, *, guardrail_identifier, guardrail_version, stream):
        self.calls.append(
            {
                "guardrail_identifier": guardrail_identifier,
                "stream": stream,
            }
        )
        return self.response


def _cfg(**overrides) -> ChatbotConfig:
    return ChatbotConfig(
        tenant_id="demo",
        agent_id="chat",
        env_name="prod",
        inference_profile_arn="arn",
        guardrail_identifier="gd",
        **overrides,
    )


def test_rejects_missing_guardrail():
    with pytest.raises(ValueError):
        ChatbotConfig(
            tenant_id="t", agent_id="a", env_name="e",
            inference_profile_arn="arn", guardrail_identifier="",
        )


def test_rejects_blank_actor_id():
    agent = ChatbotAgent(_cfg(), _FakeLLM("hi"))
    with pytest.raises(ValueError, match="actor_id"):
        agent.reply([], actor_id="")


def test_streams_by_default():
    llm = _FakeLLM("hello")
    agent = ChatbotAgent(_cfg(), llm)
    agent.reply([{"role": "user", "content": "hi"}], actor_id="u")
    assert llm.calls[0]["stream"] is True


def test_escalation_routes_on_marker():
    escalations: list[list[dict]] = []
    config = _cfg(hitl_hand_off=lambda msgs: escalations.append(msgs))
    llm = _FakeLLM("I cannot handle this <escalate/>")
    agent = ChatbotAgent(config, llm)
    out = agent.reply([{"role": "user", "content": "hard Q"}], actor_id="u")
    assert "human" in out.lower()
    assert len(escalations) == 1


def test_session_length_cap_escalates():
    escalations: list[list[dict]] = []
    config = _cfg(max_turns_per_session=1, hitl_hand_off=lambda msgs: escalations.append(msgs))
    llm = _FakeLLM("ok")
    agent = ChatbotAgent(config, llm)
    # 4 messages = 2 turns; max_turns_per_session=1 → threshold is 2.
    out = agent.reply(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ],
        actor_id="u",
    )
    assert "human" in out.lower()
    assert len(escalations) == 1
