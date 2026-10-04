"""The generated Browser tool enforces a public-network boundary on every hop.

Validating only the URL supplied by the model is insufficient: a public-looking
hostname can resolve to a private address, and a public page can redirect or load
a subresource from IMDS, loopback, or a VPC endpoint. These tests execute both
generated Browser variants against a CDP double and require the boundary to be
applied before the initial connection and to every intercepted browser request.
"""

from __future__ import annotations

import ast
import ipaddress
import json
import socket
import time
import urllib
from contextlib import contextmanager

import pytest
from app.models.components import RuntimeConfiguration
from app.models.deployment_models import RuntimeConfig
from app.services.code_generator import generate_agent_code
from app.services.deployment import generate_unified_agent_code

_SIGNED_AUTHORIZATION = "AWS4-HMAC-SHA256 Credential=must-never-be-returned"
_PUBLIC_IP = "93.184.216.34"


class _AppTool:
    def __call__(self, function):
        return function


class _BrowserClient:
    @staticmethod
    def generate_ws_headers():
        return (
            "wss://bedrock-agentcore.example/browser-streams/default/sessions/one/automation",
            {
                "Authorization": _SIGNED_AUTHORIZATION,
                "X-Amz-Date": "20260922T190000Z",
                "X-Amz-Security-Token": "temporary-token",
            },
        )


class _CdpSocket:
    """Small CDP peer that can pause document, redirect, and subresource requests."""

    def __init__(
        self,
        paused_requests: list[dict] | None = None,
        method_errors: dict[str, dict] | None = None,
    ):
        self.commands: list[dict] = []
        self._responses: list[str] = []
        self._paused_requests = list(paused_requests or [])
        self._method_errors = dict(method_errors or {})

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def send(self, raw: str):
        command = json.loads(raw)
        self.commands.append(command)
        method = command["method"]

        if method in self._method_errors:
            self._responses.append(
                json.dumps(
                    {
                        "id": command["id"],
                        "error": dict(self._method_errors[method]),
                    }
                )
            )
            return

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
            for paused in self._paused_requests:
                self._responses.append(
                    json.dumps(
                        {
                            "method": "Fetch.requestPaused",
                            "sessionId": "cdp-session",
                            "params": {
                                "requestId": paused["requestId"],
                                "request": {"url": paused["url"]},
                                "resourceType": paused.get("resourceType", "Document"),
                            },
                        }
                    )
                )
            result = {"frameId": "frame-1"}
        elif method == "Runtime.evaluate":
            expression = command.get("params", {}).get("expression", "")
            value = (
                "complete"
                if "document.readyState" in expression
                else {
                    "title": "Public page",
                    "url": "https://example.com/",
                    "text": "Public content",
                }
            )
            result = {"result": {"type": "object", "value": value}}
        else:
            result = {}

        self._responses.append(json.dumps({"id": command["id"], "result": result}))

    def recv(self, timeout=None):
        assert timeout is not None
        assert self._responses, "generated Browser code waited for a CDP message that was never requested"
        return self._responses.pop(0)


class _Resolver:
    def __init__(self, answers: dict[str, list[str] | Exception]):
        self.answers = answers
        self.hosts: list[str] = []

    def __call__(self, host, port, *_args, **_kwargs):
        normalized = str(host).lower().rstrip(".")
        self.hosts.append(normalized)
        answer = self.answers.get(normalized, [_PUBLIC_IP])
        if isinstance(answer, Exception):
            raise answer

        rows = []
        for address in answer:
            parsed = ipaddress.ip_address(address)
            if parsed.version == 6:
                rows.append(
                    (
                        socket.AF_INET6,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        (address, port, 0, 0),
                    )
                )
            else:
                rows.append(
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        (address, port),
                    )
                )
        return rows


def _sources():
    yield generate_agent_code(
        config=RuntimeConfig(
            name="browser_boundary_probe",
            model={"modelId": "us.anthropic.claude-sonnet-5"},
            systemPrompt="Browse when asked.",
        ),
        tools=["browser"],
        portable=True,
    )
    yield generate_unified_agent_code(
        RuntimeConfiguration(
            name="browser_boundary_probe",
            model={"model_id": "us.anthropic.claude-sonnet-5"},
            system_prompt="Browse when asked.",
        ),
        connected_tools=["browser"],
        region="us-east-1",
    )


