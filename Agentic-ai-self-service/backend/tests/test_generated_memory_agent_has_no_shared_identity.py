"""F-56: a generated memory agent must not invent a shared Memory identity.

Both memory entrypoints used to fall back to ``session_id="default"`` and
``actor_id="user"``. The platform's own invoke routes now always send both, but
the runtime is also reachable directly (``InvokeAgentRuntime`` from the customer's
own IAM principal, or an exported template), and every such caller that omitted
them read and wrote ONE memory stream. The fix rejects an incomplete identity
before either Memory or the model is called; silently answering statelessly
would merely turn the security bug into a hidden feature downgrade.

These tests lift the real ``invoke`` out of the generated source and run it, so
they measure the shipped text rather than a grep of it.
"""

from __future__ import annotations

import ast

import pytest
from app.models.components import RuntimeConfiguration
from app.services import codegen_templates
from app.services.code_generator import _generate_memory_agent
from app.services.deployment import generate_unified_agent_code

SESSION = "0123456789abcdef0123456789abcdef-0123456789abcdef0123456789abcdef"
ACTOR = "0123456789abcdef0123456789abcdef"


class _App:
    @staticmethod
    def entrypoint(fn):
        return fn


def _lift_invoke(source: str, namespace: dict):
    tree = ast.parse(source)  # the whole module must compile, not just the function
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "invoke")
    # invoke reports its tool calls through the receipt helpers spliced in beside it.
    exec(compile(codegen_templates.load_impl("tool_receipts"), "<tool_receipts>", "exec"), namespace)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<generated>", "exec"), namespace)
    return namespace["invoke"]


def _memory_agent_invoke():
    calls: list[tuple] = []
    model_calls: list[str] = []

    def _model(prompt):
        model_calls.append(prompt)
        return "ok"

    namespace = {
        "app": _App,
        "MEMORY_ID": "memory-AbCdEf1234",
        "_get_recent_context": lambda *a: calls.append(("read_recent", *a)) or "",
        "_get_long_term_context": lambda *a: calls.append(("read_long_term", *a[:2])) or "",
        "_save_to_memory": lambda *a: calls.append(("write", *a[:2])),
        "_get_agent": lambda: _model,
    }
    source = _generate_memory_agent("You are helpful.", "us.anthropic.claude-sonnet-5", "us-east-1")
    return _lift_invoke(source, namespace), calls, model_calls


def _unified_agent_invoke():
    configs: list[dict] = []
    model_calls: list[str] = []

    class _Config:
        def __init__(self, **kwargs):
            configs.append(kwargs)

    def _model(prompt):
        model_calls.append(prompt)
        return "ok"

    namespace = {
        "app": _App,
        "MEMORY_ID": "memory-AbCdEf1234",
        "REGION": "us-east-1",
        "SYSTEM_PROMPT": "p",
        "AgentCoreMemoryConfig": _Config,
        "AgentCoreMemorySessionManager": lambda config, region_name: ("manager", config),
        "Agent": lambda **kwargs: _model,
        "_get_model": lambda: None,
    }
    config = RuntimeConfiguration(name="mem", model={"model_id": "us.anthropic.claude-sonnet-5"}, system_prompt="p")
    source = generate_unified_agent_code(
        config, connected_tools=["memory"], memory_id="memory-AbCdEf1234", region="us-east-1"
    )
    return _lift_invoke(source, namespace), configs, model_calls


@pytest.mark.parametrize(
    "payload",
    [{"prompt": "hi"}, {"prompt": "hi", "session_id": SESSION}, {"prompt": "hi", "actor_id": ACTOR}],
    ids=["neither", "session-only", "actor-only"],
)
def test_memory_agent_without_both_ids_touches_no_memory(payload):
    invoke, calls, model_calls = _memory_agent_invoke()

    with pytest.raises(ValueError, match="both session_id and actor_id"):
        invoke(payload)
    assert calls == []
    assert model_calls == []


def test_memory_agent_with_both_ids_reads_and_writes_that_identity_only():
    invoke, calls, model_calls = _memory_agent_invoke()

    invoke({"prompt": "hi", "session_id": SESSION, "actor_id": ACTOR})

    assert calls == [
        ("read_recent", ACTOR, SESSION),
        ("read_long_term", ACTOR, SESSION),
        ("write", ACTOR, SESSION),
    ]
    assert model_calls == ["hi"]


@pytest.mark.parametrize(
    "payload",
    [{"prompt": "hi"}, {"prompt": "hi", "session_id": SESSION}, {"prompt": "hi", "actor_id": ACTOR}],
    ids=["neither", "session-only", "actor-only"],
)
def test_unified_agent_without_both_ids_builds_no_session_manager(payload):
    invoke, configs, model_calls = _unified_agent_invoke()

    with pytest.raises(ValueError, match="both session_id and actor_id"):
        invoke(payload)

    assert configs == []
    assert model_calls == []


def test_unified_agent_with_both_ids_binds_memory_to_that_identity():
    invoke, configs, model_calls = _unified_agent_invoke()

    result = invoke({"prompt": "hi", "session_id": SESSION, "actor_id": ACTOR})

    assert configs == [{"memory_id": "memory-AbCdEf1234", "session_id": SESSION, "actor_id": ACTOR}]
    assert result["session_id"] == SESSION
    assert model_calls == ["hi"]


