"""Prompt Management Library API — Phase 3 Gap 3H.

A reusable, versioned library of system prompts. Authors create a named prompt,
append versions, and pin a default. At deploy time a runtime config can
reference a library prompt instead of inlining the body.

Endpoints:
  POST   /api/prompts                          create a prompt (seeds version 1)
  GET    /api/prompts?q=&tag=&scope=all|mine   list/search (visibility-filtered)
  GET    /api/prompts/{name}                   fetch one (visibility-checked)
  PUT    /api/prompts/{name}                   update metadata (owner only)
  DELETE /api/prompts/{name}                   delete (owner only)
  POST   /api/prompts/{name}/versions          append a version (owner only)
  POST   /api/prompts/{name}/promote/{vid}     set default version (owner only)
  GET    /api/prompts/{name}/resolve?version=  resolve a body (visibility-checked)

Tenant model (see prompt_library_store docstring):
  - Visibility is owner OR same org (no public tier).
  - Mutations require ``owner_sub == caller`` (404-on-mismatch via assert_owner).
  - Cross-tenant access returns 404 (existence-non-disclosure).
  - The single-resource GET uses the SAME visibility predicate as the list
    endpoint (Bug 126 ACL-drift guard).
  - ``resolve`` is visibility-checked but NOT owner-only, so a deploying
    consumer in the org can resolve a shared prompt.

Until Gap 2E wires Cognito-group-backed orgs, every caller is in
``DEFAULT_ORG_ID`` so ``org`` ≈ "all platform users".
"""

from __future__ import annotations

import logging
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.auth import assert_owner, get_caller_sub
from app.services.rbac import require_scopes
from app.services.prompt_library_store import (
    DEFAULT_ORG_ID,
    MAX_BODY_LEN,
    PromptEntry,
    PromptVersion,
    get_prompt_library_store,
    slugify,
)

logger = logging.getLogger(__name__)


def _validate_prompt_name(name: str) -> str:
    if not name or len(name) > 128:
        raise HTTPException(status_code=400, detail="Invalid prompt_name")
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
        raise HTTPException(status_code=400, detail="Invalid prompt_name format")
    return name


def _caller_org_id(caller_sub: str) -> str:
    # Gap 2E will derive this from a Cognito group claim. For now everyone is
    # in the default org so org-visible prompts are shared platform-wide.
    return DEFAULT_ORG_ID


