"""
agenticai-task-agent — Strands blueprint, task-agent pattern.

Spec §4.1 task-agent defaults:
  * Deterministic tool-use (no stochastic tool selection in hot path)
  * Max-iteration guard (default 10)
  * Streaming invocation via LiteLLM (D-01)
  * Baseline guardrail attached (R-BED-028 GuardrailIdentifier always set)
  * Memory actor-scoped; no real end-user identity in session tags (§3.4.6)

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Protocol

# Import guards — blueprint runs under the Strands SDK in production.
# Provide a Protocol for testability without requiring Strands at test time.

log = logging.getLogger(__name__)

MAX_ITERATIONS_DEFAULT = 10
STREAM_DEFAULT = True


class LLMClient(Protocol):
    def invoke(
        self,
        messages: list[dict[str, str]],
        *,
        guardrail_identifier: str,
        guardrail_version: str,
        stream: bool,
    ) -> str: ...


@dataclass(frozen=True)
class TaskAgentConfig:
    tenant_id: str
    agent_id: str
    env_name: str
    inference_profile_arn: str
    guardrail_identifier: str
    guardrail_version: str = "DRAFT"
    max_iterations: int = MAX_ITERATIONS_DEFAULT
    stream: bool = STREAM_DEFAULT
    memory_namespace: str = ""
    # Z7-D HITL integration. Optional — when set, task-agent escalates to
    # the platform HITL Step Function whenever its self-confidence drops
    # below the threshold or when an iteration would exceed
    # `hitl_iteration_threshold`.
    hitl_state_machine_arn: str | None = None
    hitl_confidence_threshold: float = 0.7
    hitl_iteration_threshold: int | None = None  # default disabled

    def __post_init__(self) -> None:
        if not self.guardrail_identifier:
            raise ValueError(
                "guardrail_identifier is mandatory (R-BED-028 + SCP-02 + IAM deny + VPCE policy)"
            )
        if self.max_iterations < 1 or self.max_iterations > 25:
            raise ValueError("max_iterations must be 1..25")
        if not 0.0 <= self.hitl_confidence_threshold <= 1.0:
            raise ValueError("hitl_confidence_threshold must be in [0, 1]")


class HitlEscalator(Protocol):
    """Z7-D abstraction. Caller supplies a real boto3-backed implementation
    that calls Step Functions `StartExecution` against the platform HITL SF.
    Tests inject a fake."""

    def escalate(self, payload: dict[str, object], *, state_machine_arn: str) -> str: ...


class TaskAgent:
    """Deterministic task-execution agent with mandatory guardrail."""

    def __init__(
        self,
        config: TaskAgentConfig,
        llm: LLMClient,
        tools: dict[str, Callable[..., str]] | None = None,
        hitl: HitlEscalator | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.tools = tools or {}
        # Z7-D: optional HITL escalator. When None, the iteration-threshold
        # path falls through to the legacy max-iterations guard.
        self.hitl = hitl

    def run(self, task_prompt: str, *, actor_id: str, confidence: float | None = None) -> str:
        """Execute the task. `actor_id` is AgentCore's only allowed scope.

        Raises RuntimeError if the max-iteration guard trips with no HITL.
        Returns the human-approved continuation when HITL escalation resolves.
        """
        if not actor_id:
            raise ValueError("actor_id is required (spec §3.4.6 — only scoping accepted)")

        # Z7-D: pre-check confidence before LLM call.
        if (
            confidence is not None
            and confidence < self.config.hitl_confidence_threshold
            and self.hitl is not None
            and self.config.hitl_state_machine_arn
        ):
            return self._escalate(
                {
                    "reason": "low_confidence",
                    "confidence": confidence,
                    "task_prompt": task_prompt,
                    "actor_id": actor_id,
                },
            )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": task_prompt},
        ]

        for iteration in range(self.config.max_iterations):
            log.debug(
                "iteration=%d agent=%s/%s actor=%s",
                iteration,
                self.config.tenant_id,
                self.config.agent_id,
                actor_id,
            )
            # Z7-D iteration-threshold gate.
            if (
                self.config.hitl_iteration_threshold is not None
                and iteration >= self.config.hitl_iteration_threshold
                and self.hitl is not None
                and self.config.hitl_state_machine_arn
            ):
                return self._escalate(
                    {
                        "reason": "iteration_threshold",
                        "iteration": iteration,
                        "task_prompt": task_prompt,
                        "actor_id": actor_id,
                    },
                )
            response = self.llm.invoke(
                messages,
                guardrail_identifier=self.config.guardrail_identifier,
                guardrail_version=self.config.guardrail_version,
                stream=self.config.stream,
            )
            if self._is_done(response):
                return response
            messages.append({"role": "assistant", "content": response})

        raise RuntimeError(
            f"Max iterations ({self.config.max_iterations}) exceeded without completion"
        )

    def _escalate(self, payload: dict[str, object]) -> str:
        assert self.hitl is not None
        assert self.config.hitl_state_machine_arn is not None
        execution = self.hitl.escalate(payload, state_machine_arn=self.config.hitl_state_machine_arn)
        log.info("HITL escalation started: %s", execution)
        return f"<hitl-escalation-pending execution=\"{execution}\"/>"

    def _system_prompt(self) -> str:
        prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "system.md")
        if os.path.exists(prompt_path):
            with open(prompt_path, encoding="utf-8") as f:
                return f.read()
        return (
            "You are a task-execution agent. Use tools deterministically. "
            "Respond with '<done/>' when the task is complete."
        )

    @staticmethod
    def _is_done(response: str) -> bool:
        return "<done/>" in response or response.strip().endswith("DONE")
