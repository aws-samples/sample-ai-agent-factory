"""
agenticai-crewai-agent — CrewAI blueprint for the AgenticAI platform.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol


MCP_PROTOCOL_VERSION = "2025-06-18"


class GatewayClient(Protocol):
    def call_tool(
        self,
        qualified_tool_name: str,
        payload: dict[str, Any],
        *,
        protocol_version: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CrewAIAgentConfig:
    tenant_id: str
    agent_id: str
    env_name: str
    inference_profile_arn: str
    guardrail_identifier: str
    qualified_tool_names: Iterable[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.guardrail_identifier:
            raise ValueError(
                "guardrail_identifier is mandatory (R-BED-028 + SCP-02 + IAM deny + VPCE policy)"
            )


class CrewAIAgent:
    def __init__(self, config: CrewAIAgentConfig, gateway: GatewayClient) -> None:
        self.config = config
        self.gateway = gateway

    def invoke_tool(self, qualified_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if qualified_name not in self.config.qualified_tool_names:
            raise PermissionError(
                f"Tool '{qualified_name}' not in agent's allow-list "
                f"({list(self.config.qualified_tool_names)})."
            )
        return self.gateway.call_tool(
            qualified_name, payload, protocol_version=MCP_PROTOCOL_VERSION
        )
