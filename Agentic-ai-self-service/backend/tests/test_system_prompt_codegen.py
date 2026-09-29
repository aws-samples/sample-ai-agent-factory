"""The system prompt is free-text, and it is interpolated into Python source.

Six paths emit it. Three write a module-level ``SYSTEM_PROMPT = \"\"\"<canvas text>\"\"\"``
-- the live Step Functions path (``code_generator.generate_agent_code``), the
unified-component path (``deployment.generate_unified_agent_code``) and the customer
CloudFormation export (``CfnTemplateGenerator``, which routes through the first). Three
more write one ``system_prompt=\"\"\"...\"\"\"`` per canvas node: the graph, swarm and
workflow multi-agent templates. All six shared one sanitizer, so they shared its two
defects:

* A prompt *ending* in a quote closed the literal one character early and ``agent.py``
  failed to import -- a deployed runtime dead on arrival, for one stray quote.
* Curly braces were doubled, so ``Return JSON like {"id": 1}`` reached the model as
  ``Return JSON like {{"id": 1}}``. Silent, and the commonest prompt shape there is.

Neither was caught by the existing property tests in ``test_preservation.py`` and
``test_comprehensive_preservation.py``, because their ``_safe_text`` strategy blacklists
``\\``, ``"``, ``'``, ``{``, ``}`` and a backtick -- precisely the characters that break
it. A test asserting a property "for ANY safe system prompt" over an alphabet chosen to
exclude every hazard proves nothing about the hazard, so these tests use a fixed hostile
table instead and assert against real generator output.

The tool-description equivalent of this file is ``test_mcp_server_codegen.py``; both
sanitizers now delegate to ``_as_triple_quoted_body`` so they cannot drift apart.
"""

from __future__ import annotations

import ast

import pytest
from app.models.components import RuntimeConfiguration
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services import code_generator
from app.services.cfn_template_generator import CfnTemplateGenerator
from app.services.code_generator import generate_agent_code
from app.services.deployment import generate_unified_agent_code

MODEL_ID = "us.anthropic.claude-sonnet-5"

# Every one of these is something a person would plausibly type into a prompt box.
HOSTILE_PROMPTS = [
    'Answer only about "orders"',  # trailing quote -- closed the literal early
    'Ends with a quote"',
    'Say """hello""" loudly',  # an embedded triple quote
    'four """" quotes',  # more quotes in a row than the escape used to handle
    "Use the path C:\\temp\\",  # trailing backslash escapes the closing quote
    'Both \\ and " together',
    'Return JSON like {"id": 1}',  # braces used to be doubled in the emitted source
    "Braces {} matter",
    "Line one\nLine two",  # a real newline is legal here and must survive
    'A "quoted" phrase mid-sentence',
]


def _emitted_prompt(code: str) -> str | None:
    """The value ``SYSTEM_PROMPT`` actually binds to, not the source text.

    Compiling proves the module loads; it does not prove the prompt survived. So read
    the literal back with ``literal_eval``, which resolves the escapes exactly as the
    interpreter would -- that string is what the model is steered by.
    """
    for node in ast.parse(code).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "SYSTEM_PROMPT":
            return ast.literal_eval(node.value)
    return None


def _live_codegen(prompt: str) -> str:
    return generate_agent_code(RuntimeConfig(name="spt", model={"modelId": MODEL_ID}, systemPrompt=prompt))


def _unified_codegen(prompt: str) -> str:
    config = RuntimeConfiguration(name="spt", model={"model_id": MODEL_ID}, system_prompt=prompt)
    return generate_unified_agent_code(config, connected_tools=[])


def _cfn_export(prompt: str) -> str:
    request = DeployRequest(
        config=RuntimeConfig(name="spt", model={"modelId": MODEL_ID}, systemPrompt=prompt),
        nodeId="node-1",
    )
    return CfnTemplateGenerator().generate(request).agent_code


# Named so a failure says which generator broke rather than which index did.
GENERATORS = pytest.mark.parametrize(
    "generate",
    [_live_codegen, _unified_codegen, _cfn_export],
    ids=["live-codegen", "unified-codegen", "cfn-export"],
)

# The multi-agent templates escape a *per-agent* ``systemPrompt`` through the same
# helper, so they had the same two defects. They are checked separately because the
# prompt lands as a ``system_prompt=`` keyword argument rather than a module-level
# ``SYSTEM_PROMPT``, which means reading it back needs a different route.
MULTI_AGENT_GENERATORS = pytest.mark.parametrize(
    "generator_name",
    ["_generate_graph_agent", "_generate_swarm_agent", "_generate_workflow_agent"],
)


@MULTI_AGENT_GENERATORS
@pytest.mark.parametrize("prompt", HOSTILE_PROMPTS)
def test_a_per_agent_prompt_survives_the_multi_agent_templates(generator_name, prompt):
    """Graph, swarm and workflow each emit one prompt per agent in the canvas.

    A single hostile prompt on one node of a graph used to break the module for every
    node, since they share the emitted file.
    """
    generate = getattr(code_generator, generator_name)
    config = {
        "agents": [{"agentId": "a1", "systemPrompt": prompt, "modelId": MODEL_ID}],
        "edges": [],
        "nodes": [],
    }
    code = generate(code_generator._as_triple_quoted_body("Top level."), MODEL_ID, "us-east-1", "bedrock", config)
    compile(code, "<multi-agent>", "exec")
    # A keyword argument, so look for the resolved value among the module's constants
    # rather than at a known assignment.
    constants = {
        node.value
        for node in ast.walk(ast.parse(code))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert prompt in constants, f"the per-agent prompt did not survive {generator_name}"


@GENERATORS
@pytest.mark.parametrize("prompt", HOSTILE_PROMPTS)
def test_a_prompt_cannot_break_the_generated_agent(generate, prompt):
    """The whole module, because a broken literal is not a degraded prompt.

    ``agent.py`` either imports or it does not; there is no partial outcome where the
    agent runs with a slightly wrong prompt. So this compiles the emitted module rather
    than inspecting the one line that changed.
    """
    compile(generate(prompt), "<agent>", "exec")


@GENERATORS
@pytest.mark.parametrize("prompt", HOSTILE_PROMPTS)
def test_the_prompt_reaches_the_model_unchanged(generate, prompt):
    """Escaping that compiles but alters the text traded a crash for a lie.

    This is the assertion the brace doubling failed, and the reason
    ``test_a_prompt_cannot_break_the_generated_agent`` above is not sufficient on its
    own: the doubled-brace source was valid Python the whole time it was wrong.
    """
    assert _emitted_prompt(generate(prompt)) == prompt


@GENERATORS
def test_an_ordinary_prompt_is_emitted_verbatim(generate):
    """The precondition that makes the tests above mean something.

    If the helper were over-eager -- escaping something it need not -- every assertion
    above could still pass while ordinary prompts arrived full of backslashes.
    """
    ordinary = "You are a helpful assistant. Be concise."
    code = generate(ordinary)
    assert _emitted_prompt(code) == ordinary
    assert ordinary in code, "no escaping should be visible in the source for plain text"
