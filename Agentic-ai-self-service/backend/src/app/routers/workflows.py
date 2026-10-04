"""Workflow CRUD API endpoints.

This module provides REST API endpoints for workflow management:
- POST /api/workflows - Create workflow
- GET /api/workflows/{id} - Get workflow
- PUT /api/workflows/{id} - Update workflow
- DELETE /api/workflows/{id} - Delete workflow
- POST /api/workflows/{id}/deploy - Deploy workflow

Requirements: 9.1, 9.5, 11.1, 11.5, 11.6, 11.7
"""

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.services.auth import assert_owner, get_caller_sub
from app.services.rbac import require_scopes
from app.services.storage import WorkflowRevisionConflict


def _validate_workflow_id(workflow_id: str) -> str:
    """Validate workflow_id format to prevent injection attacks.

    SECURITY: Workflow IDs should be UUIDs. This rejects any ID containing
    characters that could be used for path traversal or injection.
    """
    if not workflow_id or len(workflow_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid workflow_id")
    if not re.match(r"^[a-zA-Z0-9_-]+$", workflow_id):
        raise HTTPException(status_code=400, detail="Invalid workflow_id format")
    return workflow_id


# Fields that say who may touch a record rather than what the canvas contains. They are
# stripped on export and ignored on import: a caller who could carry them across would
# be choosing a record's owner, its ACL, and the Secrets Manager reference its git sync
# reads — i.e. writing into someone else's workspace with a POST they are allowed to
# make. ARCC ``cnt_1vtvHlE7JwCaFm`` (authority must come from the authenticated
# principal, never from the request body).
_NON_EXPORTABLE_FIELDS = ("owner_sub", "acl", "workspace_id", "git_source")


def _revision_conflict(workflow_id: str, exc: WorkflowRevisionConflict) -> HTTPException:
    """The 409 a stale save gets (F-15): the server's current revision, nothing written.

    Same shape as the flows router's ``flow_version_conflict`` body so one client can handle
    both; ``currentVersion`` carries the same integer as ``currentRevision`` for that reason and
    is NOT the workflow's semver ``version``. Ownership is checked before the store is asked, so a
    non-owner never sees this body.
    """
    current = exc.current
    logger.info(
        "Workflow save refused: %s built on revision %s, row at %s",
        workflow_id,
        exc.expected,
        "missing" if current is None else current.revision,
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "workflow_revision_conflict",
            "message": (
                "This workflow was changed elsewhere since you loaded it. "
                "Reload to see the latest revision, or save again against it to overwrite."
            ),
            "currentRevision": None if current is None else current.revision,
            "currentVersion": None if current is None else current.revision,
            "updatedAt": None if current is None else current.updated_at.isoformat(),
        },
    )


def _raise_for_unreadable_row(storage, workflow_id: str, caller_sub: str, exc: Exception) -> None:
    """Turn "the stored row will not deserialize" into an actionable answer.

    An opaque 500 tells the owner nothing, and the pydantic detail cannot be returned
    because it quotes the stored content that failed to parse. So: say what is wrong
    and what to do, to the OWNER only. For anyone else this stays a 404, because
    "invalid record" would otherwise confirm that the id exists.

    The ACL cannot be consulted at all here — reading it is what failed — so a shared
    editor of a corrupt row gets the 404 too. That is the fail-closed direction
    (ARCC ``cnt_94E30Xo4RZHtSJ``); the owner can always delete and re-create.

    Always raises.
    """
    logger.warning("Workflow %s is stored in a shape that will not deserialize", workflow_id)
    raw_owner = getattr(storage, "get_owner_sub_unvalidated", None)
    exists, owner_sub = raw_owner(workflow_id) if raw_owner else (False, None)
    if exists and owner_sub is not None and owner_sub == caller_sub:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Workflow '{workflow_id}' is stored in an invalid shape and cannot be loaded. "
                "DELETE it and re-create it."
            ),
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Workflow with ID '{workflow_id}' not found",
    ) from exc


from app.models import (
    ComponentNode,
    ConnectionEdge,
    DeploymentResult,
    ValidationResult,
    Viewport,
    WorkflowDefinition,
    WorkflowMetadata,
)
from app.models.workflow import DeploymentGovernanceV1
from app.services.storage import get_workflow_storage
from app.services.validation import ValidationEngine

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

# Validation engine instance
validation_engine = ValidationEngine()


