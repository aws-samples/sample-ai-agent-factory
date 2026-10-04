"""Generated Browser tools must navigate, not merely open a session.

``BrowserClient`` exposes a signed Chrome DevTools Protocol WebSocket. It has no
``invoke("navigateAndExtract", ...)`` method, and returning the WebSocket URL to
the model does not browse anything. These tests execute the Browser tool emitted
by both generators against a CDP double and prove that it navigates, extracts
page content, and never returns signed connection headers in either success or
failure output.
"""

from __future__ import annotations

import ast
import ipaddress
import json
import socket
import urllib.parse
from contextlib import contextmanager

import pytest
from app.models.components import RuntimeConfiguration
from app.models.deployment_models import RuntimeConfig
from app.services.code_generator import generate_agent_code
from app.services.deployment import generate_unified_agent_code

_SECRET = "AWS4-HMAC-SHA256 Credential=secret-never-return"


class _AppTool:
    def __call__(self, fn):
        return fn


class _BrowserClient:
    session_id = "browser-session"

    @staticmethod
    def generate_ws_headers():
        return (
            "wss://bedrock-agentcore.example/browser-streams/default/sessions/one/automation",
            {
                "Host": "bedrock-agentcore.example",
                "Authorization": _SECRET,
                "X-Amz-Date": "20260922T190000Z",
                "X-Amz-Security-Token": "temporary-token",
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Key": "generated-by-sdk",
            },
        )


class _CdpSocket:
    def __init__(self):
        self.commands: list[dict] = []
        self._responses: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def send(self, raw: str):
        command = json.loads(raw)
        self.commands.append(command)
        method = command["method"]
        if method == "Target.getTargets":
            result = {
                "targetInfos": [
                    {
                        "targetId": "page-1",
                        "type": "page",
                        "url": "about:blank",
                    }
                ]
            }
        elif method == "Target.attachToTarget":
            result = {"sessionId": "cdp-session"}
        elif method == "Page.navigate":
            result = {"frameId": "frame-1"}
        elif method == "Runtime.evaluate":
            expression = command.get("params", {}).get("expression", "")
            value = (
                "complete"
                if "document.readyState" in expression
                else {
                    "title": "Example Domain",
                    "url": "https://example.com/",
                    "text": "Example Domain\nThis domain is for use in examples.",
                }
            )
            result = {"result": {"type": "object", "value": value}}
        else:
            result = {}
        self._responses.append(json.dumps({"id": command["id"], "result": result}))

    def recv(self, timeout=None):
        assert timeout is not None
        return self._responses.pop(0)


def _sources():
    yield generate_agent_code(
        config=RuntimeConfig(
            name="browser_probe",
            model={"modelId": "us.anthropic.claude-sonnet-5"},
            systemPrompt="Browse when asked.",
        ),
        tools=["browser"],
        portable=True,
    )
    yield generate_unified_agent_code(
        RuntimeConfiguration(
            name="browser_probe",
            model={"model_id": "us.anthropic.claude-sonnet-5"},
            system_prompt="Browse when asked.",
        ),
        connected_tools=["browser"],
        region="us-east-1",
    )


