"""Phase 3 Gap 3E — pre-built connector catalog API.

Read-only catalog endpoints over ``services.connectors_catalog``:

  GET /api/connectors        list connector summaries
  GET /api/connectors/{id}   one connector's detail (tool + credential schema)

Tenant model (intentional, see connectors_catalog docstring):
  The catalog carries NO tenant data — it is identical for every caller. The
  endpoints are therefore **auth-gated** (``Depends(get_caller_sub)`` so an
  anonymous edge caller can't reach them) but are deliberately NOT owner-scoped:
  there is nothing tenant-specific to scope. Credential storage (the owner-
  scoped Secrets Manager write) is a documented follow-up hook, not built here.

Existence-non-disclosure: ``{id}`` is validated against a strict slug regex and
unknown ids return 404 (mirroring routers/registry.py), so probing the catalog
behaves consistently with the rest of the platform.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services.auth import get_caller_sub
from app.services.connectors_catalog import get_connector, list_connectors
from app.services.rbac import require_scopes

logger = logging.getLogger(__name__)

# Slug regex per the approved design. Connector ids are short lowercase slugs
# that may include '_' and '-' (e.g. "google_drive").
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_connector_id(connector_id: str) -> str:
    if not connector_id or len(connector_id) > 64 or not _SLUG_RE.match(connector_id):
        raise HTTPException(status_code=400, detail="Invalid connector id")
    return connector_id


router = APIRouter(prefix="/api/connectors", tags=["connectors"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ConnectorSummary(BaseModel):
    """List view — deliberately omits tool_schemas / credential_schema."""

    id: str
    display_name: str
    icon: str
    category: str
    auth_type: str
    capabilities: list[str]


class ConnectorToolSchema(BaseModel):
    name: str
    description: str
    inputSchema: dict


class ConnectorDetail(ConnectorSummary):
    """Detail view — includes the tool + credential schema descriptors."""

    credential_schema: dict
    tool_schemas: list[ConnectorToolSchema]


def _summary(entry: dict) -> ConnectorSummary:
    return ConnectorSummary(
        id=entry["id"],
        display_name=entry["display_name"],
        icon=entry["icon"],
        category=entry["category"],
        auth_type=entry["auth_type"],
        capabilities=entry["capabilities"],
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ConnectorSummary], dependencies=[Depends(require_scopes("connector:read"))])
async def list_catalog(
    caller_sub: str = Depends(get_caller_sub),
) -> list[ConnectorSummary]:
    """List the pre-built connector catalog (auth-gated, not owner-scoped)."""
    return [_summary(entry) for entry in list_connectors()]


@router.get("/{connector_id}", response_model=ConnectorDetail, dependencies=[Depends(require_scopes("connector:read"))])
async def get_catalog_entry(
    connector_id: str,
    caller_sub: str = Depends(get_caller_sub),
) -> ConnectorDetail:
    """Return one connector's detail. 404 for unknown ids (non-disclosure)."""
    connector_id = _validate_connector_id(connector_id)
    entry = get_connector(connector_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    return ConnectorDetail(
        id=entry["id"],
        display_name=entry["display_name"],
        icon=entry["icon"],
        category=entry["category"],
        auth_type=entry["auth_type"],
        capabilities=entry["capabilities"],
        credential_schema=entry["credential_schema"],
        tool_schemas=[ConnectorToolSchema(**ts) for ts in entry["tool_schemas"]],
    )
