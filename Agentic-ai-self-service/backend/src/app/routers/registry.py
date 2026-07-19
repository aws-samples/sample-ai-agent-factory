"""Agent Registry API — Phase 2 Gap 2A.

Org-wide catalog for publishing, discovering, and cloning agents.

Endpoints:
  POST   /api/registry                  publish a deployed agent (canvas snapshot)
  GET    /api/registry?q=&tag=&scope=   search/list (visibility-filtered)
  GET    /api/registry/{slug}           fetch one entry (visibility-checked)
  POST   /api/registry/{slug}/clone     clone the canvas snapshot to the caller
  PUT    /api/registry/{slug}           update metadata/visibility (owner only)
  DELETE /api/registry/{slug}           unpublish (owner only)

Tenant model (see registry_store docstring):
  - ``private`` entries are visible only to ``owner_sub``.
  - ``org`` entries are visible to everyone in the same ``org_id``.
  - ``public`` entries are visible cross-org.
  - Mutations require ``owner_sub == caller`` (404-on-mismatch via assert_owner).

Until Gap 2E wires Cognito-group-backed orgs, every caller is in
``DEFAULT_ORG_ID`` so ``org`` ≈ "all platform users". This is called out in
the responses via the ``org_id`` field so the frontend can label it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services.auth import (
    _LOCAL_DEV_SUB,
    assert_owner,
    get_caller_sub,
    is_registry_admin,
)
from app.services.rbac import require_scopes
from app.services.registry_store import (
    DEFAULT_ORG_ID,
    RegistryEntry,
    get_registry_store,
    slugify,
)

logger = logging.getLogger(__name__)


def _validate_slug(slug: str) -> str:
    if not slug or len(slug) > 128:
        raise HTTPException(status_code=400, detail="Invalid agent_slug")
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", slug):
        raise HTTPException(status_code=400, detail="Invalid agent_slug format")
    return slug


def _caller_org_id(caller_sub: str) -> str:
    # Gap 2E will derive this from a Cognito group claim. For now everyone is
    # in the default org so org-visible entries are shared platform-wide.
    return DEFAULT_ORG_ID


def caller_is_admin(
    request: Request,
    caller_sub: str = Depends(get_caller_sub),
) -> bool:
    """Whether the caller has registry-admin privileges, as a FastAPI dependency.

    Wraps services.auth.is_registry_admin (group-based, local-dev=True) so the
    router gets a single, override-able injection point for admin status. Tests
    override THIS dependency to simulate admin vs developer without real Cognito.

    Guard: is_registry_admin returns True in local dev (no aws.event). To avoid
    that local-dev escalation leaking into requests where a *specific* caller
    identity has been injected (i.e. the caller is NOT the local-dev sentinel),
    we only honour the local-dev admin grant for the local-dev sentinel sub.
    This preserves single-user local full-access while keeping per-caller tenant
    isolation intact for explicitly-identified callers.
    """
    admin = is_registry_admin(request)
    aws_event = request.scope.get("aws.event") if request.scope else None
    if aws_event is None and caller_sub != _LOCAL_DEV_SUB:
        # Identity was explicitly injected (e.g. a unit-test caller); the
        # local-dev blanket-admin grant must not apply to a named caller.
        return False
    return admin


router = APIRouter(prefix="/api/registry", tags=["registry"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class PublishRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    visibility: Literal["private", "org", "public"] = "org"
    canvas_snapshot: dict
    source_runtime_name: str | None = None
    latest_version_id: str | None = None


class UpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = Field(default=None, max_length=20)
    visibility: Literal["private", "org", "public"] | None = None


class RegistryEntryResponse(BaseModel):
    org_id: str
    agent_slug: str
    display_name: str
    description: str
    tags: list[str]
    visibility: str
    latest_version_id: str | None = None
    usage_count: int
    source_runtime_name: str | None = None
    created_at: str
    updated_at: str
    is_owner: bool = False
    status: str = "approved"
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    rejection_reason: str | None = None
    # Populated ONLY on single-entry GET (detail view's Components tab), never in
    # the list response — including the full snapshot in every browse-grid card
    # would bloat the list payload. None on list items by design.
    canvas_snapshot: dict | None = None

    @classmethod
    def from_entry(
        cls,
        e: RegistryEntry,
        caller_sub: str,
        *,
        include_snapshot: bool = False,
    ) -> RegistryEntryResponse:
        return cls(
            org_id=e.org_id,
            agent_slug=e.agent_slug,
            display_name=e.display_name,
            description=e.description,
            tags=e.tags,
            visibility=e.visibility,
            latest_version_id=e.latest_version_id,
            usage_count=e.usage_count,
            source_runtime_name=e.source_runtime_name,
            created_at=e.created_at,
            updated_at=e.updated_at,
            is_owner=(e.owner_sub == caller_sub),
            status=e.status,
            reviewed_by=e.reviewed_by,
            reviewed_at=e.reviewed_at,
            rejection_reason=e.rejection_reason,
            canvas_snapshot=e.canvas_snapshot if include_snapshot else None,
        )


class CloneResponse(BaseModel):
    agent_slug: str
    display_name: str
    canvas_snapshot: dict


# ---------------------------------------------------------------------------
# Visibility helper
# ---------------------------------------------------------------------------


def _visible_to(entry: RegistryEntry, caller_sub: str, caller_org: str) -> bool:
    if entry.owner_sub == caller_sub:
        return True
    if entry.visibility == "public":
        return True
    if entry.visibility == "org" and entry.org_id == caller_org:
        return True
    return False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=RegistryEntryResponse, dependencies=[Depends(require_scopes("registry:write"))])
async def publish(
    body: PublishRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> RegistryEntryResponse:
    """Publish an agent to the registry.

    The slug is derived from display_name. If a different owner already holds
    that slug in this org, we suffix a short disambiguator so publishing never
    silently overwrites another tenant's entry (the same class of bug as
    Bug 122 — never let a tenant-supplied name collide across owners).
    """
    org_id = _caller_org_id(caller_sub)
    store = get_registry_store()
    base_slug = slugify(body.display_name)
    slug = base_slug

    existing = store.get(org_id, slug)
    if existing is not None and existing.owner_sub != caller_sub:
        # Collision with another owner — disambiguate with a sub-derived suffix.
        slug = f"{base_slug}-{caller_sub[:6]}"[:128]
        existing = store.get(org_id, slug)  # re-check the disambiguated slug

    own_existing = existing if (existing and existing.owner_sub == caller_sub) else None

    # Status on (re)publish (PR #3 review — mNemlaghi):
    #   * brand-new entry            -> pending (needs approval)
    #   * owner re-publishes, content UNCHANGED -> PRESERVE existing status, so a
    #     no-op/metadata re-publish never silently un-publishes an approved agent
    #     (which would 404 clones until an admin re-approves).
    #   * owner re-publishes, canvas snapshot CHANGED -> reset to pending (the
    #     deployed blueprint differs from what was approved, so re-review).
    if own_existing is None:
        status = "pending"
    elif own_existing.canvas_snapshot == body.canvas_snapshot:
        status = own_existing.status or "pending"
    else:
        status = "pending"

    entry = RegistryEntry(
        org_id=org_id,
        agent_slug=slug,
        owner_sub=caller_sub,
        display_name=body.display_name,
        description=body.description,
        tags=body.tags,
        visibility=body.visibility,
        latest_version_id=body.latest_version_id,
        canvas_snapshot=body.canvas_snapshot,
        source_runtime_name=body.source_runtime_name,
        usage_count=(own_existing.usage_count if own_existing else 0),
        status=status,
        # Preserve review provenance when status is carried over unchanged.
        reviewed_by=(own_existing.reviewed_by if (own_existing and status == own_existing.status) else None),
        reviewed_at=(own_existing.reviewed_at if (own_existing and status == own_existing.status) else None),
    )
    store.put(entry)
    return RegistryEntryResponse.from_entry(entry, caller_sub)


@router.get("", response_model=list[RegistryEntryResponse], dependencies=[Depends(require_scopes("registry:read"))])
async def search(
    q: str | None = Query(default=None, max_length=200),
    tag: str | None = Query(default=None, max_length=64),
    scope: Literal["all", "mine", "public", "pending"] = Query(default="all"),
    caller_sub: str = Depends(get_caller_sub),
    is_admin: bool = Depends(caller_is_admin),
) -> list[RegistryEntryResponse]:
    """List/search registry entries visible to the caller.

    Approval-workflow rules:
      - admin: scope='pending' -> the org review queue. Other scopes -> ALL
        matching entries regardless of status.
      - developer: scope='pending' -> ONLY the caller's own pending entries.
        Other scopes -> own entries (any status) PLUS non-owner entries only
        when status=='approved' AND _visible_to.
    """
    org_id = _caller_org_id(caller_sub)
    store = get_registry_store()

    if scope == "pending":
        if is_admin:
            entries = store.list_pending(org_id)
        else:
            # Developers only ever see their OWN pending submissions.
            entries = [e for e in store.list_for_owner(caller_sub) if e.status == "pending"]
        visible = entries
    else:
        if scope == "mine":
            entries = store.list_for_owner(caller_sub)
        elif scope == "public":
            entries = store.list_public()
        else:
            # "all" = everything in the caller's org + the caller's own private
            # entries (which are already in-org). list_for_org returns the org
            # rows; we then visibility/status-filter.
            entries = store.list_for_org(org_id)

        def _shows(e: RegistryEntry) -> bool:
            if e.owner_sub == caller_sub:
                return True  # own entries always visible to the owner (any status)
            if is_admin:
                return True  # admin sees all matching entries regardless of status
            # Non-owner developer: approved AND visibility-allowed only.
            return e.status == "approved" and _visible_to(e, caller_sub, org_id)

        visible = [e for e in entries if _shows(e)]

    if q:
        ql = q.lower()
        visible = [e for e in visible if ql in e.display_name.lower() or ql in e.description.lower()]
    if tag:
        visible = [e for e in visible if tag in e.tags]

    # Newest-updated first.
    visible.sort(key=lambda e: e.updated_at, reverse=True)
    return [RegistryEntryResponse.from_entry(e, caller_sub) for e in visible]


def _can_view(entry: RegistryEntry, caller_sub: str, caller_org: str, is_admin: bool) -> bool:
    """An entry is viewable if approved+visible, or the caller owns it, or admin."""
    if entry.owner_sub == caller_sub or is_admin:
        return True
    return entry.status == "approved" and _visible_to(entry, caller_sub, caller_org)


# ---------------------------------------------------------------------------
# Phase 6 (Loom) — AWS Bedrock AgentCore Agent Registry federation. Opt-in.
# DECLARED BEFORE the /{slug} routes: FastAPI matches in declaration order, so
# these literal /aws-* paths must precede the /{slug} path parameter or they'd
# be captured as a slug and 404 (caught live). Degrades gracefully when the
# feature is unconfigured / the API is absent (public preview).
# ---------------------------------------------------------------------------


class AwsRegistryEnableRequest(BaseModel):
    registry_id: str = Field(min_length=1, max_length=256)


@router.get("/aws-config", dependencies=[Depends(require_scopes("registry:read"))])
async def aws_registry_config(caller_sub: str = Depends(get_caller_sub)) -> dict:
    """Return whether AWS Agent Registry federation is enabled + reachable."""
    from app.services.aws_agent_registry import get_configured_registry_id, get_registry

    rid = get_configured_registry_id()
    if not rid:
        return {"enabled": False, "registry_id": None, "available": False}
    reg = get_registry()
    return {"enabled": True, "registry_id": rid, "available": bool(reg and reg.available())}


@router.post("/aws-config", dependencies=[Depends(require_scopes("registry:write"))])
async def aws_registry_enable(
    body: AwsRegistryEnableRequest,
    caller_sub: str = Depends(get_caller_sub),
    is_admin: bool = Depends(caller_is_admin),
) -> dict:
    """Enable AWS Agent Registry federation with a registryId. Admin only."""
    if not is_admin:
        raise HTTPException(status_code=403, detail="Requires registry-admin role")
    from app.services.aws_agent_registry import AwsAgentRegistry, set_configured_registry_id

    # Validate reachability before persisting so a typo fails loudly here.
    if not AwsAgentRegistry(body.registry_id).available():
        raise HTTPException(
            status_code=400,
            detail="Registry not reachable (check the registryId / region / permissions)",
        )
    set_configured_registry_id(body.registry_id)
    return {"enabled": True, "registry_id": body.registry_id, "available": True}


@router.get("/aws-search", dependencies=[Depends(require_scopes("registry:read"))])
async def aws_registry_search(
    q: str = Query(min_length=1, max_length=256),
    caller_sub: str = Depends(get_caller_sub),
) -> dict:
    """Semantic search across the AWS Agent Registry (empty when disabled)."""
    from app.services.aws_agent_registry import get_registry

    reg = get_registry()
    if reg is None:
        return {"enabled": False, "results": []}
    return {"enabled": True, "results": reg.search(q)}


@router.get("/{slug}", response_model=RegistryEntryResponse, dependencies=[Depends(require_scopes("registry:read"))])
async def get_entry(
    slug: str,
    caller_sub: str = Depends(get_caller_sub),
    is_admin: bool = Depends(caller_is_admin),
) -> RegistryEntryResponse:
    slug = _validate_slug(slug)
    org_id = _caller_org_id(caller_sub)
    entry = get_registry_store().get(org_id, slug)
    if entry is None or not _can_view(entry, caller_sub, org_id, is_admin):
        # 404 (not 403) — don't disclose existence of entries the caller
        # can't see. Same rule as services/auth.assert_owner.
        raise HTTPException(status_code=404, detail="Not found")
    # Single-entry detail view carries the snapshot so the UI's Components tab
    # can render the blueprint's nodes/edges without a clone side-effect.
    return RegistryEntryResponse.from_entry(entry, caller_sub, include_snapshot=True)


# Clone is a CONSUME action (copy a blueprint onto your own canvas) — it does NOT
# mutate the registry entry's ownership (the increment_usage bump is incidental
# telemetry). So it needs registry:READ, not write; otherwise the very users the
# org catalog exists to serve (browse + reuse) couldn't clone. Publish/approve/
# reject/update/delete remain registry:write (govern).
@router.post("/{slug}/clone", response_model=CloneResponse, dependencies=[Depends(require_scopes("registry:read"))])
async def clone(
    slug: str,
    caller_sub: str = Depends(get_caller_sub),
    is_admin: bool = Depends(caller_is_admin),
) -> CloneResponse:
    """Return the canvas snapshot for the caller to drop onto their canvas.

    Increments usage_count on the source entry. Does NOT mutate the registry
    entry's ownership — the clone lives entirely in the caller's own canvas/
    workflow storage once they save it. A pending entry is NOT clonable by
    non-owners (approval gates reuse).
    """
    slug = _validate_slug(slug)
    org_id = _caller_org_id(caller_sub)
    store = get_registry_store()
    entry = store.get(org_id, slug)
    if entry is None or not _can_view(entry, caller_sub, org_id, is_admin):
        raise HTTPException(status_code=404, detail="Not found")
    store.increment_usage(org_id, slug)
    return CloneResponse(
        agent_slug=entry.agent_slug,
        display_name=entry.display_name,
        canvas_snapshot=entry.canvas_snapshot,
    )


@router.put("/{slug}", response_model=RegistryEntryResponse, dependencies=[Depends(require_scopes("registry:write"))])
async def update_entry(
    slug: str,
    body: UpdateRequest,
    caller_sub: str = Depends(get_caller_sub),
    is_admin: bool = Depends(caller_is_admin),
) -> RegistryEntryResponse:
    slug = _validate_slug(slug)
    org_id = _caller_org_id(caller_sub)
    store = get_registry_store()
    entry = store.get(org_id, slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(entry.owner_sub, caller_sub)  # 404 on mismatch

    updates = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    # An empty-body PUT is a no-op — do NOT flip status (otherwise a content-less
    # request would needlessly bounce an approved entry back to pending).
    if not updates:
        return RegistryEntryResponse.from_entry(entry, caller_sub)
    # A non-admin content/visibility change requires re-review: reset to pending.
    # Admin edits preserve the existing status.
    if not is_admin:
        updates["status"] = "pending"
    updated = store.update(org_id, slug, updates)
    if updated is None:
        raise HTTPException(status_code=404, detail="Not found")
    return RegistryEntryResponse.from_entry(updated, caller_sub)


@router.delete("/{slug}", dependencies=[Depends(require_scopes("registry:write"))])
async def delete_entry(
    slug: str,
    caller_sub: str = Depends(get_caller_sub),
    is_admin: bool = Depends(caller_is_admin),
) -> dict:
    slug = _validate_slug(slug)
    org_id = _caller_org_id(caller_sub)
    store = get_registry_store()
    entry = store.get(org_id, slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    # Owner OR registry-admin may delete. Non-owner non-admin -> 404 (no
    # existence disclosure), so check admin before falling back to assert_owner.
    if not is_admin:
        assert_owner(entry.owner_sub, caller_sub)  # 404 on mismatch
    ok = store.delete(org_id, slug)
    return {"success": ok, "agent_slug": slug}


# ---------------------------------------------------------------------------
# Approval workflow (admin only)
# ---------------------------------------------------------------------------


class RejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


@router.post(
    "/{slug}/approve", response_model=RegistryEntryResponse, dependencies=[Depends(require_scopes("registry:write"))]
)
async def approve_entry(
    slug: str,
    caller_sub: str = Depends(get_caller_sub),
    is_admin: bool = Depends(caller_is_admin),
) -> RegistryEntryResponse:
    """Approve a submission. Admin (registry-admin/org-admin) only."""
    if not is_admin:
        raise HTTPException(status_code=403, detail="Requires registry-admin role")
    slug = _validate_slug(slug)
    org_id = _caller_org_id(caller_sub)
    store = get_registry_store()
    if store.get(org_id, slug) is None:
        raise HTTPException(status_code=404, detail="Not found")
    now = datetime.now(timezone.utc).isoformat()
    updated = store.update(
        org_id,
        slug,
        {
            "status": "approved",
            "reviewed_by": caller_sub,
            "reviewed_at": now,
            "rejection_reason": None,
        },
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Not found")
    return RegistryEntryResponse.from_entry(updated, caller_sub)


@router.post(
    "/{slug}/reject", response_model=RegistryEntryResponse, dependencies=[Depends(require_scopes("registry:write"))]
)
async def reject_entry(
    slug: str,
    body: RejectRequest | None = None,
    caller_sub: str = Depends(get_caller_sub),
    is_admin: bool = Depends(caller_is_admin),
) -> RegistryEntryResponse:
    """Reject a submission. Admin (registry-admin/org-admin) only."""
    if not is_admin:
        raise HTTPException(status_code=403, detail="Requires registry-admin role")
    slug = _validate_slug(slug)
    org_id = _caller_org_id(caller_sub)
    store = get_registry_store()
    if store.get(org_id, slug) is None:
        raise HTTPException(status_code=404, detail="Not found")
    now = datetime.now(timezone.utc).isoformat()
    updated = store.update(
        org_id,
        slug,
        {
            "status": "rejected",
            "reviewed_by": caller_sub,
            "reviewed_at": now,
            "rejection_reason": (body.reason if body else None),
        },
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Not found")
    return RegistryEntryResponse.from_entry(updated, caller_sub)


# (AWS Agent Registry federation routes are declared ABOVE the /{slug} routes —
# see near the top of the endpoint section — so literal /aws-* paths aren't
# swallowed by the /{slug} path parameter.)
