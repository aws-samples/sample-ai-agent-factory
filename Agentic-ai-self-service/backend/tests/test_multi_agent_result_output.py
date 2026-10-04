"""Regression tests for user-facing Graph and Swarm responses.

Strands' GraphResult and SwarmResult are dataclasses without ``__str__``.
Returning ``str(result)`` therefore exposes orchestration bookkeeping instead
of the final assistant answer. These tests execute the helper from the emitted
agent source and pin its wiring into both multi-agent templates.
"""

import ast
from types import SimpleNamespace

from app.services.code_generator import _generate_graph_agent, _generate_swarm_agent


def _agent_result(text: str):
    return SimpleNamespace(message={"content": [{"text": text}]})


def _node_result(value):
    return SimpleNamespace(result=value)


def _generated_source(kind: str) -> str:
    config = {
        "agents": [
            {"agentId": "researcher", "systemPrompt": "Research."},
            {"agentId": "writer", "systemPrompt": "Write."},
        ],
        "edges": [{"source": "researcher", "target": "writer"}],
        "entryPoint": "researcher",
    }
    generator = _generate_graph_agent if kind == "graph" else _generate_swarm_agent
    return generator(
        system_prompt="Coordinate the answer.",
        model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
        region="us-east-1",
        provider="bedrock",
        multi_agent_config=config,
    )


def _helper_from(source: str):
    """Compile only the helper from the exact generated runtime source."""
    tree = ast.parse(source)
    helper = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_multi_agent_final_text"
    )
    module = ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[]))
    namespace: dict = {}
    exec(compile(module, "<generated-agent>", "exec"), namespace)
    return namespace["_multi_agent_final_text"]


def test_graph_returns_last_executed_agents_text_not_dataclass_repr():
    source = _generated_source("graph")
    render = _helper_from(source)
    graph_result = SimpleNamespace(
        results={
            "researcher": _node_result(_agent_result("intermediate research")),
            "writer": _node_result(_agent_result("final customer answer")),
        },
        execution_order=[
            SimpleNamespace(node_id="researcher"),
            SimpleNamespace(node_id="writer"),
        ],
    )

    assert render(graph_result) == "final customer answer"
    assert 'return {"response": _multi_agent_final_text(result)}' in source
    assert 'return {"response": str(result)}' not in source


def test_swarm_uses_last_text_bearing_handoff_and_unwraps_nested_results():
    source = _generated_source("swarm")
    render = _helper_from(source)
    nested = SimpleNamespace(
        results={"writer": _node_result(_agent_result("nested final answer"))},
        node_history=[SimpleNamespace(node_id="writer")],
    )
    swarm_result = SimpleNamespace(
        results={
            "researcher": _node_result(_agent_result("first answer")),
            "writer": _node_result(nested),
            "failed": _node_result(RuntimeError("credential=must-not-leak")),
        },
        node_history=[
            SimpleNamespace(node_id="researcher"),
            SimpleNamespace(node_id="writer"),
            SimpleNamespace(node_id="failed"),
            # A swarm can hand control back to an earlier node. Its last
            # appearance must decide the answer, not its first appearance.
            SimpleNamespace(node_id="writer"),
        ],
    )

    assert render(swarm_result) == "nested final answer"
    assert 'return {"response": _multi_agent_final_text(result)}' in source
    assert 'return {"response": str(result)}' not in source


def test_multi_agent_text_fallback_never_stringifies_exception_or_object_repr():
    render = _helper_from(_generated_source("graph"))
    opaque = SimpleNamespace(
        results={"failed": _node_result(RuntimeError("Bearer secret-token"))},
        execution_order=[SimpleNamespace(node_id="failed")],
    )

    output = render(opaque)

    assert output == "Multi-agent execution completed without a text response."
    assert "secret-token" not in output
    assert "namespace(" not in output
