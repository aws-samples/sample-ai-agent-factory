"""Bounded MCP client for an owner-resolved AgentCore Runtime.

This module deliberately accepts only a boto3 data-plane client and a
server-resolved runtime ARN.  Tenant ownership, target account/region, and
protocol selection belong to the router/resolver; callers cannot supply raw
AWS targets, arbitrary MCP methods, routing headers, or protocol versions.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any

from app.services.mcp_gateway_protocol import MCP_SUPPORTED_VERSIONS

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = MCP_SUPPORTED_VERSIONS[0]
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ARGUMENT_BYTES = 64 * 1024
MAX_SCHEMA_BYTES = 128 * 1024
MAX_TOOL_NAME_LENGTH = 256
MAX_TOOL_COUNT = 256
MAX_PAGES = 20
MAX_JSON_DEPTH = 12
MAX_JSON_NODES = 2048
MAX_STRING_BYTES = 32 * 1024
MAX_CURSOR_LENGTH = 1024
DEFAULT_DEADLINE_SECONDS = 26.0

_ALLOWED_METHODS = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    }
)
_SESSION_RE = re.compile(r"^[\x21-\x7e]{1,1024}$")
# AgentCore routes an MCP request by its RUNTIME session, never by Mcp-Session-Id alone.
# Its initialize reply carries an Mcp-Session-Id equal to the runtime session it created;
# a later request carrying that id only as Mcp-Session-Id lands in a brand-new runtime
# session (a new microVM) and comes back with a new Mcp-Session-Id. Measured live
# 2026-09-30 on the gallery MCP runtime: initialize returned mcp == runtime; tools/list
# with the MCP id alone rotated both ids on every call; with the returned id pinned as
# both, both stayed stable. (A runtime id supplied on initialize itself is ignored; the
# service issues its own.) So every request that continues a session pins the runtime
# session to the same id, the mapping AgentCore itself chose. The bounds are AgentCore's
# runtimeSessionId limits; an id outside them cannot name a runtime session and is
# forwarded as the MCP id only.
_RUNTIME_SESSION_MIN_LENGTH = 33
_RUNTIME_SESSION_MAX_LENGTH = 256


class McpInvocationError(RuntimeError):
    """Base class for a product-owned MCP invocation failure."""


class McpInputError(McpInvocationError):
    """The caller's bounded tool input is invalid."""


class McpRuntimeUnavailable(McpInvocationError):
    """The AgentCore data plane could not complete the request."""


class McpPermissionError(McpInvocationError):
    """The platform's own role may not invoke this runtime on behalf of a user.

    ``invoke_agent_runtime`` with ``runtimeUserId`` is authorised as
    ``bedrock-agentcore:InvokeAgentRuntimeForUser``, a different action from the plain
    invoke. Live (2026-09-28) the denial was reported as "temporarily unavailable" with a
    retry button, which no retry could ever satisfy; it is an operator misconfiguration.
    """

    action = "bedrock-agentcore:InvokeAgentRuntimeForUser"


class McpProtocolError(McpInvocationError):
    """The runtime returned a malformed or incompatible MCP response."""


class McpRemoteError(McpInvocationError):
    """The MCP server returned a structured JSON-RPC error."""

    def __init__(self, *, code: int | None = None) -> None:
        super().__init__("The MCP runtime rejected the operation")
        self.code = code


@dataclass(frozen=True)
class McpReply:
    message: dict[str, Any] | None
    session_id: str | None
    protocol_version: str
    status_code: int


@dataclass(frozen=True)
class McpToolsResult:
    tools: list[dict[str, Any]]
    session_id: str | None
    protocol_version: str
    server_info: dict[str, str]


@dataclass(frozen=True)
class McpToolCallResult:
    content: list[dict[str, Any]]
    structured_content: Any
    is_error: bool
    session_id: str | None
    protocol_version: str


