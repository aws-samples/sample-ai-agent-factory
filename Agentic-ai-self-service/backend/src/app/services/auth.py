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
import os

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
        raise HTTPException(status_code=401, detail="Caller identity not available")

    # Local dev path. No header injection — tests use dependency_overrides.
    return _LOCAL_DEV_SUB


def assert_owner(record_owner_sub: str | None, caller_sub: str) -> None:
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


def extract_cognito_groups(request: Request) -> list[str]:
    """Return the caller's ``cognito:groups`` as a normalized list of strings.

    The claim may arrive as a Python list, a JSON-array string (``"[a, b]"``),
    or a bracketed/space/comma-joined string depending on how the HTTP API JWT
    authorizer serializes it — so we parse all shapes. Returns ``[]`` when the
    claim is absent or unparseable (fail-closed: no groups → no scopes).

    NOTE: returns ``[]`` in local dev (no ``aws.event``). Callers that grant
    local-dev full access (get_caller_role, is_registry_admin, rbac) handle the
    no-event case explicitly BEFORE calling this — do not infer local-dev here.
    """
    import json as _json

    aws_event = request.scope.get("aws.event") if request.scope else None
    if aws_event is None:
        return []

    try:
        authz = aws_event["requestContext"]["authorizer"]
        jwt = authz.get("jwt") or authz
        claims = jwt.get("claims") or jwt
    except (KeyError, TypeError, AttributeError) as e:
        logger.warning("Could not extract claims from authorizer: %s", e)
        return []

    groups = _parse_group_claim(claims.get("cognito:groups"))

    # Loom-study 1.1 — 3rd-party IdP federation. A federated user's groups arrive
    # under the IdP's own claim (e.g. Okta "groups"), NOT cognito:groups. When
    # OIDC_GROUPS_CLAIM is configured, ALSO read that claim and map its values to
    # our internal g-*/t-* vocabulary via OIDC_GROUP_MAP (JSON {external:internal}).
    # Unmapped external groups are dropped (fail-closed: an unknown IdP group
    # grants nothing). This keeps rbac.GROUP_SCOPES keyed on our own names.
    ext_claim = os.environ.get("OIDC_GROUPS_CLAIM")
    if ext_claim:
        ext_groups = _parse_group_claim(claims.get(ext_claim))
        if ext_groups:
            try:
                mapping = _json.loads(os.environ.get("OIDC_GROUP_MAP") or "{}")
            except ValueError:
                mapping = {}
            for g in ext_groups:
                mapped = mapping.get(g)
                if mapped:
                    groups.append(str(mapped))
    # De-dup while preserving order.
    seen: set[str] = set()
    return [g for g in groups if not (g in seen or seen.add(g))]


def _parse_group_claim(raw) -> list[str]:
    """Parse a group claim that may be a list, JSON-array string, or delimited."""
    import json as _json

    if isinstance(raw, list):
        return [str(g) for g in raw]
    if isinstance(raw, str) and raw:
        s = raw.strip()
        if s.startswith("["):
            try:
                parsed = _json.loads(s)
                if isinstance(parsed, list):
                    return [str(g) for g in parsed]
            except ValueError:
                pass
        s = s.strip("[]")
        return [g.strip() for g in s.replace(",", " ").split() if g.strip()]
    return []


def get_caller_claims(request: Request) -> dict:
    """Return ALL JWT claims the authorizer injected for the caller (or {}).

    Reuses the same authorizer-context extraction as get_caller_sub /
    extract_cognito_groups. Used by the token-info endpoint (Loom-study 1.3) to
    show the signed-in user their decoded identity/claims. Returns {} in local
    dev or when no authorizer context is present.
    """
    aws_event = request.scope.get("aws.event") if request.scope else None
    if aws_event is None:
        return {}
    try:
        authz = aws_event["requestContext"]["authorizer"]
        jwt = authz.get("jwt") or authz
        claims = jwt.get("claims") or jwt
        return dict(claims) if isinstance(claims, dict) else {}
    except (KeyError, TypeError, AttributeError):
        return {}


# Gap 2E: org-wide RBAC role from the Cognito group claim. ADVISORY ONLY.
# The per-workflow ACL (services/workspace_acl.py) is the authoritative authz
# check for view/edit/share; this role must NEVER bypass that ACL (a group=
# editor must not let a user edit another tenant's private workflow). It exists
# for a future org-admin escalation path only.
_ROLE_PRECEDENCE = {"org-admin": 3, "editor": 2, "viewer": 1, "none": 0}


def get_caller_role(request: Request) -> str:
    """Return the caller's highest-privilege Cognito group role.

    One of 'org-admin' | 'editor' | 'viewer' | 'none'. Group parsing is shared
    with the rest of the auth layer via extract_cognito_groups.
    """
    aws_event = request.scope.get("aws.event") if request.scope else None
    if aws_event is None:
        # Local dev: single-user keeps full access, like _LOCAL_DEV_SUB.
        return "org-admin"

    best = "none"
    for g in extract_cognito_groups(request):
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

    Reuses the shared cognito:groups claim parsing (extract_cognito_groups). In
    local dev (no aws.event) returns True, mirroring get_caller_role's
    full-access local path.
    """
    aws_event = request.scope.get("aws.event") if request.scope else None
    if aws_event is None:
        # Local dev: single-user keeps full access, like get_caller_role.
        return True

    return any(g in _REGISTRY_ADMIN_GROUPS for g in extract_cognito_groups(request))