class WorkflowCreateRequest(BaseModel):
    """Request body for creating a workflow.

    ``nodes`` and ``edges`` are TYPED, and that is load-bearing rather than tidy.
    They used to be a bare ``list``, so FastAPI validated nothing about their
    contents and the shape error surfaced later, in the wrong place — see
    :class:`WorkflowUpdateRequest`, where it corrupted the stored record.

    ARCC ``cnt_VlYhNEFt6msJmr`` exit criterion: "Strong input validation is
    performed on objects before deserialization takes place." ``cnt_94E30Xo4RZHtSJ``
    (fail closed) is why the refusal belongs at the boundary and not downstream.
    """

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=2000, default="")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", default="1.0.0")
    nodes: list[ComponentNode] = Field(default_factory=list)
    edges: list[ConnectionEdge] = Field(default_factory=list)
    viewport: Viewport | None = None
    metadata: WorkflowMetadata
    governance: DeploymentGovernanceV1 = Field(default_factory=DeploymentGovernanceV1)


class WorkflowUpdateRequest(BaseModel):
    """Request body for updating a workflow.

    ``nodes``/``edges`` were a bare ``list | None`` here, and this endpoint is where
    that cost real data. Measured live against a deployed stack:

      1. ``PUT`` with ``nodes=[{"id":"x","type":"agent","data":{"nope":1}}]`` passed
         request validation, because nothing described a node.
      2. ``update_workflow`` merged it with ``existing.model_copy(update=...)``, and
         ``model_copy`` does NOT validate — pydantic copies the raw dicts straight
         into ``WorkflowDefinition.nodes``.
      3. The row was written to DynamoDB, and only THEN did building the response
         raise ``ValidationError`` — an unhandled 500 with no detail.
      4. From that moment ``_deserialize_workflow`` raised on every read, so
         ``GET``, ``PUT``, ``DELETE`` and ``/validate`` all returned 500. Both
         ``delete_workflow`` and ``update_workflow`` read-before-write, so the row
         could be neither repaired nor removed through the API.
      5. ``list_all`` swallows the per-item failure with a ``logger.warning``, so
         ``GET /api/workflows`` still returned ``[]`` and the damage was invisible.

    Typing these two fields closes the whole chain at step 1, which is the only step
    where the caller still gets an actionable answer (a 422 naming the bad field).
    """

    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    version: str | None = Field(None, pattern=r"^\d+\.\d+\.\d+$")
    nodes: list[ComponentNode] | None = None
    edges: list[ConnectionEdge] | None = None
    viewport: Viewport | None = None
    metadata: WorkflowMetadata | None = None
    governance: DeploymentGovernanceV1 | None = None
    # F-15: the ``revision`` this save was built on. Absent == a client that does not send one
    # (still fenced by the store on the row it read). Never copied into the row: the store stamps.
    expected_revision: int | None = Field(None, alias="expectedRevision", ge=0)
    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_governance(cls, value):
        """Absent means "preserve"; an explicit null must never erase governance."""
        if isinstance(value, dict) and "governance" in value and value["governance"] is None:
            raise ValueError("governance must be an object when provided")
        return value


class WorkflowResponse(BaseModel):
    """Response for workflow operations."""

    workflow: WorkflowDefinition
    message: str


class DeleteResponse(BaseModel):
    """Response for delete operation."""

    success: bool
    message: str


@router.post(
    "",
    response_model=WorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scopes("agent:write"))],
)
async def create_workflow(
    request: WorkflowCreateRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> WorkflowResponse:
    """Create a new workflow.

    Requirements: 9.1
    """
    import uuid

    now = datetime.now(timezone.utc)

    # A ``ValidationError`` here is a CLIENT error, so it must not become a 500.
    # ``WorkflowDefinition`` carries a cross-field validator the request model cannot
    # replicate (``validate_edge_references``), so an edge naming a node that is not
    # in ``nodes`` reaches this constructor however well-typed the request is.
    # Reported without the pydantic detail, which quotes the value it rejected.
    try:
        workflow = WorkflowDefinition(
            id=str(uuid.uuid4()),
            name=request.name,
            description=request.description,
            version=request.version,
            nodes=request.nodes,
            edges=request.edges,
            viewport=request.viewport or Viewport(x=0, y=0, zoom=1.0),
            metadata=request.metadata,
            governance=request.governance,
            created_at=now,
            updated_at=now,
            owner_sub=caller_sub,
        )
    except ValidationError as e:
        logger.warning("Rejected a workflow that would not validate: %s", e)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The workflow is not valid; nothing was created",
        ) from e

    try:
        created = get_workflow_storage().create(workflow)
        return WorkflowResponse(
            workflow=created,
            message="Workflow created successfully",
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workflow already exists",
        ) from e


