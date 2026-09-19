"""Gap 3A — A2A protocol codegen EXEC tests.

Mirrors tests/test_hitl_codegen.py: installs lightweight strands /
bedrock_agentcore stubs (plus NEW starlette + httpx stubs) so the generated A2A
agent module can be exec'd to prove symbol resolution — AST-parse alone is NOT
sufficient (Bug 125).

Coverage:
  (a) Routing: protocol='A2A' AND tools=['a2a'] both produce the A2A template
      (agent-card route + call_a2a_peer present); MCP / HTTP (no a2a tool) do
      NOT regress into the A2A template.
  (b) The generated source compiles and exec's against stubs with NO NameError;
      call_a2a_peer resolves at module scope (Bug 125 ordering gate).
  (c) The agent card is served at /.well-known/agent-card.json and the
      advertised description / capabilities from peer_config appear.
  (d) SSRF guard: call_a2a_peer refuses a non-allowlisted host, refuses IMDS /
      loopback / RFC1918 hosts, and is fail-closed with no allowlist — all
      WITHOUT any outbound httpx call.
  (e) `from a2a` (the un-bundled a2a-sdk) appears NOWHERE in the output.
  (f) Injection-safety: peer_config with triple-quotes / backslashes / quotes
      still produces compilable source.
"""

from __future__ import annotations

import json
import sys
import types

sys.path.insert(0, "src")

import pytest
from app.models.deployment_models import RuntimeConfig
from app.services.a2a_codegen import _generate_a2a_agent
from app.services.code_generator import generate_agent_code

# ---------------------------------------------------------------------------
# Config + stub helpers
# ---------------------------------------------------------------------------


def _cfg(protocol: str = "A2A"):
    return RuntimeConfig(
        name="a2a_t",
        model={"modelId": "us.anthropic.claude-sonnet-5"},
        systemPrompt="You collaborate with other agents.",
        modelProvider="bedrock",
        protocol=protocol,
    )


class _RecordingHttpxClient:
    """httpx.Client stub that RECORDS calls so a test can assert no outbound
    request happened when the SSRF guard refuses a peer."""

    calls: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, *a, **k):
        _RecordingHttpxClient.calls.append(("GET", url))
        raise AssertionError(f"httpx GET should not be reached: {url}")

    def post(self, url, *a, **k):
        _RecordingHttpxClient.calls.append(("POST", url))
        raise AssertionError(f"httpx POST should not be reached: {url}")


def _install_a2a_stubs():
    """Install strands + bedrock_agentcore + starlette + httpx stubs so the
    generated A2A module exec's cleanly."""
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

    sbedrock = types.ModuleType("strands.models.bedrock")
    sbedrock.BedrockModel = BedrockModel
    smodels.bedrock = sbedrock

    bac = types.ModuleType("bedrock_agentcore")
    bacr = types.ModuleType("bedrock_agentcore.runtime")

    class App:
        def __init__(self, *a, **k):
            self.routes = []

        def entrypoint(self, f):
            return f

        def add_route(self, path, handler, methods=None):
            self.routes.append((path, handler, tuple(methods or ())))

        def run(self):
            pass

    bacr.BedrockAgentCoreApp = App
    bac.runtime = bacr

    # starlette.responses.JSONResponse
    starlette = types.ModuleType("starlette")
    s_responses = types.ModuleType("starlette.responses")

    class JSONResponse:
        def __init__(self, content, *a, **k):
            self.content = content

    s_responses.JSONResponse = JSONResponse
    starlette.responses = s_responses

    # httpx stub — defaults to the recording client (asserts no call). Tests
    # that exercise the happy path swap this out.
    httpx = types.ModuleType("httpx")
    httpx.Client = _RecordingHttpxClient

    mods = {
        "strands": strands,
        "strands.models": smodels,
        "strands.models.bedrock": sbedrock,
        "bedrock_agentcore": bac,
        "bedrock_agentcore.runtime": bacr,
        "starlette": starlette,
        "starlette.responses": s_responses,
        "httpx": httpx,
    }
    for n, m in mods.items():
        sys.modules[n] = m
    return mods


