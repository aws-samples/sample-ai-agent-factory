"""Every mounted API route declares a scope, and the UI's group tables match the backend's.

Walks the routes of the two real apps (the workflow Lambda's ``app`` and the
deployment Lambda's ``deployment_app``) after every router is included, rather
than parsing decorators: that is what API Gateway actually reaches, and it sees a
router-level ``dependencies=`` or a parameter ``Depends`` as well as a decorator's.

Measured before the fix: 24 of 108 routes had no scope, including POST /api/deploy,
DELETE /api/runtime/{runtime_id}, POST /api/test-runtime, the five /api/flows
routes and git-token/git-sync. A caller in no group (zero scopes) could deploy,
delete and invoke even with RBAC_ENFORCE=true, because only a declared scope is
ever enforced. Ownership (owner_sub) still confined those calls to the caller's own
rows; the gap was the capability check.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
from app.services import auth, rbac

_REPO = pathlib.Path(__file__).resolve().parents[2]

# The only routes a caller in no group may reach: liveness, the caller's own
# decoded token (how a user sees which groups and scopes they hold), and the
# public webhook ingress. The webhook carries no JWT by design -- an external
# sender has none -- so it cannot declare a Cognito scope; it authenticates the
# request body with a per-trigger HMAC (x-agentcore-signature) and returns one
# 401 for missing/inactive/bad-signature so the endpoint is not a
# trigger-existence oracle. See app.routers.triggers.receive_webhook.
UNGUARDED = {
    ("main", "GET", "/health"),
    ("deployment", "GET", "/health"),
    ("deployment", "GET", "/api/identity/token-info"),
    ("deployment", "POST", "/hooks/{runtime_name}/{trigger_id}"),
}

# The routes that had no scope, pinned to the one they now require.
PINNED = {
    ("deployment", "POST", "/api/deploy"): {"agent:write"},
    ("deployment", "GET", "/api/deploy/{deployment_id}"): {"agent:read"},
    ("deployment", "GET", "/api/deployments"): {"agent:read"},
    ("deployment", "POST", "/api/test-runtime"): {"invoke"},
    ("deployment", "POST", "/api/test-runtime-stream"): {"invoke"},
    ("deployment", "POST", "/api/runtime/import"): {"agent:write"},
    ("deployment", "DELETE", "/api/runtime/{runtime_id}"): {"agent:write"},
    ("deployment", "POST", "/api/generate-tool"): {"agent:write"},
    ("deployment", "GET", "/api/generate-tool/{job_id}"): {"agent:read"},
    ("deployment", "POST", "/api/test-tool"): {"agent:write"},
    ("deployment", "GET", "/api/test-tool/{test_id}"): {"agent:read"},
    ("deployment", "POST", "/api/generate-canvas"): {"agent:write"},
    ("deployment", "POST", "/api/generate-cfn-template"): {"agent:write"},
    ("deployment", "POST", "/api/export-python"): {"agent:write"},
    ("main", "POST", "/api/flows"): {"agent:write"},
    ("main", "GET", "/api/flows"): {"agent:read"},
    ("main", "GET", "/api/flows/{flow_id}"): {"agent:read"},
    ("main", "PUT", "/api/flows/{flow_id}"): {"agent:write"},
    ("main", "DELETE", "/api/flows/{flow_id}"): {"agent:write"},
    ("main", "POST", "/api/workflows/{workflow_id}/git-token"): {"agent:write"},
    ("main", "POST", "/api/workflows/{workflow_id}/git-sync"): {"agent:write"},
}


def _declared_scopes(dependant) -> set[str]:
    found, stack = set(), [dependant]
    while stack:
        dep = stack.pop()
        found |= set(getattr(dep.call, "required_scopes", ()))
        stack.extend(dep.dependencies)
    return found


def _routes(app):
    """Every effective route, including those of included routers (FastAPI mounts
    them lazily as _IncludedRouter, whose routes are not in ``app.routes``)."""
    from fastapi.routing import APIRoute

    for route in app.routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "effective_route_contexts"):
            yield from route.effective_route_contexts()


@pytest.fixture(scope="module")
def mounted() -> dict[tuple[str, str, str], set[str]]:
    from app.deployment_handler import deployment_app
    from app.main import app

    table = {}
    for name, application in (("main", app), ("deployment", deployment_app)):
        for route in _routes(application):
            for method in route.methods:
                table[(name, method, route.path)] = _declared_scopes(route.dependant)
    return table


def test_the_walk_sees_the_included_routers(mounted):
    """An enumeration that misses included routers would report everything guarded."""
    assert ("main", "POST", "/api/workflows") in mounted
    assert ("deployment", "GET", "/api/admin/audit") in mounted
    assert len(mounted) >= 100, len(mounted)


def test_every_route_but_liveness_and_token_info_declares_a_scope(mounted):
    unguarded = {key for key, scopes in mounted.items() if not scopes}
    assert unguarded == UNGUARDED, sorted(unguarded ^ UNGUARDED)


@pytest.mark.parametrize("key", sorted(PINNED), ids=lambda k: f"{k[1]} {k[2]}")
def test_formerly_unguarded_routes_require_their_scope(mounted, key):
    assert mounted[key] == PINNED[key]


def test_a_standard_user_can_build_deploy_and_invoke_but_not_administer(mounted):
    """docs/PERSONAS.md: a standard user builds, deploys and invokes their own agents."""
    held = rbac.GROUP_SCOPES["g-users-default"]
    for key in [k for k, scopes in PINNED.items() if k[1] != "GET"]:
        assert mounted[key] <= held, key
    assert "admin" not in held
    assert mounted[("deployment", "GET", "/api/admin/audit")] - held


# ---------------------------------------------------------------------------
# Frontend parity: the UI hides what the backend denies, from the same tables.
# ---------------------------------------------------------------------------


def _ts_group_scopes() -> dict[str, set[str]]:
    src = (_REPO / "frontend/src/auth/scopes.ts").read_text()
    resources = re.findall(r"'([a-z]+)'", re.search(r"const RESOURCES = \[(.*?)\] as const", src, re.S).group(1))
    expand = {
        "...allReadWrite()": {f"{r}:{a}" for r in resources for a in ("read", "write")},
        "...allRead()": {f"{r}:read" for r in resources},
    }
    body = re.search(r"const GROUP_SCOPES: Record<string, string\[\]> = \{(.*?)\n\};", src, re.S).group(1)
    table = {}
    for name, items in re.findall(
        r"^\s*'?([a-z-]+)'?:\s*\[(.*?)\],\s*$",
        body,
        re.M | re.S,
    ):
        scopes = set()
        for item in (i.strip() for i in items.split(",")):
            if not item:
                continue
            scopes |= expand[item] if item in expand else {ast.literal_eval(item)}
        table[name] = scopes
    return table


def test_the_frontend_group_table_matches_the_backend():
    frontend = _ts_group_scopes()
    assert len(frontend) == len(rbac.GROUP_SCOPES), sorted(frontend)
    assert frontend == rbac.GROUP_SCOPES


def test_the_frontend_registry_admins_match_the_backend():
    src = (_REPO / "frontend/src/auth/useIsRegistryAdmin.ts").read_text()
    listed = re.search(r"REGISTRY_ADMIN_GROUPS: readonly string\[\] = \[(.*?)\];", src, re.S).group(1)
    assert set(re.findall(r"'([a-z-]+)'", listed)) == auth._REGISTRY_ADMIN_GROUPS


def test_the_pool_s_admin_groups_are_registry_admins():
    """g-admins-registry holds registry:write and is the group the pool creates, so it
    must be able to moderate; before, only the legacy registry-admin could."""
    assert {"g-admins-registry", "g-admins-super"} <= auth._REGISTRY_ADMIN_GROUPS