@router.get("", response_model=list[WorkflowDefinition], dependencies=[Depends(require_scopes("agent:read"))])
async def list_workflows(
    caller_sub: str = Depends(get_caller_sub),
) -> list[WorkflowDefinition]:
    """List workflows owned by the caller.

    Tenant-isolation (Critic Finding 3): strict ``owner_sub == caller_sub``.
    Pre-tenancy records (``owner_sub=None``) are excluded for every caller;
    making them visible to "local-dev only" via a wildcard is the same trap
    as the legacy-row bypass — fix is an explicit backfill, not a wildcard.
    Filtering happens in Python because the underlying storage may be
    DynamoDB (no GSI on owner_sub yet) or in-memory; flip to a query when
    scale demands it.
    """
    from app.services.workspace_acl import Acl, can_view

    storage = get_workflow_storage()
    # Gap 2E: include workflows shared with the caller (editor/viewer) in
    # addition to owned ones. list_by_owner (if present) only returns owned
    # rows, so when it exists we still scan list_all for shared rows and union.
    list_by_owner = getattr(storage, "list_by_owner", None)
    owned: list = list(list_by_owner(caller_sub)) if callable(list_by_owner) else []
    owned_ids = {wf.id for wf in owned}

    result = list(owned)
    for wf in storage.list_all():
        if wf.id in owned_ids:
            continue
        owner_sub = getattr(wf, "owner_sub", None)
        if owner_sub == caller_sub or can_view(
            Acl.normalize(getattr(wf, "acl", None), owner_sub=owner_sub),
            caller_sub,
            owner_sub=owner_sub,
        ):
            result.append(wf)
    return result


@router.get("/{workflow_id}", response_model=WorkflowDefinition, dependencies=[Depends(require_scopes("agent:read"))])
async def get_workflow(
    workflow_id: str,
    caller_sub: str = Depends(get_caller_sub),
) -> WorkflowDefinition:
    """Get a workflow by ID. Caller must own it OR be a shared viewer/editor.

    Gap 2E (Bug M-1 fix): a workflow shared with the caller (acl.viewers /
    acl.editors) is viewable. Owner-only used to 404 shared editors who could
    see the row in the LIST endpoint — an inconsistency the security review
    flagged. Denial still returns 404 (existence-non-disclosure), never 403.

    Requirements: 9.5
    """
    from app.services.workspace_acl import Acl, can_view

    workflow_id = _validate_workflow_id(workflow_id)
    storage = get_workflow_storage()
    try:
        workflow = storage.get(workflow_id)
    except ValidationError as e:
        _raise_for_unreadable_row(storage, workflow_id, caller_sub, e)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    owner_sub = getattr(workflow, "owner_sub", None)
    if not can_view(
        Acl.normalize(getattr(workflow, "acl", None), owner_sub=owner_sub),
        caller_sub,
        owner_sub=owner_sub,
    ):
        # 404 (not 403) — don't disclose existence to unauthorized callers.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    return workflow