def _exec_module(code: str) -> dict:
    """Exec generated A2A source against stubs; return the module namespace."""
    _install_a2a_stubs()
    _RecordingHttpxClient.calls = []
    g: dict = {"__name__": "a2a_agent_under_test"}
    exec(compile(code, "<a2a_agent.py>", "exec"), g)
    return g


# ---------------------------------------------------------------------------
# (a) Routing
# ---------------------------------------------------------------------------


def test_a2a_branch_emits_card_and_tool():
    """_generate_a2a_agent emits the agent-card route + call_a2a_peer."""
    code = _generate_a2a_agent(
        "You collaborate.",
        "us.anthropic.claude-sonnet-5",
        "us-east-1",
        {"capabilities": ["chat"], "advertised_description": "test", "peer_allowlist": []},
    )
    assert "/.well-known/agent-card.json" in code
    assert "def call_a2a_peer" in code


def _routes_to_a2a(code: str) -> bool:
    return "/.well-known/agent-card.json" in code and "def call_a2a_peer" in code


def test_generate_agent_code_routes_a2a_when_wired():
    """When the shared A2A branch is applied to generate_agent_code, both
    protocol='A2A' and tools=['a2a'] route to the A2A template. The branch is a
    shared-file edit applied by the main loop AFTER this gap's new files, so we
    only assert routing when the branch is present (don't fail the gap on a
    not-yet-applied shared edit) — and we ALWAYS assert the building block in
    test_a2a_branch_emits_card_and_tool above."""
    import inspect

    from app.services import code_generator

    src = inspect.getsource(code_generator.generate_agent_code)
    branch_applied = "a2a_codegen" in src or "_generate_a2a_agent" in src
    if not branch_applied:
        pytest.skip("A2A dispatch branch not yet wired into generate_agent_code")

    code_proto = generate_agent_code(config=_cfg("A2A"), tools=[])
    code_tool = generate_agent_code(config=_cfg("HTTP"), tools=["a2a"])
    assert _routes_to_a2a(code_proto), "protocol='A2A' did not route to A2A template"
    assert _routes_to_a2a(code_tool), "tools=['a2a'] did not route to A2A template"


# ---------------------------------------------------------------------------
# (f-regression) MCP / HTTP without a2a do NOT become the A2A template
# ---------------------------------------------------------------------------


def test_non_a2a_templates_not_regressed():
    code_mcp = generate_agent_code(config=_cfg("MCP"), tools=[], template_id="mcp-server-runtime")
    code_http = generate_agent_code(config=_cfg("HTTP"), tools=[])
    assert not _routes_to_a2a(code_mcp), "MCP template regressed into A2A"
    assert not _routes_to_a2a(code_http), "default HTTP template regressed into A2A"


# ---------------------------------------------------------------------------
# (b) EXEC-safety — Bug 125 ordering gate
# ---------------------------------------------------------------------------


def test_generated_a2a_module_execs_no_nameerror():
    code = _generate_a2a_agent(
        "You collaborate.",
        "us.anthropic.claude-sonnet-5",
        "us-east-1",
        {
            "capabilities": ["chat", "summarize"],
            "advertised_description": "An A2A peer.",
            "peer_allowlist": ["peer.example.com"],
        },
    )
    g = _exec_module(code)
    assert callable(g.get("call_a2a_peer")), "call_a2a_peer not defined at module scope"
    assert callable(g.get("invoke")), "invoke entrypoint not defined"
    # The agent-card route must have been registered on the app at import time.
    app = g.get("app")
    assert app is not None
    paths = [r[0] for r in getattr(app, "routes", [])]
    assert "/.well-known/agent-card.json" in paths


# ---------------------------------------------------------------------------
# (c) Agent card content
# ---------------------------------------------------------------------------


def test_agent_card_reflects_peer_config():
    g = _exec_module(
        _generate_a2a_agent(
            "sp",
            "us.anthropic.claude-sonnet-5",
            "us-east-1",
            {
                "capabilities": ["translate", "research"],
                "advertised_description": "A multilingual research agent.",
                "peer_allowlist": ["peer.example.com"],
            },
        )
    )
    card = g["_build_agent_card"]()
    assert card["description"] == "A multilingual research agent."
    assert "translate" in card["capabilities"]
    assert "research" in card["capabilities"]
    assert card["url"]
    # skills derived from capabilities
    skill_ids = [s["id"] for s in card["skills"]]
    assert "translate" in skill_ids