def _validate_session_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SESSION_RE.fullmatch(value):
        raise McpProtocolError("The MCP runtime returned an invalid session identifier")
    return value


def _validate_tool_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TOOL_NAME_LENGTH
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise McpInputError("Invalid MCP tool name")
    return value


def _validate_json_tree(value: Any, *, max_bytes: int, label: str) -> bytes:
    """Validate JSON shape/depth/size without accepting NaN or oversized trees."""

    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise McpInputError(f"{label} contains too many values")
        if depth > MAX_JSON_DEPTH:
            raise McpInputError(f"{label} is nested too deeply")

        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise McpInputError(f"{label} contains a non-finite number")
            continue
        if isinstance(current, str):
            if len(current.encode("utf-8")) > MAX_STRING_BYTES:
                raise McpInputError(f"{label} contains an oversized string")
            continue
        if isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str):
                    raise McpInputError(f"{label} object keys must be strings")
                if not key or len(key.encode("utf-8")) > 1024:
                    raise McpInputError(f"{label} contains an invalid object key")
                stack.append((item, depth + 1))
            continue
        raise McpInputError(f"{label} contains a non-JSON value")

    try:
        encoded = json.dumps(
            value,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise McpInputError(f"{label} is not valid JSON") from exc
    if len(encoded) > max_bytes:
        raise McpInputError(f"{label} exceeds the product size limit")
    return encoded


def validate_tool_arguments(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise McpInputError("MCP tool arguments must be a JSON object")
    _validate_json_tree(
        arguments,
        max_bytes=MAX_ARGUMENT_BYTES,
        label="MCP tool arguments",
    )
    return arguments


def _read_bounded(value: Any) -> bytes:
    if hasattr(value, "read"):
        try:
            payload = value.read(MAX_RESPONSE_BYTES + 1)
        except TypeError:
            payload = value.read()
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray)):
        payload = bytes(value)
    elif value is None:
        payload = b""
    else:
        raise McpProtocolError("The MCP runtime returned an unsupported response body")
    if not isinstance(payload, (bytes, bytearray)):
        raise McpProtocolError("The MCP runtime returned an unsupported response body")
    payload = bytes(payload)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise McpProtocolError("The MCP runtime response exceeded the product limit")
    return payload


def _parse_message(
    payload: bytes,
    *,
    expected_id: int | None,
) -> dict[str, Any] | None:
    try:
        text = payload.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise McpProtocolError("The MCP runtime response was not UTF-8") from exc
    if not text:
        return None

    value: Any
    try:
        value = json.loads(text)
    except json.JSONDecodeError as json_error:
        candidates: list[dict[str, Any]] = []
        data_lines: list[str] = []
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if not line:
                if data_lines:
                    encoded = "\n".join(data_lines)
                    data_lines = []
                    if encoded != "[DONE]":
                        try:
                            candidate = json.loads(encoded)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(candidate, dict):
                            candidates.append(candidate)
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        if data_lines:
            try:
                candidate = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                candidate = None
            if isinstance(candidate, dict):
                candidates.append(candidate)
        if not candidates:
            raise McpProtocolError("The MCP runtime returned neither JSON nor parseable SSE") from json_error
        if expected_id is None:
            value = candidates[0]
        else:
            value = next(
                (candidate for candidate in candidates if candidate.get("id") == expected_id),
                candidates[0],
            )
    if not isinstance(value, dict):
        raise McpProtocolError("The MCP runtime response was not a JSON object")
    return value


