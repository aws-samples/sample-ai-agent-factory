"""Protocol and secret-handling tests for scripts/verify-mcp-protocol.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def _load_script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "verify-mcp-protocol.py"
    spec = importlib.util.spec_from_file_location("verify_mcp_protocol", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def verifier():
    return _load_script()


@pytest.fixture(autouse=True)
def protocol_version_env(monkeypatch):
    monkeypatch.setenv(
        "MCP_VERIFY_PROTOCOL_VERSIONS_JSON",
        '["2025-03-26"]',
    )


def _response(verifier, status=200, *, session=None, body=b""):
    headers = {"mcp-session-id": session} if session else {}
    return verifier.HttpResponse(status=status, headers=headers, body=body)


def _tool(name: str, *, header: bool = False):
    tenant = {"type": "string"}
    if header:
        tenant["x-mcp-header"] = "X-Tenant"
    return {
        "name": name,
        "description": "An inert probe",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "tenant": tenant,
            },
        },
    }


def test_rpc_body_accepts_json_and_selects_the_matching_sse_event(verifier):
    message = {"jsonrpc": "2.0", "id": 1, "result": {}}
    notification = {"jsonrpc": "2.0", "method": "notifications/progress"}

    assert verifier._parse_rpc_body(json.dumps(message).encode()) == message
    stream = (
        f"event: message\ndata: {json.dumps(notification)}\n\nevent: message\ndata: {json.dumps(message)}\n\n"
    ).encode()
    assert verifier._parse_rpc_body(stream, expected_id=1) == message


def test_http_keeps_bearer_secret_out_of_process_arguments_and_disables_curlrc(
    verifier,
    monkeypatch,
):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["timeout"] = kwargs["timeout"]
        request_headers = Path(args[args.index("--header") + 1][1:])
        seen["headers"] = request_headers.read_text()
        seen["mode"] = request_headers.stat().st_mode & 0o777
        Path(args[args.index("--dump-header") + 1]).write_text("HTTP/2 200\r\nMcp-Session-Id: session-1\r\n\r\n")
        Path(args[args.index("--output") + 1]).write_bytes(b"{}")
        return subprocess.CompletedProcess(args, 0, stdout="200", stderr="")

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)
    response = verifier._http(
        "POST",
        "https://example.com/mcp",
        {"Authorization": "Bearer super-secret-token"},
        b"{}",
    )

    assert "super-secret-token" not in " ".join(seen["args"])
    assert "Bearer super-secret-token" in seen["headers"]
    assert seen["args"][1] == "--disable"
    assert "--max-filesize" in seen["args"]
    assert seen["mode"] == 0o600
    assert seen["timeout"] == 50
    assert response.headers["mcp-session-id"] == "session-1"


def test_current_rpc_emits_namespaced_meta_and_mirrored_routing_headers(
    verifier,
    monkeypatch,
):
    captured = {}
    tool_name = "target name/with spaces"
    tool = {
        "name": tool_name,
        "inputSchema": {
            "type": "object",
            "properties": {
                "tenant": {
                    "type": "string",
                    "x-mcp-header": "X-Tenant",
                },
                "context": {
                    "type": "object",
                    "properties": {
                        "locale": {
                            "type": "string",
                            "x-mcp-header": "X-Locale",
                        }
                    },
                },
            },
        },
    }

    def fake_http(method, url, headers, body=None, **_kwargs):
        captured.update(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": json.loads(body),
            }
        )
        response = {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {
                "resultType": "complete",
                "content": [{"type": "text", "text": "ok"}],
            },
        }
        return _response(verifier, body=json.dumps(response).encode())

    monkeypatch.setattr(verifier, "_http", fake_http)
    response, message = verifier._rpc(
        "https://example.com/mcp",
        "token",
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {
                    "tenant": "tenant-a",
                    "context": {"locale": "日本語"},
                },
            },
        },
        protocol_version="2026-07-28",
        tool=tool,
    )

    assert response.status == 200
    assert message and message["id"] == 7
    assert captured["headers"]["MCP-Protocol-Version"] == "2026-07-28"
    assert captured["headers"]["Mcp-Method"] == "tools/call"
    assert captured["headers"]["Mcp-Name"] == "target%20name%2Fwith%20spaces"
    assert captured["headers"]["Mcp-Param-X-Tenant"] == "tenant-a"
    assert captured["headers"]["Mcp-Param-X-Locale"].startswith("=?UTF-8?B?")
    meta = captured["payload"]["params"]["_meta"]
    assert meta == {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {
            "name": "agentcore-flows-production-verifier",
            "version": "2.0",
        },
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def test_full_legacy_verifier_sequences_auth_session_tools_and_termination(
    verifier,
    monkeypatch,
):
    tool_name = "MCPServerRuntime___probe"
    monkeypatch.setenv("MCP_VERIFY_URL", "https://example.com/mcp")
    monkeypatch.setenv("MCP_VERIFY_BEARER_TOKEN", "real-token")
    monkeypatch.setenv(
        "MCP_VERIFY_CALLS_JSON",
        json.dumps(
            [
                {
                    "name": tool_name,
                    "arguments": {"message": "ping"},
                    "expectContains": "probe-ok",
                }
            ]
        ),
    )
    monkeypatch.setenv("MCP_VERIFY_EXPECT_TOOLS_JSON", json.dumps([tool_name]))
    calls = []
    closed = []

    def fake_rpc(
        _url,
        token,
        payload,
        *,
        protocol_version,
        session_id=None,
        tool=None,
        header_overrides=None,
    ):
        calls.append(
            (
                token,
                payload,
                session_id,
                protocol_version,
                tool,
                header_overrides,
            )
        )
        method = payload["method"]
        request_id = payload.get("id")
        if token is None:
            return _response(verifier, 401), None
        if token != "real-token":
            return _response(verifier, 403), None
        if method == "initialize":
            return _response(verifier, session="session-1"), {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "serverInfo": {
                        "name": "test-server",
                        "version": "1.0",
                    },
                },
            }
        if method == "notifications/initialized":
            return _response(verifier, 202), None
        if method == "tools/list":
            if session_id and (session_id != "session-1" or closed):
                return _response(verifier, 404), None
            return _response(verifier), {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": [_tool(tool_name)]},
            }
        if payload["params"]["name"] == tool_name:
            return _response(verifier), {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "probe-ok",
                        }
                    ]
                },
            }
        return _response(verifier), {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32602,
                "message": "Unknown tool",
            },
        }

    def fake_http(method, url, headers, body=None, **_kwargs):
        closed.append((method, url, headers, body))
        return _response(verifier, 204)

    monkeypatch.setattr(verifier, "_rpc", fake_rpc)
    monkeypatch.setattr(verifier, "_http", fake_http)

    verifier.verify()

    assert all(call[3] == "2025-03-26" for call in calls)
    assert all(call[2] == "session-1" for call in calls[3:8])
    assert calls[8][2].startswith("invalid-")
    assert calls[9][2] == "session-1"
    assert calls[10][2] == "session-1"
    assert [call[1]["method"] for call in calls] == [
        "initialize",
        "initialize",
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/list",
        "tools/list",
        "tools/call",
        "tools/list",
        "tools/call",
        "tools/list",
    ]
    assert closed and closed[0][0] == "DELETE"


def test_full_current_verifier_sequences_discovery_routing_and_tools(
    verifier,
    monkeypatch,
):
    version = "2026-07-28"
    tool_name = "current___probe"
    monkeypatch.setenv("MCP_VERIFY_URL", "https://example.com/mcp")
    monkeypatch.setenv("MCP_VERIFY_BEARER_TOKEN", "real-token")
    monkeypatch.setenv("MCP_VERIFY_PROTOCOL_VERSIONS_JSON", json.dumps([version]))
    monkeypatch.setenv(
        "MCP_VERIFY_CALLS_JSON",
        json.dumps(
            [
                {
                    "name": tool_name,
                    "arguments": {
                        "message": "ping",
                        "tenant": "tenant-a",
                    },
                    "expectContains": "probe-ok",
                }
            ]
        ),
    )
    calls = []

    def fake_rpc(
        _url,
        token,
        payload,
        *,
        protocol_version,
        session_id=None,
        tool=None,
        header_overrides=None,
    ):
        calls.append(
            (
                token,
                payload,
                session_id,
                protocol_version,
                tool,
                header_overrides,
            )
        )
        assert protocol_version == version
        assert session_id is None
        method = payload["method"]
        request_id = payload.get("id")
        if token is None:
            return _response(verifier, 401), None
        if token != "real-token":
            return _response(verifier, 403), None
        if header_overrides:
            return _response(verifier, 400), {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32020,
                    "message": "MCP-Protocol-Error",
                },
            }
        if method == "server/discover":
            return _response(verifier), {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": "complete",
                    "ttlMs": 0,
                    "cacheScope": "private",
                    "supportedVersions": [version, "2025-11-25"],
                    "capabilities": {"tools": {}},
                    "_meta": {
                        "io.modelcontextprotocol/serverInfo": {
                            "name": "test-server",
                            "version": "2.0",
                        }
                    },
                },
            }
        if method == "tools/list":
            return _response(verifier), {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": "complete",
                    "ttlMs": 1000,
                    "cacheScope": "private",
                    "tools": [_tool(tool_name, header=True)],
                },
            }
        if payload["params"]["name"] == tool_name:
            return _response(verifier), {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": "complete",
                    "content": [
                        {
                            "type": "text",
                            "text": "probe-ok",
                        }
                    ],
                },
            }
        return _response(verifier, 404), {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": "Unknown tool",
            },
        }

    monkeypatch.setattr(verifier, "_rpc", fake_rpc)

    verifier.verify()

    methods = [call[1]["method"] for call in calls]
    assert methods == [
        "server/discover",
        "server/discover",
        "server/discover",
        "tools/list",
        "tools/list",
        "tools/call",
        "tools/list",
        "tools/list",
        "tools/list",
        "tools/call",
        "tools/call",
        "tools/call",
    ]
    assert sum(bool(call[5]) for call in calls) == 5


def test_current_discover_accepts_an_absent_optional_server_info(verifier):
    verifier._validate_current_discover_result(
        {
            "resultType": "complete",
            "ttlMs": 0,
            "cacheScope": "private",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {}},
        },
        "2026-07-28",
    )


def test_current_discover_requires_result_type(verifier):
    with pytest.raises(
        verifier.VerificationError,
        match="resultType=complete",
    ):
        verifier._validate_current_discover_result(
            {
                "ttlMs": 0,
                "cacheScope": "private",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}},
                # AgentCore currently returns this non-standard extension but
                # omits the required Result.resultType field.
                "serverInfo": {
                    "name": "agentcore-gateway",
                    "version": "1.0.0",
                },
            },
            "2026-07-28",
        )


def test_current_version_rejects_nonconforming_discover_before_tool_calls(
    verifier,
    monkeypatch,
):
    monkeypatch.setattr(verifier, "_assert_auth_rejected", lambda *_args, **_kwargs: None)

    def fake_rpc(_url, _token, payload, **_kwargs):
        assert payload["method"] == "server/discover"
        return _response(verifier), {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {
                "ttlMs": 0,
                "cacheScope": "private",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}},
            },
        }

    monkeypatch.setattr(verifier, "_rpc", fake_rpc)
    monkeypatch.setattr(
        verifier,
        "_exercise_tools",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a nonconforming discover result reached tool calls")
        ),
    )

    with pytest.raises(verifier.VerificationError, match="resultType=complete"):
        verifier._verify_current_version(
            "https://example.com/mcp",
            "token",
            [{"name": "probe", "arguments": {}, "expectContains": "canary"}],
            ["probe"],
            require_all_tools=True,
            id_base=1000,
        )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("ttlMs", True, "ttlMs"),
        ("cacheScope", "shared", "cacheScope"),
        ("instructions", [], "instructions"),
        ("_meta", [], "result _meta"),
        (
            "_meta",
            {"io.modelcontextprotocol/serverInfo": {"name": "missing-version"}},
            "serverInfo",
        ),
    ],
)
def test_current_discover_rejects_malformed_required_or_present_optional_fields(
    verifier,
    field,
    value,
    error,
):
    result = {
        "resultType": "complete",
        "ttlMs": 0,
        "cacheScope": "private",
        "supportedVersions": ["2026-07-28"],
        "capabilities": {"tools": {}},
    }
    result[field] = value
    with pytest.raises(verifier.VerificationError, match=error):
        verifier._validate_current_discover_result(result, "2026-07-28")


def test_verifier_rejects_an_authentication_probe_that_succeeds(
    verifier,
    monkeypatch,
):
    monkeypatch.setenv("MCP_VERIFY_URL", "https://example.com/mcp")
    monkeypatch.setenv("MCP_VERIFY_BEARER_TOKEN", "real-token")
    monkeypatch.setenv(
        "MCP_VERIFY_CALLS_JSON",
        '[{"name":"probe","arguments":{},"expectContains":"canary"}]',
    )
    monkeypatch.setattr(
        verifier,
        "_rpc",
        lambda *_args, **_kwargs: (
            _response(verifier, 200),
            {
                "jsonrpc": "2.0",
                "id": 9001,
                "result": {},
            },
        ),
    )

    with pytest.raises(verifier.VerificationError, match="expected 401/403"):
        verifier.verify()


def test_http_auth_failure_may_have_a_plain_non_json_body(
    verifier,
    monkeypatch,
):
    monkeypatch.setattr(
        verifier,
        "_http",
        lambda *_args, **_kwargs: verifier.HttpResponse(
            status=401,
            headers={},
            body=b"Unauthorized",
        ),
    )

    response, message = verifier._rpc(
        "https://example.com/mcp",
        None,
        verifier._initialize_payload(1, "2025-03-26"),
        protocol_version="2025-03-26",
    )

    assert response.status == 401
    assert message is None


def test_tool_discovery_follows_every_cursor(verifier, monkeypatch):
    requests = []

    def fake_rpc(
        _url,
        _token,
        payload,
        *,
        protocol_version,
        session_id=None,
        **_kwargs,
    ):
        requests.append((payload, session_id))
        assert protocol_version == "2025-03-26"
        request_id = payload["id"]
        cursor = payload["params"].get("cursor")
        first = {
            "tools": [_tool("one")],
            "nextCursor": "next",
        }
        second = {"tools": [_tool("two")]}
        return _response(verifier), {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": first if cursor is None else second,
        }

    monkeypatch.setattr(verifier, "_rpc", fake_rpc)

    names, next_id = verifier._list_tools(
        "https://example.com/mcp",
        "token",
        "session",
        12,
        protocol_version="2025-03-26",
    )

    assert set(names) == {"one", "two"}
    assert next_id == 14
    assert requests[1][0]["params"] == {"cursor": "next"}


def test_malformed_tool_schema_does_not_count_as_discovery(verifier, monkeypatch):
    monkeypatch.setattr(
        verifier,
        "_rpc",
        lambda *_args, **_kwargs: (
            _response(verifier),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {
                            "name": "broken",
                        }
                    ]
                },
            },
        ),
    )

    with pytest.raises(verifier.VerificationError, match="inputSchema"):
        verifier._list_tools(
            "https://example.com/mcp",
            "token",
            None,
            1,
            protocol_version="2025-03-26",
        )


def test_current_tool_schema_rejects_ambiguous_or_nonprimitive_header_bindings(
    verifier,
):
    duplicate = {
        "name": "duplicate",
        "inputSchema": {
            "type": "object",
            "properties": {
                "one": {
                    "type": "string",
                    "x-mcp-header": "X-Tenant",
                },
                "two": {
                    "type": "string",
                    "x-mcp-header": "x-tenant",
                },
            },
        },
    }
    with pytest.raises(verifier.VerificationError, match="duplicate x-mcp-header"):
        verifier._tool_parameter_headers(duplicate, {})

    nonprimitive = {
        "name": "nonprimitive",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ratio": {
                    "type": "number",
                    "x-mcp-header": "X-Ratio",
                }
            },
        },
    }
    with pytest.raises(verifier.VerificationError, match="invalid x-mcp-header"):
        verifier._tool_parameter_headers(nonprimitive, {"ratio": 1.5})

    integer = {
        "name": "integer",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "x-mcp-header": "X-Count",
                }
            },
        },
    }
    with pytest.raises(verifier.VerificationError, match="declared integer"):
        verifier._tool_parameter_headers(integer, {"count": 1.5})

    unresolved = {
        "name": "unresolved",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tenant": {
                    "$ref": "#/$defs/tenant",
                }
            },
            "$defs": {
                "tenant": {
                    "type": "string",
                    "x-mcp-header": "X-Tenant",
                }
            },
        },
    }
    with pytest.raises(verifier.VerificationError, match=r"x-mcp-header with \$ref"):
        verifier._tool_parameter_headers(unresolved, {"tenant": "a"})


def test_current_tool_name_uses_the_2026_grammar(verifier):
    verifier._validate_tool_descriptor(_tool("valid.tool-name_1"), "2026-07-28")

    with pytest.raises(verifier.VerificationError, match="1-64 character grammar"):
        verifier._validate_tool_descriptor(_tool("bad/name"), "2026-07-28")
    with pytest.raises(verifier.VerificationError, match="1-64 character grammar"):
        verifier._validate_tool_descriptor(_tool("a" * 65), "2026-07-28")


def test_current_tools_list_requires_cache_metadata(verifier, monkeypatch):
    monkeypatch.setattr(
        verifier,
        "_rpc",
        lambda *_args, **_kwargs: (
            _response(verifier),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "resultType": "complete",
                    "tools": [_tool("probe")],
                },
            },
        ),
    )

    with pytest.raises(verifier.VerificationError, match="ttlMs"):
        verifier._list_tools(
            "https://example.com/mcp",
            "token",
            None,
            1,
            protocol_version="2026-07-28",
        )


@pytest.mark.parametrize(
    ("protocol_version", "block", "canary"),
    [
        ("2025-03-26", {"type": "text", "text": "text-canary"}, "text-canary"),
        (
            "2025-03-26",
            {
                "type": "image",
                "data": "aW1hZ2UtY2FuYXJ5",
                "mimeType": "image/canary",
            },
            "image/canary",
        ),
        (
            "2025-03-26",
            {
                "type": "audio",
                "data": "YXVkaW8tY2FuYXJ5",
                "mimeType": "audio/canary",
            },
            "audio/canary",
        ),
        (
            "2025-03-26",
            {
                "type": "resource",
                "resource": {
                    "uri": "urn:test:text-canary",
                    "text": "embedded text",
                },
            },
            "text-canary",
        ),
        (
            "2025-03-26",
            {
                "type": "resource",
                "resource": {
                    "uri": "urn:test:blob-canary",
                    "blob": "YmxvYi1jYW5hcnk=",
                },
            },
            "blob-canary",
        ),
        (
            "2025-06-18",
            {
                "type": "resource_link",
                "name": "linked-canary",
                "uri": "https://example.com/resource",
                "size": 0,
            },
            "linked-canary",
        ),
        (
            "2026-07-28",
            {
                "type": "text",
                "text": "current-canary",
                "annotations": {
                    "audience": ["assistant"],
                    "priority": 1,
                    "lastModified": "2026-09-22T00:00:00Z",
                },
                "_meta": {},
            },
            "current-canary",
        ),
    ],
)
def test_tool_result_accepts_each_version_valid_content_shape(
    verifier,
    protocol_version,
    block,
    canary,
):
    result = {"content": [block]}
    if protocol_version == "2026-07-28":
        result["resultType"] = "complete"
    verifier._require_tool_result(
        _response(verifier),
        {"jsonrpc": "2.0", "id": 1, "result": result},
        1,
        "probe",
        [canary],
        protocol_version=protocol_version,
    )


@pytest.mark.parametrize(
    ("protocol_version", "block", "error"),
    [
        ("2025-03-26", "text", "non-object"),
        ("2025-03-26", {}, "valid type"),
        ("2025-03-26", {"type": "unknown", "value": "canary"}, "unsupported"),
        ("2025-03-26", {"type": "text"}, "string text"),
        (
            "2025-03-26",
            {"type": "image", "data": "not base64", "mimeType": "image/png"},
            "invalid base64 image",
        ),
        (
            "2025-03-26",
            {"type": "audio", "data": "Y2FuYXJ5"},
            "mimeType",
        ),
        (
            "2025-03-26",
            {"type": "resource_link", "name": "canary", "uri": "urn:test:canary"},
            "before that content type existed",
        ),
        (
            "2025-06-18",
            {"type": "resource_link", "name": "canary", "uri": "relative/path"},
            "absolute uri",
        ),
        (
            "2025-06-18",
            {"type": "resource_link", "name": "canary", "uri": "urn:test:canary", "size": True},
            "resource_link size",
        ),
        (
            "2025-06-18",
            {
                "type": "resource_link",
                "name": "canary",
                "uri": "urn:test:canary",
                "size": float("nan"),
            },
            "resource_link size",
        ),
        (
            "2025-03-26",
            {"type": "resource", "resource": {"uri": "urn:test:canary"}},
            "exactly one",
        ),
        (
            "2025-03-26",
            {
                "type": "resource",
                "resource": {
                    "uri": "urn:test:canary",
                    "text": "canary",
                    "blob": "Y2FuYXJ5",
                },
            },
            "exactly one",
        ),
        (
            "2026-07-28",
            {
                "type": "text",
                "text": "canary",
                "annotations": {"priority": True},
            },
            "annotations priority",
        ),
        (
            "2026-07-28",
            {"type": "text", "text": "canary", "_meta": []},
            "malformed _meta",
        ),
    ],
)
def test_tool_result_rejects_malformed_or_version_invalid_content(
    verifier,
    protocol_version,
    block,
    error,
):
    result = {"content": [block]}
    if protocol_version == "2026-07-28":
        result["resultType"] = "complete"
    with pytest.raises(verifier.VerificationError, match=error):
        verifier._require_tool_result(
            _response(verifier),
            {"jsonrpc": "2.0", "id": 1, "result": result},
            1,
            "probe",
            ["canary"],
            protocol_version=protocol_version,
        )


def test_pre_2026_structured_content_must_be_an_object(verifier):
    with pytest.raises(verifier.VerificationError, match="non-object structuredContent"):
        verifier._require_tool_result(
            _response(verifier),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "canary"}],
                    "structuredContent": ["not", "an", "object"],
                },
            },
            1,
            "probe",
            ["canary"],
            protocol_version="2025-11-25",
        )

    verifier._require_tool_result(
        _response(verifier),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "resultType": "complete",
                "content": [{"type": "text", "text": "canary"}],
                "structuredContent": ["valid", "in", "2026"],
            },
        },
        1,
        "probe",
        ["canary"],
        protocol_version="2026-07-28",
    )


def test_current_tools_list_must_be_deterministic(verifier, monkeypatch):
    list_calls = 0

    def fake_rpc(_url, _token, payload, **_kwargs):
        nonlocal list_calls
        request_id = payload["id"]
        if payload["method"] == "tools/list":
            list_calls += 1
            tools = [_tool("one"), _tool("two")] if list_calls == 1 else [_tool("two"), _tool("one")]
            return _response(verifier), {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": "complete",
                    "ttlMs": 1000,
                    "cacheScope": "private",
                    "tools": tools,
                },
            }
        raise AssertionError("a nondeterministic list should fail before tool calls")

    monkeypatch.setattr(verifier, "_rpc", fake_rpc)

    with pytest.raises(verifier.VerificationError, match="not deterministic"):
        verifier._exercise_tools(
            "https://example.com/mcp",
            "token",
            None,
            1,
            [
                {
                    "name": "one",
                    "arguments": {},
                    "expectContains": ["canary"],
                }
            ],
            [],
            protocol_version="2026-07-28",
            require_all_tools=False,
        )


def test_unknown_tool_server_crash_is_not_a_fail_closed_pass(
    verifier,
    monkeypatch,
):
    monkeypatch.setattr(
        verifier,
        "_rpc",
        lambda *_args, **_kwargs: (
            _response(verifier, 500),
            None,
        ),
    )

    with pytest.raises(verifier.VerificationError, match="crashed"):
        verifier._assert_unknown_tool_rejected(
            "https://example.com/mcp",
            "token",
            None,
            1,
            {"known"},
            protocol_version="2025-03-26",
        )


def test_unknown_tool_auth_error_is_not_a_routing_rejection(
    verifier,
    monkeypatch,
):
    monkeypatch.setattr(
        verifier,
        "_rpc",
        lambda *_args, **_kwargs: (
            _response(verifier, 403),
            None,
        ),
    )

    with pytest.raises(verifier.VerificationError, match="does not prove"):
        verifier._assert_unknown_tool_rejected(
            "https://example.com/mcp",
            "token",
            None,
            1,
            {"known"},
            protocol_version="2025-03-26",
        )


def test_invalid_configuration_fails_before_network(verifier, monkeypatch):
    monkeypatch.setenv("MCP_VERIFY_URL", "https://example.com/mcp")
    monkeypatch.setenv("MCP_VERIFY_BEARER_TOKEN", "token")
    monkeypatch.setenv("MCP_VERIFY_CALLS_JSON", "[]")
    monkeypatch.setenv(
        "MCP_VERIFY_PROTOCOL_VERSIONS_JSON",
        '["2025-03-26","2025-03-26"]',
    )

    with pytest.raises(verifier.VerificationError, match="duplicate version"):
        verifier.verify()

    monkeypatch.setenv(
        "MCP_VERIFY_PROTOCOL_VERSIONS_JSON",
        '["2099-01-01"]',
    )
    with pytest.raises(verifier.VerificationError, match="unsupported verifier"):
        verifier.verify()

    monkeypatch.setenv(
        "MCP_VERIFY_PROTOCOL_VERSIONS_JSON",
        '["2025-03-26"]',
    )
    monkeypatch.setenv(
        "MCP_VERIFY_CALLS_JSON",
        '[{"name":"probe","arguments":{},"expectContains":"canary"}]',
    )
    monkeypatch.setenv("MCP_VERIFY_REQUIRE_ALL_TOOLS", "yes")
    with pytest.raises(verifier.VerificationError, match="true or false"):
        verifier.verify()

    monkeypatch.setenv("MCP_VERIFY_URL", "http://example.com/mcp")
    with pytest.raises(verifier.VerificationError, match="plain HTTPS"):
        verifier.verify()
