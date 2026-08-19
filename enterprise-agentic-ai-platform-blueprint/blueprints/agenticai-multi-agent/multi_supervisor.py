"""
agenticai-multi-agent — supervisor.

The supervisor routes sub-tasks to worker agents via `bedrock-agentcore:
InvokeAgentRuntime`. Cross-agent calls within the same account are
permitted by the AgentCore Runtime RBP (spec §3.1.3).

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


class WorkerClient(Protocol):
    def invoke(self, task: str, *, actor_id: str) -> str: ...


class HitlEscalator(Protocol):
    """Z7-D: pluggable HITL hook. Real impl calls Step Functions
    StartExecution against the platform HITL state machine."""

    def escalate(self, payload: dict[str, object], *, state_machine_arn: str) -> str: ...


@dataclass(frozen=True)
class SupervisorConfig:
    tenant_id: str
    agent_id: str
    env_name: str
    inference_profile_arn: str
    guardrail_identifier: str
    workers: dict[str, WorkerClient]   # worker-name -> client
    max_dispatches: int = 20
    guardrail_version: str = "DRAFT"
    # Z7-D: HITL — multi-agent systems are explicitly classified `high` risk
    # in the EU AI Act mapping (chain-of-effect risk). Make HITL the default
    # for any worker_name that ends in '-sensitive' OR when `force_hitl` is
    # set on a per-dispatch basis.
    hitl_state_machine_arn: str | None = None
    hitl_sensitive_worker_suffix: str = "-sensitive"

    def __post_init__(self) -> None:
        if not self.guardrail_identifier:
            raise ValueError("guardrail_identifier mandatory (R-BED-028)")
        if not self.workers:
            raise ValueError("at least one worker is required")


class SupervisorAgent:
    def __init__(self, config: SupervisorConfig, hitl: HitlEscalator | None = None) -> None:
        self.config = config
        self.hitl = hitl

    def dispatch(
        self,
        tasks: list[tuple[str, str]],
        actor_id: str,
        *,
        force_hitl: set[str] | None = None,
    ) -> dict[str, str]:
        """Dispatch (worker-name, task) pairs; return worker-name -> result.

        Each dispatched invocation propagates `actor_id` for memory scoping.
        Z7-D: workers whose name ends with `hitl_sensitive_worker_suffix`
        OR are listed in `force_hitl` route through the platform HITL SF
        instead of being invoked directly.
        """
        if len(tasks) > self.config.max_dispatches:
            raise RuntimeError(
                f"refusing to dispatch {len(tasks)} tasks - max {self.config.max_dispatches}"
            )
        results: dict[str, str] = {}
        force = force_hitl or set()
        for worker_name, task in tasks:
            if worker_name not in self.config.workers:
                raise KeyError(f"no worker named '{worker_name}' in supervisor config")
            sensitive = worker_name.endswith(self.config.hitl_sensitive_worker_suffix)
            forced = worker_name in force
            if (sensitive or forced) and self.hitl and self.config.hitl_state_machine_arn:
                exec_arn = self.hitl.escalate(
                    {
                        "reason": "sensitive_worker" if sensitive else "force_hitl",
                        "worker": worker_name,
                        "task": task,
                        "actor_id": actor_id,
                    },
                    state_machine_arn=self.config.hitl_state_machine_arn,
                )
                results[worker_name] = f"<hitl-escalation-pending execution=\"{exec_arn}\"/>"
                continue
            log.debug("dispatch worker=%s task_len=%d", worker_name, len(task))
            results[worker_name] = self.config.workers[worker_name].invoke(
                task, actor_id=actor_id
            )
        return results
