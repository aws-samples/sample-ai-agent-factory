"""
agenticai-langgraph-agent — LangGraph blueprint for the AgenticAI platform.

Mirrors the Strands chatbot blueprint's invariants:
  - Bedrock Guardrail required on every model invocation.
  - Tools resolved from the platform tool catalogue (qualified MCP names).
  - MCP protocol version pinned to 2025-06-18.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol


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
class LangGraphAgentConfig:
    tenant_id: str
    agent_id: str
    env_name: str
    inference_profile_arn: str
    guardrail_identifier: str
    guardrail_version: str = "DRAFT"
    qualified_tool_names: Iterable[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.guardrail_identifier:
            raise ValueError(
                "guardrail_identifier is mandatory (R-BED-028 + SCP-02 + IAM deny + VPCE policy)"
            )


class LangGraphAgent:
    """Minimal LangGraph-shaped agent.

    The actual graph wiring (StateGraph etc.) is left to the customer; this
    skeleton encapsulates the platform-mandated invariants so the customer's
    graph nodes can call `invoke_tool` without re-deriving them.
    """

    def __init__(self, config: LangGraphAgentConfig, gateway: GatewayClient) -> None:
        self.config = config
        self.gateway = gateway

    def invoke_tool(self, qualified_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if qualified_name not in self.config.qualified_tool_names:
            raise PermissionError(
                f"Tool '{qualified_name}' not in agent's allow-list "
                f"({list(self.config.qualified_tool_names)}). "
                "Subscribe via D03TenantAllocation.allowedToolIds."
            )
        return self.gateway.call_tool(
            qualified_name, payload, protocol_version=MCP_PROTOCOL_VERSION
        )
