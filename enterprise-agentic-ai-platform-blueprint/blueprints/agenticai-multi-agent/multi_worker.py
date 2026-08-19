"""
agenticai-multi-agent — worker.

A specialised agent invoked by the supervisor. Lightweight; the heavy work
(guardrail, memory, inference profile) is handled at the per-worker
AgenticApp L3 instance in the CDK stack.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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
class WorkerConfig:
    tenant_id: str
    agent_id: str
    env_name: str
    specialty: str   # e.g. 'search', 'summariser', 'ticketing'
    inference_profile_arn: str
    guardrail_identifier: str
    guardrail_version: str = "DRAFT"


class Worker:
    def __init__(self, config: WorkerConfig, llm: LLMClient) -> None:
        if not config.guardrail_identifier:
            raise ValueError("guardrail_identifier mandatory (R-BED-028)")
        self.config = config
        self.llm = llm

    def invoke(self, task: str, *, actor_id: str) -> str:
        if not actor_id:
            raise ValueError("actor_id is required (spec §3.4.6)")
        system = f"You are the {self.config.specialty} specialist. Respond concisely."
        return self.llm.invoke(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": task},
            ],
            guardrail_identifier=self.config.guardrail_identifier,
            guardrail_version=self.config.guardrail_version,
            stream=False,
        )
