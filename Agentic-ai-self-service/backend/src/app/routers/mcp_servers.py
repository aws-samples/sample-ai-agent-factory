"""MCP-server registry API — the verified external MCP catalog, browsable.

Read-only endpoints over ``services.mcp_catalog`` so the Registry UI can present
the verified external MCP servers that can be wired as an AgentCore Gateway
``mcpServer`` target (alongside published agent blueprints).

Tenant model (mirrors routers/connectors): the catalog carries NO tenant data —
it is identical for every caller. Endpoints are **auth-gated** (``registry:read``,
so a standard user who can browse the registry can also browse MCP servers) but
NOT owner-scoped. Unknown ids return 404 (non-disclosure), consistent with the
rest of the platform.

  GET /api/mcp-servers          list MCP-server summaries (optional ?tier=&?live_testable=)
  GET /api/mcp-servers/{id}     one MCP server's full detail (endpoint/auth/tools)
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services.auth import get_caller_sub
from app.services.mcp_catalog import get_mcp_server, list_mcp_servers
from app.services.rbac import require_scopes

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

router = APIRouter(prefix="/api/mcp-servers", tags=["mcp-servers"])


def _validate_id(server_id: str) -> str:
    if not server_id or len(server_id) > 64 or not _SLUG_RE.match(server_id):
        raise HTTPException(status_code=400, detail="Invalid MCP server id")
    return server_id


class McpServerSummary(BaseModel):
    """List view — the fields the Registry UI's MCP cards need."""

    id: str
    display_name: str
    publisher: str
    category: str
    tier: str
    verified: str
    auth_type: str
    live_testable: bool
    endpoint: str | None = None


class McpServerDetail(McpServerSummary):
    """Detail view — adds credential guidance + example tools."""

    credentials_needed: str
    example_tools: list[str]
    api_key_descriptor: dict | None = None
    oauth_descriptor: dict | None = None


def _summary(e: dict) -> McpServerSummary:
    return McpServerSummary(
        id=e["id"],
        display_name=e["display_name"],
        publisher=e["publisher"],
        category=e["category"],
        tier=e["tier"],
        verified=e["verified"],
        auth_type=e["auth_type"],
        live_testable=bool(e.get("live_testable")),
        endpoint=e.get("endpoint"),
    )


@router.get("", response_model=list[McpServerSummary], dependencies=[Depends(require_scopes("registry:read"))])
async def list_catalog(
    tier: str | None = None,
    live_testable: bool | None = None,
    caller_sub: str = Depends(get_caller_sub),
) -> list[McpServerSummary]:
    """List the verified external MCP-server catalog (auth-gated, not owner-scoped)."""
    entries = list_mcp_servers()
    if tier:
        entries = [e for e in entries if e["tier"] == tier]
    if live_testable is not None:
        entries = [e for e in entries if bool(e.get("live_testable")) == live_testable]
    return [_summary(e) for e in entries]


@router.get("/{server_id}", response_model=McpServerDetail, dependencies=[Depends(require_scopes("registry:read"))])
async def get_catalog_entry(
    server_id: str,
    caller_sub: str = Depends(get_caller_sub),
) -> McpServerDetail:
    """Return one MCP server's detail. 404 for unknown ids (non-disclosure)."""
    server_id = _validate_id(server_id)
    e = get_mcp_server(server_id)
    if e is None:
        raise HTTPException(status_code=404, detail="Not found")
    return McpServerDetail(
        **_summary(e).model_dump(),
        credentials_needed=e.get("credentials_needed", ""),
        example_tools=e.get("example_tools", []),
        api_key_descriptor=e.get("api_key_descriptor"),
        oauth_descriptor=e.get("oauth_descriptor"),
    )
