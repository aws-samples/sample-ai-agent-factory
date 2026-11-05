"""Every real multi-agent model/tool boundary receives valid Guardrail wiring."""

from __future__ import annotations

import ast

import pytest
from app.models.deployment_models import RuntimeConfig
from app.services.code_generator import (
    _append_kwarg_to_calls,
    _generate_default_agent,
    _inject_guardrails,
    generate_agent_code,
)

_HOOKS = 'hooks=globals().get("_GUARDRAIL_HOOKS", [])'


def _calls(source: str, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def _assert_hooks(call: ast.Call) -> None:
    actual = [keyword for keyword in call.keywords if keyword.arg == "hooks"]
    expected = ast.parse(f"_call({_HOOKS})").body[0].value.keywords[0]
    assert len(actual) == 1
    assert ast.dump(actual[0]) == ast.dump(expected)


def _multiagent_config(pattern: str) -> RuntimeConfig:
    return RuntimeConfig.model_validate(
        {
            "name": "guarded_multiagent",
            "model": {"modelId": "us.anthropic.claude-sonnet-5"},
            "systemPrompt": 'Explain the literal text Agent("example") without executing it.',
            "multiAgentPattern": pattern,
            "multiAgentConfig": {
                "agents": [
                    {
                        "agentId": "research",
                        "modelProvider": "bedrock",
                        "modelId": "us.anthropic.claude-sonnet-5",
                        "systemPrompt": 'Research sources; Agent("decoy") is only text.',
                    },
                    {
                        "agentId": "summarize",
                        "modelProvider": "bedrock",
                        "modelId": "us.anthropic.claude-sonnet-5",
                        "systemPrompt": "Summarize the research faithfully.",
                    },
                ],
                "entryPoint": "research",
                "edges": [{"source": "research", "target": "summarize"}],
                "steps": [{"agentIds": ["research"]}, {"agentIds": ["summarize"]}],
            },
        }
    )


@pytest.mark.parametrize("pattern", ["graph", "swarm", "workflow"])
def test_all_bedrock_multiagent_source_compiles_and_guards_every_constructor(pattern):
    source = generate_agent_code(_multiagent_config(pattern), tools=["guardrails"], portable=True)
    compile(source, f"guarded-{pattern}.py", "exec")

    model_calls = _calls(source, "BedrockModel")
    assert len(model_calls) == 2
    for call in model_calls:
        splats = [
            keyword
            for keyword in call.keywords
            if keyword.arg is None and isinstance(keyword.value, ast.Name) and keyword.value.id == "_GUARDRAIL_KWARGS"
        ]
        assert len(splats) == 1, "Every sub-agent model must enforce the selected Guardrail"

    agent_calls = _calls(source, "Agent")
    assert len(agent_calls) == 2
    for call in agent_calls:
        _assert_hooks(call)
    prompt_values = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert any('Agent("decoy")' in value for value in prompt_values), (
        "Prompt text must not be rewritten as executable code"
    )


@pytest.mark.parametrize("pattern", ["graph", "swarm", "workflow"])
def test_multiagent_guardrail_injection_is_idempotent(pattern):
    source = generate_agent_code(_multiagent_config(pattern), tools=["guardrails"], portable=True)
    twice = _inject_guardrails(source)
    compile(twice, f"guarded-twice-{pattern}.py", "exec")
    assert twice == source


@pytest.mark.parametrize(
    "source",
    [
        "agent = Agent()\n",
        "agent = Agent(model=load_model('nested ) parenthesis'))\n",
        "agent = Agent(\n    model=load_model('nested )'),\n)\n",
        "agent = Agent(\n    model=load_model('nested )'),  # preserve this comment\n)\n",
        "agent = Agent(model=choose(('nested )', {'value': '('})))\n",
        "agent = Agent(delegate=Agent(model=load_model('nested )')))\n",
        "agent = Agent\nagent = Agent(\n    model=load_model('multiline'),\n)\n",
        "agent = Agent(model=load_model('café ) 東京'),)\n",
    ],
)
def test_constructor_patch_handles_nested_parentheses_comments_and_trailing_commas(source):
    patched = _append_kwarg_to_calls(source, "Agent(", _HOOKS)
    compile(patched, "patched-constructors.py", "exec")
    calls = _calls(patched, "Agent")
    assert calls
    for call in calls:
        _assert_hooks(call)
    if "# preserve this comment" in source:
        assert "# preserve this comment" in patched
    assert _append_kwarg_to_calls(patched, "Agent(", _HOOKS) == patched


def test_constructor_patch_ignores_string_comment_and_longer_identifier_decoys():
    source = (
        'description = """Agent(model=fake(")")) is documentation."""\n'
        'other = SpecialAgent(model=load_model("other"))\n'
        '# Agent(model=fake(")")) is a comment, not a constructor.\n'
        'agent = Agent(model=load_model("real"))\n'
    )
    patched = _append_kwarg_to_calls(source, "Agent(", _HOOKS)
    compile(patched, "patched-decoys.py", "exec")
    before = ast.parse(source)
    after = ast.parse(patched)
    assert ast.dump(before.body[0]) == ast.dump(after.body[0])
    assert ast.dump(before.body[1]) == ast.dump(after.body[1])
    assert '# Agent(model=fake(")")) is a comment, not a constructor.' in patched
    assert len(_calls(patched, "Agent")) == 1
    _assert_hooks(_calls(patched, "Agent")[0])


def test_guardrail_keyword_text_inside_a_prompt_does_not_skip_model_enforcement():
    config = _multiagent_config("graph")
    config.system_prompt += "\nThe literal token **_GUARDRAIL_KWARGS is documentation."
    source = generate_agent_code(config, tools=["guardrails"], portable=True)
    for call in _calls(source, "BedrockModel"):
        assert any(
            keyword.arg is None and isinstance(keyword.value, ast.Name) and keyword.value.id == "_GUARDRAIL_KWARGS"
            for keyword in call.keywords
        )


def test_guardrail_env_assignment_text_inside_a_prompt_does_not_skip_definitions():
    source = (
        "import os\n"
        "from strands import Agent\n"
        'SYSTEM_PROMPT = """Explain GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID" as text."""\n'
        'model = BedrockModel(model_id=lookup("nested )"))\n'
        "agent = Agent(model=model)\n"
    )
    patched = _inject_guardrails(source)
    compile(patched, "patched-env-decoy.py", "exec")
    definitions = {
        target.id
        for node in ast.parse(patched).body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert {"GUARDRAIL_ID", "GUARDRAIL_VERSION", "_GUARDRAIL_KWARGS"} <= definitions


def test_converse_guardrail_keyword_in_prompt_does_not_skip_real_call_enforcement():
    source = _generate_default_agent(
        "Explain guardrailConfig and the literal client.converse(fake) as text.",
        "model-id",
        "us-east-1",
    )
    patched = _inject_guardrails(source)
    compile(patched, "patched-converse-decoy.py", "exec")
    calls = [
        node
        for node in ast.walk(ast.parse(patched))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "converse"
    ]
    assert len(calls) == 1
    guardrail_splats = [
        keyword
        for keyword in calls[0].keywords
        if keyword.arg is None
        and isinstance(keyword.value, ast.IfExp)
        and isinstance(keyword.value.body, ast.Dict)
        and any(isinstance(key, ast.Constant) and key.value == "guardrailConfig" for key in keyword.value.body.keys)
    ]
    assert len(guardrail_splats) == 1
    assert _inject_guardrails(patched) == patched