@router.put("/{workflow_id}", response_model=WorkflowResponse, dependencies=[Depends(require_scopes("agent:write"))])
async def update_workflow(
    workflow_id: str,
    request: WorkflowUpdateRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> WorkflowResponse:
    """Update an existing workflow. Caller must own it OR be a shared editor.

    Gap 2E (Bug M-1 fix): editors granted via acl.editors can update the
    workflow's nodes/edges/etc. Viewers cannot (can_edit is False for them).
    Denial returns 404 (existence-non-disclosure), never 403. The acl + owner
    fields themselves are NOT updatable here — sharing goes through the
    dedicated /share endpoint (routers/workspaces.py) which is owner-only — so
    an editor cannot escalate themselves to owner or re-share.

    Requirements: 9.1
    """
    from app.services.workspace_acl import Acl, can_edit

    workflow_id = _validate_workflow_id(workflow_id)
    storage = get_workflow_storage()
    try:
        existing = storage.get(workflow_id)
    except ValidationError as e:
        # A corrupt row cannot be repaired by a PUT, because the merge starts from the
        # parsed record. Telling the owner to delete it is the only honest answer; it
        # used to be an opaque 500 that made the row look permanently wedged.
        _raise_for_unreadable_row(storage, workflow_id, caller_sub, e)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    owner_sub = getattr(existing, "owner_sub", None)
    if not can_edit(
        Acl.normalize(getattr(existing, "acl", None), owner_sub=owner_sub),
        caller_sub,
        owner_sub=owner_sub,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )

    # Build updated workflow with only provided fields
    update_data = {}
    if request.name is not None:
        update_data["name"] = request.name
    if request.description is not None:
        update_data["description"] = request.description
    if request.version is not None:
        update_data["version"] = request.version
    if request.nodes is not None:
        update_data["nodes"] = request.nodes
    if request.edges is not None:
        update_data["edges"] = request.edges
    if request.viewport is not None:
        update_data["viewport"] = request.viewport
    if request.metadata is not None:
        update_data["metadata"] = request.metadata
    if "governance" in request.model_fields_set:
        update_data["governance"] = request.governance

    updated_workflow = existing.model_copy(update=update_data)

    # ``model_copy`` copies values in VERBATIM — it runs no validators at all, not
    # even the model's own ``@model_validator``. Two distinct things need this call:
    #
    #   * ``WorkflowDefinition.validate_edge_references`` is a cross-field check that
    #     no request model CAN make, because a PUT may send edges while keeping the
    #     stored nodes. Without this, ``edges=[{source: "nope"}]`` is persisted and
    #     the row becomes unreadable.
    #   * the next field added to ``WorkflowUpdateRequest`` with a loose type would
    #     otherwise walk the same path straight into DynamoDB, and fail as a 500
    #     AFTER the write, leaving a row that cannot be read, repaired or deleted.
    #
    # The detail deliberately does not echo the pydantic error: on this path the
    # rejected value came from the caller, but the merged object also carries stored
    # content, and a validation message quotes the input it rejected.
    try:
        WorkflowDefinition.model_validate(updated_workflow.model_dump())
    except ValidationError as e:
        logger.warning("Rejected an update to %s that would not re-validate: %s", workflow_id, e)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The updated workflow is not valid; no changes were saved",
        ) from e

    try:
        result = get_workflow_storage().update(
            workflow_id, updated_workflow, expected_revision=request.expected_revision
        )
    except WorkflowRevisionConflict as exc:
        raise _revision_conflict(workflow_id, exc) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )

    return WorkflowResponse(
        workflow=result,
        message="Workflow updated successfully",
    )


@router.delete("/{workflow_id}", response_model=DeleteResponse, dependencies=[Depends(require_scopes("agent:write"))])
async def delete_workflow(
    workflow_id: str,
    caller_sub: str = Depends(get_caller_sub),
) -> DeleteResponse:
    """Delete a workflow by ID. Caller must own it.

    A row that cannot be DESERIALIZED is still deletable by its owner. That is not a
    nicety: the ownership check reads ``owner_sub`` off the parsed model, so when
    parsing is what fails, delete fails before it authorizes anything and the row is
    permanently stuck — and ``list_all`` hides it, so the owner cannot even see what
    they are stuck with. Rows written before the validation fix above are exactly
    this shape, so the escape hatch reads ``owner_sub`` as a raw attribute instead.
    The caller must still own it; an unreadable row is not an unowned one.

    Requirements: 9.1
    """
    workflow_id = _validate_workflow_id(workflow_id)
    storage = get_workflow_storage()
    try:
        existing = storage.get(workflow_id)
    except ValidationError as e:
        logger.warning("Workflow %s cannot be deserialized; deleting it unparsed", workflow_id)
        raw_owner = getattr(storage, "get_owner_sub_unvalidated", None)
        if raw_owner is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Workflow record is unreadable and this storage backend cannot remove it",
            ) from e
        exists, owner_sub = raw_owner(workflow_id)
        if not exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow with ID '{workflow_id}' not found",
            ) from e
        assert_owner(owner_sub, caller_sub)
        storage.delete(workflow_id)
        return DeleteResponse(
            success=True,
            message=f"Workflow '{workflow_id}' was unreadable and has been deleted",
        )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    assert_owner(getattr(existing, "owner_sub", None), caller_sub)

    deleted = storage.delete(workflow_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )

    return DeleteResponse(
        success=True,
        message=f"Workflow '{workflow_id}' deleted successfully",
    )