def _lift_browser_tool(source: str, connect):
    """Lift ``browse_web`` plus every top-level helper and constant it reaches."""
    tree = ast.parse(source)
    bindings = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node
    for supplied in (
        "REGION",
        "browser_session",
        "ipaddress",
        "json",
        "socket",
        "time",
        "tool",
        "urllib",
        "_ws_connect",
    ):
        bindings.pop(supplied, None)
    required, selected = {"browse_web"}, set()
    while True:
        before = set(required)
        for name in list(required):
            if name in bindings:
                selected.add(id(bindings[name]))
                required.update(n.id for n in ast.walk(bindings[name]) if isinstance(n, ast.Name))
        if required == before:
            break
    nodes = [node for node in tree.body if id(node) in selected]

    @contextmanager
    def _browser_session(_region):
        yield _BrowserClient()

    namespace = {
        "REGION": "us-east-1",
        "browser_session": _browser_session,
        "ipaddress": ipaddress,
        "json": json,
        "socket": socket,
        "time": __import__("time"),
        "tool": _AppTool(),
        "urllib": urllib,
        "_ws_connect": connect,
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
            "<generated-browser-tool>",
            "exec",
        ),
        namespace,
    )
    return namespace["browse_web"]


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """The boundary resolves every host; answer publicly without touching the network."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))],
    )


@pytest.mark.parametrize("source", list(_sources()), ids=["step-codegen", "unified-codegen"])
def test_generated_browser_navigates_and_extracts_content(source):
    compile(source, "<generated-agent>", "exec")
    socket = _CdpSocket()
    connection_args: dict = {}

    def _connect(url, **kwargs):
        connection_args.update({"url": url, **kwargs})
        return socket

    browse = _lift_browser_tool(source, _connect)
    result = json.loads(browse("https://example.com", "Read the page"))

    assert result == {
        "title": "Example Domain",
        "url": "https://example.com/",
        "text": "Example Domain\nThis domain is for use in examples.",
        "task": "Read the page",
    }
    methods = [command["method"] for command in socket.commands]
    assert "Page.navigate" in methods
    assert "Runtime.evaluate" in methods
    assert connection_args["proxy"] is None
    assert connection_args["additional_headers"] == {
        "Authorization": _SECRET,
        "X-Amz-Date": "20260922T190000Z",
        "X-Amz-Security-Token": "temporary-token",
    }
    assert _SECRET not in json.dumps(result)


@pytest.mark.parametrize("source", list(_sources()), ids=["step-codegen", "unified-codegen"])
def test_generated_browser_error_does_not_expose_signed_connection_details(source):
    def _connect(_url, **_kwargs):
        raise RuntimeError(f"connection rejected; headers={_SECRET}")

    browse = _lift_browser_tool(source, _connect)
    result = json.loads(browse("https://example.com", "Read the page"))

    assert result["error"] == "Browser navigation failed"
    assert result["url_requested"] == "https://example.com"
    assert _SECRET not in json.dumps(result)


def _lift_helpers(source: str, names: tuple[str, ...]) -> dict:
    tree = ast.parse(source)
    keep = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name.startswith(("_browser_", "_cdp_")))
        or (isinstance(node, ast.Assign) and any(getattr(t, "id", "").startswith("_BROWSER_") for t in node.targets))
    ]
    namespace = {"ipaddress": ipaddress, "json": json, "socket": socket, "urllib": urllib}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<browser-helpers>", "exec"), namespace)
    return {name: namespace[name] for name in names}


@pytest.mark.parametrize("source", list(_sources()), ids=["step-codegen", "unified-codegen"])
@pytest.mark.parametrize(
    "literal",
    [
        "::ffff:169.254.169.254",  # IPv4-mapped IMDS
        "64:ff9b::a9fe:a9fe",  # NAT64 IMDS
        "2002:0a00:0001::1",  # 6to4 wrapping 10.0.0.1
        "100.64.0.1",  # carrier-grade NAT, not private by is_private but not global
        "0.0.0.0",
        "::1",
        "224.0.0.1",
        "fe80::1",
    ],
)
def test_embedded_and_non_global_addresses_are_refused(source, literal):
    reason = _lift_helpers(source, ("_browser_host_block_reason",))["_browser_host_block_reason"]

    assert reason(literal) is not None


@pytest.mark.parametrize("source", list(_sources()), ids=["step-codegen", "unified-codegen"])
@pytest.mark.parametrize(
    ("url", "blocked"),
    [
        ("https://example.com/a", False),
        ("data:text/plain,hi", False),
        ("blob:https://example.com/x", False),
        ("file:///etc/passwd", True),
        ("ftp://example.com/", True),
        ("http://user:pw@example.com/", True),
        ("http://[::1/", True),  # unparseable
    ],
)
def test_the_per_request_url_rule(source, url, blocked):
    rule = _lift_helpers(source, ("_browser_url_block_reason",))["_browser_url_block_reason"]

    assert (rule(url) is not None) is blocked


class _AckingWs:
    """Replies to every command in order; ``fail`` names methods whose reply is a CDP error."""

    def __init__(self, fail=(), prelude=()):
        self.sent: list[dict] = []
        self.inbox: list[dict] = list(prelude)
        self.fail = set(fail)

    def send(self, raw):
        command = json.loads(raw)
        self.sent.append(command)
        if command["method"] in self.fail:
            self.inbox.append({"id": command["id"], "error": {"code": -32000, "message": "no"}})
        else:
            self.inbox.append({"id": command["id"], "result": {}})

    def recv(self, timeout=None):
        return json.dumps(self.inbox.pop(0))


def _state():
    return {"next_id": 0, "blocked": 0, "replies": {}, "ignored": set()}


_CHILD_ATTACHED = {"method": "Target.attachedToTarget", "params": {"sessionId": "child"}}


@pytest.mark.parametrize("source", list(_sources()), ids=["step-codegen", "unified-codegen"])
def test_a_child_target_is_intercepted_before_it_is_released(source):
    """A frame or worker starts paused; interception must be confirmed before it runs."""
    handle = _lift_helpers(source, ("_cdp_handle_event",))["_cdp_handle_event"]
    ws = _AckingWs()

    handle(ws, _state(), _CHILD_ATTACHED)

    methods = [(m["method"], m.get("sessionId")) for m in ws.sent]
    assert methods == [
        ("Fetch.enable", "child"),
        ("Target.setAutoAttach", "child"),
        ("Runtime.runIfWaitingForDebugger", "child"),
    ]
    assert len({m["id"] for m in ws.sent}) == len(ws.sent)


@pytest.mark.parametrize("source", list(_sources()), ids=["step-codegen", "unified-codegen"])
@pytest.mark.parametrize("method", ["Fetch.enable", "Target.setAutoAttach"])
def test_a_child_whose_boundary_is_rejected_is_never_released(source, method):
    handle = _lift_helpers(source, ("_cdp_handle_event",))["_cdp_handle_event"]
    ws = _AckingWs(fail={method})

    with pytest.raises(RuntimeError, match=method):
        handle(ws, _state(), _CHILD_ATTACHED)

    assert "Runtime.runIfWaitingForDebugger" not in {m["method"] for m in ws.sent}


@pytest.mark.parametrize("source", list(_sources()), ids=["step-codegen", "unified-codegen"])
def test_an_outer_reply_that_arrives_during_nested_child_setup_is_not_lost(source):
    """The outer call's error reply lands while the child setup is pumping; it must still raise."""
    helpers = _lift_helpers(source, ("_cdp_call",))
    state = _state()

    class _Ws(_AckingWs):
        def send(self, raw):
            command = json.loads(raw)
            self.sent.append(command)
            if command["method"] == "Page.navigate":
                # The child attaches first; the navigate ERROR arrives before the
                # child's own setup replies.
                self.inbox.append(_CHILD_ATTACHED)
                self.inbox.append({"id": command["id"], "error": {"code": -32000, "message": "x"}})
            else:
                self.inbox.append({"id": command["id"], "result": {}})

    ws = _Ws()
    with pytest.raises(RuntimeError, match="Page.navigate"):
        helpers["_cdp_call"](ws, state, "Page.navigate", {"url": "https://example.com/"}, "page")

    assert state["replies"] == {}  # nothing buffered is left unclaimed
    # Only the fire-and-forget release is still outstanding; its reply is simply unread.
    release = next(m["id"] for m in ws.sent if m["method"] == "Runtime.runIfWaitingForDebugger")
    assert state["ignored"] == {release}
