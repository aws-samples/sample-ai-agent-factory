"""Tool-use receipts: a generated agent reports each tool call its own loop executed.

The live matrix (stage30) recorded every HTTP gallery's model-path tool proof as an OPEN
invariant: the product returned model text only, so a JSON tool result could not be told
apart from one the model invented. Generated agents now return ``tool_receipts`` (name,
status, SHA-256 argument digests) read from the Converse-format conversation their own
loop wrote, and every invoke route relays a validated copy.

The generated-code tests lift the real functions out of the generated source and run
them against fakes, so they measure the shipped text rather than a grep of it.
"""

from __future__ import annotations

import ast
import hashlib
import json

import pytest
from app import deployment_handler as dh
from app import stream_handler as sh
from app.models.deployment_models import RuntimeConfig
from app.services import codegen_templates, step_clients
from app.services.code_generator import generate_agent_code
from app.services.runtime_invocation import parse_tool_receipts
from fastapi.testclient import TestClient

OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
RUNTIME = "receipts_runtime_abc123"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/{RUNTIME}"
SESSION = "0123456789abcdef0123456789abcdef-0123456789abcdef0123456789abcdef"
ACTOR = "0123456789abcdef0123456789abcdef"


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _receipt(name: str, arguments: dict, status: str = "success") -> dict:
    return {
        "name": name,
        "status": status,
        "input_sha256": _digest(arguments),
        "argument_sha256": {key: _digest(value) for key, value in arguments.items()},
    }


def _helpers() -> dict:
    namespace: dict = {}
    exec(compile(codegen_templates.load_impl("tool_receipts"), "<tool_receipts>", "exec"), namespace)
    return namespace


def _use(use_id: str, name: str, arguments) -> dict:
    return {"role": "assistant", "content": [{"toolUse": {"toolUseId": use_id, "name": name, "input": arguments}}]}


def _result(use_id: str, status: str = "success") -> dict:
    return {
        "role": "user",
        "content": [{"toolResult": {"toolUseId": use_id, "status": status, "content": [{"text": "{}"}]}}],
    }


# ---------------------------------------------------------------------------
# The deployed-code template
# ---------------------------------------------------------------------------


def test_receipts_cover_only_the_new_calls_with_status_and_argument_digests():
    helpers = _helpers()
    messages = [_use("old", "stale_tool", {"a": 1}), _result("old")]
    seen = helpers["_tool_use_ids"](messages)
    messages += [
        {
            "role": "assistant",
            "content": [
                {"text": "calling"},
                {"toolUse": {"toolUseId": "t1", "name": "DynamicTools___get_weather", "input": {"location": "Dublin"}}},
            ],
        },
        _result("t1"),
        _use("t2", "DynamicTools___get_order", {"order_id": "ORD-1"}),
        _result("t2", status="error"),
        _use("t3", "DynamicTools___list_orders", {}),
    ]

    assert helpers["_tool_receipts"](messages, exclude=seen) == [
        _receipt("DynamicTools___get_weather", {"location": "Dublin"}),
        _receipt("DynamicTools___get_order", {"order_id": "ORD-1"}, status="error"),
        _receipt("DynamicTools___list_orders", {}, status="missing"),
    ]


def test_a_loop_decided_status_and_a_fitted_alias_are_honoured():
    helpers = _helpers()
    alias = "get_regional_availability_1a2b3c4d"
    messages = [_use("t1", alias, {"region": "eu"}), _result("t1")]

    receipts = helpers["_tool_receipts"](
        messages,
        statuses={"t1": "error"},
        names={alias: "srv___get_regional_availability"},
    )

    assert receipts == [_receipt("srv___get_regional_availability", {"region": "eu"}, status="error")]


def test_receipts_are_bounded_and_tolerate_malformed_messages():
    helpers = _helpers()
    messages = [None, {"content": "text"}, {"content": [None, {"toolUse": "x"}, {"toolUse": {"name": "no-id"}}]}]
    messages += [_use(f"t{index}", "tool", {"i": index}) for index in range(70)]
    messages.append(_use("bad-input", "tool", "not-an-object"))

    receipts = helpers["_tool_receipts"](messages)

    assert len(receipts) == 64
    assert receipts[0] == _receipt("tool", {"i": 0}, status="missing")
    assert helpers["_tool_receipts"](None) == []
    assert helpers["_tool_use_ids"](None) == set()
    assert helpers["_tool_receipts"]([_use("x", "tool", "not-an-object")]) == [_receipt("tool", {}, "missing")]


