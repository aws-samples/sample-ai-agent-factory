"""Scope-based RBAC/ABAC — enforced authorization on the FastAPI control plane.

Loom-inspired (see tasks plan): a two-dimensional group model layered over the
existing Cognito-group claim.

  * **Type group** (``t-admin`` / ``t-user``) — drives which UI a caller sees.
    Not enforced server-side; the frontend reads it. Present here so the
    mapping is authoritative in one place.
  * **Resource group** (``g-admins-*`` / ``g-users-*``) — grants capability
    SCOPES. This is what ``require_scopes()`` enforces on every endpoint.

Design invariants (do NOT violate — they mirror the security notes in auth.py):

  1. **Scopes never bypass tenant isolation.** ``require_scopes()`` gates the
     *capability* to call an endpoint. The innermost per-record ownership check
     (``services.auth.assert_owner`` / ``workspace_acl``) is still authoritative
     for *which* rows a caller may touch. A caller with ``agent:write`` still
     gets 404 on another tenant's private agent.
  2. **Fail-closed.** No groups → no scopes → 403 (when enforcing). The one
     exception is local dev (no ``aws.event``), which grants all scopes so the
     single-user dev loop keeps working — exactly like get_caller_sub's
     ``local-dev`` sentinel and get_caller_role's org-admin default.
  3. **Enforcing unless told otherwise.** ``RBAC_ENFORCE`` (the stack sets
     ``true``) 403s a missing scope. Only an explicit false value (``false``,
     ``0``, ``no``, ``off``) selects the advisory mode, which logs the decision
     and allows — the escape hatch for an upgrade whose existing users have no
     group yet (docs/RBAC_ROLLOUT.md). An absent or unrecognised value enforces.

Tests override the ``require_scopes`` dependency's identity source the same way
the rest of the codebase does — via ``app.dependency_overrides`` — rather than
injecting a caller-controlled header (which would be a trust bypass).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from fastapi import HTTPException, Request

from app.services.auth import extract_cognito_groups

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scope vocabulary
# ---------------------------------------------------------------------------
# Resource-oriented scopes. Keep this list small and stable — every router
# endpoint declares the scope(s) it needs. ``admin`` is a super-scope that
# implies every other scope (see caller_scopes).

SCOPE_INVOKE = "invoke"
SCOPE_ADMIN = "admin"

_RESOURCES = (
    "agent",
    "registry",
    "prompt",
    "tag",
    "cost",
    "eval",
    "workspace",
    "connector",
    "trigger",
    "hitl",
    "observability",
    "settings",
)

# Every resource gets a read + write scope; plus the two standalone scopes.
SCOPES: frozenset[str] = frozenset(
    [SCOPE_INVOKE, SCOPE_ADMIN] + [f"{r}:read" for r in _RESOURCES] + [f"{r}:write" for r in _RESOURCES]
)


def _all_read_write() -> set[str]:
    return {f"{r}:read" for r in _RESOURCES} | {f"{r}:write" for r in _RESOURCES}


def _all_read() -> set[str]:
    return {f"{r}:read" for r in _RESOURCES}


# ---------------------------------------------------------------------------
# Group → scope mapping (the ABAC/RBAC grant table)
# ---------------------------------------------------------------------------
# Resource groups grant scopes. A caller may belong to several; scopes union.
# Legacy groups (org-admin / registry-admin / editor / viewer) are mapped too
# so existing users keep working without a re-grant (backward compatible).

GROUP_SCOPES: dict[str, set[str]] = {
    # --- Loom-style resource groups ---
    "g-admins-super": {SCOPE_ADMIN, SCOPE_INVOKE} | _all_read_write(),
    "g-admins-registry": {"registry:read", "registry:write"},
    "g-admins-security": {
        "settings:read",
        "settings:write",
        "observability:read",
        "tag:read",
        "tag:write",
    },
    "g-admins-cost": {"cost:read", "cost:write"},
    # A registry developer can publish and maintain entries, but is deliberately
    # absent from auth._REGISTRY_ADMIN_GROUPS and therefore cannot approve/reject.
    "registry-developer": {"registry:read", "registry:write"},
    # A standard user builds, deploys and invokes their own agents (docs/PERSONAS.md);
    # owner_sub still confines every one of those calls to the caller's own rows.
    "g-users-default": {
        SCOPE_INVOKE,
        "agent:read",
        "agent:write",
        "cost:read",
        "prompt:read",
        "registry:read",
        "tag:read",
    },
    # --- Legacy groups (backward compatible) ---
    "org-admin": {SCOPE_ADMIN, SCOPE_INVOKE} | _all_read_write(),
    "registry-admin": {"registry:read", "registry:write"},
    "editor": {SCOPE_INVOKE} | _all_read_write(),
    "viewer": {SCOPE_INVOKE} | _all_read(),
}


def rbac_enforcing() -> bool:
    """False only when RBAC_ENFORCE is explicitly false; absent or unrecognised enforces."""
    return os.environ.get("RBAC_ENFORCE", "").strip().lower() not in ("0", "false", "no", "off")


def caller_scopes(request: Request) -> set[str]:
    """Resolve the caller's effective scope set from their Cognito groups.

    Local dev (no aws.event): all scopes (single-user full access).
    ``admin`` super-scope expands to every scope. Unknown groups contribute
    nothing (fail-closed).
    """
    aws_event = request.scope.get("aws.event") if request.scope else None
    if aws_event is None:
        return set(SCOPES)  # local-dev full access

    granted: set[str] = set()
    for g in extract_cognito_groups(request):
        granted |= GROUP_SCOPES.get(g, set())

    if SCOPE_ADMIN in granted:
        return set(SCOPES)
    return granted


def has_scopes(request: Request, required: tuple[str, ...]) -> bool:
    """Whether the caller holds every required scope (admin implies all)."""
    held = caller_scopes(request)
    if SCOPE_ADMIN in held:
        return True
    return all(scope in held for scope in required)


def require_scopes(*required: str) -> Callable[[Request], None]:
    """FastAPI dependency factory: enforce that the caller holds *required*.

    Usage::

        @router.post("/api/agents", dependencies=[Depends(require_scopes("agent:write"))])

    Behavior is governed by RBAC_ENFORCE:
      * enforcing → raise 403 when a scope is missing.
      * advisory  → log the would-be denial and allow (observe-only rollout).

    Raises 500 at import/first-call if a bogus scope name is passed (guards
    against typos silently granting access).
    """
    unknown = [s for s in required if s not in SCOPES]
    if unknown:
        raise ValueError(f"require_scopes: unknown scope(s) {unknown}; valid: {sorted(SCOPES)}")

    def _dep(request: Request) -> None:
        if has_scopes(request, required):
            return
        held = sorted(caller_scopes(request))
        if rbac_enforcing():
            logger.warning(
                "RBAC deny: caller lacks %s (held=%s) path=%s",
                list(required),
                held,
                request.scope.get("path"),
            )
            raise HTTPException(
                status_code=403,
                detail=f"Missing required scope(s): {', '.join(required)}",
            )
        # Advisory mode: record what WOULD have been denied, then allow.
        # WARNING (not info): this is an actionable operational signal — the
        # RBAC_ROLLOUT runbook + the WouldDeny CloudWatch metric filter depend on
        # this line being emitted at Lambda's default log level (INFO is filtered).
        logger.warning(
            "RBAC advisory (would-deny): caller lacks %s (held=%s) path=%s",
            list(required),
            held,
            request.scope.get("path"),
        )

    # Read by the route-coverage test, which proves every mounted route declares a scope.
    _dep.required_scopes = required
    return _dep
