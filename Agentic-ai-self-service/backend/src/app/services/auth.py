"""Tenant-isolation helpers.

API Gateway validates Cognito JWTs at the edge (HTTP API + JWT authorizer),
but the FastAPI handlers historically didn't extract the caller's identity.
That meant every Cognito-authenticated user could read and modify every other
user's workflows / flows / deployments via list_all() Scans with no filter.
Verified live 2026-05-16; tasks/lessons.md Bug 37.

This module exposes a single helper, ``get_caller_sub(request)``, which reads
the Cognito ``sub`` claim from the Mangum-wrapped event. Routers use it to
stamp ``owner_sub`` on records and to scope reads/writes.

In local dev (no Mangum event), returns ``"local-dev"`` so single-user dev
flows continue to work. **There is no test-injection header** — tests that
need a different sub MUST use ``app.dependency_overrides[get_caller_sub]``,
because a request header is caller-controlled and would re-introduce a
header-trust bypass exactly equivalent to "no auth at all" if the in-Lambda
heuristic ever returns False on a production code path (Critic Finding 3).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


_LOCAL_DEV_SUB = "local-dev"


def get_caller_sub(request: Request) -> str:
    """Return the Cognito ``sub`` (stable, opaque user identifier).

    Order of precedence (defensive — Mangum's event shape varies):
      1. ``request.scope["aws.event"]["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]``
         (HTTP API + JWT authorizer — production path)
      2. ``request.scope["aws.event"]["requestContext"]["authorizer"]["claims"]["sub"]``
         (legacy REST API authorizer shape)
      3. ``"local-dev"`` when the FastAPI app is run outside Lambda

    Tests should override this dependency rather than send a sub-injection
    header — see module docstring.

    Raises:
        HTTPException 401 if running in Lambda but no claim is present
        (never silently fall through to a shared identity in prod).
    """
    aws_event = request.scope.get("aws.event") if request.scope else None
    in_lambda = aws_event is not None

    if in_lambda:
        try:
            authz = aws_event["requestContext"]["authorizer"]
            jwt = authz.get("jwt") or authz
            claims = jwt.get("claims") or jwt
            sub = claims.get("sub")
            if sub:
                return str(sub)
        except (KeyError, TypeError, AttributeError) as e:
            logger.warning("Could not extract sub from request authorizer: %s", e)
        # In Lambda but no sub → reject. Don't silently fall back.
        raise HTTPException(
            status_code=401, detail="Caller identity not available"
        )

    # Local dev path. No header injection — tests use dependency_overrides.
    return _LOCAL_DEV_SUB


def assert_owner(record_owner_sub: Optional[str], caller_sub: str) -> None:
    """Raise 404 when the caller doesn't own a record (existence-non-disclosure).

    404 hides existence — a 403 would tell an attacker the record exists.

    Critic Finding 3 fix: a record with ``owner_sub is None`` is **legacy**
    (pre-tenant-isolation) data. We treat it as **not found** to every caller.
    Previously the helper early-returned, granting every authenticated user
    access to every legacy row. The correct migration is a one-time backfill
    of ``owner_sub`` on legacy rows; until then, legacy rows are invisible.

    A record owned by ``_LOCAL_DEV_SUB`` is dev-mode data and must not match
    a real Cognito sub from a Lambda caller — so we reject any caller-sub
    that doesn't equal the record's owner, even when the record's owner is
    the local-dev sentinel.
    """
    # Legacy / un-owned rows: 404 to every caller.
    if record_owner_sub is None:
        raise HTTPException(status_code=404, detail="Not found")
    if record_owner_sub != caller_sub:
        raise HTTPException(status_code=404, detail="Not found")


# Gap 2E: org-wide RBAC role from the Cognito group claim. ADVISORY ONLY.
# The per-workflow ACL (services/workspace_acl.py) is the authoritative authz
# check for view/edit/share; this role must NEVER bypass that ACL (a group=
# editor must not let a user edit another tenant's private workflow). It exists
# for a future org-admin escalation path only.
_ROLE_PRECEDENCE = {"org-admin": 3, "editor": 2, "viewer": 1, "none": 0}


def get_caller_role(request: Request) -> str:
    """Return the caller's highest-privilege Cognito group role.

    One of 'org-admin' | 'editor' | 'viewer' | 'none'. Mirrors the defensive
    authorizer-claim walk in get_caller_sub. The cognito:groups claim may be a
    JSON-array string, a comma/space-joined string, or a list depending on the
    HTTP API JWT authorizer/token serialization, so we parse all shapes.
    """
    import json as _json

    aws_event = request.scope.get("aws.event") if request.scope else None
    if aws_event is None:
        # Local dev: single-user keeps full access, like _LOCAL_DEV_SUB.
        return "org-admin"

    groups: list[str] = []
    try:
        authz = aws_event["requestContext"]["authorizer"]
        jwt = authz.get("jwt") or authz
        claims = jwt.get("claims") or jwt
        raw = claims.get("cognito:groups")
    except (KeyError, TypeError, AttributeError) as e:
        logger.warning("Could not extract cognito:groups from authorizer: %s", e)
        raw = None

    if isinstance(raw, list):
        groups = [str(g) for g in raw]
    elif isinstance(raw, str) and raw:
        s = raw.strip()
        if s.startswith("["):
            try:
                parsed = _json.loads(s)
                if isinstance(parsed, list):
                    groups = [str(g) for g in parsed]
            except ValueError:
                groups = []
        if not groups:
            # Cognito also delivers groups as a bracketed/space/comma list.
            s = s.strip("[]")
            groups = [g.strip() for g in s.replace(",", " ").split() if g.strip()]

    best = "none"
    for g in groups:
        if _ROLE_PRECEDENCE.get(g, 0) > _ROLE_PRECEDENCE[best]:
            best = g
    return best


# Registry RBAC: approvers belong to the 'registry-admin' Cognito group. We also
# accept the legacy 'org-admin' group so existing platform admins keep approval
# rights without a re-grant. This is distinct from get_caller_role/_ROLE_PRECEDENCE
# (which workspace_acl depends on) — do NOT fold these together.
_REGISTRY_ADMIN_GROUPS = {"registry-admin", "org-admin"}


def is_registry_admin(request: Request) -> bool:
    """True if the caller is a registry approver (group registry-admin/org-admin).

    Reuses the exact defensive cognito:groups claim parsing from get_caller_role
    (list / JSON-array string / comma-or-space-joined string). In local dev (no
    aws.event) returns True, mirroring get_caller_role's full-access local path.
    """
    import json as _json

    aws_event = request.scope.get("aws.event") if request.scope else None
    if aws_event is None:
        # Local dev: single-user keeps full access, like get_caller_role.
        return True

    groups: list[str] = []
    try:
        authz = aws_event["requestContext"]["authorizer"]
        jwt = authz.get("jwt") or authz
        claims = jwt.get("claims") or jwt
        raw = claims.get("cognito:groups")
    except (KeyError, TypeError, AttributeError) as e:
        logger.warning("Could not extract cognito:groups from authorizer: %s", e)
        raw = None

    if isinstance(raw, list):
        groups = [str(g) for g in raw]
    elif isinstance(raw, str) and raw:
        s = raw.strip()
        if s.startswith("["):
            try:
                parsed = _json.loads(s)
                if isinstance(parsed, list):
                    groups = [str(g) for g in parsed]
            except ValueError:
                groups = []
        if not groups:
            # Cognito also delivers groups as a bracketed/space/comma list.
            s = s.strip("[]")
            groups = [g.strip() for g in s.replace(",", " ").split() if g.strip()]

    return any(g in _REGISTRY_ADMIN_GROUPS for g in groups)