@router.post(
    "/{workflow_id}/validate", response_model=ValidationResult, dependencies=[Depends(require_scopes("agent:write"))]
)
async def validate_workflow(
    workflow_id: str,
    caller_sub: str = Depends(get_caller_sub),
) -> ValidationResult:
    """Validate a workflow configuration. Caller must own it OR be a shared viewer.

    The ownership check is NOT redundant with the ``agent:write`` scope. Without it,
    any authenticated caller could validate any workflow id in the table, and a
    ``ValidationResult`` names the offending nodes and their configuration problems —
    so the endpoint disclosed another tenant's canvas, and its 404-vs-200 answered
    "does this id exist". Denial returns 404, matching :func:`get_workflow`.

    Requirements: 8.1, 8.2, 8.3
    """
    from app.services.workspace_acl import Acl, can_view

    workflow_id = _validate_workflow_id(workflow_id)
    storage = get_workflow_storage()
    try:
        workflow = storage.get(workflow_id)
    except ValidationError as e:
        _raise_for_unreadable_row(storage, workflow_id, caller_sub, e)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    owner_sub = getattr(workflow, "owner_sub", None)
    if not can_view(
        Acl.normalize(getattr(workflow, "acl", None), owner_sub=owner_sub),
        caller_sub,
        owner_sub=owner_sub,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )

    result = validation_engine.validate_workflow(workflow)
    return result


# ============================================================================
# Import/Export Endpoints
# ============================================================================


class ImportRequest(BaseModel):
    """Request body for importing a workflow from JSON."""

    workflow_json: dict


class ImportResponse(BaseModel):
    """Response for import operation."""

    workflow: WorkflowDefinition
    message: str
    validation_errors: list[str] = Field(default_factory=list)


class ImportErrorResponse(BaseModel):
    """Response for failed import operation."""

    success: bool = False
    errors: list[str]


class ExportResponse(BaseModel):
    """Response for export operation."""

    workflow_json: dict
    message: str


@router.post("/import", response_model=ImportResponse, dependencies=[Depends(require_scopes("agent:write"))])
async def import_workflow(
    request: ImportRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> ImportResponse:
    """Import a workflow from JSON. The imported row is owned by the CALLER.

    Ownership used to come from the request body, which made this endpoint a
    cross-tenant write: ``workflow_json`` was handed to ``model_validate`` whole, so an
    ``agent:write`` caller could import a canvas with ``owner_sub`` set to somebody
    else's Cognito sub — or with an ``acl`` naming themselves an editor of it, or a
    ``git_source.token_ref`` pointing at a secret in another tenant's namespace — and
    the row was created exactly as described. Omitting the field was no better: it
    stored ``owner_sub=None``, which ``assert_owner`` treats as a legacy row and hides
    from every caller, so the import silently produced a record nobody could read or
    delete.

    Both are the same bug — authority taken from the body instead of the token — so all
    four authority fields are dropped and ``owner_sub`` is stamped from the verified
    caller. ARCC ``cnt_1vtvHlE7JwCaFm``.

    Requirements: 14.1, 14.2, 14.3
    """
    import uuid

    from pydantic import ValidationError as PydanticValidationError

    validation_errors: list[str] = []

    try:
        # Copy before mutating: ``request.workflow_json`` is the caller's dict and the
        # error paths below re-read it.
        workflow_data = dict(request.workflow_json)
        for authority_field in _NON_EXPORTABLE_FIELDS:
            workflow_data.pop(authority_field, None)
        workflow_data["owner_sub"] = caller_sub

        # Generate new ID if not provided or if it conflicts
        if "id" not in workflow_data or not workflow_data["id"]:
            workflow_data["id"] = str(uuid.uuid4())

        # Set timestamps if not provided
        now = datetime.now(timezone.utc)
        if "created_at" not in workflow_data:
            workflow_data["created_at"] = now.isoformat()
        if "updated_at" not in workflow_data:
            workflow_data["updated_at"] = now.isoformat()

        # Parse and validate the workflow
        workflow = WorkflowDefinition.model_validate(workflow_data)

        # Check if workflow with same ID exists. A row that exists but cannot be
        # deserialized still OCCUPIES the id, so a parse failure here means "taken",
        # not "free" — treating it as free would overwrite it, and an id taken from
        # the request body is one the caller chose.
        try:
            existing = get_workflow_storage().get(workflow.id)
        except ValidationError:
            existing = True
        if existing:
            # Generate new ID to avoid conflict
            workflow = workflow.model_copy(update={"id": str(uuid.uuid4())})

        # Store the workflow
        created = get_workflow_storage().create(workflow)

        return ImportResponse(
            workflow=created,
            message="Workflow imported successfully",
            validation_errors=validation_errors,
        )

    except PydanticValidationError as e:
        # Extract validation error messages
        errors = []
        for error in e.errors():
            loc = ".".join(str(x) for x in error["loc"])
            msg = error["msg"]
            errors.append(f"{loc}: {msg}")

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "errors": errors,
                "message": "Invalid workflow JSON schema",
            },
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "errors": [str(e)],
                "message": "Failed to import workflow",
            },
        ) from e


