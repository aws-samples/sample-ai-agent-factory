"""Agent Spec v2, read surface (flag ``specV2Read``).

  GET  /api/specs/{flow_id}          the caller's saved flow projected as Spec v2: migrated on read,
                                     projected and content-hashed; nothing is written (the v1
                                     document stays the stored truth until the write flag ships)
  POST /api/specs/validate           strict validation of a posted v2 document: 200 with the
                                     normalized document and its contentHash, or 422 whose body
                                     names every offending field by JSON pointer
  POST /api/specs/{flow_id}/compile  the exact ``POST /api/deploy`` body the spec compiles to, so a
                                     caller can see (and the live probe can prove) that a migrated
                                     spec deploys through the unchanged pipeline

Every route is declared with ``require_feature("specV2Read")`` BEFORE its scope, so while the flag is
off each answers the exact ``404 {"detail": "Not Found"}`` of a nonexistent route to every caller.
Tenant model as in routers/flows.py: ``get_caller_sub`` identifies the caller, a flow that is not the
caller's answers 404, never 403. Scope ``agent:read`` for all three: validation and compilation are
pure functions of their input and write nothing.

Mounted on the WORKFLOW Lambda (``app/main.py``): it reads the flows table through the same
``get_flow_storage()`` the flows router uses. API Gateway routes live in infra/stacks/platform/api.py.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.schemas.agent_spec_v2 import AgentSpecV2
from app.services import spec_service
from app.services.auth import assert_owner, get_caller_sub
from app.services.feature_flags import require_feature
from app.services.flow_storage import get_flow_storage
from app.services.rbac import require_scopes
from app.services.spec_compiler import SpecCompileError, compile_deploy_request
from app.services.spec_migration import SpecMigrationError, is_spec_v2, migrate_v1_flow

logger = logging.getLogger(__name__)

FLAG = "specV2Read"
router = APIRouter(prefix="/api/specs", tags=["specs"])
_GATES = [Depends(require_feature(FLAG)), Depends(require_scopes("agent:read"))]
_FLOW_ID = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
#: The largest document a validate call accepts; the flows table caps items at 400 KB anyway.
MAX_DOCUMENT_BYTES = 400_000


class ValidationProblem(BaseModel):
    path: str = Field(description="JSON pointer to the offending field, e.g. /logic/nodes/0/type")
    message: str
    type: str


class ValidateResponse(BaseModel):
    ok: bool = True
    contentHash: str
    spec: dict[str, Any]


def _validate_flow_id(flow_id: str) -> str:
    if not _FLOW_ID.match(flow_id or ""):
        raise HTTPException(status_code=400, detail="Invalid flow_id format")
    return flow_id


def _load_owned_flow(flow_id: str, caller_sub: str):
    try:
        flow = get_flow_storage().get(flow_id)
    except ClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Storage service unavailable"
        ) from exc
    if flow is None:
        raise HTTPException(status_code=404, detail=f"Flow '{flow_id}' not found")
    assert_owner(getattr(flow, "owner_sub", None), caller_sub)
    return flow


def spec_for_flow(flow, caller_sub: str) -> AgentSpecV2:
    """The flow's document as a validated, projected, hashed v2 spec. Lazy migration, no write."""
    document = flow.workflow if isinstance(flow.workflow, dict) else {}
    try:
        if is_spec_v2(document):
            spec = spec_service.load(document)
        else:
            spec = migrate_v1_flow(
                document, spec_id=flow.id, name=flow.name, owner_sub=getattr(flow, "owner_sub", None) or caller_sub
            )
    except (SpecMigrationError, spec_service.SpecValidationError) as exc:
        logger.warning("Flow %s cannot be read as Spec v2: %s", flow.id, type(exc).__name__)
        raise HTTPException(
            status_code=422,
            detail={"message": "stored flow cannot be read as Spec v2", "errors": getattr(exc, "errors", [])},
        ) from exc
    return spec_service.normalize(spec)


@router.get("/{flow_id}", dependencies=_GATES)
async def read_spec(flow_id: str, caller_sub: str = Depends(get_caller_sub)) -> dict[str, Any]:
    flow = _load_owned_flow(_validate_flow_id(flow_id), caller_sub)
    spec = spec_for_flow(flow, caller_sub)
    return spec.model_dump(by_alias=True, mode="json")


@router.post("/validate", response_model=ValidateResponse, dependencies=_GATES)
async def validate_spec(request: Request) -> ValidateResponse:
    body = await request.body()
    if len(body) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="spec document too large")
    try:
        document = await request.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": "body is not JSON", "errors": [{"path": "", "message": str(exc)[:200], "type": "json"}]},
        ) from exc
    if not isinstance(document, dict):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "spec must be a JSON object",
                "errors": [{"path": "", "message": "expected an object", "type": "type"}],
            },
        )
    try:
        spec = spec_service.load(document)
    except spec_service.SpecValidationError as exc:
        raise HTTPException(status_code=422, detail={"message": "spec is invalid", "errors": exc.errors}) from None
    normalized = spec_service.normalize(spec)
    return ValidateResponse(
        contentHash=normalized.meta.content_hash or "", spec=normalized.model_dump(by_alias=True, mode="json")
    )


@router.post("/{flow_id}/compile", dependencies=_GATES)
async def compile_spec(flow_id: str, caller_sub: str = Depends(get_caller_sub)) -> dict[str, Any]:
    flow = _load_owned_flow(_validate_flow_id(flow_id), caller_sub)
    spec = spec_for_flow(flow, caller_sub)
    try:
        return compile_deploy_request(spec)
    except SpecCompileError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "errors": [{"path": "/logic/nodes", "message": str(exc), "type": "compile"}]},
        ) from None
