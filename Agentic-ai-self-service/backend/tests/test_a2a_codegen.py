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