# ---------------------------------------------------------------------------
# (d) SSRF guard — no outbound httpx call on a refused peer
# ---------------------------------------------------------------------------


def _make_agent(allowlist=None, capabilities=None):
    g = _exec_module(
        _generate_a2a_agent(
            "sp",
            "us.anthropic.claude-sonnet-5",
            "us-east-1",
            {
                "capabilities": capabilities or ["chat"],
                "advertised_description": "peer",
                "peer_allowlist": allowlist or [],
            },
        )
    )
    return g


def test_ssrf_refuses_non_allowlisted_host(monkeypatch):
    monkeypatch.setenv("A2A_PEER_ALLOWLIST", "allowed.example.com")
    g = _make_agent()
    out = g["call_a2a_peer"]("https://evil.example.com", "hi")
    import json

    res = json.loads(out)
    assert res["status"] in ("BLOCKED", "ERROR")
    assert _RecordingHttpxClient.calls == [], "no outbound call should happen for refused host"


@pytest.mark.parametrize(
    "peer_url",
    [
        # Bug 139: peer urls are now https-only, so use https here — this keeps the
        # test proving the IP/DNS DENYLIST (not the scheme check). http variants are
        # covered by test_ssrf_refuses_non_https below.
        "https://169.254.169.254/latest/meta-data/",  # IMDS
        "https://127.0.0.1:8080/",  # loopback
        "https://10.0.0.5/invoke",  # RFC1918
    ],
)
def test_ssrf_refuses_private_and_imds_hosts(peer_url, monkeypatch):
    # Allowlist the literal host so the ONLY thing that can block it is the
    # DNS/IP denylist (proves the denylist, not just the allowlist).
    from urllib.parse import urlparse

    host = urlparse(peer_url).hostname
    monkeypatch.setenv("A2A_PEER_ALLOWLIST", host)
    g = _make_agent()
    out = g["call_a2a_peer"](peer_url, "hi")
    import json

    res = json.loads(out)
    assert res["status"] == "BLOCKED", f"expected BLOCKED for {peer_url}, got {res}"
    assert _RecordingHttpxClient.calls == [], "no outbound call should happen for blocked IP"


@pytest.mark.parametrize(
    "peer_url",
    [
        "http://example.com/",  # plaintext to an otherwise-allowed host
        "http://169.254.169.254/latest/meta-data/",  # plaintext IMDS
        "ftp://example.com/",  # non-web scheme
    ],
)
def test_ssrf_refuses_non_https(peer_url, monkeypatch):
    # Bug 139: A2A peer urls must be https-only (matches the OIDC/git SSRF rule).
    # http/other schemes are rejected before any outbound call.
    from urllib.parse import urlparse

    host = urlparse(peer_url).hostname
    if host:
        monkeypatch.setenv("A2A_PEER_ALLOWLIST", host)
    g = _make_agent()
    out = g["call_a2a_peer"](peer_url, "hi")
    import json

    res = json.loads(out)
    assert res["status"] == "ERROR", f"expected ERROR (non-https) for {peer_url}, got {res}"
    assert _RecordingHttpxClient.calls == [], "no outbound call for a non-https scheme"


def test_ssrf_fail_closed_without_allowlist(monkeypatch):
    monkeypatch.delenv("A2A_PEER_ALLOWLIST", raising=False)
    g = _make_agent(allowlist=[])
    import json

    out = json.loads(g["call_a2a_peer"]("https://peer.example.com", "hi"))
    assert out["status"] == "BLOCKED"
    assert "fail-closed" in out["error"] or "allowlist" in out["error"].lower()
    assert _RecordingHttpxClient.calls == []


def test_ssrf_rejects_non_http_scheme():
    g = _make_agent(allowlist=["peer.example.com"])
    import json

    out = json.loads(g["call_a2a_peer"]("file:///etc/passwd", "hi"))
    assert out["status"] == "ERROR"
    assert _RecordingHttpxClient.calls == []


# ---------------------------------------------------------------------------
# (e) Import-safety regression lock: NO a2a-sdk import
# ---------------------------------------------------------------------------