# --- The documented invoke must be one the agent accepts ------------------------------
#
# Refusing an incomplete identity makes every README that says ``{"prompt": "hello"}`` a
# set of instructions that fails. These run the README's own command -- the CFN one
# through bash with ``aws`` stubbed, so the shell quoting is measured too -- and hand the
# payload it would send to the exported agent's own ``invoke``.

import json  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import subprocess  # noqa: E402

from app.models.deployment_models import DeployRequest, RuntimeConfig  # noqa: E402
from app.services.cfn_template_generator import CfnTemplateGenerator  # noqa: E402
from app.services.python_exporter import build_python_project  # noqa: E402

_STUB_AWS = """#!/bin/bash
if [ "$1" = cloudformation ]; then echo stub-output; exit 0; fi
printf '%s\\0' "$@" > "$ARGS_OUT"
"""


def _documented_cfn_invoke(tmp_path, readme: str) -> tuple[str, dict]:
    block = next(
        b for b in re.findall(r"```bash\n(.*?)```", readme, re.S) if "invoke-agent-runtime" in b and "RUNTIME_ARN=" in b
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(_STUB_AWS)
    (bin_dir / "uuidgen").write_text("#!/bin/bash\necho 3F2504E0-4F89-41D3-9A0C-0305E82C3301\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    args_out = tmp_path / "args"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "ARGS_OUT": str(args_out), "HOME": str(tmp_path)}
    subprocess.run(["bash", "-euc", block.replace("cat response.json", "")], cwd=tmp_path, env=env, check=True)
    argv = args_out.read_bytes().split(b"\0")[:-1]
    args = [a.decode() for a in argv]
    return args[args.index("--runtime-session-id") + 1], json.loads(args[args.index("--payload") + 1])


def _stubbed_namespace(model_calls: list) -> dict:
    """Enough of either memory generator's module globals for ``invoke`` to run."""

    def _model(prompt):
        model_calls.append(prompt)
        return "ok"

    return {
        "app": _App,
        "MEMORY_ID": "memory-AbCdEf1234",
        "REGION": "us-east-1",
        "SYSTEM_PROMPT": "p",
        "AgentCoreMemoryConfig": lambda **kwargs: kwargs,
        "AgentCoreMemorySessionManager": lambda config, region_name: config,
        "Agent": lambda **kwargs: _model,
        "_get_model": lambda: None,
        "_get_recent_context": lambda *a: "",
        "_get_long_term_context": lambda *a: "",
        "_save_to_memory": lambda *a: None,
        "_get_agent": lambda: _model,
    }


def _cfn_bundle(memory: bool):
    kwargs = {"memory_config": {"enabled": True}} if memory else {}
    return CfnTemplateGenerator().generate(
        DeployRequest(
            config=RuntimeConfig(name="readmemem", model={"modelId": "us.anthropic.claude-sonnet-5"}),
            nodeId="node-1",
            **kwargs,
        )
    )


def test_cfn_readme_invoke_for_a_memory_agent_is_accepted_by_that_agent(tmp_path):
    bundle = _cfn_bundle(memory=True)
    runtime_session, payload = _documented_cfn_invoke(tmp_path, bundle.readme)

    assert payload["session_id"] == runtime_session
    assert 33 <= len(payload["session_id"]) <= 100
    assert payload["actor_id"]

    model_calls: list[str] = []
    invoke = _lift_invoke(bundle.agent_code, _stubbed_namespace(model_calls))
    with pytest.raises(ValueError):  # it IS a Memory agent: the old documented call is refused
        invoke({"prompt": "hello"})
    invoke(payload)
    assert model_calls, "the documented invoke never reached the model"


def test_cfn_readme_invoke_without_memory_is_unchanged(tmp_path):
    _, payload = _documented_cfn_invoke(tmp_path, _cfn_bundle(memory=False).readme)

    assert payload == {"prompt": "hello"}


def _documented_local_invoke(readme: str) -> dict:
    block = next(b for b in re.findall(r"```bash\n(.*?)```", readme, re.S) if "localhost:8080/invocations" in b)
    argv = shlex.split(block)
    return json.loads(argv[argv.index("-d") + 1])


def test_python_export_readme_invoke_for_a_memory_agent_is_accepted_by_that_agent():
    project = build_python_project(
        DeployRequest(
            config=RuntimeConfig(name="readmemem", model={"modelId": "us.anthropic.claude-sonnet-5"}),
            nodeId="node-1",
            memory_config={"enabled": True, "memoryId": "memory-AbCdEf1234"},
        )
    )
    payload = _documented_local_invoke(project["README.md"])

    assert 33 <= len(payload["session_id"]) <= 100
    assert payload["actor_id"]
    model_calls: list[str] = []
    invoke = _lift_invoke(project["agent.py"], _stubbed_namespace(model_calls))
    with pytest.raises(ValueError):  # it IS a Memory agent: the old documented call is refused
        invoke({"prompt": "hello"})
    invoke(payload)
    assert model_calls, "the documented invoke never reached the model"


def test_python_export_readme_invoke_without_memory_is_unchanged():
    project = build_python_project(
        DeployRequest(
            config=RuntimeConfig(name="plain", model={"modelId": "us.anthropic.claude-sonnet-5"}), nodeId="node-1"
        )
    )

    assert _documented_local_invoke(project["README.md"]) == {"prompt": "hello"}