@pytest.mark.parametrize(
    ("known", "text", "status"),
    [
        (False, "anything", "error"),
        (True, json.dumps({"error": "tool_unavailable"}), "error"),
        (True, json.dumps({"message": "No results found"}), "success"),
        (True, json.dumps([{"title": "t", "url": "https://x"}]), "success"),
        (True, "plain text", "success"),
    ],
)
def test_a_hand_rolled_loop_status(known: bool, text: str, status: str):
    assert _helpers()["_tool_result_status"](known, text) == status


# ---------------------------------------------------------------------------
# Every generated single-agent entrypoint
# ---------------------------------------------------------------------------


class _App:
    @staticmethod
    def entrypoint(fn):
        return fn


class _FakeAgent:
    """A Strands-shaped agent: calling it appends toolUse/toolResult turns to .messages."""

    def __init__(self, calls: list[tuple[str, dict, str]], *, earlier: bool = True) -> None:
        self.messages = [_use("earlier", "old_tool", {}), _result("earlier")] if earlier else []
        self._calls = calls
        self.prompts: list[str] = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        for index, (name, arguments, status) in enumerate(self._calls):
            self.messages.append(_use(f"new-{index}", name, arguments))
            self.messages.append(_result(f"new-{index}", status))
        return "done"


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        name="receipts_probe",
        model={"modelId": "us.anthropic.claude-sonnet-5"},
        systemPrompt="Use tools when asked.",
    )


