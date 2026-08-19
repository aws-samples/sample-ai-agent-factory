"""
Real CrewAI integration. Lazy-imports `crewai`. Z7-G: replaces the
Strands-shaped scaffold with a real Crew + Agent + Task pipeline.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

from typing import Any, Callable

from crewai_agent import CrewAIAgentConfig, MCP_PROTOCOL_VERSION


def build_crew(
    config: CrewAIAgentConfig,
    invoke_tool: Callable[[str, dict[str, Any]], dict[str, Any]],
):
    """Construct a CrewAI `Crew` with a single `Agent` whose tools are the
    qualified MCP names from the platform tool catalogue. Lazy-imports
    `crewai` so the rest of the blueprint imports without it."""
    try:
        from crewai import Agent, Crew, Task  # type: ignore[import-not-found]
        from crewai.tools import BaseTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "crewai is not installed. Install the pinned dependency set: "
            "`pip install -r requirements.txt` (versions are pinned with == to "
            "the tested releases; crewai is pinned past CVE-2026-2285/2286)."
        ) from exc

    if not config.guardrail_identifier:
        raise ValueError("guardrail_identifier mandatory (R-BED-028)")

    class McpQualifiedTool(BaseTool):
        name: str
        description: str
        qualified: str

        def _run(self, **kwargs):
            if self.qualified not in config.qualified_tool_names:
                raise PermissionError(f"Tool {self.qualified} not subscribed by config")
            return invoke_tool(self.qualified, kwargs)

    tools = [
        McpQualifiedTool(
            name=q.replace("___", "_"),
            description=f"MCP tool {q} (protocol {MCP_PROTOCOL_VERSION})",
            qualified=q,
        )
        for q in config.qualified_tool_names
    ]

    agent = Agent(
        role=f"{config.agent_id} agent",
        goal="Execute tasks within platform-defined guardrails.",
        backstory="Bound by Bedrock Guardrails + per-tool Cedar; never bypasses the catalogue.",
        tools=tools,
        verbose=False,
    )
    task = Task(
        description="Process the input task using only qualified MCP tools.",
        expected_output="A short result string.",
        agent=agent,
    )
    return Crew(agents=[agent], tasks=[task], verbose=False)


__all__ = ["build_crew", "MCP_PROTOCOL_VERSION"]
