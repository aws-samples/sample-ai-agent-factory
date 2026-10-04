"""Every FastAPI route the platform mounts is reachable through a synthesized API Gateway route.

The two API Lambdas (workflow ``app`` and deployment ``deployment_app``) mount
their routers lazily; the HTTP API needs an explicit ``add_routes()`` per path
(Bug 21). Nothing linked the two sides, so a router mounted in code with no
matching API Gateway route 404s at the edge while every in-process unit test
still passes -- exactly how ``POST /api/test-mcp-runtime/{tools,call}`` shipped
mounted-but-unroutable until this oracle was added.

This test synthesizes the real ``PlatformStack`` template, enumerates every
mounted route via the same lazy-router walk RBAC coverage uses, and asserts each
mounted (method, path) is covered by some synthesized API Gateway RouteKey. A
``{proxy+}`` route covers one-or-more trailing segments; a ``{param}`` covers
exactly one. Any future router that forgets its edge route fails here.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

# The FastAPI apps live in the backend ``app`` package. infra/conftest.py puts the
# infra dir (which contains the CDK entrypoint ``app.py``) on sys.path[0], so
# ``import app`` would otherwise resolve to that module and shadow the package.
# Force backend/src to the FRONT: a plain "if not in sys.path" guard would leave a
# path that is already present but sitting BEHIND the infra dir, letting app.py win.
_BACKEND_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "backend" / "src")
while _BACKEND_SRC in sys.path:
    sys.path.remove(_BACKEND_SRC)
sys.path.insert(0, _BACKEND_SRC)

VERBS = {"GET", "POST", "PUT", "DELETE", "PATCH"}


def _mounted_routes(app) -> set[tuple[str, str]]:
    """(method, path) for every effective route, including lazily-included routers."""
    from fastapi.routing import APIRoute

    out: set[tuple[str, str]] = set()
    routes = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            routes.append(route)
        elif hasattr(route, "effective_route_contexts"):
            routes.extend(route.effective_route_contexts())
    for route in routes:
        for method in route.methods & VERBS:
            out.add((method, route.path))
    return out


def _synth_route_keys() -> list[tuple[str, str]]:
    """(method, path-pattern) for every AWS::ApiGatewayV2::Route in the synthesized stack."""
    import aws_cdk as cdk
    from aws_cdk.assertions import Template
    from stacks.platform_stack import PlatformStack

    app = cdk.App()
    stack = PlatformStack(
        app,
        "RouteParityStack",
        environment_name="test",
        project_name="agentcore-workflow",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    tpl = Template.from_stack(stack).to_json()
    keys: list[tuple[str, str]] = []
    for res in tpl["Resources"].values():
        if res["Type"] == "AWS::ApiGatewayV2::Route":
            parts = res["Properties"]["RouteKey"].split(" ", 1)
            if len(parts) == 2:
                keys.append((parts[0], parts[1]))
    return keys


def _segs(path: str) -> list[str]:
    return [s for s in path.strip("/").split("/") if s]


def _covers(gw_method: str, gw_path: str, method: str, path: str) -> bool:
    """Does an API Gateway RouteKey cover a mounted (method, path)?

    ``ANY`` matches any method. ``{proxy+}`` matches one-or-more trailing segments.
    A single ``{param}`` segment (on either side) matches exactly one segment.
    """
    if gw_method != "ANY" and gw_method != method:
        return False
    g, p = _segs(gw_path), _segs(path)
    for i, gs in enumerate(g):
        if gs == "{proxy+}":
            return len(p) >= i + 1  # greedy: covers the remaining segments
        if i >= len(p):
            return False
        if gs.startswith("{") and gs.endswith("}"):
            continue  # single-segment parameter matches any one segment
        if gs != p[i]:
            return False
    return len(p) == len(g)


@pytest.fixture(scope="module")
def route_keys() -> list[tuple[str, str]]:
    return _synth_route_keys()


@pytest.fixture(scope="module")
def apps():
    # ``import app`` is ambiguous here: infra/ (on sys.path via conftest AND
    # re-prepended by pytest's prepend import mode) holds the CDK entrypoint
    # ``app.py``, which shadows the backend ``app`` PACKAGE. Reorder sys.path at
    # fixture-EXECUTION time -- the module-level reorder is defeated because pytest
    # re-inserts infra/ at sys.path[0] after the test module is imported. Also drop
    # any stale non-package ``app`` a prior test cached under that name.
    while _BACKEND_SRC in sys.path:
        sys.path.remove(_BACKEND_SRC)
    sys.path.insert(0, _BACKEND_SRC)
    stale = sys.modules.get("app")
    if stale is not None and not hasattr(stale, "__path__"):
        sys.modules.pop("app", None)

    import app as app_pkg

    # Prove the shadow was avoided: we imported the backend package, not infra/app.py.
    resolved = app_pkg.__file__ or ""
    assert os.path.join("backend", "src", "app") in resolved, f"wrong 'app' imported: {resolved}"

    from app.deployment_handler import deployment_app
    from app.main import app as main_app

    return {"main": main_app, "deployment": deployment_app}


def test_the_walk_and_the_synth_both_produced_routes(route_keys, apps):
    """Guard the oracle itself: an empty side would make every assertion below vacuous."""
    assert len(route_keys) >= 50, len(route_keys)
    total = sum(len(_mounted_routes(a)) for a in apps.values())
    assert total >= 100, total


def test_the_standalone_mcp_runtime_route_is_synthesized(route_keys):
    """The exact route that shipped missing: POST /api/test-mcp-runtime/{proxy+}."""
    assert ("POST", "/api/test-mcp-runtime/{proxy+}") in route_keys


def test_the_mcp_runtime_route_actually_covers_both_mounted_subpaths(route_keys):
    for path in ("/api/test-mcp-runtime/tools", "/api/test-mcp-runtime/call"):
        assert any(_covers(gm, gp, "POST", path) for gm, gp in route_keys), path


def test_every_mounted_route_is_reachable_through_api_gateway(route_keys, apps):
    uncovered = sorted(
        f"{method} {path}"
        for name, app in apps.items()
        for method, path in _mounted_routes(app)
        if not any(_covers(gm, gp, method, path) for gm, gp in route_keys)
    )
    assert not uncovered, "mounted but unroutable at API Gateway:\n" + "\n".join(uncovered)
