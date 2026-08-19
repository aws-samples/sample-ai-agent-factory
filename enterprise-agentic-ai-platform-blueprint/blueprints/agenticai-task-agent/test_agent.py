"""
Unit tests for the task-agent blueprint.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest

from task_agent import TaskAgent, TaskAgentConfig  # type: ignore[import-not-found]


class _FakeLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict] = []

    def invoke(self, messages, *, guardrail_identifier, guardrail_version, stream):
        self.calls.append(
            {
                "messages": messages,
                "guardrail_identifier": guardrail_identifier,
                "guardrail_version": guardrail_version,
                "stream": stream,
            }
        )
        return self.response


def _cfg(**overrides) -> TaskAgentConfig:
    return TaskAgentConfig(
        tenant_id="demo",
        agent_id="task",
        env_name="nonprod",
        inference_profile_arn="arn:aws:bedrock:us-west-2:444444444444:inference-profile/demo",
        guardrail_identifier="guardrail-abc",
        **overrides,
    )


def test_rejects_blank_guardrail_identifier():
    with pytest.raises(ValueError, match="guardrail_identifier"):
        TaskAgentConfig(
            tenant_id="t",
            agent_id="a",
            env_name="e",
            inference_profile_arn="arn",
            guardrail_identifier="",
        )


def test_rejects_bad_max_iterations():
    with pytest.raises(ValueError, match="max_iterations"):
        _cfg(max_iterations=0)
    with pytest.raises(ValueError, match="max_iterations"):
        _cfg(max_iterations=26)


def test_rejects_blank_actor_id():
    agent = TaskAgent(_cfg(), _FakeLLM("<done/>"))
    with pytest.raises(ValueError, match="actor_id"):
        agent.run("do the thing", actor_id="")


def test_propagates_guardrail_on_every_call():
    llm = _FakeLLM("<done/>")
    agent = TaskAgent(_cfg(), llm)
    agent.run("do X", actor_id="user-123")
    assert len(llm.calls) == 1
    assert llm.calls[0]["guardrail_identifier"] == "guardrail-abc"
    assert llm.calls[0]["guardrail_version"] == "DRAFT"
    assert llm.calls[0]["stream"] is True


def test_max_iteration_guard_trips():
    # Never-done response forces looping until the guard.
    llm = _FakeLLM("keep going...")
    agent = TaskAgent(_cfg(max_iterations=2), llm)
    with pytest.raises(RuntimeError, match="Max iterations"):
        agent.run("loop", actor_id="u")
    assert len(llm.calls) == 2


# Z7-D: HITL escalation tests.

class _FakeHitl:
    def __init__(self, exec_arn: str = "arn:aws:states:us-east-1:111111111111:execution:HITL:abc") -> None:
        self.exec_arn = exec_arn
        self.calls: list[dict] = []

    def escalate(self, payload, *, state_machine_arn):
        self.calls.append({"payload": payload, "sm": state_machine_arn})
        return self.exec_arn


def test_hitl_escalates_on_low_confidence():
    cfg = _cfg(
        hitl_state_machine_arn="arn:aws:states:us-east-1:111111111111:stateMachine:AgenticAI-HITL",
        hitl_confidence_threshold=0.7,
    )
    llm = _FakeLLM("not used")
    hitl = _FakeHitl()
    agent = TaskAgent(cfg, llm, hitl=hitl)
    out = agent.run("task", actor_id="u", confidence=0.3)
    assert "<hitl-escalation-pending" in out
    assert len(hitl.calls) == 1
    assert hitl.calls[0]["payload"]["reason"] == "low_confidence"
    assert llm.calls == []  # LLM never invoked when escalating up-front


def test_hitl_escalates_on_iteration_threshold():
    cfg = _cfg(
        max_iterations=5,
        hitl_state_machine_arn="arn:aws:states:us-east-1:111111111111:stateMachine:AgenticAI-HITL",
        hitl_iteration_threshold=2,
    )
    llm = _FakeLLM("keep going")
    hitl = _FakeHitl()
    agent = TaskAgent(cfg, llm, hitl=hitl)
    out = agent.run("task", actor_id="u")
    assert "<hitl-escalation-pending" in out
    assert hitl.calls[0]["payload"]["reason"] == "iteration_threshold"
    assert hitl.calls[0]["payload"]["iteration"] == 2
    assert len(llm.calls) == 2  # 2 LLM calls before threshold trip


def test_hitl_no_escalation_when_disabled():
    # No hitl arn ⇒ falls through to legacy max-iterations behaviour.
    llm = _FakeLLM("<done/>")
    agent = TaskAgent(_cfg(max_iterations=2), llm, hitl=_FakeHitl())
    out = agent.run("task", actor_id="u", confidence=0.1)
    assert out == "<done/>"


def test_hitl_threshold_validation():
    with pytest.raises(ValueError, match="confidence_threshold"):
        TaskAgentConfig(
            tenant_id="d", agent_id="a", env_name="e",
            inference_profile_arn="arn",
            guardrail_identifier="g",
            hitl_confidence_threshold=2.0,
        )
