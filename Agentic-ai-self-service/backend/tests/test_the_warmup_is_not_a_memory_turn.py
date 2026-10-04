"""F-56: the deploy-time warmup must not be stored as a Memory turn.

The UI pings every new runtime (``POST /api/test-runtime`` with ``input: "ping"``) so the
first real chat does not pay the cold start. With the tenant-bound identity in place that
ping is a fully valid Memory invocation: on acfe2e-p0920 (2026-09-22) a request shaped
like it stored ``ping`` plus the model's reply as the owner's first event. The UI had
avoided that only by never warming a Memory runtime at all. The ping now carries an
explicit ``warmup`` marker and every runtime is warmed; both generated Memory
entrypoints return on the marker before Memory and the model, but only after the
MEMORY_ID check, so a misconfigured runtime still fails its warmup loudly.
"""

from __future__ import annotations

import ast
import json

import pytest
from app import deployment_handler as dh
from app.models.components import RuntimeConfiguration
from app.services import codegen_templates, step_clients
from app.services.code_generator import _generate_memory_agent
from app.services.deployment import generate_unified_agent_code
from fastapi.testclient import TestClient

OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
RUNTIME = "memory_runtime_abc123"
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


def _namespace(touched: list, memory_id: str) -> dict:
    """Every Memory and model entry point either generator's ``invoke`` can reach."""

    def _touch(name):
        return lambda *a, **k: touched.append(name) or ""

    return {
        "app": _App,
        "MEMORY_ID": memory_id,
        "REGION": "us-east-1",
        "SYSTEM_PROMPT": "p",
        "AgentCoreMemoryConfig": _touch("memory_config"),
        "AgentCoreMemorySessionManager": _touch("session_manager"),
        "Agent": lambda **k: _touch("model"),
        "_get_model": _touch("model_init"),
        "_get_recent_context": _touch("read_recent"),
        "_get_long_term_context": _touch("read_long_term"),
        "_save_to_memory": _touch("write"),
        "_get_agent": lambda: _touch("model"),
    }


def _memory_agent_source() -> str:
    return _generate_memory_agent("You are helpful.", "us.anthropic.claude-sonnet-5", "us-east-1")


def _unified_agent_source() -> str:
    config = RuntimeConfiguration(name="mem", model={"model_id": "us.anthropic.claude-sonnet-5"}, system_prompt="p")
    return generate_unified_agent_code(
        config, connected_tools=["memory"], memory_id="memory-AbCdEf1234", region="us-east-1"
    )


GENERATORS = pytest.mark.parametrize("source", [_memory_agent_source, _unified_agent_source], ids=["memory", "unified"])


# --- the generated agents ------------------------------------------------------------


@GENERATORS
def test_a_warmup_reaches_neither_memory_nor_the_model(source):
    touched: list[str] = []
    invoke = _lift_invoke(source(), _namespace(touched, "memory-AbCdEf1234"))

    # The live ping: no session supplied by the UI, identity minted by the route.
    result = invoke({"prompt": "ping", "session_id": SESSION, "actor_id": ACTOR, "warmup": True})

    assert result == {"response": "", "warmup": True}
    assert touched == []


@GENERATORS
def test_a_warmup_still_fails_on_a_runtime_without_memory_id(source):
    touched: list[str] = []
    invoke = _lift_invoke(source(), _namespace(touched, ""))

    with pytest.raises(RuntimeError, match="MEMORY_ID"):
        invoke({"prompt": "ping", "warmup": True})
    with pytest.raises(RuntimeError, match="MEMORY_ID"):
        invoke({"prompt": "hi", "session_id": SESSION, "actor_id": ACTOR})
    assert touched == []


@GENERATORS
@pytest.mark.parametrize("marker", ["true", 1, "yes", False, None])
def test_only_a_literal_true_is_a_warmup(source, marker):
    """A truthy look-alike is a real turn, so it still needs a full identity and is stored."""
    touched: list[str] = []
    invoke = _lift_invoke(source(), _namespace(touched, "memory-AbCdEf1234"))

    invoke({"prompt": "hi", "session_id": SESSION, "actor_id": ACTOR, "warmup": marker})

    assert "model" in touched