@router.get(
    "/{workflow_id}/export", response_model=ExportResponse, dependencies=[Depends(require_scopes("agent:read"))]
)
async def export_workflow(
    workflow_id: str,
    caller_sub: str = Depends(get_caller_sub),
) -> ExportResponse:
    """Export a workflow as JSON. Caller must own it OR be a shared viewer/editor.

    This endpoint had NO ownership check: an ``agent:read`` scope plus a workflow id
    returned another tenant's entire canvas — every node, every configuration, and the
    ``git_source``/``acl`` fields with it. The scope answers "may this caller read
    workflows", never "may they read THIS one". Same 404-on-denial as
    :func:`get_workflow`, so it is not an existence oracle either.

    ``owner_sub``, ``acl``, ``workspace_id`` and ``git_source`` are stripped from the
    exported document. They describe who may touch the record, not what the canvas is,
    and the export is the input to :func:`import_workflow` — round-tripping them is
    how a caller would try to claim a record for someone else.

    Requirements: 14.1, 14.2
    """
    from app.services.workspace_acl import Acl, can_view

    workflow_id = _validate_workflow_id(workflow_id)
    storage = get_workflow_storage()
    try:
        workflow = storage.get(workflow_id)
    except ValidationError as e:
        _raise_for_unreadable_row(storage, workflow_id, caller_sub, e)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )
    owner_sub = getattr(workflow, "owner_sub", None)
    if not can_view(
        Acl.normalize(getattr(workflow, "acl", None), owner_sub=owner_sub),
        caller_sub,
        owner_sub=owner_sub,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' not found",
        )

    # Convert to JSON-serializable dict
    workflow_json = workflow.model_dump(mode="json")
    for authority_field in _NON_EXPORTABLE_FIELDS:
        workflow_json.pop(authority_field, None)

    return ExportResponse(
        workflow_json=workflow_json,
        message="Workflow exported successfully",
    )


# ============================================================================
# Deployment Endpoint
# ============================================================================


class DeployRequest(BaseModel):
    """Request body for deploying a workflow."""

    aws_region: str = Field(pattern=r"^[a-z]{2}(-[a-z]+-\d+)?$", max_length=30)
    vpc_config: dict | None = None
    enable_cloudwatch: bool = True
    enable_cloudtrail: bool = True


@router.post(
    "/{workflow_id}/deploy", response_model=DeploymentResult, dependencies=[Depends(require_scopes("agent:write"))]
)
async def deploy_workflow(
    workflow_id: str,
    request: DeployRequest,
) -> DeploymentResult:
    """Refuse the retired in-process deployment implementation.

    ``WorkflowExecutor`` created AWS resources inside the workflow API process,
    but never created the persistent DeploymentState row that its manifest
    appends require. A failed append was logged and ignored, so this route could
    report success for resources that the product delete path could never
    enumerate. It also required broad create/delete permissions on the
    internet-facing workflow API Lambda.

    There is now one deployment boundary everywhere—local and Lambda:
    ``POST /api/deploy``. It creates the durable row first and then uses the
    Step Functions workflow whose per-step roles, compensation, manifest seal,
    and failure cleanup are exercised in production. Keeping a local-only
    escape hatch would preserve the unjournaled path under a different flag.
    """
    del workflow_id, request
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "This legacy in-process deployment route is retired. "
            "Use POST /api/deploy, which runs the durable deployment state machine."
        ),
    )