def _build_invoke_params(
    *,
    runtime_arn: str,
    runtime_user_id: str,
    method: str,
    params: dict[str, Any] | None,
    request_id: int | None,
    session_id: str | None = None,
    tool_name: str | None = None,
) -> dict[str, Any]:
    if method not in _ALLOWED_METHODS:
        raise McpInputError("The product does not permit this MCP method")
    if not runtime_arn.startswith("arn:") or ":bedrock-agentcore:" not in runtime_arn or ":runtime/" not in runtime_arn:
        raise McpProtocolError("The deployment record carried an invalid runtime ARN")
    if not isinstance(runtime_user_id, str) or not runtime_user_id or len(runtime_user_id) > 1024:
        raise McpProtocolError("The authenticated runtime user identifier is invalid")
    if method == "tools/call":
        tool_name = _validate_tool_name(tool_name)
    elif tool_name is not None:
        raise McpInputError("Only tools/call may carry a tool name")

    message: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": method,
    }
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    payload = _validate_json_tree(
        message,
        max_bytes=MAX_ARGUMENT_BYTES + MAX_SCHEMA_BYTES,
        label="MCP request",
    )

    invoke: dict[str, Any] = {
        "agentRuntimeArn": runtime_arn,
        "qualifier": "DEFAULT",
        "contentType": "application/json",
        "accept": "application/json, text/event-stream",
        "mcpProtocolVersion": MCP_PROTOCOL_VERSION,
        "mcpMethod": method,
        "runtimeUserId": runtime_user_id,
        "payload": payload,
    }
    normalized_session = _validate_session_id(session_id)
    if normalized_session:
        invoke["mcpSessionId"] = normalized_session
        if _RUNTIME_SESSION_MIN_LENGTH <= len(normalized_session) <= _RUNTIME_SESSION_MAX_LENGTH:
            invoke["runtimeSessionId"] = normalized_session
    if tool_name is not None:
        invoke["mcpName"] = tool_name
    return invoke


def _invoke_rpc(
    client: Any,
    *,
    runtime_arn: str,
    runtime_user_id: str,
    method: str,
    params: dict[str, Any] | None,
    request_id: int | None,
    session_id: str | None = None,
    tool_name: str | None = None,
    deadline: float,
) -> McpReply:
    if time.monotonic() >= deadline:
        raise McpRuntimeUnavailable("The MCP request exceeded the product deadline")
    try:
        response = client.invoke_agent_runtime(
            **_build_invoke_params(
                runtime_arn=runtime_arn,
                runtime_user_id=runtime_user_id,
                method=method,
                params=params,
                request_id=request_id,
                session_id=session_id,
                tool_name=tool_name,
            )
        )
    except McpInvocationError:
        raise
    except Exception as exc:
        error_code = str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code", ""))
        logger.warning(
            "AgentCore MCP %s failed (%s%s)",
            method,
            type(exc).__name__,
            f", code={error_code}" if error_code else "",
        )
        if error_code == "AccessDeniedException":
            raise McpPermissionError(
                "The platform is not permitted to invoke this MCP runtime on behalf of a "
                f"user; its role needs {McpPermissionError.action}."
            ) from exc
        raise McpRuntimeUnavailable("The MCP runtime could not be reached") from exc

    if not isinstance(response, dict):
        raise McpProtocolError("The AgentCore MCP response was not an object")
    status = response.get("statusCode")
    if not isinstance(status, int):
        raise McpProtocolError("The AgentCore MCP response had no status code")
    if status < 200 or status >= 300:
        if 400 <= status < 500:
            raise McpRemoteError()
        raise McpRuntimeUnavailable("The MCP runtime returned a service failure")

    response_protocol = response.get("mcpProtocolVersion") or MCP_PROTOCOL_VERSION
    if response_protocol != MCP_PROTOCOL_VERSION:
        raise McpProtocolError("The MCP runtime negotiated an unexpected protocol version")
    response_content_type = response.get("contentType")
    if response_content_type and not str(response_content_type).lower().startswith(
        ("application/json", "text/event-stream")
    ):
        raise McpProtocolError("The MCP runtime returned an unexpected content type")

    message = _parse_message(
        _read_bounded(response.get("response")),
        expected_id=request_id,
    )
    if request_id is None:
        if message is not None:
            raise McpProtocolError("An MCP notification unexpectedly returned a JSON-RPC message")
    else:
        if message is None or message.get("jsonrpc") != "2.0" or message.get("id") != request_id:
            raise McpProtocolError("The MCP runtime returned a mismatched JSON-RPC response")
        error = message.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            raise McpRemoteError(code=code if isinstance(code, int) else None)
        if not isinstance(message.get("result"), dict):
            raise McpProtocolError("The MCP runtime returned no result object")

    return McpReply(
        message=message,
        session_id=_validate_session_id(response.get("mcpSessionId") or session_id),
        protocol_version=str(response_protocol),
        status_code=status,
    )


