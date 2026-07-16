"""Phase 2 Gap 2D — HITL codegen injection regression tests.

Locks in the fix for Bug 125 (the injected human_approval @tool + its usage
must be module-import-safe — definitions before any module-level usage, no
forward _HITL_TOOLS reference in an Agent(...) constructor). The original
injector appended at EOF after `if __name__ == "__main__"`, leaving
_HITL_TOOLS undefined when invoke() ran — a live NameError 500.

These tests EXEC the generated module against lightweight strands /
bedrock_agentcore stubs to prove symbol resolution, exactly the failure mode
that slipped past the AST-only check the repair workflow ran.
"""

from __future__ import annotations

import sys
import types

sys.path.insert(0, "src")

import pytest

from app.models.deployment_models import RuntimeConfig
from app.services.code_generator import generate_agent_code


def _cfg():
    return RuntimeConfig(
        name="hitl_t",
        model={"modelId": "us.anthropic.claude-sonnet-5"},
        systemPrompt="You approve sensitive actions.",
        modelProvider="bedrock",
    )


def _install_strands_stubs():
    """Install minimal strands + bedrock_agentcore stubs so a generated
    agent module can be exec'd to verify symbol resolution."""
    strands = types.ModuleType("strands")

    class Agent:
        def __init__(self, *a, **k):
            self.kwargs = k

        def __call__(self, *a, **k):
            return "stub-response"

    def tool(f=None, **k):
        return f if f else (lambda g: g)

    strands.Agent = Agent
    strands.tool = tool

    smodels = types.ModuleType("strands.models")

    class BedrockModel:
        def __init__(self, *a, **k):
            pass

    smodels.BedrockModel = BedrockModel
    strands.models = smodels

    # strands.models.bedrock (used by some templates)
    sbedrock = types.ModuleType("strands.models.bedrock")
    sbedrock.BedrockModel = BedrockModel
    smodels.bedrock = sbedrock

    bac = types.ModuleType("bedrock_agentcore")
    bacr = types.ModuleType("bedrock_agentcore.runtime")

    class App:
        def entrypoint(self, f):
            return f

        def run(self):
            pass

    bacr.BedrockAgentCoreApp = App
    bac.runtime = bacr

    mods = {
        "strands": strands,
        "strands.models": smodels,
        "strands.models.bedrock": sbedrock,
        "bedrock_agentcore": bac,
        "bedrock_agentcore.runtime": bacr,
    }
    for n, m in mods.items():
        sys.modules[n] = m


@pytest.mark.parametrize("tools", [["hitl"], ["hitl", "memory"]])
def test_hitl_tool_injected_and_module_imports(tools):
    code = generate_agent_code(config=_cfg(), tools=tools, observability_enabled=False)
    # The human_approval @tool must be present (the headline-use-case fix).
    assert "def human_approval" in code, f"HITL tool missing for tools={tools}"
    # It must be inlined into an Agent(...) constructor, not a forward ref.
    assert "_HITL_TOOLS" not in _agent_constructor_args(code), (
        "Agent(...) must inline human_approval, never reference forward _HITL_TOOLS"
    )
    # Exec the module against stubs — this is what catches the Bug-125 NameError.
    _install_strands_stubs()
    g: dict = {"__name__": "agent_under_test"}
    exec(compile(code, "<agent.py>", "exec"), g)
    assert callable(g.get("human_approval")), "human_approval not defined at module scope"


def test_hitl_only_canvas_routes_through_injection():
    """A canvas with ONLY hitl (no browser/kb/memory/gateway) is the headline
    use case the original 2D silently no-op'd. Confirm it gets the tool."""
    code = generate_agent_code(config=_cfg(), tools=["hitl"], observability_enabled=False)
    assert "def human_approval" in code
    assert "human_approval" in _agent_constructor_args(code)


def test_no_hitl_no_injection():
    code = generate_agent_code(config=_cfg(), tools=[], observability_enabled=False)
    assert "def human_approval" not in code


def _agent_constructor_args(code: str) -> str:
    """Return the concatenated args of every Agent(...) call (paren-balanced)."""
    import re

    out = []
    for m in re.finditer(r"\bAgent\(", code):
        start = m.end()
        depth = 1
        j = start
        while j < len(code) and depth:
            if code[j] == "(":
                depth += 1
            elif code[j] == ")":
                depth -= 1
            j += 1
        out.append(code[start : j - 1])
    return " ".join(out)
