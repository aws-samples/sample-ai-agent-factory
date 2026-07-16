"""Live model catalog API (Loom-study 5.1).

  GET /api/models  → the merged live Bedrock model catalog for the picker.

Auth-gated (any signed-in user can list models — it's needed to configure a
deploy). No tenant data; not owner-scoped. Read-only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.services.auth import get_caller_sub
from app.services.model_catalog import list_models
from app.services.rbac import require_scopes

router = APIRouter(prefix="/api/models", tags=["models"])


class ModelOption(BaseModel):
    provider: str
    modelId: str
    label: str
    maxTokens: int
    source: str | None = None


@router.get("", response_model=list[ModelOption], dependencies=[Depends(require_scopes("agent:read"))])
async def get_models(caller_sub: str = Depends(get_caller_sub)) -> list[ModelOption]:
    """Return the live Bedrock model catalog (curated overlay + live discovery)."""
    return [ModelOption(**m) for m in list_models()]