def _initialize(
    client: Any,
    *,
    runtime_arn: str,
    runtime_user_id: str,
    deadline: float,
) -> tuple[str | None, dict[str, str]]:
    reply = _invoke_rpc(
        client,
        runtime_arn=runtime_arn,
        runtime_user_id=runtime_user_id,
        method="initialize",
        params={
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": "agentic-ai-self-service",
                "version": "1.0",
            },
        },
        request_id=1,
        deadline=deadline,
    )
    result = (reply.message or {}).get("result", {})
    if result.get("protocolVersion") != MCP_PROTOCOL_VERSION:
        raise McpProtocolError("The MCP runtime did not confirm the requested protocol version")
    if not isinstance(result.get("capabilities"), dict):
        raise McpProtocolError("The MCP runtime returned no capabilities object")
    server_info = result.get("serverInfo")
    if not isinstance(server_info, dict):
        raise McpProtocolError("The MCP runtime returned no server identity")
    server_name = server_info.get("name")
    server_version = server_info.get("version")
    if (
        not isinstance(server_name, str)
        or not server_name
        or len(server_name) > 256
        or not isinstance(server_version, str)
        or not server_version
        or len(server_version) > 128
    ):
        raise McpProtocolError("The MCP runtime returned invalid server identity")

    _invoke_rpc(
        client,
        runtime_arn=runtime_arn,
        runtime_user_id=runtime_user_id,
        method="notifications/initialized",
        params={},
        request_id=None,
        session_id=reply.session_id,
        deadline=deadline,
    )
    return reply.session_id, {"name": server_name, "version": server_version}