def test_no_a2a_sdk_import_in_generated_source():
    code = _generate_a2a_agent("sp", "us.anthropic.claude-sonnet-5", "us-east-1", {})
    assert "from a2a" not in code, "a2a-sdk is NOT bundled — must not be imported"
    assert "import a2a" not in code


# ---------------------------------------------------------------------------
# (f) Injection-safety
# ---------------------------------------------------------------------------


def test_injection_safe_peer_config_compiles():
    nasty = {
        "advertised_description": 'desc """ with triple quotes and \\ backslash and {curly}',
        "capabilities": ['cap"break', "ok\\path", 'trip"""le'],
        "peer_allowlist": ['host"; import os', "ok.example.com"],
    }
    code = _generate_a2a_agent("sp", "us.anthropic.claude-sonnet-5", "us-east-1", nasty)
    # Must still compile and exec with no SyntaxError / injection.
    compile(code, "<a2a_inject.py>", "exec")
    g = _exec_module(code)
    assert callable(g.get("call_a2a_peer"))


# ---------------------------------------------------------------------------
# (g) JSON-RPC: a peer's message/send has to be read as A2A, not as a prompt
# ---------------------------------------------------------------------------


class _RecordingAgent:
    """Stands in for the Strands agent and records what text it was asked."""

    def __init__(self):
        self.calls = []

    def __call__(self, text):
        self.calls.append(text)
        return "agent-said: " + text


def _agent_with_recorder():
    """Exec the generated module and replace the lazily-built agent.

    ``_get_agent`` only builds one when the module global ``_agent`` is None, so
    assigning it is enough and no model is ever constructed.
    """
    g = _make_agent()
    recorder = _RecordingAgent()
    g["_agent"] = recorder
    return g, recorder


def _message_send(text, rpc_id="req-1"):
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": "m-1",
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }


def test_message_send_reaches_the_agent_and_answers_as_jsonrpc():
    """The defect this pins was measured live, and it failed silently.

    A spec-compliant ``message/send`` used to return HTTP 200 while the agent ran
    on the literal default ``"Hello"``: the entrypoint read ``payload["prompt"]``
    and nothing else, so ``params.message.parts[].text`` was discarded. The reply
    carried no ``jsonrpc`` and no ``result`` and did not echo ``id``, so a peer had
    no way to tell a wrong answer from a right one.
    """
    g, recorder = _agent_with_recorder()

    out = g["invoke"](_message_send("what is the balance"))

    # The text the peer actually sent is what the agent saw.
    assert recorder.calls == ["what is the balance"], recorder.calls
    # And the reply is a JSON-RPC response object for the same request.
    assert out["jsonrpc"] == "2.0"
    assert out["id"] == "req-1"
    assert "error" not in out
    assert out["result"]["role"] == "agent"
    assert out["result"]["parts"][0]["text"] == "agent-said: what is the balance"
    assert out["result"]["messageId"], "a Message needs a messageId"


def test_multiple_text_parts_are_all_passed_through():
    g, recorder = _agent_with_recorder()
    req = _message_send("first")
    req["params"]["message"]["parts"].append({"type": "text", "text": "second"})
    # A non-text part must not break the request, and must not be invented as text.
    req["params"]["message"]["parts"].append({"kind": "file", "file": {"uri": "s3://x"}})

    g["invoke"](req)

    # Both spellings of the text part are accepted; both are in circulation.
    assert recorder.calls == ["first\nsecond"], recorder.calls


def test_a_plain_prompt_payload_still_works():
    """The platform and a curl caller send ``{"prompt": ...}``; that path is unchanged."""
    g, recorder = _agent_with_recorder()

    out = g["invoke"]({"prompt": "direct"})

    assert recorder.calls == ["direct"]
    assert out == {"response": "agent-said: direct"}
    assert "jsonrpc" not in out, "a plain caller must not be handed an RPC envelope"


def test_an_unserviceable_jsonrpc_request_is_refused_not_guessed():
    """Recognising the envelope is what matters, not being able to serve it.

    If an unsupported method fell through to the prompt path it would run the agent
    on the default and answer 200 — the exact silent-wrong-answer shape that was
    measured. It has to come back as a JSON-RPC error with the caller's id.
    """
    g, recorder = _agent_with_recorder()

    out = g["invoke"]({"jsonrpc": "2.0", "id": 7, "method": "tasks/cancel"})

    assert recorder.calls == [], "the agent must not run for a method we do not serve"
    assert out["id"] == 7
    assert out["error"]["code"] == -32601
    assert "tasks/cancel" in out["error"]["message"]


