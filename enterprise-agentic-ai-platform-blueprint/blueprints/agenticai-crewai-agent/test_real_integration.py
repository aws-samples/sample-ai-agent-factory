"""
Real CrewAI integration test. Skips when crewai isn't installed.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest

pytest.importorskip("crewai", reason="crewai not installed; run pip install -r requirements.txt to enable")

from crewai_agent import CrewAIAgentConfig  # type: ignore[import-not-found]
from crewai_real import build_crew  # type: ignore[import-not-found]


def test_crew_constructs_with_qualified_tools():
    cfg = CrewAIAgentConfig(
        tenant_id="demo",
        agent_id="primary",
        env_name="prod",
        inference_profile_arn="arn",
        guardrail_identifier="gd",
        qualified_tool_names=("target-demo___tool-echo", "target-demo___tool-ping"),
    )

    invocations: list[tuple[str, dict]] = []

    def invoke_tool(q, p):
        invocations.append((q, p))
        return {"ok": True}

    crew = build_crew(cfg, invoke_tool)
    # CrewAI crews carry agents + tasks; assert the agent has 2 tools.
    assert len(crew.agents) == 1
    assert len(crew.agents[0].tools) == 2