def test_a_real_turn_on_the_memory_agent_is_still_stored():
    touched: list[str] = []
    invoke = _lift_invoke(_memory_agent_source(), _namespace(touched, "memory-AbCdEf1234"))

    invoke({"prompt": "hi", "session_id": SESSION, "actor_id": ACTOR})

    assert touched == ["read_recent", "read_long_term", "model", "write"]


@GENERATORS
def test_the_warmup_return_sits_after_the_memory_id_check_by_ast(source):
    """Pins the order independently of the stubs: the MEMORY_ID guard, then the warmup."""
    tree = ast.parse(source())
    invoke = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "invoke")
    guards = [ast.unparse(stmt.test) for stmt in invoke.body if isinstance(stmt, ast.If)]

    assert guards.index("not MEMORY_ID") < guards.index("payload.get('warmup') is True")
    assert guards.index("payload.get('warmup') is True") < guards.index("not session_id or not actor_id")


def test_a_non_memory_agent_still_answers_the_warmup():
    """Without Memory there is nothing to pollute: the ping keeps warming the model path."""
    config = RuntimeConfiguration(name="plain", model={"model_id": "us.anthropic.claude-sonnet-5"}, system_prompt="p")
    source = generate_unified_agent_code(config, connected_tools=[], region="us-east-1")

    assert "warmup" not in source


# --- the route and the UI ------------------------------------------------------------


class _Agentcore:
    def __init__(self) -> None:
        self.invocations: list[dict] = []

    def invoke_agent_runtime(self, **kwargs):
        self.invocations.append(kwargs)
        return {"response": json.dumps({"response": ""}).encode(), "statusCode": 200}


class _Session:
    def __init__(self, client):
        self._client = client

    def client(self, service, **_kwargs):
        return self._client


@pytest.fixture
def api(monkeypatch):
    agentcore = _Agentcore()
    record = {
        "deployment_id": "5bb2084b-d586-46d6-a5f3-494cd24cfc89",
        "runtime_id": RUNTIME,
        "runtime_arn": f"arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/{RUNTIME}",
        "user_id": OWNER,
        "deployment_mode": "runtime",
        "status": "succeeded",
        "memory_result": {"memory_id": "memory-AbCdEf1234", "ready": True},
    }

    class _Store:
        _table = object()

    monkeypatch.setattr(dh, "_get_state_store", lambda: _Store())
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda _t, rid: record if rid == RUNTIME else None)
    monkeypatch.setattr(step_clients, "session_for_event", lambda _e: _Session(agentcore))
    monkeypatch.setattr(dh, "_maybe_promote_policy", lambda *a, **k: False)
    event = {
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": OWNER, "cognito:groups": ["g-users-default"]}}}}
    }

    async def _inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await dh.deployment_app(scope, receive, send)

    return TestClient(_inject), agentcore


def test_the_route_forwards_the_warmup_marker(api):
    client, agentcore = api

    # Exactly the body DeployPanel.warmupRuntime sends.
    resp = client.post(
        "/api/test-runtime", json={"endpoint": "", "input": "ping", "runtimeId": RUNTIME, "warmup": True}
    )

    assert resp.status_code == 200, resp.text
    payload = json.loads(agentcore.invocations[0]["payload"])
    assert payload["warmup"] is True
    assert payload["session_id"] and payload["actor_id"]  # identity still resolved and bound


def test_an_ordinary_test_invoke_carries_no_marker(api):
    client, agentcore = api

    client.post("/api/test-runtime", json={"input": "hi", "runtimeId": RUNTIME})

    assert "warmup" not in json.loads(agentcore.invocations[0]["payload"])


def test_the_ui_warmup_sends_the_marker():
    from pathlib import Path

    panel = Path(__file__).resolve().parents[2] / "frontend/src/components/deploy/DeployPanel.tsx"
    body = panel.read_text().split("const warmupRuntime", 1)[1].split("}, []);", 1)[0]

    assert "warmup: true" in body