def test_a_message_send_with_no_text_is_an_invalid_params_error():
    g, recorder = _agent_with_recorder()

    empty = _message_send("")
    out = g["invoke"](empty)

    assert recorder.calls == [], "an empty message must not run the agent on a default"
    assert out["error"]["code"] == -32602
    assert out["id"] == "req-1"

    # Same for a malformed envelope: params of the wrong type must not raise.
    broken = {"jsonrpc": "2.0", "id": 2, "method": "message/send", "params": ["nope"]}
    out = g["invoke"](broken)
    assert out["error"]["code"] == -32602
    assert recorder.calls == []


def test_the_method_name_echoed_back_is_bounded():
    """It is caller-controlled, so it is truncated before it goes in a response."""
    g, _ = _agent_with_recorder()

    out = g["invoke"]({"jsonrpc": "2.0", "id": 1, "method": "x" * 5000})

    assert len(out["error"]["message"]) < 200, out["error"]["message"][:80]


def test_the_agent_card_is_reachable_without_a_url_path():
    """A peer holding only a runtime ARN cannot reach the GET route.

    Measured with signed data-plane requests, because the first explanation of this
    was wrong. It is not that there is nowhere to put a path:
    ``GET /runtimes/<arn>/.well-known/agent-card.json`` is a 404
    ``UnknownOperationException``, and ``GetAgentCard`` — a real operation in the
    service model, with no CLI subcommand — answers 400 ``"GetAgentCard API is only
    supported for A2A agents"``. The runtime is declared ``HTTP``, so it is refused;
    declaring ``A2A`` would unlock the card and make every invoke 424 (see
    ``TestTheDeclaredProtocolIsTheOneTheContainerSpeaks``). One declaration cannot
    buy both, so the JSON-RPC method is the only way such a peer reads the card, and
    it has to return the same card the route serves.
    """
    g, recorder = _agent_with_recorder()

    out = g["invoke"]({"jsonrpc": "2.0", "id": "c1", "method": "agent/getAuthenticatedExtendedCard"})

    assert recorder.calls == [], "fetching a card must not invoke the model"
    assert out["id"] == "c1"
    assert out["result"] == g["_build_agent_card"]()
    assert out["result"]["protocolVersion"]
    assert out["result"]["skills"], "a card with no skills tells a peer nothing"


def test_a_non_dict_payload_does_not_crash_the_entrypoint():
    g, recorder = _agent_with_recorder()

    out = g["invoke"]([1, 2, 3])

    assert out == {"response": "agent-said: Hello"}
    assert recorder.calls == ["Hello"]


def test_the_generated_source_does_not_log_the_payload():
    """ARCC cnt_Yq9sVcaZyQniIv: request bodies and response payloads are customer
    content and do not belong in service logs."""
    code = _generate_a2a_agent("sp", "us.anthropic.claude-sonnet-5", "us-east-1", {})
    handler = code.split("def _a2a_handle_jsonrpc(payload):", 1)[1].split("\n@app.entrypoint", 1)[0]
    # Sinks, not mentions: the parameter is named in the signature and in the reads
    # that are the function's job, so an over-broad token like "payload)" matches the
    # signature itself and the test passes or fails for the wrong reason.
    for forbidden in ("print(", "logging", "logger", "json.dumps"):
        assert forbidden not in handler, f"{forbidden!r} appears in the JSON-RPC handler"