def _lift(source: str, names: set[str], namespace: dict) -> dict:
    assert "__TOOL_RECEIPTS__" not in source
    assert source.count("def _tool_receipts(") == 1
    tree = ast.parse(source)  # the whole module must compile, not just the lifted functions
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert sorted(node.name for node in nodes) == sorted(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<generated>", "exec"), namespace)
    return namespace


CALLS = [
    ("DynamicTools___get_weather", {"location": "Dublin"}, "success"),
    ("DynamicTools___get_order", {"order_id": "ORD-12345"}, "error"),
]
EXPECTED = [
    _receipt("DynamicTools___get_weather", {"location": "Dublin"}),
    _receipt("DynamicTools___get_order", {"order_id": "ORD-12345"}, status="error"),
]


def test_the_gateway_agent_returns_receipts_for_this_turn_only():
    agent = _FakeAgent(CALLS)
    source = generate_agent_code(_config(), tools=["gateway"], template_id="strands-gateway-agent", portable=True)
    namespace = _lift(
        source,
        {"invoke"},
        {**_helpers(), "app": _App, "_get_agent": lambda: agent, "_TOOL_NAME_ALIASES": {}},
    )

    assert namespace["invoke"]({"prompt": "go"}) == {"response": "done", "tool_receipts": EXPECTED}


def test_the_gateway_agent_reports_a_fitted_name_as_published():
    alias = "get_weather_0a1b2c3d"
    agent = _FakeAgent([(alias, {"location": "Dublin"}, "success")])
    source = generate_agent_code(_config(), tools=["gateway"], template_id="strands-gateway-agent", portable=True)
    assert "_TOOL_NAME_ALIASES[_alias] = _name" in source
    namespace = _lift(
        source,
        {"invoke"},
        {
            **_helpers(),
            "app": _App,
            "_get_agent": lambda: agent,
            "_TOOL_NAME_ALIASES": {alias: "cfgtgt-lambda-0___get_weather"},
        },
    )

    receipts = namespace["invoke"]({"prompt": "go"})["tool_receipts"]
    assert receipts == [_receipt("cfgtgt-lambda-0___get_weather", {"location": "Dublin"})]


def test_the_tools_agent_returns_receipts_for_this_turn_only():
    agent = _FakeAgent(CALLS)
    source = generate_agent_code(_config(), tools=["browser"], portable=True)
    namespace = _lift(source, {"invoke", "_final_text"}, {**_helpers(), "app": _App, "_get_agent": lambda: agent})

    result = namespace["invoke"]({"prompt": "go"})

    assert result["tool_receipts"] == EXPECTED
    assert result["response"] == "done"


def test_the_memory_agent_returns_receipts_for_this_turn_only():
    agent = _FakeAgent(CALLS)
    source = generate_agent_code(_config(), tools=["memory"], portable=True)
    namespace = _lift(
        source,
        {"invoke"},
        {
            **_helpers(),
            "app": _App,
            "MEMORY_ID": "memory-AbCdEf1234",
            "_get_recent_context": lambda *_args: "",
            "_get_long_term_context": lambda *_args: "",
            "_save_to_memory": lambda *_args: None,
            "_get_agent": lambda: agent,
        },
    )

    result = namespace["invoke"]({"prompt": "go", "session_id": SESSION, "actor_id": ACTOR})

    assert result == {"response": "done", "tool_receipts": EXPECTED}


def test_the_default_strands_agent_returns_receipts():
    agent = _FakeAgent(CALLS, earlier=False)
    source = generate_agent_code(_config())
    namespace = _lift(
        source,
        {"invoke"},
        {**_helpers(), "app": _App, "Agent": lambda **_kwargs: agent, "load_model": lambda: None, "SYSTEM_PROMPT": "p"},
    )

    assert namespace["invoke"]({"prompt": "go"}) == {"response": "done", "tool_receipts": EXPECTED}


class _Converse:
    """Bedrock Converse: one tool_use turn, then a final answer."""

    def __init__(self, tool_calls: list[tuple[str, dict]]) -> None:
        self._tool_calls = tool_calls
        self.requests: list[list] = []

    def converse(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"])))
        if len(self.requests) == 1:
            content = [
                {"toolUse": {"toolUseId": f"u{index}", "name": name, "input": arguments}}
                for index, (name, arguments) in enumerate(self._tool_calls)
            ]
            return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": "tool_use"}
        return {"output": {"message": {"role": "assistant", "content": [{"text": "final"}]}}, "stopReason": "end_turn"}


def test_the_web_search_loop_returns_receipts_and_keeps_its_converse_request_unchanged():
    bedrock = _Converse([("get_weather", {"location": "Dublin"}), ("nope", {"x": 1})])
    source = generate_agent_code(_config(), template_id="web-search-agent")
    namespace = _lift(
        source,
        {"invoke", "_converse_loop"},
        {
            **_helpers(),
            "app": _App,
            "_get_bedrock": lambda: bedrock,
            "MODEL_ID": "m",
            "SYSTEM_PROMPT": "p",
            "TOOL_CONFIG": {"tools": []},
            "TOOL_HANDLERS": {"get_weather": lambda args: json.dumps({"location": args["location"]})},
        },
    )

    result = namespace["invoke"]({"prompt": "weather please"})

    assert result == {
        "response": "final",
        "tool_receipts": [_receipt("get_weather", {"location": "Dublin"}), _receipt("nope", {"x": 1}, "error")],
    }
    # The loop decides status beside the conversation; the toolResult blocks it sends
    # back to Bedrock keep their previous shape, since not every model accepts `status`.
    tool_results = bedrock.requests[1][2]["content"]
    assert [set(block["toolResult"]) for block in tool_results] == [{"toolUseId", "content"}] * 2


# ---------------------------------------------------------------------------
# The route-side validator
# ---------------------------------------------------------------------------


GOOD = [_receipt("DynamicTools___get_weather", {"location": "Dublin"})]


def test_valid_receipts_are_returned_exactly():
    body = json.dumps({"response": "ok", "tool_receipts": GOOD})

    assert parse_tool_receipts(body) == GOOD
    assert parse_tool_receipts(body.encode()) == GOOD
    assert parse_tool_receipts(json.dumps({"response": "ok", "tool_receipts": []})) == []


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "not json",
        json.dumps({"response": "ok"}),
        json.dumps(["tool_receipts"]),
        json.dumps({"tool_receipts": "x"}),
        json.dumps({"tool_receipts": GOOD * 65}),
        json.dumps({"tool_receipts": [{**GOOD[0], "extra": 1}]}),
        json.dumps({"tool_receipts": [{**GOOD[0], "status": "ok"}]}),
        json.dumps({"tool_receipts": [{**GOOD[0], "name": ""}]}),
        json.dumps({"tool_receipts": [{**GOOD[0], "name": "bad\nname"}]}),
        json.dumps({"tool_receipts": [{**GOOD[0], "input_sha256": "A" * 64}]}),
        json.dumps({"tool_receipts": [{**GOOD[0], "argument_sha256": {"": "a" * 64}}]}),
        json.dumps({"tool_receipts": [{**GOOD[0], "argument_sha256": {"k": "short"}}]}),
        json.dumps({"tool_receipts": [{**GOOD[0], "argument_sha256": {f"k{i}": "a" * 64 for i in range(33)}}]}),
        json.dumps({"tool_receipts": [GOOD[0], "not-a-receipt"]}),
    ],
)
def test_absent_or_malformed_receipts_are_dropped_whole(body):
    assert parse_tool_receipts(body) is None