def _top_level_bindings(tree: ast.Module) -> dict[str, ast.AST]:
    bindings: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node
    return bindings


def _browser_dependency_nodes(source: str) -> list[ast.AST]:
    """Lift browse_web plus whichever generated helpers/constants it references."""
    tree = ast.parse(source)
    bindings = _top_level_bindings(tree)
    # These are deliberately supplied by the controlled execution namespace.
    # Pulling the generated REGION assignment into the lifted fragment would
    # also pull unrelated module initialization through its ``os`` dependency.
    for external_name in (
        "REGION",
        "_socket",
        "_ws_connect",
        "browser_session",
        "ipaddress",
        "json",
        "socket",
        "time",
        "tool",
        "urllib",
    ):
        bindings.pop(external_name, None)
    required = {"browse_web"}
    selected_ids: set[int] = set()

    while True:
        before = set(required)
        for name in list(required):
            node = bindings.get(name)
            if node is None:
                continue
            selected_ids.add(id(node))
            required.update(
                child.id for child in ast.walk(node) if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
            )
        if required == before:
            break

    nodes = [node for node in tree.body if id(node) in selected_ids]
    assert any(isinstance(node, ast.FunctionDef) and node.name == "browse_web" for node in nodes)
    return nodes


def _lift_browser_tool(source: str, connect):
    @contextmanager
    def _browser_session(_region):
        yield _BrowserClient()

    namespace = {
        "REGION": "us-east-1",
        "_socket": socket,
        "_ws_connect": connect,
        "browser_session": _browser_session,
        "ipaddress": ipaddress,
        "json": json,
        "socket": socket,
        "time": time,
        "tool": _AppTool(),
        "urllib": urllib,
    }
    exec(
        compile(
            ast.fix_missing_locations(
                ast.Module(
                    body=_browser_dependency_nodes(source),
                    type_ignores=[],
                )
            ),
            "<generated-browser-network-boundary>",
            "exec",
        ),
        namespace,
    )
    return namespace["browse_web"]


def _method_commands(socket_double: _CdpSocket, method: str) -> list[dict]:
    return [command for command in socket_double.commands if command["method"] == method]


def _assert_private_refusal(result: dict):
    assert "error" in result
    message = str(result["error"]).lower()
    assert any(word in message for word in ("private", "internal", "local", "blocked"))
    assert _SIGNED_AUTHORIZATION not in json.dumps(result)


@pytest.mark.parametrize("source", list(_sources()), ids=["step-codegen", "unified-codegen"])
@pytest.mark.parametrize(
    "answers",
    [
        pytest.param(["10.0.0.7"], id="private-a"),
        pytest.param(["fd00::7"], id="private-aaaa"),
        pytest.param([_PUBLIC_IP, "169.254.169.254"], id="mixed-public-and-imds"),
    ],
)
def test_initial_hostname_must_resolve_only_to_public_addresses(
    source,
    answers,
    monkeypatch,
):
    resolver = _Resolver({"public-looking.example": answers})
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    connection_attempts = []

    def _connect(*args, **kwargs):
        connection_attempts.append((args, kwargs))
        return _CdpSocket()

    browse = _lift_browser_tool(source, _connect)
    result = json.loads(browse("https://public-looking.example/data"))

    _assert_private_refusal(result)
    assert resolver.hosts == ["public-looking.example"]
    assert connection_attempts == [], "the managed Browser must not start after DNS resolves private"


@pytest.mark.parametrize("source", list(_sources()), ids=["step-codegen", "unified-codegen"])
def test_initial_dns_failure_is_fail_closed(source, monkeypatch):
    resolver = _Resolver({"unresolved.example": socket.gaierror("name does not resolve")})
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    connection_attempts = []

    def _connect(*args, **kwargs):
        connection_attempts.append((args, kwargs))
        return _CdpSocket()

    browse = _lift_browser_tool(source, _connect)
    result = json.loads(browse("https://unresolved.example/data"))

    assert "error" in result
    assert connection_attempts == []
    assert _SIGNED_AUTHORIZATION not in json.dumps(result)


