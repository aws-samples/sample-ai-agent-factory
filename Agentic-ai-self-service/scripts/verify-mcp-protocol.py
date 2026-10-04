#!/usr/bin/env python3
"""Verify a deployed MCP endpoint's complete client-visible protocol contract.

This script is intentionally read-only with respect to AWS. Provision the
runtime/gateway through the product, mint its bearer token, then provide:

  MCP_VERIFY_URL=https://.../mcp
  MCP_VERIFY_BEARER_TOKEN=...
  MCP_VERIFY_PROTOCOL_VERSIONS_JSON='[
    "2025-11-25",
    "2025-06-18",
    "2025-03-26"
  ]'
  MCP_VERIFY_CALLS_JSON='[
    {"name":"target___probe","arguments":{},"expectContains":"probe-ok"}
  ]'

Optional ``MCP_VERIFY_EXPECT_TOOLS_JSON`` is a JSON list of additional tool
names that discovery must contain. ``MCP_VERIFY_REQUIRE_ALL_TOOLS=true``
requires the call matrix to exercise every discovered tool.

For every explicitly listed version, the verifier proves:
  * missing and invalid bearer tokens are rejected;
  * legacy versions complete initialize + notifications/initialized;
  * 2026-07-28 completes stateless server/discover and validates routing headers;
  * an advertised legacy MCP session is propagated and cannot bypass auth;
  * tools/list exposes object-shaped input schemas for every expected/called tool;
  * every configured tool call returns valid MCP content and an upstream canary;
  * an unknown tool fails closed without passing a server crash;
  * a server-issued legacy session can be terminated and is then unusable.

Secrets stay out of logs and process arguments: curl reads request headers from
a mode-0600 temporary file, and ``--disable`` prevents a local ``.curlrc`` from
turning on tracing or redirecting those headers.

The verifier retains a strict 2026-07-28 path for future service-conformance
checks. The product does not currently advertise that version: a live
AgentCore Gateway ``server/discover`` response omitted the mandatory
``resultType`` field on 2026-09-22. Add it to the explicit version list only
when testing whether that external service gap has closed.
"""

from __future__ import annotations

import base64
import copy
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_HEADER_BYTES = 128 * 1024
_CURRENT_PROTOCOL_VERSION = "2026-07-28"
_KNOWN_PROTOCOL_VERSIONS = (
    _CURRENT_PROTOCOL_VERSION,
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
)
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_CURRENT_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ABSOLUTE_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s\x00-\x1f\x7f]*$")
_CLIENT_INFO = {
    "name": "agentcore-flows-production-verifier",
    "version": "2.0",
}