def _validated_tool_descriptor(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise McpProtocolError("The MCP runtime returned a malformed tool descriptor")
    try:
        name = _validate_tool_name(value.get("name"))
    except McpInputError as exc:
        raise McpProtocolError("The MCP runtime returned an invalid tool name") from exc
    input_schema = value.get("inputSchema")
    if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
        raise McpProtocolError("The MCP runtime returned an invalid tool input schema")
    try:
        _validate_json_tree(
            input_schema,
            max_bytes=MAX_SCHEMA_BYTES,
            label="MCP tool input schema",
        )
    except McpInputError as exc:
        raise McpProtocolError(str(exc)) from exc

    descriptor: dict[str, Any] = {
        "name": name,
        "inputSchema": input_schema,
    }
    for key, limit in (("title", 256), ("description", 8192)):
        field = value.get(key)
        if field is not None:
            if not isinstance(field, str) or len(field) > limit:
                raise McpProtocolError(f"The MCP runtime returned an invalid tool {key}")
            descriptor[key] = field
    for key in ("outputSchema", "annotations"):
        field = value.get(key)
        if field is not None:
            if not isinstance(field, dict):
                raise McpProtocolError(f"The MCP runtime returned invalid tool {key}")
            try:
                _validate_json_tree(
                    field,
                    max_bytes=MAX_SCHEMA_BYTES,
                    label=f"MCP tool {key}",
                )
            except McpInputError as exc:
                raise McpProtocolError(str(exc)) from exc
            descriptor[key] = field
    return descriptor


def list_tools(
    client: Any,
    *,
    runtime_arn: str,
    runtime_user_id: str,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> McpToolsResult:
    """Initialize one MCP session and return a bounded, fully paginated tool list."""

    deadline = time.monotonic() + deadline_seconds
    session_id, server_info = _initialize(
        client,
        runtime_arn=runtime_arn,
        runtime_user_id=runtime_user_id,
        deadline=deadline,
    )
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_names: set[str] = set()
    tools: list[dict[str, Any]] = []
    request_id = 2
    protocol_version = MCP_PROTOCOL_VERSION

    for _ in range(MAX_PAGES):
        reply = _invoke_rpc(
            client,
            runtime_arn=runtime_arn,
            runtime_user_id=runtime_user_id,
            method="tools/list",
            params={"cursor": cursor} if cursor else {},
            request_id=request_id,
            session_id=session_id,
            deadline=deadline,
        )
        protocol_version = reply.protocol_version
        session_id = reply.session_id
        result = (reply.message or {}).get("result", {})
        page = result.get("tools")
        if not isinstance(page, list):
            raise McpProtocolError("The MCP runtime returned no tools list")
        for raw_tool in page:
            tool = _validated_tool_descriptor(raw_tool)
            name = tool["name"]
            if name in seen_names:
                raise McpProtocolError("The MCP runtime returned a duplicate tool name")
            seen_names.add(name)
            tools.append(tool)
            if len(tools) > MAX_TOOL_COUNT:
                raise McpProtocolError("The MCP runtime returned too many tools")

        next_cursor = result.get("nextCursor")
        if next_cursor is None:
            return McpToolsResult(
                tools=tools,
                session_id=session_id,
                protocol_version=protocol_version,
                server_info=server_info,
            )
        if (
            not isinstance(next_cursor, str)
            or not next_cursor
            or len(next_cursor) > MAX_CURSOR_LENGTH
            or next_cursor in seen_cursors
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in next_cursor)
        ):
            raise McpProtocolError("The MCP runtime returned an invalid pagination cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        request_id += 1

    raise McpProtocolError("The MCP tools list exceeded the pagination limit")


def call_tool(
    client: Any,
    *,
    runtime_arn: str,
    runtime_user_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    session_id: str | None = None,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> McpToolCallResult:
    """Call one named MCP tool, initializing a session when none was supplied."""

    tool_name = _validate_tool_name(tool_name)
    arguments = validate_tool_arguments(arguments)
    deadline = time.monotonic() + deadline_seconds
    if session_id is None:
        session_id, _ = _initialize(
            client,
            runtime_arn=runtime_arn,
            runtime_user_id=runtime_user_id,
            deadline=deadline,
        )
    else:
        session_id = _validate_session_id(session_id)

    reply = _invoke_rpc(
        client,
        runtime_arn=runtime_arn,
        runtime_user_id=runtime_user_id,
        method="tools/call",
        params={"name": tool_name, "arguments": arguments},
        request_id=1001,
        session_id=session_id,
        tool_name=tool_name,
        deadline=deadline,
    )
    result = (reply.message or {}).get("result", {})
    content = result.get("content")
    if not isinstance(content, list) or not content or len(content) > 256:
        raise McpProtocolError("The MCP tool returned no valid content")
    if not all(isinstance(block, dict) for block in content):
        raise McpProtocolError("The MCP tool returned malformed content")
    try:
        _validate_json_tree(
            content,
            max_bytes=MAX_RESPONSE_BYTES,
            label="MCP tool content",
        )
        structured_content = result.get("structuredContent")
        if structured_content is not None:
            _validate_json_tree(
                structured_content,
                max_bytes=MAX_RESPONSE_BYTES,
                label="MCP structured content",
            )
    except McpInputError as exc:
        raise McpProtocolError(str(exc)) from exc

    return McpToolCallResult(
        content=content,
        structured_content=structured_content,
        is_error=result.get("isError") is True,
        session_id=reply.session_id,
        protocol_version=reply.protocol_version,
    )
