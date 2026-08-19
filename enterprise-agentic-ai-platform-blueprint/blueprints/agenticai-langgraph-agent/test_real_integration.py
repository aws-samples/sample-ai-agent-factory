"""
Real LangGraph integration test. Skips when langgraph is not installed —
the test ships in the repo so customers running `pip install -r
requirements.txt` get coverage for free.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest

pytest.importorskip("langgraph", reason="langgraph not installed; run pip install -r requirements.txt to enable")

from langgraph_agent import LangGraphAgentConfig  # type: ignore[import-not-found]
from langgraph_real import build_state_graph  # type: ignore[import-not-found]


def test_state_graph_compiles_and_runs_through_a_tool_call():
    cfg = LangGraphAgentConfig(
        tenant_id="demo",
        agent_id="primary",
        env_name="prod",
        inference_profile_arn="arn",
        guardrail_identifier="gd",
        qualified_tool_names=("target-demo___tool-echo",),
    )
    invocations: list[tuple[str, dict]] = []

    def invoke_tool(qualified, payload):
        invocations.append((qualified, payload))
        return {"ok": True}

    graph = build_state_graph(cfg, invoke_tool)
    out = graph.invoke({
        "messages": [
            {"role": "user", "content": "echo hi"},
            {"role": "assistant", "tool_call": {"name": "target-demo___tool-echo", "arguments": {"message": "hi"}}},
        ],
    })
    assert (out.get("tool_results") or []) == [{"ok": True}]
    assert invocations == [("target-demo___tool-echo", {"message": "hi"})]


def test_state_graph_rejects_unsubscribed_tool():
    cfg = LangGraphAgentConfig(
        tenant_id="demo",
        agent_id="primary",
        env_name="prod",
        inference_profile_arn="arn",
        guardrail_identifier="gd",
        qualified_tool_names=("target-demo___tool-echo",),
    )

    def invoke_tool(*_a, **_k):  # pragma: no cover
        raise AssertionError("tool was invoked despite not being subscribed")

    graph = build_state_graph(cfg, invoke_tool)
    with pytest.raises(PermissionError):
        graph.invoke({
            "messages": [
                {"role": "assistant", "tool_call": {"name": "target-demo___tool-other", "arguments": {}}},
            ],
        })