class VerificationError(RuntimeError):
    """The deployed endpoint did not satisfy the MCP contract."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def _validate_url(raw: str) -> str:
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise VerificationError("MCP_VERIFY_URL contains an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise VerificationError("MCP_VERIFY_URL must be a plain HTTPS endpoint on port 443")
    return raw


def _parse_headers(raw: str) -> dict[str, str]:
    """Return the final response's headers from curl's dump-header output."""
    headers: dict[str, str] = {}
    for line in raw.splitlines():
        if line.startswith("HTTP/"):
            headers = {}
            continue
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return headers


def _http(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None = None,
    *,
    timeout_seconds: int = 45,
) -> HttpResponse:
    for name, value in headers.items():
        if any(ch in name + value for ch in "\r\n"):
            raise VerificationError("An HTTP header contained a newline")

    with tempfile.TemporaryDirectory(prefix="mcp-verify-") as directory:
        root = Path(directory)
        request_headers = root / "request-headers"
        response_headers = root / "response-headers"
        response_body = root / "response-body"
        request_headers.write_text(
            "".join(f"{name}: {value}\n" for name, value in headers.items()),
            encoding="utf-8",
        )
        request_headers.chmod(0o600)

        curl = shutil.which("curl")
        if not curl or not Path(curl).is_absolute():
            raise VerificationError("curl is required and must resolve to an absolute executable path")
        args = [
            curl,
            # This MUST be curl's first option. Otherwise ~/.curlrc can enable
            # verbose/trace output, redirects, or alternate output files and
            # disclose the bearer token read from request_headers.
            "--disable",
            "-sS",
            "--proto",
            "=https",
            "--max-filesize",
            str(_MAX_BODY_BYTES),
            "--connect-timeout",
            "10",
            "--max-time",
            str(timeout_seconds),
            "--request",
            method,
            "--header",
            f"@{request_headers}",
            "--dump-header",
            str(response_headers),
            "--output",
            str(response_body),
            "--write-out",
            "%{http_code}",
        ]
        if body is not None:
            request_body = root / "request-body"
            request_body.write_bytes(body)
            request_body.chmod(0o600)
            args.extend(["--data-binary", f"@{request_body}"])
        args.append(url)

        try:
            completed = subprocess.run(  # noqa: S603 - absolute executable, list-form argv
                args,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds + 5,
            )
        except subprocess.TimeoutExpired as exc:
            raise VerificationError("curl did not stop after its configured request deadline") from exc
        if completed.returncode != 0:
            raise VerificationError(
                f"curl transport failed with exit {completed.returncode}: {completed.stderr.strip()[:240]}"
            )
        try:
            status = int(completed.stdout.strip())
        except ValueError as exc:
            raise VerificationError("curl returned no parseable HTTP status") from exc
        payload = response_body.read_bytes()
        if len(payload) > _MAX_BODY_BYTES:
            raise VerificationError(f"MCP response exceeded {_MAX_BODY_BYTES} bytes")
        raw_response_headers = response_headers.read_bytes()
        if len(raw_response_headers) > _MAX_HEADER_BYTES:
            raise VerificationError(f"MCP response headers exceeded {_MAX_HEADER_BYTES} bytes")
        return HttpResponse(
            status=status,
            headers=_parse_headers(raw_response_headers.decode("utf-8", errors="replace")),
            body=payload,
        )


def _parse_rpc_body(
    body: bytes,
    expected_id: int | str | None = None,
) -> dict[str, Any] | None:
    text = body.decode("utf-8", errors="strict").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as json_exc:
        candidates: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            candidate = line[5:].strip()
            if not candidate or candidate == "[DONE]":
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                candidates.append(parsed)
        if not candidates:
            raise VerificationError("MCP response was neither JSON nor parseable SSE JSON") from json_exc
        if expected_id is None:
            value = candidates[0]
        else:
            value = next(
                (candidate for candidate in candidates if candidate.get("id") == expected_id),
                candidates[0],
            )
    if not isinstance(value, dict):
        raise VerificationError("MCP response must be a JSON object")
    return value


def _is_current_protocol(protocol_version: str) -> bool:
    return protocol_version == _CURRENT_PROTOCOL_VERSION


def _current_request_meta(protocol_version: str) -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": protocol_version,
        "io.modelcontextprotocol/clientInfo": dict(_CLIENT_INFO),
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def _payload_for_protocol(payload: dict[str, Any], protocol_version: str) -> dict[str, Any]:
    wire_payload = copy.deepcopy(payload)
    if not _is_current_protocol(protocol_version):
        return wire_payload
    params = wire_payload.setdefault("params", {})
    if not isinstance(params, dict):
        raise VerificationError("a current-protocol JSON-RPC request must carry an object params value")
    existing_meta = params.get("_meta", {})
    if not isinstance(existing_meta, dict):
        raise VerificationError("a current-protocol request carried a non-object _meta value")
    params["_meta"] = {**existing_meta, **_current_request_meta(protocol_version)}
    return wire_payload


def _header_scalar(value: Any, schema_type: str) -> str:
    if schema_type == "boolean" and isinstance(value, bool):
        text = "true" if value else "false"
    elif schema_type == "integer" and isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 9_007_199_254_740_991:
            raise VerificationError("an x-mcp-header integer exceeded the interoperable safe range")
        text = str(value)
    elif schema_type == "string" and isinstance(value, str):
        text = value
    else:
        raise VerificationError(f"an x-mcp-header value did not match its declared {schema_type} schema")
    if "\r" in text or "\n" in text:
        raise VerificationError("an x-mcp-header parameter contained a newline")
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return f"=?UTF-8?B?{encoded}?="
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise VerificationError("an x-mcp-header parameter contained a control character")
    return text


def _x_mcp_header_bindings(schema: dict[str, Any]) -> list[tuple[tuple[str, ...], str, str]]:
    """Validate and return ``(argument path, HTTP name, primitive type)`` bindings."""
    bindings: list[tuple[tuple[str, ...], str, str]] = []
    header_paths: dict[str, tuple[str, ...]] = {}

    def contains_key(value: Any, key: str) -> bool:
        if isinstance(value, dict):
            return key in value or any(contains_key(child, key) for child in value.values())
        if isinstance(value, list):
            return any(contains_key(child, key) for child in value)
        return False

    if contains_key(schema, "x-mcp-header") and contains_key(schema, "$ref"):
        raise VerificationError(
            "an inputSchema combines x-mcp-header with $ref; the verifier refuses to omit a binding it cannot resolve"
        )

    def walk(node: dict[str, Any], path: tuple[str, ...]) -> None:
        properties = node.get("properties", {})
        if properties is None:
            return
        if not isinstance(properties, dict):
            raise VerificationError(f"an MCP inputSchema has malformed properties at {'.'.join(path) or '$'}")
        for property_name, property_schema in properties.items():
            if not isinstance(property_name, str) or not isinstance(property_schema, dict):
                raise VerificationError("an MCP inputSchema property is malformed")
            child_path = (*path, property_name)
            annotation = property_schema.get("x-mcp-header")
            if annotation is not None:
                schema_type = property_schema.get("type")
                if (
                    not isinstance(annotation, str)
                    or not annotation
                    or not _HEADER_NAME_RE.fullmatch(annotation)
                    or schema_type not in {"string", "integer", "boolean"}
                ):
                    raise VerificationError(f"invalid x-mcp-header metadata at {'.'.join(child_path)}")
                lower_name = annotation.lower()
                if lower_name in header_paths:
                    raise VerificationError(
                        "duplicate x-mcp-header name "
                        f"{annotation!r} at {'.'.join(header_paths[lower_name])} "
                        f"and {'.'.join(child_path)}"
                    )
                header_paths[lower_name] = child_path
                bindings.append((child_path, annotation, schema_type))
            walk(property_schema, child_path)

    walk(schema, ())
    return bindings


def _tool_parameter_headers(tool: dict[str, Any], arguments: dict[str, Any]) -> dict[str, str]:
    """Mirror every supplied ``x-mcp-header`` argument into ``Mcp-Param-*``."""
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        raise VerificationError(f"tool {tool.get('name')!r} has no object inputSchema")
    headers: dict[str, str] = {}

    for path, annotation, schema_type in _x_mcp_header_bindings(schema):
        value: Any = arguments
        present = True
        for segment in path:
            if not isinstance(value, dict) or segment not in value:
                present = False
                break
            value = value[segment]
        if present:
            headers[f"Mcp-Param-{annotation}"] = _header_scalar(value, schema_type)
    return headers


def _rpc(
    url: str,
    token: str | None,
    payload: dict[str, Any],
    *,
    protocol_version: str,
    session_id: str | None = None,
    tool: dict[str, Any] | None = None,
    header_overrides: dict[str, str | None] | None = None,
) -> tuple[HttpResponse, dict[str, Any] | None]:
    wire_payload = _payload_for_protocol(payload, protocol_version)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": protocol_version,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if _is_current_protocol(protocol_version):
        method = wire_payload.get("method")
        if not isinstance(method, str) or not method:
            raise VerificationError("a current-protocol request has no JSON-RPC method")
        headers["Mcp-Method"] = method
        if method == "tools/call":
            params = wire_payload.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
            if not isinstance(name, str) or not name or not isinstance(arguments, dict):
                raise VerificationError("a current-protocol tools/call request is malformed")
            headers["Mcp-Name"] = quote(name, safe="")
            if tool is not None:
                headers.update(_tool_parameter_headers(tool, arguments))
    for name, value in (header_overrides or {}).items():
        if value is None:
            headers.pop(name, None)
        else:
            headers[name] = value
    response = _http(
        "POST",
        url,
        headers,
        json.dumps(wire_payload, separators=(",", ":")).encode("utf-8"),
    )
    try:
        message = _parse_rpc_body(response.body, wire_payload.get("id"))
    except VerificationError:
        # HTTP-layer rejections often carry an empty or plain API Gateway body.
        # Successful responses still have to be valid JSON-RPC or SSE JSON.
        if response.status < 400:
            raise
        message = None
    return response, message


def _initialize_payload(request_id: int, protocol_version: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": dict(_CLIENT_INFO),
        },
    }


def _discover_payload(request_id: int) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "server/discover",
        "params": {},
    }


def _require_success(
    response: HttpResponse,
    message: dict[str, Any] | None,
    request_id: int,
) -> dict[str, Any]:
    if response.status != 200:
        raise VerificationError(f"JSON-RPC request {request_id} returned HTTP {response.status}")
    if not message or message.get("jsonrpc") != "2.0" or message.get("id") != request_id:
        raise VerificationError(f"JSON-RPC request {request_id} returned a mismatched response")
    if message.get("error") is not None:
        raise VerificationError(f"JSON-RPC request {request_id} returned error: {message['error']}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise VerificationError(f"JSON-RPC request {request_id} returned no result object")
    return result


def _load_json_env(name: str, default: Any) -> Any:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"{name} is not valid JSON") from exc


def _load_protocol_versions() -> list[str]:
    versions = _load_json_env("MCP_VERIFY_PROTOCOL_VERSIONS_JSON", None)
    if not isinstance(versions, list) or not versions:
        raise VerificationError("MCP_VERIFY_PROTOCOL_VERSIONS_JSON must be a non-empty JSON list")
    if not all(isinstance(version, str) and version for version in versions):
        raise VerificationError("MCP_VERIFY_PROTOCOL_VERSIONS_JSON must contain only version strings")
    if len(set(versions)) != len(versions):
        raise VerificationError("MCP_VERIFY_PROTOCOL_VERSIONS_JSON contains a duplicate version")
    unknown = set(versions) - set(_KNOWN_PROTOCOL_VERSIONS)
    if unknown:
        raise VerificationError(f"unsupported verifier protocol version(s): {sorted(unknown)}")
    return versions


def _load_bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise VerificationError(f"{name} must be true or false")


def _load_call_matrix() -> list[dict[str, Any]]:
    calls = _load_json_env("MCP_VERIFY_CALLS_JSON", None)
    if not isinstance(calls, list) or not calls:
        raise VerificationError("MCP_VERIFY_CALLS_JSON must be a non-empty JSON list")
    if len(calls) > 100:
        raise VerificationError("MCP_VERIFY_CALLS_JSON may contain at most 100 calls")
    normalized: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            raise VerificationError(f"call matrix item {index} is not an object")
        name = call.get("name")
        arguments = call.get("arguments", {})
        canaries = call.get("expectContains")
        if not isinstance(name, str) or not name or len(name) > 256:
            raise VerificationError(f"call matrix item {index} has an invalid name")
        if not isinstance(arguments, dict):
            raise VerificationError(f"call matrix item {index} arguments must be an object")
        if isinstance(canaries, str):
            canaries = [canaries]
        if (
            not isinstance(canaries, list)
            or not canaries
            or not all(isinstance(value, str) and value for value in canaries)
        ):
            raise VerificationError(f"call matrix item {index} needs a non-empty expectContains string or list")
        normalized.append(
            {
                "name": name,
                "arguments": arguments,
                "expectContains": canaries,
            }
        )
    return normalized


def _probe_payload(protocol_version: str, request_id: int) -> dict[str, Any]:
    if _is_current_protocol(protocol_version):
        return _discover_payload(request_id)
    return _initialize_payload(request_id, protocol_version)


def _assert_auth_rejected(
    url: str,
    token: str | None,
    request_id: int,
    *,
    protocol_version: str,
    session_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    response, _ = _rpc(
        url,
        token,
        payload or _probe_payload(protocol_version, request_id),
        protocol_version=protocol_version,
        session_id=session_id,
    )
    if response.status not in (401, 403):
        raise VerificationError(
            f"{protocol_version} authentication fail-closed probe returned HTTP {response.status}, expected 401/403"
        )


def _is_rpc_rejection(message: dict[str, Any] | None, request_id: int) -> bool:
    if not message or message.get("jsonrpc") != "2.0" or message.get("id") != request_id:
        return False
    error = message.get("error")
    if isinstance(error, dict):
        return isinstance(error.get("code"), int) and isinstance(error.get("message"), str) and bool(error["message"])
    result = message.get("result")
    return isinstance(result, dict) and result.get("isError") is True


def _require_fail_closed_rejection(
    response: HttpResponse,
    message: dict[str, Any] | None,
    request_id: int,
    label: str,
) -> None:
    if response.status in (400, 404):
        return
    if response.status >= 500:
        raise VerificationError(f"{label} crashed the server with HTTP {response.status}")
    if response.status == 200 and _is_rpc_rejection(message, request_id):
        return
    if 400 <= response.status < 500:
        raise VerificationError(
            f"{label} returned unexpected HTTP {response.status}; "
            "this does not prove method/session routing rejected the request"
        )
    raise VerificationError(f"{label} did not fail closed")


def _assert_unknown_tool_rejected(
    url: str,
    token: str,
    session_id: str | None,
    request_id: int,
    known_tools: set[str],
    *,
    protocol_version: str,
) -> None:
    unknown = f"definitely_missing_{uuid.uuid4().hex}"
    if unknown in known_tools:
        raise AssertionError("generated unknown tool name collided")
    response, message = _rpc(
        url,
        token,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": unknown, "arguments": {}},
        },
        protocol_version=protocol_version,
        session_id=session_id,
    )
    _require_fail_closed_rejection(response, message, request_id, "unknown-tool probe")


def _assert_session_rejected(
    url: str,
    token: str,
    session_id: str,
    request_id: int,
    *,
    protocol_version: str,
    label: str,
) -> None:
    response, message = _rpc(
        url,
        token,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/list",
            "params": {},
        },
        protocol_version=protocol_version,
        session_id=session_id,
    )
    _require_fail_closed_rejection(response, message, request_id, label)


def _validate_session_id(session_id: str) -> str:
    if not session_id or len(session_id) > 1024 or any(ord(char) < 0x21 or ord(char) > 0x7E for char in session_id):
        raise VerificationError("initialize returned an invalid Mcp-Session-Id header")
    return session_id


def _validate_tool_descriptor(
    tool: Any,
    protocol_version: str,
) -> tuple[str, dict[str, Any]]:
    if not isinstance(tool, dict):
        raise VerificationError("tools/list returned a non-object tool")
    name = tool.get("name")
    if not isinstance(name, str) or not name or len(name) > 256:
        raise VerificationError("tools/list returned a tool without a valid name")
    if _is_current_protocol(protocol_version) and not _CURRENT_TOOL_NAME_RE.fullmatch(name):
        raise VerificationError(f"current-protocol tool name {name!r} violates the 1-64 character grammar")
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise VerificationError(f"tool {name!r} returned no object inputSchema")
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise VerificationError(f"tool {name!r} returned malformed inputSchema properties")
    _x_mcp_header_bindings(schema)
    return name, tool


def _validate_current_cache_contract(result: dict[str, Any], label: str) -> None:
    ttl_ms = result.get("ttlMs")
    cache_scope = result.get("cacheScope")
    if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool) or ttl_ms < 0:
        raise VerificationError(f"{label} returned no non-negative integer ttlMs")
    if cache_scope not in {"public", "private"}:
        raise VerificationError(f"{label} returned no valid cacheScope")


def _validate_current_discover_result(
    result: dict[str, Any],
    protocol_version: str,
) -> None:
    """Validate the 2026 ``server/discover`` result without requiring SHOULDs."""

    if result.get("resultType") != "complete":
        raise VerificationError("server/discover did not return resultType=complete")
    _validate_current_cache_contract(result, "server/discover")

    supported_versions = result.get("supportedVersions")
    if (
        not isinstance(supported_versions, list)
        or not supported_versions
        or any(not isinstance(version, str) or not version for version in supported_versions)
        or protocol_version not in supported_versions
    ):
        raise VerificationError("server/discover omitted the requested protocol version")

    capabilities = result.get("capabilities")
    if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict):
        raise VerificationError("server/discover returned no tools capability")

    instructions = result.get("instructions")
    if "instructions" in result and not isinstance(instructions, str):
        raise VerificationError("server/discover returned malformed instructions")

    # ResultMetaObject says servers SHOULD stamp their identity, not MUST.  An
    # absent stamp is interoperable; a present stamp still has to satisfy the
    # Implementation schema.
    result_meta = result.get("_meta")
    if "_meta" not in result:
        return
    if not isinstance(result_meta, dict):
        raise VerificationError("server/discover returned malformed result _meta")
    server_info = result_meta.get("io.modelcontextprotocol/serverInfo")
    if server_info is None:
        return
    if (
        not isinstance(server_info, dict)
        or not isinstance(server_info.get("name"), str)
        or not server_info["name"]
        or not isinstance(server_info.get("version"), str)
        or not server_info["version"]
    ):
        raise VerificationError("server/discover returned malformed serverInfo")


def _list_tools(
    url: str,
    token: str,
    session_id: str | None,
    first_request_id: int,
    *,
    protocol_version: str,
) -> tuple[dict[str, dict[str, Any]], int]:
    discovered: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    request_id = first_request_id
    for _ in range(100):
        params = {"cursor": cursor} if cursor else {}
        response, message = _rpc(
            url,
            token,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/list",
                "params": params,
            },
            protocol_version=protocol_version,
            session_id=session_id,
        )
        result = _require_success(response, message, request_id)
        if _is_current_protocol(protocol_version):
            if result.get("resultType") != "complete":
                raise VerificationError("current-protocol tools/list did not return resultType=complete")
            _validate_current_cache_contract(result, "current-protocol tools/list")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise VerificationError("tools/list returned no tools list")
        for raw_tool in tools:
            name, tool = _validate_tool_descriptor(raw_tool, protocol_version)
            if name in discovered:
                raise VerificationError(f"tools/list returned duplicate tool name {name!r}")
            discovered[name] = tool
        request_id += 1
        next_cursor = result.get("nextCursor")
        if next_cursor is None:
            return discovered, request_id
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            raise VerificationError("tools/list returned an invalid or repeated cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise VerificationError("tools/list pagination did not terminate within 100 pages")


def _scalar_result_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, bool):
        yield "true" if value else "false"
    elif isinstance(value, (int, float)) and not isinstance(value, complex):
        yield str(value)
    elif isinstance(value, list):
        for item in value:
            yield from _scalar_result_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _scalar_result_values(item)


def _require_string_field(
    value: dict[str, Any],
    field: str,
    label: str,
    *,
    nonempty: bool = False,
) -> str:
    found = value.get(field)
    if not isinstance(found, str) or (nonempty and not found):
        qualifier = "non-empty " if nonempty else ""
        raise VerificationError(f"{label} returned no {qualifier}string {field}")
    return found


def _validate_optional_content_metadata(value: dict[str, Any], label: str) -> None:
    meta = value.get("_meta")
    if "_meta" in value and not isinstance(meta, dict):
        raise VerificationError(f"{label} returned malformed _meta")

    annotations = value.get("annotations")
    if "annotations" not in value:
        return
    if not isinstance(annotations, dict):
        raise VerificationError(f"{label} returned malformed annotations")
    audience = annotations.get("audience")
    if "audience" in annotations and (
        not isinstance(audience, list) or any(item not in {"user", "assistant"} for item in audience)
    ):
        raise VerificationError(f"{label} returned malformed annotations audience")
    priority = annotations.get("priority")
    if "priority" in annotations and (
        not isinstance(priority, (int, float)) or isinstance(priority, bool) or not 0 <= priority <= 1
    ):
        raise VerificationError(f"{label} returned malformed annotations priority")
    last_modified = annotations.get("lastModified")
    if "lastModified" in annotations and not isinstance(last_modified, str):
        raise VerificationError(f"{label} returned malformed annotations lastModified")


def _validate_absolute_uri(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ABSOLUTE_URI_RE.fullmatch(value):
        raise VerificationError(f"{label} returned no valid absolute uri")
    return value


def _validate_content_block(
    block: Any,
    protocol_version: str,
    label: str,
) -> None:
    if not isinstance(block, dict):
        raise VerificationError(f"{label} returned a non-object MCP content block")
    block_type = block.get("type")
    if not isinstance(block_type, str) or not block_type:
        raise VerificationError(f"{label} returned an MCP content block without a valid type")
    _validate_optional_content_metadata(block, label)

    if block_type == "text":
        _require_string_field(block, "text", label)
        return

    if block_type in {"image", "audio"}:
        encoded = _require_string_field(block, "data", label)
        _require_string_field(block, "mimeType", label, nonempty=True)
        try:
            base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise VerificationError(f"{label} returned invalid base64 {block_type} data") from exc
        return

    if block_type == "resource_link":
        if protocol_version == "2025-03-26":
            raise VerificationError(f"{label} returned resource_link before that content type existed")
        _require_string_field(block, "name", label, nonempty=True)
        _validate_absolute_uri(block.get("uri"), label)
        for field in ("title", "description", "mimeType"):
            if field in block and not isinstance(block[field], str):
                raise VerificationError(f"{label} returned malformed resource_link {field}")
        size = block.get("size")
        if "size" in block and (
            not isinstance(size, (int, float)) or isinstance(size, bool) or not math.isfinite(size) or size < 0
        ):
            raise VerificationError(f"{label} returned malformed resource_link size")
        return

    if block_type == "resource":
        resource = block.get("resource")
        if not isinstance(resource, dict):
            raise VerificationError(f"{label} returned no embedded resource object")
        _validate_absolute_uri(resource.get("uri"), label)
        mime_type = resource.get("mimeType")
        if "mimeType" in resource and not isinstance(mime_type, str):
            raise VerificationError(f"{label} returned malformed embedded resource mimeType")
        meta = resource.get("_meta")
        if "_meta" in resource and not isinstance(meta, dict):
            raise VerificationError(f"{label} returned malformed embedded resource _meta")
        has_text = "text" in resource
        has_blob = "blob" in resource
        if has_text == has_blob:
            raise VerificationError(f"{label} embedded resource must contain exactly one of text or blob")
        field = "text" if has_text else "blob"
        encoded_or_text = _require_string_field(resource, field, label)
        if field == "blob":
            try:
                base64.b64decode(encoded_or_text, validate=True)
            except (ValueError, TypeError) as exc:
                raise VerificationError(f"{label} returned invalid base64 embedded resource blob") from exc
        return

    raise VerificationError(f"{label} returned unsupported MCP content type {block_type!r}")


def _require_tool_result(
    response: HttpResponse,
    message: dict[str, Any] | None,
    request_id: int,
    tool_name: str,
    canaries: list[str],
    *,
    protocol_version: str,
) -> None:
    result = _require_success(response, message, request_id)
    if result.get("isError") is True:
        raise VerificationError(f"tool {tool_name} returned isError=true")
    if _is_current_protocol(protocol_version) and result.get("resultType") != "complete":
        raise VerificationError(f"tool {tool_name} did not return resultType=complete")
    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise VerificationError(f"tool {tool_name} returned no MCP content blocks")
    for block in content:
        _validate_content_block(block, protocol_version, f"tool {tool_name}")
    if (
        "structuredContent" in result
        and protocol_version in {"2025-06-18", "2025-11-25"}
        and not isinstance(result["structuredContent"], dict)
    ):
        raise VerificationError(f"tool {tool_name} returned non-object structuredContent for {protocol_version}")
    values = list(_scalar_result_values(content))
    if "structuredContent" in result:
        values.extend(_scalar_result_values(result["structuredContent"]))
    result_text = "\n".join(values).lower()
    if not any(canary.lower() in result_text for canary in canaries):
        raise VerificationError(f"tool {tool_name} returned no configured upstream canary")


def _require_tool_matrix(
    discovered: dict[str, dict[str, Any]],
    calls: list[dict[str, Any]],
    expected_tools: list[str],
    *,
    require_all_tools: bool,
) -> None:
    required_names = set(expected_tools) | {call["name"] for call in calls}
    missing = required_names - set(discovered)
    if missing:
        raise VerificationError(f"tools/list omitted expected tools: {sorted(missing)}")
    if require_all_tools:
        uncalled = set(discovered) - {call["name"] for call in calls}
        if uncalled:
            raise VerificationError(f"call matrix does not exercise discovered tools: {sorted(uncalled)}")


def _exercise_tools(
    url: str,
    token: str,
    session_id: str | None,
    first_request_id: int,
    calls: list[dict[str, Any]],
    expected_tools: list[str],
    *,
    protocol_version: str,
    require_all_tools: bool,
) -> tuple[dict[str, dict[str, Any]], int]:
    discovered, next_id = _list_tools(
        url,
        token,
        session_id,
        first_request_id,
        protocol_version=protocol_version,
    )
    _require_tool_matrix(
        discovered,
        calls,
        expected_tools,
        require_all_tools=require_all_tools,
    )
    if _is_current_protocol(protocol_version):
        repeated, next_id = _list_tools(
            url,
            token,
            session_id,
            next_id,
            protocol_version=protocol_version,
        )
        if list(repeated) != list(discovered) or repeated != discovered:
            raise VerificationError("current-protocol tools/list was not deterministic across identical requests")
    for call in calls:
        tool = discovered[call["name"]]
        call_response, call_message = _rpc(
            url,
            token,
            {
                "jsonrpc": "2.0",
                "id": next_id,
                "method": "tools/call",
                "params": {
                    "name": call["name"],
                    "arguments": call["arguments"],
                },
            },
            protocol_version=protocol_version,
            session_id=session_id,
            tool=tool,
        )
        _require_tool_result(
            call_response,
            call_message,
            next_id,
            call["name"],
            call["expectContains"],
            protocol_version=protocol_version,
        )
        next_id += 1
    return discovered, next_id


def _assert_current_header_rejected(
    url: str,
    token: str,
    payload: dict[str, Any],
    request_id: int,
    *,
    label: str,
    tool: dict[str, Any] | None = None,
    overrides: dict[str, str | None],
) -> None:
    response, message = _rpc(
        url,
        token,
        payload,
        protocol_version=_CURRENT_PROTOCOL_VERSION,
        tool=tool,
        header_overrides=overrides,
    )
    _require_fail_closed_rejection(response, message, request_id, label)


def _assert_current_routing_contract(
    url: str,
    token: str,
    first_request_id: int,
    calls: list[dict[str, Any]],
    discovered: dict[str, dict[str, Any]],
) -> int:
    request_id = first_request_id
    list_payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/list",
        "params": {},
    }
    _assert_current_header_rejected(
        url,
        token,
        list_payload,
        request_id,
        label="missing Mcp-Method probe",
        overrides={"Mcp-Method": None},
    )
    request_id += 1
    list_payload["id"] = request_id
    _assert_current_header_rejected(
        url,
        token,
        list_payload,
        request_id,
        label="mismatched Mcp-Method probe",
        overrides={"Mcp-Method": "tools/call"},
    )
    request_id += 1
    list_payload["id"] = request_id
    _assert_current_header_rejected(
        url,
        token,
        list_payload,
        request_id,
        label="mismatched MCP-Protocol-Version probe",
        overrides={"MCP-Protocol-Version": "2025-11-25"},
    )
    request_id += 1

    first_call = calls[0]
    tool = discovered[first_call["name"]]
    call_payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": first_call["name"],
            "arguments": first_call["arguments"],
        },
    }
    _assert_current_header_rejected(
        url,
        token,
        call_payload,
        request_id,
        label="mismatched Mcp-Name probe",
        tool=tool,
        overrides={"Mcp-Name": quote(f"{first_call['name']}-mismatch", safe="")},
    )
    request_id += 1

    mirrored = _tool_parameter_headers(tool, first_call["arguments"])
    if mirrored:
        header_name, header_value = next(iter(mirrored.items()))
        call_payload["id"] = request_id
        _assert_current_header_rejected(
            url,
            token,
            call_payload,
            request_id,
            label=f"mismatched {header_name} probe",
            tool=tool,
            overrides={header_name: f"{header_value}-mismatch"},
        )
        request_id += 1
    return request_id


def _verify_legacy_version(
    url: str,
    token: str,
    protocol_version: str,
    calls: list[dict[str, Any]],
    expected_tools: list[str],
    *,
    require_all_tools: bool,
    id_base: int,
) -> None:
    print(f"[{protocol_version}] rejecting missing and invalid bearer tokens", flush=True)
    _assert_auth_rejected(url, None, id_base + 9001, protocol_version=protocol_version)
    _assert_auth_rejected(
        url,
        "invalid.invalid.invalid",
        id_base + 9002,
        protocol_version=protocol_version,
    )

    request_id = id_base + 1
    print(f"[{protocol_version}] initialize + notifications/initialized", flush=True)
    response, message = _rpc(
        url,
        token,
        _initialize_payload(request_id, protocol_version),
        protocol_version=protocol_version,
    )
    initialized = _require_success(response, message, request_id)
    if initialized.get("protocolVersion") != protocol_version:
        raise VerificationError(
            f"initialize negotiated {initialized.get('protocolVersion')!r}, expected {protocol_version!r}"
        )
    if not isinstance(initialized.get("capabilities"), dict):
        raise VerificationError("initialize returned no capabilities object")
    server_info = initialized.get("serverInfo")
    if (
        not isinstance(server_info, dict)
        or not isinstance(server_info.get("name"), str)
        or not server_info["name"]
        or not isinstance(server_info.get("version"), str)
        or not server_info["version"]
    ):
        raise VerificationError("initialize returned no valid serverInfo")
    raw_session_id = response.headers.get("mcp-session-id")
    session_id = _validate_session_id(raw_session_id) if raw_session_id is not None else None
    request_id += 1

    notification_response, notification_message = _rpc(
        url,
        token,
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        protocol_version=protocol_version,
        session_id=session_id,
    )
    if notification_response.status not in (200, 202, 204):
        raise VerificationError(f"notifications/initialized returned HTTP {notification_response.status}")
    if notification_message is not None:
        raise VerificationError("notifications/initialized incorrectly returned a JSON-RPC response")

    if session_id:
        session_probe = {
            "jsonrpc": "2.0",
            "id": id_base + 9101,
            "method": "tools/list",
            "params": {},
        }
        _assert_auth_rejected(
            url,
            None,
            id_base + 9101,
            protocol_version=protocol_version,
            session_id=session_id,
            payload=session_probe,
        )
        session_probe["id"] = id_base + 9102
        _assert_auth_rejected(
            url,
            "invalid.invalid.invalid",
            id_base + 9102,
            protocol_version=protocol_version,
            session_id=session_id,
            payload=session_probe,
        )

    print(f"[{protocol_version}] tools/list + {len(calls)} canary call(s)", flush=True)
    discovered, request_id = _exercise_tools(
        url,
        token,
        session_id,
        request_id,
        calls,
        expected_tools,
        protocol_version=protocol_version,
        require_all_tools=require_all_tools,
    )

    if session_id:
        print(f"[{protocol_version}] rejecting a fabricated session", flush=True)
        _assert_session_rejected(
            url,
            token,
            f"invalid-{uuid.uuid4().hex}",
            request_id,
            protocol_version=protocol_version,
            label="fabricated-session probe",
        )
        request_id += 1

    print(f"[{protocol_version}] rejecting an unknown tool", flush=True)
    _assert_unknown_tool_rejected(
        url,
        token,
        session_id,
        request_id,
        set(discovered),
        protocol_version=protocol_version,
    )
    request_id += 1

    if not session_id:
        print(f"[{protocol_version}] server issued no optional session id", flush=True)
        return
    print(f"[{protocol_version}] terminating and invalidating the session", flush=True)
    close_response = _http(
        "DELETE",
        url,
        {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": protocol_version,
            "Mcp-Session-Id": session_id,
        },
    )
    if close_response.status == 405:
        return
    if close_response.status not in (200, 202, 204):
        raise VerificationError(f"MCP session termination returned HTTP {close_response.status}")
    _assert_session_rejected(
        url,
        token,
        session_id,
        request_id,
        protocol_version=protocol_version,
        label="terminated-session probe",
    )


def _verify_current_version(
    url: str,
    token: str,
    calls: list[dict[str, Any]],
    expected_tools: list[str],
    *,
    require_all_tools: bool,
    id_base: int,
) -> None:
    protocol_version = _CURRENT_PROTOCOL_VERSION
    print(f"[{protocol_version}] rejecting missing and invalid bearer tokens", flush=True)
    _assert_auth_rejected(url, None, id_base + 9001, protocol_version=protocol_version)
    _assert_auth_rejected(
        url,
        "invalid.invalid.invalid",
        id_base + 9002,
        protocol_version=protocol_version,
    )

    request_id = id_base + 1
    print(f"[{protocol_version}] stateless server/discover", flush=True)
    response, message = _rpc(
        url,
        token,
        _discover_payload(request_id),
        protocol_version=protocol_version,
    )
    discovered_server = _require_success(response, message, request_id)
    if response.headers.get("mcp-session-id") is not None:
        raise VerificationError("2026-07-28 server/discover incorrectly issued a stateful session id")
    _validate_current_discover_result(discovered_server, protocol_version)
    request_id += 1

    print(f"[{protocol_version}] tools/list + {len(calls)} canary call(s)", flush=True)
    discovered, request_id = _exercise_tools(
        url,
        token,
        None,
        request_id,
        calls,
        expected_tools,
        protocol_version=protocol_version,
        require_all_tools=require_all_tools,
    )

    print(f"[{protocol_version}] validating routing-header/body agreement", flush=True)
    request_id = _assert_current_routing_contract(
        url,
        token,
        request_id,
        calls,
        discovered,
    )

    print(f"[{protocol_version}] rejecting an unknown tool", flush=True)
    _assert_unknown_tool_rejected(
        url,
        token,
        None,
        request_id,
        set(discovered),
        protocol_version=protocol_version,
    )


def verify() -> None:
    url = _validate_url(os.environ.get("MCP_VERIFY_URL", ""))
    token = os.environ.get("MCP_VERIFY_BEARER_TOKEN", "")
    if not token:
        raise VerificationError("MCP_VERIFY_BEARER_TOKEN is required")
    protocol_versions = _load_protocol_versions()
    calls = _load_call_matrix()
    expected_tools = _load_json_env("MCP_VERIFY_EXPECT_TOOLS_JSON", [])
    if (
        not isinstance(expected_tools, list)
        or not all(isinstance(name, str) and name for name in expected_tools)
        or len(set(expected_tools)) != len(expected_tools)
    ):
        raise VerificationError("MCP_VERIFY_EXPECT_TOOLS_JSON must be a duplicate-free JSON list of names")
    require_all_tools = _load_bool_env("MCP_VERIFY_REQUIRE_ALL_TOOLS")

    for index, protocol_version in enumerate(protocol_versions):
        id_base = index * 10000
        if _is_current_protocol(protocol_version):
            _verify_current_version(
                url,
                token,
                calls,
                expected_tools,
                require_all_tools=require_all_tools,
                id_base=id_base,
            )
        else:
            _verify_legacy_version(
                url,
                token,
                protocol_version,
                calls,
                expected_tools,
                require_all_tools=require_all_tools,
                id_base=id_base,
            )

    print(
        "PASS: "
        + ", ".join(protocol_versions)
        + f"; tools/list + {len(calls)} canary call(s) per version; auth/routing/unknown-tool fail-closed",
        flush=True,
    )


if __name__ == "__main__":
    try:
        verify()
    except VerificationError as exc:
        print(f"FAIL: {exc}", flush=True)
        raise SystemExit(1) from exc
