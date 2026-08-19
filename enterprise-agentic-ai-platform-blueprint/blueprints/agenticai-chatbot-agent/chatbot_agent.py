"""
agenticai-chatbot-agent — Strands blueprint, chatbot pattern with HITL.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

log = logging.getLogger(__name__)


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
class ChatbotConfig:
    tenant_id: str
    agent_id: str
    env_name: str
    inference_profile_arn: str
    guardrail_identifier: str
    guardrail_version: str = "DRAFT"
    max_turns_per_session: int = 50
    stream: bool = True
    hitl_hand_off: Callable[[list[dict[str, str]]], None] | None = None
    memory_namespace: str = ""

    def __post_init__(self) -> None:
        if not self.guardrail_identifier:
            raise ValueError(
                "guardrail_identifier is mandatory (R-BED-028 + SCP-02 + IAM deny + VPCE policy)"
            )


class ChatbotAgent:
    """Multi-turn conversation agent with HITL escalation."""

    def __init__(self, config: ChatbotConfig, llm: LLMClient) -> None:
        self.config = config
        self.llm = llm

    def reply(
        self,
        session_messages: list[dict[str, str]],
        *,
        actor_id: str,
    ) -> str:
        if not actor_id:
            raise ValueError("actor_id is required (spec §3.4.6)")
        if len(session_messages) > self.config.max_turns_per_session * 2:
            # Escalate — session has run too long without resolution.
            if self.config.hitl_hand_off:
                self.config.hitl_hand_off(session_messages)
            return "I'll connect you to a human who can help further."

        response = self.llm.invoke(
            session_messages,
            guardrail_identifier=self.config.guardrail_identifier,
            guardrail_version=self.config.guardrail_version,
            stream=self.config.stream,
        )

        if self._needs_escalation(response):
            if self.config.hitl_hand_off:
                self.config.hitl_hand_off(session_messages + [{"role": "assistant", "content": response}])
            return (
                "I'm not able to resolve this confidently. "
                "I'm handing you off to a human agent."
            )

        return response

    @staticmethod
    def _needs_escalation(response: str) -> bool:
        markers = ("<escalate/>", "CANNOT_RESOLVE", "HITL_REQUIRED")
        return any(m in response for m in markers)