@pytest.mark.parametrize("source", list(_sources()), ids=["step-codegen", "unified-codegen"])
def test_every_redirect_and_subresource_is_intercepted_and_revalidated(
    source,
    monkeypatch,
):
    resolver = _Resolver(
        {
            "example.com": [_PUBLIC_IP],
            "public-cdn.example": ["1.1.1.1"],
            "private-redirect.example": ["127.0.0.1"],
            "private-assets.example": ["169.254.169.254"],
        }
    )
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    paused = [
        {
            "requestId": "public-document",
            "url": "https://example.com/",
            "resourceType": "Document",
        },
        {
            "requestId": "private-redirect",
            "url": "http://private-redirect.example/admin",
            "resourceType": "Document",
        },
        {
            "requestId": "private-subresource",
            "url": "http://private-assets.example/latest/meta-data/",
            "resourceType": "Image",
        },
        {
            "requestId": "public-subresource",
            "url": "https://public-cdn.example/app.js",
            "resourceType": "Script",
        },
    ]
    cdp = _CdpSocket(paused)
    browse = _lift_browser_tool(source, lambda *_args, **_kwargs: cdp)
    result = json.loads(browse("https://example.com/"))

    methods = [command["method"] for command in cdp.commands]
    assert "Fetch.enable" in methods
    assert methods.index("Fetch.enable") < methods.index("Page.navigate")

    fetch_enable = _method_commands(cdp, "Fetch.enable")[0]
    patterns = fetch_enable.get("params", {}).get("patterns")
    if patterns:
        assert all("resourceType" not in pattern for pattern in patterns)
        url_patterns = {pattern.get("urlPattern", "*") for pattern in patterns}
        assert "*" in url_patterns or {
            "http://*",
            "https://*",
        }.issubset(url_patterns)

    continued = {
        command.get("params", {}).get("requestId") for command in _method_commands(cdp, "Fetch.continueRequest")
    }
    failed = {command.get("params", {}).get("requestId") for command in _method_commands(cdp, "Fetch.failRequest")}
    assert {"public-document", "public-subresource"} <= continued
    assert {"private-redirect", "private-subresource"} <= failed
    assert not (continued & failed)
    assert {
        "example.com",
        "public-cdn.example",
        "private-redirect.example",
        "private-assets.example",
    } <= set(resolver.hosts)
    assert _SIGNED_AUTHORIZATION not in json.dumps(result)


@pytest.mark.parametrize("source", list(_sources()), ids=["step-codegen", "unified-codegen"])
def test_a_second_dns_answer_cannot_rebind_an_intercepted_request_private(
    source,
    monkeypatch,
):
    answers = iter([[_PUBLIC_IP], ["127.0.0.1"]])
    seen = []

    def _rebind(host, port, *_args, **_kwargs):
        seen.append(str(host).lower().rstrip("."))
        address = next(answers)[0]
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _rebind)
    cdp = _CdpSocket(
        [
            {
                "requestId": "rebound-document",
                "url": "https://rebind.example/",
                "resourceType": "Document",
            }
        ]
    )
    browse = _lift_browser_tool(source, lambda *_args, **_kwargs: cdp)
    result = json.loads(browse("https://rebind.example/"))

    failed = {command.get("params", {}).get("requestId") for command in _method_commands(cdp, "Fetch.failRequest")}
    assert "rebound-document" in failed
    assert seen == ["rebind.example", "rebind.example"]
    assert _SIGNED_AUTHORIZATION not in json.dumps(result)


@pytest.mark.parametrize("source", list(_sources()), ids=["step-codegen", "unified-codegen"])
@pytest.mark.parametrize(
    "security_method",
    [
        pytest.param("Fetch.enable", id="request-interception"),
        pytest.param("Target.setAutoAttach", id="child-target-interception"),
    ],
)
def test_browser_refuses_to_navigate_when_its_network_boundary_cannot_be_enabled(
    source,
    security_method,
    monkeypatch,
):
    """A rejected security command cannot be treated like an ignorable async reply."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _Resolver({"example.com": [_PUBLIC_IP]}),
    )
    cdp = _CdpSocket(
        method_errors={
            security_method: {
                "code": -32601,
                "message": "security command unavailable",
            }
        }
    )
    browse = _lift_browser_tool(source, lambda *_args, **_kwargs: cdp)

    result = json.loads(browse("https://example.com/"))

    assert result["error"] == "Browser navigation failed"
    assert "Page.navigate" not in {command["method"] for command in cdp.commands}, (
        "no request may leave after the Browser rejected a boundary command"
    )
    assert _SIGNED_AUTHORIZATION not in json.dumps(result)
