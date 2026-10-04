"""Product-owned MCP discovery and tool-call routes for AgentCore runtimes."""

from __future__ import annotations

import logging
from typing import Any

from botocore.config import Config as BotoConfig
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.services.auth import get_caller_sub
from app.services.rbac import require_scopes
from app.services.runtime_mcp import (
    McpInputError,
    McpPermissionError,
    McpProtocolError,
    McpRemoteError,
    McpRuntimeUnavailable,
    call_tool,
    list_tools,
)
from app.services.runtime_target_context import (
    resolve_owned_deployment_runtime_target,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/test-mcp-runtime", tags=["runtime-mcp"])

_AGENTCORE_CONFIG = BotoConfig(
    connect_timeout=2,
    read_timeout=8,
    retries={"max_attempts": 0},
)


class _McpRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    deployment_id: str = Field(
        alias="deploymentId",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class McpToolsRequest(_McpRequest):
    pass


class McpToolCallRequest(_McpRequest):
    tool_name: str = Field(
        alias="toolName",
        min_length=1,
        max_length=256,
    )
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = Field(
        alias="sessionId",
        default=None,
        min_length=1,
        max_length=1024,
        pattern=r"^[\x21-\x7e]+$",
    )


class McpToolsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    protocol_version: str = Field(alias="protocolVersion")
    session_id: str | None = Field(alias="sessionId", default=None)
    server_info: dict[str, str] = Field(alias="serverInfo")
    tools: list[dict[str, Any]]


class McpToolCallResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    protocol_version: str = Field(alias="protocolVersion")
    session_id: str | None = Field(alias="sessionId", default=None)
    content: list[dict[str, Any]]
    structured_content: Any = Field(alias="structuredContent", default=None)
    is_error: bool = Field(alias="isError", default=False)


def _mcp_http_error(operation: str, exc: Exception) -> HTTPException:
    """Map bounded service errors without exposing AWS/runtime internals."""

    code = exc.code if isinstance(exc, McpRemoteError) else None
    logger.warning(
        "Product MCP %s failed (%s%s)",
        operation,
        type(exc).__name__,
        f", code={code}" if code is not None else "",
    )
    if isinstance(exc, McpInputError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, McpRemoteError):
        return HTTPException(
            status_code=422,
            detail="The MCP runtime rejected this operation.",
        )
    if isinstance(exc, McpProtocolError):
        return HTTPException(
            status_code=502,
            detail="The MCP runtime returned an invalid protocol response.",
        )
    if isinstance(exc, McpPermissionError):
        # A permanent platform misconfiguration, not a transient: say so, name the
        # missing action (an operator fact, not an AWS internal), and do not invite a retry.
        return HTTPException(status_code=500, detail=str(exc))
    return HTTPException(
        status_code=503,
        detail="The MCP runtime is temporarily unavailable. Try again shortly.",
    )


def _mcp_client(deployment_id: str, caller_sub: str):
    target = resolve_owned_deployment_runtime_target(
        deployment_id,
        caller_sub,
        required_protocol="MCP",
    )
    try:
        client = target.client(
            "bedrock-agentcore",
            config=_AGENTCORE_CONFIG,
        )
    except Exception as exc:
        logger.warning(
            "Could not create AgentCore MCP client (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="The MCP runtime is temporarily unavailable. Try again shortly.",
        ) from exc
    return target, client


@router.post(
    "/tools",
    response_model=McpToolsResponse,
    response_model_by_alias=True,
    dependencies=[Depends(require_scopes("invoke"))],
)
async def discover_runtime_tools(
    request: McpToolsRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> McpToolsResponse:
    target, client = _mcp_client(request.deployment_id, caller_sub)
    try:
        result = list_tools(
            client,
            runtime_arn=target.runtime_arn,
            runtime_user_id=caller_sub,
        )
    except (McpInputError, McpPermissionError, McpProtocolError, McpRemoteError, McpRuntimeUnavailable) as exc:
        raise _mcp_http_error("tool discovery", exc) from exc
    return McpToolsResponse(
        protocolVersion=result.protocol_version,
        sessionId=result.session_id,
        serverInfo=result.server_info,
        tools=result.tools,
    )


@router.post(
    "/call",
    response_model=McpToolCallResponse,
    response_model_by_alias=True,
    dependencies=[Depends(require_scopes("invoke"))],
)
async def call_runtime_tool(
    request: McpToolCallRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> McpToolCallResponse:
    target, client = _mcp_client(request.deployment_id, caller_sub)
    try:
        result = call_tool(
            client,
            runtime_arn=target.runtime_arn,
            runtime_user_id=caller_sub,
            tool_name=request.tool_name,
            arguments=request.arguments,
            session_id=request.session_id,
        )
    except (McpInputError, McpPermissionError, McpProtocolError, McpRemoteError, McpRuntimeUnavailable) as exc:
        raise _mcp_http_error("tool call", exc) from exc
    return McpToolCallResponse(
        protocolVersion=result.protocol_version,
        sessionId=result.session_id,
        content=result.content,
        structuredContent=result.structured_content,
        isError=result.is_error,
    )
