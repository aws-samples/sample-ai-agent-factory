"""Workspace policy admin API (plan §3.B, implementation step 1).

Routes (router prefix ``/api/workspaces``), every one behind the ``workspacePolicy`` feature flag
FIRST (off: the exact 404 of a nonexistent route) and the ``admin`` scope SECOND:

  GET    /api/workspaces/policies                 list every workspace policy in the org
  GET    /api/workspaces/{workspaceId}/policy     read one policy (404 when none is stored); ``ETag``
                                                  carries its revision
  PUT    /api/workspaces/{workspaceId}/policy     create (``If-None-Match: *``) or replace
                                                  (``If-Match: "<revision>"``) a policy (strict schema)
  DELETE /api/workspaces/{workspaceId}/policy     delete a policy (204; idempotent)

ROUTING: this router is mounted on the DEPLOYMENT Lambda (``app/deployment_handler.py``), not the
workflow Lambda, because policies live in the shared org-config table that only the deployment
Lambda has a grant for (tag policies and HITL approval policies already live there). The existing
``routers/workspaces.py`` (workflow sharing ACLs, ``GET /api/workspaces``) is a different concept
on the workflow Lambda; the two share the ``/api/workspaces`` prefix and nothing else, and these
paths are disjoint from its one GET.

Authorization: ``admin`` scope only (``g-admins-super`` today). A policy says what OTHER people may
deploy, so it is an org-admin control, not a per-workspace one; ``adminGroups`` on the policy is
carried for the later UI and for component C, not consulted here. The ``workspaceId`` in the path
must equal the one in the body on PUT, so a policy cannot be filed under another workspace's key.

Replacement is never blind. Every stored policy has a revision (served as the ``ETag`` of GET and
PUT, a quoted integer). A PUT must say what it is based on: ``If-Match: "<revision>"`` to replace the
policy as read, or ``If-None-Match: *`` to create one that does not exist yet. The store enforces the
precondition atomically with the write, so two admins editing the same policy cannot overwrite each
other: the second save is ``412 policy_revision_conflict`` naming the current revision and nothing
changes (a stale form can therefore never remove an approver group or an allow-list entry another
admin just added). A PUT with neither header is ``428 precondition_required``; a request that cannot
mean one thing (both headers, ``If-Match: *``, a non-numeric revision) is ``400``.

Fail closed: a store failure is 503 and nothing is written.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse

from app.schemas.workspace_policy import WorkspacePolicy
from app.services.feature_flags import require_feature
from app.services.rbac import require_scopes
from app.services.workspace_policy_service import (
    DEFAULT_ORG_ID,
    InvalidWorkspaceIdError,
    StoredPolicy,
    WorkspacePolicyError,
    WorkspacePolicyRevisionConflict,
    WorkspacePolicyStore,
    get_workspace_policy_store,
    validate_workspace_id,
)

logger = logging.getLogger(__name__)

FLAG = "workspacePolicy"
router = APIRouter(prefix="/api/workspaces", tags=["workspace-policies"])
_GATES = [Depends(require_feature(FLAG)), Depends(require_scopes("admin"))]

_STORE_UNAVAILABLE = "The workspace policy store is unavailable. Nothing was changed."

#: Wire-level ``detail.code`` values of the precondition refusals; tests and the UI assert them.
PRECONDITION_REQUIRED = "precondition_required"
POLICY_REVISION_CONFLICT = "policy_revision_conflict"

_PRECONDITION_HELP = (
    'send If-Match: "<revision>" (the ETag of the policy as you read it) to replace an existing policy, '
    "or If-None-Match: * to create one that does not exist yet"
)


def _store() -> WorkspacePolicyStore:
    return get_workspace_policy_store()


def _workspace_id(workspace_id: str) -> str:
    try:
        return validate_workspace_id(workspace_id)
    except InvalidWorkspaceIdError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _store_failure(exc: Exception, operation: str) -> HTTPException:
    # Type only: a botocore message echoes request parameters.
    logger.error("Workspace policy %s failed (%s)", operation, type(exc).__name__)
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_STORE_UNAVAILABLE)


def _as_document(policy: WorkspacePolicy) -> dict[str, Any]:
    return policy.model_dump(by_alias=True, mode="json")


def etag_of(revision: int) -> str:
    """The strong validator a revision is served as: a quoted integer."""
    return f'"{revision}"'


def _policy_response(record: StoredPolicy) -> Response:
    return JSONResponse(content=_as_document(record.policy), headers={"ETag": etag_of(record.revision)})


def parse_if_match(value: str) -> int:
    """The revision an ``If-Match`` names. Only a strong, numeric validator is accepted: ``*`` would
    mean "replace whatever is there", which is exactly the blind write this header exists to forbid,
    and a weak validator (``W/``) never satisfies ``If-Match``."""
    token = value.strip()
    if token == "*":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"If-Match: * would replace the policy without knowing what it says now; {_PRECONDITION_HELP}",
        )
    if len(token) >= 2 and token[0] == token[-1] == '"':
        token = token[1:-1]
    if not token.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"If-Match must be the policy's ETag, a quoted revision number; {_PRECONDITION_HELP}",
        )
    return int(token)


def expected_revision(if_match: str | None, if_none_match: str | None) -> int | None:
    """What the request claims the table holds: ``None`` for "nothing yet" (``If-None-Match: *``), a
    revision for ``If-Match``. Neither header is ``428``; both, or an ``If-None-Match`` other than
    ``*``, is ``400``: a write whose precondition is ambiguous is not attempted."""
    if if_match is not None and if_none_match is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"send either If-Match or If-None-Match, not both; {_PRECONDITION_HELP}",
        )
    if if_none_match is not None:
        if if_none_match.strip() != "*":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"If-None-Match must be *, meaning the policy must not exist yet; {_PRECONDITION_HELP}",
            )
        return None
    if if_match is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail={
                "code": PRECONDITION_REQUIRED,
                "message": (f"A workspace policy is never replaced blind: {_PRECONDITION_HELP}. Nothing was changed."),
            },
        )
    return parse_if_match(if_match)


def _revision_conflict(exc: WorkspacePolicyRevisionConflict) -> HTTPException:
    workspace_id, expected, current = exc.workspace_id, exc.expected_revision, exc.current_revision
    if expected is None:
        message = (
            f"A policy for workspace '{workspace_id}' already exists (revision {current}); read it and send "
            "If-Match with its ETag to replace it. Nothing was changed."
        )
    elif current is None:
        message = (
            f"The policy for workspace '{workspace_id}' no longer exists, so there was nothing at revision "
            f"{expected} to replace; send If-None-Match: * to create it. Nothing was changed."
        )
    else:
        message = (
            f"The policy for workspace '{workspace_id}' changed since it was read (revision {expected}, now "
            f"{current}); read the current policy and reapply the change. Nothing was changed."
        )
    return HTTPException(
        status_code=status.HTTP_412_PRECONDITION_FAILED,
        detail={
            "code": POLICY_REVISION_CONFLICT,
            "workspaceId": workspace_id,
            "expectedRevision": expected,
            "currentRevision": current,
            "message": message,
        },
    )


@router.get("/policies", dependencies=_GATES)
def list_policies() -> list[dict[str, Any]]:
    try:
        policies = _store().list(DEFAULT_ORG_ID)
    except (ClientError, BotoCoreError, WorkspacePolicyError) as exc:
        raise _store_failure(exc, "list") from exc
    return [_as_document(policy) for policy in policies]


@router.get("/{workspace_id}/policy", dependencies=_GATES)
def get_policy(workspace_id: str) -> Response:
    workspace_id = _workspace_id(workspace_id)
    try:
        record = _store().read(DEFAULT_ORG_ID, workspace_id)
    except (ClientError, BotoCoreError, WorkspacePolicyError) as exc:
        raise _store_failure(exc, "read") from exc
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No policy is stored for workspace '{workspace_id}'"
        )
    return _policy_response(record)


@router.put("/{workspace_id}/policy", dependencies=_GATES)
def put_policy(
    workspace_id: str,
    body: WorkspacePolicy,
    if_match: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    workspace_id = _workspace_id(workspace_id)
    if body.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"workspaceId in the body ({body.workspace_id!r}) must equal the one in the path ({workspace_id!r})"
            ),
        )
    expected = expected_revision(if_match, if_none_match)
    try:
        stored = _store().put(DEFAULT_ORG_ID, body, expected_revision=expected)
    except WorkspacePolicyRevisionConflict as exc:
        logger.info("Workspace policy write refused, revision conflict: workspace=%s", workspace_id)
        raise _revision_conflict(exc) from exc
    except (ClientError, BotoCoreError, WorkspacePolicyError) as exc:
        raise _store_failure(exc, "write") from exc
    logger.info("Workspace policy stored: workspace=%s revision=%s", workspace_id, stored.revision)
    return _policy_response(stored)


@router.delete("/{workspace_id}/policy", dependencies=_GATES, status_code=status.HTTP_204_NO_CONTENT)
def delete_policy(workspace_id: str) -> Response:
    workspace_id = _workspace_id(workspace_id)
    try:
        _store().delete(DEFAULT_ORG_ID, workspace_id)
    except (ClientError, BotoCoreError) as exc:
        raise _store_failure(exc, "delete") from exc
    logger.info("Workspace policy deleted: workspace=%s", workspace_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