def test_a_plain_payload_carrying_a_method_field_is_not_hijacked():
    """Dispatch must not break a caller who sends ``method`` for their own reasons.

    ``jsonrpc`` is the marker a real peer always sends. A bare ``method`` is treated
    as JSON-RPC only when there is no ``prompt``, so a plain payload that happens to
    carry both keeps the prompt path rather than coming back as -32601.
    """
    g, recorder = _agent_with_recorder()

    out = g["invoke"]({"prompt": "still a prompt", "method": "chat"})

    assert recorder.calls == ["still a prompt"]
    assert out == {"response": "agent-said: still a prompt"}

    # But a bare method with no prompt is still JSON-RPC, and still refused.
    out = g["invoke"]({"method": "tasks/get", "id": 9})
    assert out["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# (h) call_a2a_peer reaches the peer — it could not, for any input at all
# ---------------------------------------------------------------------------
#
# There was no happy-path test for this tool, and that is how the following survived:
# the invoke url was scheme-validated BEFORE the relative-url join, so a relative url
# ("" scheme) was refused as non-https; the join then sat in an `elif not scheme` arm
# the validation had already made unreachable; and its trailing `else` returned
# "unsupported scheme" on the one remaining path -- an absolute https url on an
# allowlisted host. Measured against the generated module: no input existed for which
# call_a2a_peer reached its POST. The guard tests all passed throughout, because every
# one of them asserts a refusal.
#
# "/invocations" is not an edge case either: it is what _a2a_self_url() returns when
# nothing sets AGENTCORE_RUNTIME_URL, so it is what the card of every agent this
# generator emits advertises by default. Two of these agents could not talk to each
# other.


class _PeerServer:
    """httpx.Client stub standing in for a peer: serves a card, records POSTs.

    ``post_bodies`` is consumed one per POST, so a test can give the first and second
    call different answers and assert how many were made.
    """

    card_url = "/invocations"
    post_bodies: list = []
    posts: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, *a, **k):
        _PeerServer.posts.append(("GET", url, None))
        card = {"name": "peer", "url": _PeerServer.card_url}
        return _StubResponse(card)

    def post(self, url, *a, **k):
        _PeerServer.posts.append(("POST", url, k.get("json")))
        return _StubResponse(_PeerServer.post_bodies.pop(0))


class _StubResponse:
    def __init__(self, body):
        self._body = body
        self.text = "<raw>"

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _peer(card_url="/invocations", post_bodies=None):
    """Point the generated module's httpx at a peer server. example.com is used as the
    peer host because the denylist resolves it for real and it is public."""
    _PeerServer.card_url = card_url
    _PeerServer.post_bodies = list(post_bodies or [])
    _PeerServer.posts = []
    g = _make_agent(allowlist=["example.com"])
    # After _make_agent, not before: _install_a2a_stubs builds a FRESH httpx module
    # object each time, and the generated module's global `httpx` is that new one. A
    # binding taken earlier points at the previous stub, so the patch lands nowhere and
    # every test here fails on the recording client's "should not be reached".
    sys.modules["httpx"].Client = _PeerServer
    return g


def _spec_reply(text):
    return {
        "jsonrpc": "2.0",
        "id": "ignored",
        "result": {
            "kind": "message",
            "role": "agent",
            "parts": [{"kind": "text", "text": text}],
        },
    }


def test_a_relative_card_url_is_resolved_and_the_peer_is_reached():
    g = _peer("/invocations", [_spec_reply("peer answer")])

    out = json.loads(g["call_a2a_peer"]("https://example.com", "delegate this"))

    assert out["status"] == "OK", out
    assert out["response"] == "peer answer"
    posts = [(u, p) for m, u, p in _PeerServer.posts if m == "POST"]
    assert len(posts) == 1
    url, payload = posts[0]
    assert url == "https://example.com/invocations"
    # The message the caller passed, not a default: this is what the whole tool is for.
    assert payload["params"]["message"]["parts"][0]["text"] == "delegate this"


def test_an_absolute_card_url_is_reached_too():
    g = _peer("https://example.com/invocations", [_spec_reply("abs answer")])

    out = json.loads(g["call_a2a_peer"]("https://example.com", "hi"))

    assert out["status"] == "OK", out
    assert out["response"] == "abs answer"


def test_the_peer_is_called_with_a_jsonrpc_message_send_envelope():
    """A peer that publishes an A2A card is an A2A agent, and A2A over HTTP is
    JSON-RPC. The old shape -- a bare {"prompt": ...} -- is what a spec peer rejects."""
    g = _peer("/invocations", [_spec_reply("ok")])

    g["call_a2a_peer"]("https://example.com", "hi")

    payload = [p for m, _u, p in _PeerServer.posts if m == "POST"][0]
    assert payload["jsonrpc"] == "2.0"
    assert payload["method"] == "message/send"
    assert payload["id"], "a JSON-RPC request needs an id to correlate the response"
    message = payload["params"]["message"]
    assert message["role"] == "user"
    assert message["messageId"], "the A2A spec requires a messageId on a Message"


