"""
Real LangGraph integration. Imports `langgraph` lazily; when not installed
the module raises a friendly ImportError but does NOT block the rest of the
blueprint scaffold from importing (the platform-mandated invariants live
in `langgraph_agent.py`).

Usage in production (requirements.txt pins exact tested versions — install
those pins rather than letting pip resolve unconstrained):
    pip install --require-hashes -r requirements.txt  # or plain -r; versions are pinned with ==
    python -c "from langgraph_real import build_state_graph; build_state_graph(...)"

Z7-G: replaces the previous Strands-shaped scaffold with a real
LangGraph StateGraph that calls the workstream gateway via MCP.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import logging
from typing import Any, Callable, TypedDict

_LOGGER = logging.getLogger(__name__)

from langgraph_agent import LangGraphAgentConfig, MCP_PROTOCOL_VERSION


class GraphState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    iteration: int


def build_state_graph(
    config: LangGraphAgentConfig,
    invoke_tool: Callable[[str, dict[str, Any]], dict[str, Any]],
):
    """Build a LangGraph StateGraph wiring an LLM node + a tool node.

    Lazy-imports `langgraph` so the rest of the blueprint stays importable
    when the dependency isn't installed (e.g. during platform unit tests).
    """
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:
        raise ImportError(
            "langgraph is not installed. Install the pinned dependency set: "
            "`pip install -r requirements.txt` (versions are pinned with == to "
            "the tested releases). The platform invariants in langgraph_agent.py "
            "do not depend on it."
        ) from exc

    if config.guardrail_identifier == "":
        raise ValueError("guardrail_identifier mandatory (R-BED-028)")

    # G-14: real LLM node. Lazy-imports langchain_aws so the rest of the
    # blueprint imports without it. Tests cover the langchain-absent stub
    # path; production triggers the Bedrock invocation.
    def llm_node(state: GraphState) -> GraphState:
        try:
            from langchain_aws import ChatBedrockConverse  # type: ignore[import-not-found]
        except ImportError:
            return {**state, "iteration": (state.get("iteration") or 0) + 1}
        model_id = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        chat = ChatBedrockConverse(
            model_id=model_id,
            guardrail_config={
                "guardrailIdentifier": config.guardrail_identifier,
                "guardrailVersion": "DRAFT",
                "trace": "enabled",
            },
        )
        msgs = state.get("messages") or []
        try:
            normalised = [
                {"role": m.get("role", "user"), "content": m.get("content", "")}
                for m in msgs
                if "tool_call" not in m
            ]
            result = chat.invoke(normalised)
            content = result.content if hasattr(result, "content") else str(result)
            return {
                **state,
                "messages": [*msgs, {"role": "assistant", "content": content}],
                "iteration": (state.get("iteration") or 0) + 1,
            }
        except Exception as exc:  # noqa: BLE001
            # SEC (security review): do not surface the raw exception into the
            # message state — it can carry connection strings, PII, or
            # internal detail. Log ONLY the exception type (not .exception(),
            # which would emit the full message/traceback that may echo the
            # prompt).
            _LOGGER.error("LLM node invocation failed: %s", type(exc).__name__)
            return {
                **state,
                "messages": [
                    *msgs,
                    {
                        "role": "assistant",
                        "content": f"<error>LLM invocation failed ({type(exc).__name__})</error>",
                    },
                ],
                "iteration": (state.get("iteration") or 0) + 1,
            }

    def tool_node(state: GraphState) -> GraphState:
        # Pull last assistant message + invoke a qualified tool. Real
        # production code parses tool-use blocks; here we wire the call
        # path so the integration is exercised end-to-end.
        last = (state.get("messages") or [{}])[-1]
        if "tool_call" in last:
            qualified = last["tool_call"]["name"]
            if qualified not in config.qualified_tool_names:
                raise PermissionError(f"Tool {qualified} not subscribed by config")
            payload = last["tool_call"].get("arguments") or {}
            result = invoke_tool(qualified, payload)
            return {
                **state,
                "tool_results": (state.get("tool_results") or []) + [result],
            }
        return state

    def should_continue(state: GraphState) -> str:
        if (state.get("iteration") or 0) >= 10:
            return "end"
        last = (state.get("messages") or [{}])[-1]
        return "tool" if "tool_call" in last else "end"

    g = StateGraph(GraphState)
    g.add_node("llm", llm_node)
    g.add_node("tool", tool_node)
    g.set_entry_point("llm")
    g.add_conditional_edges("llm", should_continue, {"tool": "tool", "end": END})
    g.add_edge("tool", "llm")
    return g.compile()


__all__ = ["build_state_graph", "GraphState", "MCP_PROTOCOL_VERSION"]