router = APIRouter(prefix="/api/prompts", tags=["prompts"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreatePromptRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    body: str = Field(min_length=1, max_length=MAX_BODY_LEN)


class UpdatePromptRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    tags: Optional[list[str]] = Field(default=None, max_length=20)


class AddPromptVersionRequest(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_BODY_LEN)


class PromptVersionResponse(BaseModel):
    version_id: str
    body: str
    created_at: str
    created_by: str

    @classmethod
    def from_model(cls, v: PromptVersion) -> "PromptVersionResponse":
        return cls(
            version_id=v.version_id,
            body=v.body,
            created_at=v.created_at,
            created_by=v.created_by,
        )


class PromptEntryResponse(BaseModel):
    org_id: str
    prompt_name: str
    display_name: str
    description: str
    tags: list[str]
    versions: list[PromptVersionResponse]
    default_version_id: Optional[str] = None
    created_at: str
    updated_at: str
    is_owner: bool = False

    @classmethod
    def from_entry(cls, e: PromptEntry, caller_sub: str) -> "PromptEntryResponse":
        return cls(
            org_id=e.org_id,
            prompt_name=e.prompt_name,
            display_name=e.display_name,
            description=e.description,
            tags=e.tags,
            versions=[PromptVersionResponse.from_model(v) for v in e.versions],
            default_version_id=e.default_version_id,
            created_at=e.created_at,
            updated_at=e.updated_at,
            is_owner=(e.owner_sub == caller_sub),
        )


class AddVersionResponse(BaseModel):
    prompt_name: str
    version_id: str
    default_version_id: Optional[str] = None


class PromoteResponse(BaseModel):
    success: bool
    prompt_name: str
    default_version_id: str


class ResolvePromptResponse(BaseModel):
    prompt_name: str
    version_id: str
    body: str


# ---------------------------------------------------------------------------
# Visibility helper
# ---------------------------------------------------------------------------


def _visible_to(entry: PromptEntry, caller_sub: str, caller_org: str) -> bool:
    if entry.owner_sub == caller_sub:
        return True
    # Org tier — prompts have no public tier.
    if entry.org_id == caller_org:
        return True
    return False


def resolve_visible_body(
    org_id: str,
    prompt_name: str,
    version_id: Optional[str],
    caller_sub: str,
) -> Optional[tuple[str, str]]:
    """Resolve a prompt body for a caller, enforcing org/owner visibility.

    Shared by the ``/resolve`` endpoint and the deploy-time resolution hook so
    both apply the SAME visibility predicate (never a blind store.get that
    would leak a foreign prompt). Returns ``(version_id, body)`` or ``None`` if
    the prompt is invisible/missing or the version/default can't be resolved.
    """
    store = get_prompt_library_store()
    entry = store.get(org_id, prompt_name)
    if entry is None or not _visible_to(entry, caller_sub, org_id):
        return None
    target = version_id or entry.default_version_id
    if not target:
        return None
    for v in entry.versions:
        if v.version_id == target:
            return (target, v.body)
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=PromptEntryResponse, dependencies=[Depends(require_scopes("prompt:write"))])
async def create_prompt(
    body: CreatePromptRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> PromptEntryResponse:
    """Create a library prompt, seeding an initial version from ``body``.

    The slug is derived from display_name. If a different owner already holds
    that slug in this org, we suffix a short disambiguator so creating never
    silently overwrites another tenant's prompt (Bug 122). The same owner
    re-creating overwrites their own prompt in place.
    """
    org_id = _caller_org_id(caller_sub)
    store = get_prompt_library_store()
    base_slug = slugify(body.display_name)
    slug = base_slug

    existing = store.get(org_id, slug)
    if existing is not None and existing.owner_sub != caller_sub:
        # Collision with another owner — disambiguate with a sub-derived suffix.
        slug = f"{base_slug}-{caller_sub[:6]}"[:128]

    from app.services.prompt_library_store import new_prompt_version_id
    from datetime import datetime, timezone

    version_id = new_prompt_version_id()
    initial = PromptVersion(
        version_id=version_id,
        body=body.body,
        created_at=datetime.now(timezone.utc).isoformat(),
        created_by=caller_sub,
    )
    entry = PromptEntry(
        org_id=org_id,
        prompt_name=slug,
        owner_sub=caller_sub,
        display_name=body.display_name,
        description=body.description,
        tags=body.tags,
        versions=[initial],
        default_version_id=version_id,
    )
    store.put(entry)
    return PromptEntryResponse.from_entry(entry, caller_sub)


@router.get("", response_model=list[PromptEntryResponse], dependencies=[Depends(require_scopes("prompt:read"))])
async def list_prompts(
    q: Optional[str] = Query(default=None, max_length=200),
    tag: Optional[str] = Query(default=None, max_length=64),
    scope: Literal["all", "mine"] = Query(default="all"),
    caller_sub: str = Depends(get_caller_sub),
) -> list[PromptEntryResponse]:
    """List/search prompts visible to the caller, newest-updated first."""
    org_id = _caller_org_id(caller_sub)
    store = get_prompt_library_store()

    if scope == "mine":
        entries = store.list_for_owner(caller_sub)
    else:
        entries = store.list_for_org(org_id)

    visible = [e for e in entries if _visible_to(e, caller_sub, org_id)]

    if q:
        ql = q.lower()
        visible = [
            e
            for e in visible
            if ql in e.display_name.lower() or ql in e.description.lower()
        ]
    if tag:
        visible = [e for e in visible if tag in e.tags]

    visible.sort(key=lambda e: e.updated_at, reverse=True)
    return [PromptEntryResponse.from_entry(e, caller_sub) for e in visible]


@router.get("/{name}", response_model=PromptEntryResponse, dependencies=[Depends(require_scopes("prompt:read"))])
async def get_prompt(
    name: str,
    caller_sub: str = Depends(get_caller_sub),
) -> PromptEntryResponse:
    name = _validate_prompt_name(name)
    org_id = _caller_org_id(caller_sub)
    entry = get_prompt_library_store().get(org_id, name)
    if entry is None or not _visible_to(entry, caller_sub, org_id):
        # 404 (not 403) — same visibility predicate as the list (Bug 126).
        raise HTTPException(status_code=404, detail="Not found")
    return PromptEntryResponse.from_entry(entry, caller_sub)


@router.put("/{name}", response_model=PromptEntryResponse, dependencies=[Depends(require_scopes("prompt:write"))])
async def update_prompt(
    name: str,
    body: UpdatePromptRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> PromptEntryResponse:
    name = _validate_prompt_name(name)
    org_id = _caller_org_id(caller_sub)
    store = get_prompt_library_store()
    entry = store.get(org_id, name)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(entry.owner_sub, caller_sub)  # 404 on mismatch

    updates = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not updates:
        return PromptEntryResponse.from_entry(entry, caller_sub)
    updated = store.update(org_id, name, updates)
    if updated is None:
        raise HTTPException(status_code=404, detail="Not found")
    return PromptEntryResponse.from_entry(updated, caller_sub)


@router.delete("/{name}", dependencies=[Depends(require_scopes("prompt:write"))])
async def delete_prompt(
    name: str,
    caller_sub: str = Depends(get_caller_sub),
) -> dict:
    name = _validate_prompt_name(name)
    org_id = _caller_org_id(caller_sub)
    store = get_prompt_library_store()
    entry = store.get(org_id, name)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(entry.owner_sub, caller_sub)  # 404 on mismatch
    ok = store.delete(org_id, name)
    return {"success": ok, "prompt_name": name}


@router.post("/{name}/versions", response_model=AddVersionResponse, dependencies=[Depends(require_scopes("prompt:write"))])
async def add_prompt_version(
    name: str,
    body: AddPromptVersionRequest,
    caller_sub: str = Depends(get_caller_sub),
) -> AddVersionResponse:
    name = _validate_prompt_name(name)
    org_id = _caller_org_id(caller_sub)
    store = get_prompt_library_store()
    entry = store.get(org_id, name)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(entry.owner_sub, caller_sub)  # 404 on mismatch
    version_id = store.add_version(org_id, name, body.body, caller_sub)
    if version_id is None:
        raise HTTPException(status_code=404, detail="Not found")
    updated = store.get(org_id, name)
    return AddVersionResponse(
        prompt_name=name,
        version_id=version_id,
        default_version_id=updated.default_version_id if updated else None,
    )


@router.post("/{name}/promote/{version_id}", response_model=PromoteResponse, dependencies=[Depends(require_scopes("prompt:write"))])
async def promote_prompt_version(
    name: str,
    version_id: str,
    caller_sub: str = Depends(get_caller_sub),
) -> PromoteResponse:
    name = _validate_prompt_name(name)
    org_id = _caller_org_id(caller_sub)
    store = get_prompt_library_store()
    entry = store.get(org_id, name)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(entry.owner_sub, caller_sub)  # 404 on mismatch
    ok = store.promote(org_id, name, version_id)
    if not ok:
        # The prompt exists + the caller owns it, so the only failure is an
        # unknown version_id → 409 (mirror versions.py promote 409 pattern).
        raise HTTPException(
            status_code=409,
            detail=f"Unknown version_id '{version_id}' for prompt '{name}'.",
        )
    return PromoteResponse(
        success=True, prompt_name=name, default_version_id=version_id
    )


@router.get("/{name}/resolve", response_model=ResolvePromptResponse, dependencies=[Depends(require_scopes("prompt:read"))])
async def resolve_prompt(
    name: str,
    version: Optional[str] = Query(default=None, max_length=64),
    caller_sub: str = Depends(get_caller_sub),
) -> ResolvePromptResponse:
    """Resolve a prompt body. Visibility-checked but NOT owner-only, so an
    org consumer can resolve a shared prompt at deploy time."""
    name = _validate_prompt_name(name)
    org_id = _caller_org_id(caller_sub)
    resolved = resolve_visible_body(org_id, name, version, caller_sub)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Not found")
    version_id, prompt_body = resolved
    return ResolvePromptResponse(
        prompt_name=name, version_id=version_id, body=prompt_body
    )
