"""Phase 1 Gap 1E — agent generator validator unit tests.

Live Bedrock invocations are skipped here (those happen in the live
verification step). These tests focus on the structural validator that
gates every model-generated spec before it reaches the frontend.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from app.services.agent_generator import _validate_spec  # noqa: E402


def _runtime_node(suffix="rt", name="my_agent"):
    return {
        "idSuffix": suffix,
        "type": "runtime",
        "label": "Runtime",
        "position": {"x": 500, "y": 300},
        "configuration": {
            "name": name,
            "framework": "strands_agents",
            "modelProvider": "bedrock",
            "model": {"modelId": "us.anthropic.claude-sonnet-5"},
            "systemPrompt": "You are helpful.",
            "protocol": "HTTP",
            "pythonRuntime": "PYTHON_3_13",
        },
    }


def test_minimal_runtime_only_spec_passes():
    spec = {"nodes": [_runtime_node()], "edges": []}
    assert _validate_spec(spec) is None


def test_rejects_missing_runtime():
    spec = {
        "nodes": [
            {
                "idSuffix": "mem",
                "type": "memory",
                "label": "Memory",
                "position": {"x": 0, "y": 0},
                "configuration": {"enabled": True},
            }
        ],
        "edges": [],
    }
    err = _validate_spec(spec)
    assert err is not None
    assert "runtime" in err


def test_rejects_two_runtimes():
    spec = {
        "nodes": [_runtime_node("rt1"), _runtime_node("rt2", name="other")],
        "edges": [],
    }
    err = _validate_spec(spec)
    assert err is not None
    assert "exactly one runtime" in err


def test_rejects_duplicate_id_suffixes():
    rt = _runtime_node()
    dup = dict(rt, type="memory")
    spec = {"nodes": [rt, dup], "edges": []}
    err = _validate_spec(spec)
    assert err is not None


def test_rejects_runtime_without_system_prompt():
    rt = _runtime_node()
    rt["configuration"].pop("systemPrompt")
    spec = {"nodes": [rt], "edges": []}
    err = _validate_spec(spec)
    assert err is not None
    assert "systemPrompt" in err


def test_rejects_runtime_without_name():
    rt = _runtime_node()
    rt["configuration"].pop("name")
    spec = {"nodes": [rt], "edges": []}
    err = _validate_spec(spec)
    assert err is not None
    assert "name" in err


def test_rejects_oversized_runtime_name():
    rt = _runtime_node(name="x" * 80)
    spec = {"nodes": [rt], "edges": []}
    err = _validate_spec(spec)
    assert err is not None
    assert "name" in err


def test_rejects_orphan_support_node():
    spec = {
        "nodes": [
            _runtime_node("rt"),
            {
                "idSuffix": "mem",
                "type": "memory",
                "label": "Memory",
                "position": {"x": 0, "y": 0},
                "configuration": {"enabled": True},
            },
        ],
        "edges": [],  # No edge from mem -> rt
    }
    err = _validate_spec(spec)
    assert err is not None
    assert "no edge to runtime" in err


def test_rejects_edge_referencing_unknown_suffix():
    spec = {
        "nodes": [_runtime_node("rt")],
        "edges": [
            {"sourceIdSuffix": "ghost", "targetIdSuffix": "rt", "connectionType": "data"}
        ],
    }
    err = _validate_spec(spec)
    assert err is not None
    assert "ghost" in err


def _tool_node(suffix, tool_id="duckduckgo_search", is_custom=False, input_schema=None):
    cfg = {
        "name": tool_id,
        "toolId": tool_id,
        "description": "A tool.",
        "enabled": True,
        "isCustom": is_custom,
    }
    if input_schema is not None:
        cfg["inputSchema"] = input_schema
    return {
        "idSuffix": suffix,
        "type": "tool",
        "label": tool_id,
        "position": {"x": 250, "y": 100},
        "configuration": cfg,
    }


def _gateway_node(suffix="gw"):
    return {
        "idSuffix": suffix,
        "type": "gateway",
        "label": "Gateway",
        "position": {"x": 500, "y": 100},
        "configuration": {"name": "Gateway", "tools": [], "auth": "cognito"},
    }


def test_rejects_tool_connected_directly_to_runtime():
    """The reported bug: tool -> runtime edge ('Cannot connect tool to runtime')."""
    spec = {
        "nodes": [_runtime_node("rt"), _gateway_node("gw"), _tool_node("t1")],
        "edges": [
            {"sourceIdSuffix": "gw", "targetIdSuffix": "rt", "connectionType": "data"},
            {"sourceIdSuffix": "t1", "targetIdSuffix": "rt", "connectionType": "data"},
        ],
    }
    err = _validate_spec(spec)
    assert err is not None
    assert "Cannot connect tool to runtime" in err or "directly to the runtime" in err


def test_rejects_tool_without_gateway():
    spec = {
        "nodes": [_runtime_node("rt"), _tool_node("t1")],
        "edges": [
            {"sourceIdSuffix": "t1", "targetIdSuffix": "rt", "connectionType": "data"},
        ],
    }
    err = _validate_spec(spec)
    assert err is not None
    assert "gateway" in err


def test_rejects_tool_with_no_edge_to_gateway():
    spec = {
        "nodes": [_runtime_node("rt"), _gateway_node("gw"), _tool_node("t1")],
        "edges": [
            {"sourceIdSuffix": "gw", "targetIdSuffix": "rt", "connectionType": "data"},
            # t1 is orphaned — no edge to the gateway
        ],
    }
    err = _validate_spec(spec)
    assert err is not None
    assert "t1" in err


def test_rejects_gateway_without_edge_to_runtime():
    spec = {
        "nodes": [_runtime_node("rt"), _gateway_node("gw"), _tool_node("t1")],
        "edges": [
            {"sourceIdSuffix": "t1", "targetIdSuffix": "gw", "connectionType": "data"},
            # gateway never reaches the runtime
        ],
    }
    err = _validate_spec(spec)
    assert err is not None
    # The gateway is a support node, so it must reach the runtime.
    assert "gw" in err and "runtime" in err


def test_canonical_tool_gateway_runtime_spec_passes():
    """The correct shape: tool -> gateway -> runtime."""
    spec = {
        "nodes": [
            _runtime_node("rt"),
            _gateway_node("gw"),
            _tool_node("t1", tool_id="duckduckgo_search"),
            _tool_node(
                "t2",
                tool_id="create_jira_ticket",
                is_custom=True,
                input_schema={
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
            ),
        ],
        "edges": [
            {"sourceIdSuffix": "gw", "targetIdSuffix": "rt", "connectionType": "data"},
            {"sourceIdSuffix": "t1", "targetIdSuffix": "gw", "connectionType": "data"},
            {"sourceIdSuffix": "t2", "targetIdSuffix": "gw", "connectionType": "data"},
        ],
    }
    assert _validate_spec(spec) is None


def test_full_spec_with_memory_and_guardrails_passes():
    spec = {
        "nodes": [
            _runtime_node("rt"),
            {
                "idSuffix": "mem",
                "type": "memory",
                "label": "Memory",
                "position": {"x": 250, "y": 100},
                "configuration": {"enabled": True, "name": "AgentMemory"},
            },
            {
                "idSuffix": "gr",
                "type": "guardrails",
                "label": "Guardrails",
                "position": {"x": 750, "y": 100},
                "configuration": {"enabled": True, "name": "Guardrails"},
            },
        ],
        "edges": [
            {"sourceIdSuffix": "mem", "targetIdSuffix": "rt", "connectionType": "data"},
            {"sourceIdSuffix": "gr", "targetIdSuffix": "rt", "connectionType": "control"},
        ],
    }
    assert _validate_spec(spec) is None