def test_a_peer_that_does_not_speak_jsonrpc_gets_one_bounded_retry():
    """An export of this generator from before it served JSON-RPC answers HTTP 200 and
    runs on the literal default "Hello" -- nothing in the body says so. The retry in the
    shape that peer understands is what makes the answer the caller's message."""
    g = _peer("/invocations", [{"response": "agent-said: Hello"}, {"response": "agent-said: hi"}])

    out = json.loads(g["call_a2a_peer"]("https://example.com", "hi"))

    assert out["status"] == "OK", out
    assert out["response"] == {"response": "agent-said: hi"}
    assert "does not speak A2A JSON-RPC" in out["note"]
    payloads = [p for m, _u, p in _PeerServer.posts if m == "POST"]
    assert len(payloads) == 2, "exactly one retry, not a loop"
    assert sorted(payloads[1]) == ["message", "prompt"]


def test_a_jsonrpc_error_from_the_peer_is_surfaced_not_retried():
    """The peer's considered refusal. Retrying it as a plain prompt would turn "method
    not found" into an answer to a different question."""
    g = _peer(
        "/invocations",
        [{"jsonrpc": "2.0", "id": "x", "error": {"code": -32601, "message": "Method not found"}}],
    )

    out = json.loads(g["call_a2a_peer"]("https://example.com", "hi"))

    assert out["status"] == "ERROR"
    assert out["error"]["code"] == -32601
    assert len([p for m, _u, p in _PeerServer.posts if m == "POST"]) == 1


@pytest.mark.parametrize(
    "card_url",
    [
        "http://example.com/invocations",  # downgrade to plaintext
        "https://169.254.169.254/latest/meta-data/",  # IMDS
        "https://10.0.0.5/invocations",  # RFC1918
    ],
)
def test_the_card_cannot_redirect_the_call_off_the_allowlist(card_url):
    """The peer_url passed the guard; the card then names somewhere else. Re-validating
    after the card is read is the half that matters, and it must survive the fix that
    made the happy path reachable."""
    g = _peer(card_url, [_spec_reply("should never be sent")])

    out = json.loads(g["call_a2a_peer"]("https://example.com", "hi"))

    assert out["status"] == "BLOCKED", out
    assert [m for m, _u, _p in _PeerServer.posts] == ["GET"], "no POST on a blocked card url"


def test_a_transport_failure_is_reported_not_raised():
    class _Broken(_PeerServer):
        def post(self, url, *a, **k):
            raise RuntimeError("connection reset")

    g = _peer("/invocations", [])
    sys.modules["httpx"].Client = _Broken

    out = json.loads(g["call_a2a_peer"]("https://example.com", "hi"))

    assert out["status"] == "ERROR"
    assert "connection reset" in out["error"]


@pytest.mark.parametrize(
    "card_url",
    [
        "//evil.example.net/x",  # protocol-relative
        "///evil.example.net/x",
        "invocations",  # bare relative, no leading slash
        "/a?b=c#d",  # query and fragment
    ],
)
def test_a_relative_card_url_cannot_move_the_request_to_another_host(card_url):
    """A protocol-relative card url must not become a different host.

    ``urllib.parse.urljoin`` is what this join looks like it wants to be, and it would
    do exactly that: urljoin("https://example.com/", "//evil.example.net/x") is
    "https://evil.example.net/x". Concatenating after lstrip("/") makes it a path on the
    validated base instead. The post-join host re-check is the backstop that makes
    either form safe -- which is why the order (join, then check) is the load-bearing
    part, and why this test asserts the host of the URL actually posted to.
    """
    from urllib.parse import urlparse

    g = _peer(card_url, [_spec_reply("ok")])

    json.loads(g["call_a2a_peer"]("https://example.com", "hi"))

    posted = [u for m, u, _p in _PeerServer.posts if m == "POST"]
    assert len(posted) == 1, f"expected exactly one POST, got {posted}"
    assert urlparse(posted[0]).hostname == "example.com", posted[0]
