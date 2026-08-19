"""Unit tests for the LangGraph blueprint.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest

from langgraph_agent import (  # type: ignore[import-not-found]
    LangGraphAgent,
    LangGraphAgentConfig,
    MCP_PROTOCOL_VERSION,
)


class _FakeGateway:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call_tool(self, qualified_tool_name, payload, *, protocol_version):
        self.calls.append(
            {"name": qualified_tool_name, "payload": payload, "version": protocol_version}
        )
        return {"ok": True}


def _cfg(**overrides) -> LangGraphAgentConfig:
    return LangGraphAgentConfig(
        tenant_id="demo",
        agent_id="primary",
        env_name="prod",
        inference_profile_arn="arn",
        guardrail_identifier="gd",
        qualified_tool_names=("target-demo___tool-echo",),
        **overrides,
    )


def test_rejects_missing_guardrail():
    with pytest.raises(ValueError):
        LangGraphAgentConfig(
            tenant_id="t",
            agent_id="a",
            env_name="e",
            inference_profile_arn="arn",
            guardrail_identifier="",
        )


def test_invoke_known_tool_passes_protocol_version():
    gw = _FakeGateway()
    agent = LangGraphAgent(_cfg(), gw)
    res = agent.invoke_tool("target-demo___tool-echo", {"message": "hi"})
    assert res == {"ok": True}
    assert gw.calls == [
        {
            "name": "target-demo___tool-echo",
            "payload": {"message": "hi"},
            "version": MCP_PROTOCOL_VERSION,
        }
    ]


def test_invoke_unknown_tool_rejected():
    gw = _FakeGateway()
    agent = LangGraphAgent(_cfg(), gw)
    with pytest.raises(PermissionError):
        agent.invoke_tool("target-demo___tool-ping", {})


def test_protocol_version_locked_to_2025_06_18():
    assert MCP_PROTOCOL_VERSION == "2025-06-18"
