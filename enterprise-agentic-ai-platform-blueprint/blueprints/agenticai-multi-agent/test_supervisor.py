"""Unit tests for the multi-agent supervisor blueprint.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest

from multi_supervisor import SupervisorAgent, SupervisorConfig  # type: ignore[import-not-found]
from multi_worker import Worker, WorkerConfig  # type: ignore[import-not-found]


class _FakeLLM:
    def invoke(self, messages, *, guardrail_identifier, guardrail_version, stream):
        return f"worker-did={messages[-1]['content']}|gd={guardrail_identifier}"


def _worker(specialty: str) -> Worker:
    return Worker(
        WorkerConfig(
            tenant_id="t",
            agent_id=f"w-{specialty}",
            env_name="nonprod",
            specialty=specialty,
            inference_profile_arn="arn",
            guardrail_identifier="gd-wx",
        ),
        _FakeLLM(),
    )


def test_supervisor_requires_workers():
    with pytest.raises(ValueError, match="at least one worker"):
        SupervisorConfig(
            tenant_id="t", agent_id="s", env_name="nonprod",
            inference_profile_arn="arn", guardrail_identifier="gd",
            workers={},
        )


def test_supervisor_dispatches_to_named_worker():
    search = _worker("search")
    supervisor = SupervisorAgent(
        SupervisorConfig(
            tenant_id="t", agent_id="s", env_name="nonprod",
            inference_profile_arn="arn", guardrail_identifier="gd",
            workers={"search": search},
        )
    )
    results = supervisor.dispatch([("search", "find X")], actor_id="u")
    assert "worker-did=find X" in results["search"]
    assert "gd=gd-wx" in results["search"]


def test_supervisor_rejects_unknown_worker():
    supervisor = SupervisorAgent(
        SupervisorConfig(
            tenant_id="t", agent_id="s", env_name="nonprod",
            inference_profile_arn="arn", guardrail_identifier="gd",
            workers={"search": _worker("search")},
        )
    )
    with pytest.raises(KeyError):
        supervisor.dispatch([("unknown", "task")], actor_id="u")


def test_dispatch_respects_max_limit():
    supervisor = SupervisorAgent(
        SupervisorConfig(
            tenant_id="t", agent_id="s", env_name="nonprod",
            inference_profile_arn="arn", guardrail_identifier="gd",
            max_dispatches=1,
            workers={"search": _worker("search")},
        )
    )
    with pytest.raises(RuntimeError, match="max 1"):
        supervisor.dispatch([("search", "a"), ("search", "b")], actor_id="u")


def test_worker_requires_actor_id():
    w = _worker("search")
    with pytest.raises(ValueError, match="actor_id"):
        w.invoke("task", actor_id="")


# Z7-D: HITL escalation tests for the supervisor.

class _FakeHitl:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def escalate(self, payload, *, state_machine_arn):
        self.calls.append({"payload": payload, "sm": state_machine_arn})
        return "arn:aws:states:us-east-1:111111111111:execution:HITL:abc"


def test_sensitive_worker_routes_through_hitl():
    fake_workers = {"refunds-sensitive": _worker("refunds-sensitive"), "search": _worker("search")}
    cfg = SupervisorConfig(
        tenant_id="t", agent_id="sup", env_name="e",
        inference_profile_arn="arn", guardrail_identifier="g",
        workers=fake_workers,
        hitl_state_machine_arn="arn:aws:states:us-east-1:111111111111:stateMachine:HITL",
    )
    hitl = _FakeHitl()
    sup = SupervisorAgent(cfg, hitl=hitl)
    out = sup.dispatch([("refunds-sensitive", "process refund X"), ("search", "find Y")], actor_id="u")
    assert "<hitl-escalation-pending" in out["refunds-sensitive"]
    assert out["search"].startswith("worker-did=")
    assert len(hitl.calls) == 1
    assert hitl.calls[0]["payload"]["reason"] == "sensitive_worker"


def test_force_hitl_overrides_per_dispatch():
    fake_workers = {"search": _worker("search")}
    cfg = SupervisorConfig(
        tenant_id="t", agent_id="sup", env_name="e",
        inference_profile_arn="arn", guardrail_identifier="g",
        workers=fake_workers,
        hitl_state_machine_arn="arn:aws:states:us-east-1:111111111111:stateMachine:HITL",
    )
    hitl = _FakeHitl()
    sup = SupervisorAgent(cfg, hitl=hitl)
    out = sup.dispatch([("search", "task")], actor_id="u", force_hitl={"search"})
    assert "<hitl-escalation-pending" in out["search"]
    assert hitl.calls[0]["payload"]["reason"] == "force_hitl"


def test_no_hitl_without_state_machine_arn():
    # Even with a HITL escalator, no SF arn means no escalation.
    fake_workers = {"refunds-sensitive": _worker("refunds-sensitive")}
    cfg = SupervisorConfig(
        tenant_id="t", agent_id="sup", env_name="e",
        inference_profile_arn="arn", guardrail_identifier="g",
        workers=fake_workers,
    )
    hitl = _FakeHitl()
    sup = SupervisorAgent(cfg, hitl=hitl)
    out = sup.dispatch([("refunds-sensitive", "task")], actor_id="u")
    assert out["refunds-sensitive"].startswith("worker-did=")
    assert hitl.calls == []