# ---------------------------------------------------------------------------
# The three invoke surfaces
# ---------------------------------------------------------------------------


def _record() -> dict:
    return {
        "deployment_id": "5bb2084b-d586-46d6-a5f3-494cd24cfc89",
        "runtime_id": RUNTIME,
        "runtime_arn": RUNTIME_ARN,
        "user_id": OWNER,
        "deployment_mode": "runtime",
        "status": "succeeded",
    }


class _Store:
    _table = object()


class _Agentcore:
    def __init__(self, body: dict) -> None:
        self._body = body

    def invoke_agent_runtime(self, **kwargs):
        return {
            "response": json.dumps(self._body).encode(),
            "runtimeSessionId": kwargs.get("runtimeSessionId") or SESSION,
            "statusCode": 200,
        }


class _Session:
    def __init__(self, client: _Agentcore) -> None:
        self._client = client

    def client(self, service, **_kwargs):
        assert service == "bedrock-agentcore"
        return self._client


def _client() -> TestClient:
    event = {
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": OWNER, "cognito:groups": ["g-users-default"]}}},
        }
    }

    async def _inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await dh.deployment_app(scope, receive, send)

    return TestClient(_inject)


def _patch_runtime(monkeypatch, body: dict, module) -> None:
    monkeypatch.setattr(module, "_get_state_store", lambda: _Store())
    monkeypatch.setattr(
        module, "_scan_for_runtime", lambda _table, runtime_id: _record() if runtime_id == RUNTIME else None
    )
    monkeypatch.setattr(step_clients, "session_for_event", lambda _event: _Session(_Agentcore(body)))
    monkeypatch.setattr(dh, "_maybe_promote_policy", lambda *args, **kwargs: False)


def _done(text: str) -> dict:
    events = [json.loads(line.removeprefix("data: ")) for line in text.splitlines() if line.startswith("data: ")]
    done = [event for event in events if event["type"] == "done"]
    assert len(done) == 1
    return done[0]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"response": "ok", "tool_receipts": GOOD}, GOOD),
        ({"response": "ok"}, None),
        ({"response": "ok", "tool_receipts": [{"name": "forged"}]}, None),
    ],
)
def test_the_browser_stream_relays_only_validated_receipts(monkeypatch, body, expected):
    _patch_runtime(monkeypatch, body, dh)

    response = _client().post("/api/test-runtime-stream", json={"runtimeId": RUNTIME, "input": "hi"})

    assert response.status_code == 200, response.text
    done = _done(response.text)
    assert done["full_response"] == "ok"
    assert done.get("tool_receipts") == expected
    assert ("tool_receipts" in done) is (expected is not None)


@pytest.mark.parametrize(
    ("body", "expected"),
    [({"response": "ok", "tool_receipts": GOOD}, GOOD), ({"response": "ok"}, None)],
)
def test_the_sync_route_returns_validated_receipts(monkeypatch, body, expected):
    _patch_runtime(monkeypatch, body, dh)

    response = _client().post("/api/test-runtime", json={"runtimeId": RUNTIME, "input": "hi"})

    assert response.status_code == 200, response.text
    assert response.json()["response"] == "ok"
    assert response.json()["toolReceipts"] == expected


def test_the_function_url_stream_relays_validated_receipts(monkeypatch):
    _patch_runtime(monkeypatch, {"response": "ok", "tool_receipts": GOOD}, sh)
    monkeypatch.setattr(
        sh.step_clients,
        "session_for_event",
        lambda _event: _Session(_Agentcore({"response": "ok", "tool_receipts": GOOD})),
    )

    written: list[bytes] = []
    sh._stream_invoke(written.append, {"runtimeId": RUNTIME, "input": "hi"}, OWNER)

    done = _done(b"".join(written).decode())
    assert done["full_response"] == "ok"
    assert done["tool_receipts"] == GOOD
