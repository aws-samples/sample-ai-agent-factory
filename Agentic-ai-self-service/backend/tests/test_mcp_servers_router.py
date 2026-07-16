"""Tests for the MCP-server catalog router (/api/mcp-servers).

Pure FastAPI TestClient over the router with get_caller_sub overridden — no AWS,
no moto (the catalog has no AWS dependency). Mirrors test_connectors_catalog's
approach.
"""

from __future__ import annotations

import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from app.routers.mcp_servers import router as mcp_router  # noqa: E402
from app.services.auth import _LOCAL_DEV_SUB, get_caller_sub  # noqa: E402


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(mcp_router)
    app.dependency_overrides[get_caller_sub] = lambda: _LOCAL_DEV_SUB
    return TestClient(app)


def test_list_returns_catalog():
    r = _client().get("/api/mcp-servers")
    assert r.status_code == 200, r.text
    data = r.json()
    ids = {e["id"] for e in data}
    assert {"aws-knowledge", "databricks", "notion"} <= ids
    # Summary shape only (no example_tools / credentials in list).
    assert "example_tools" not in data[0]
    assert {"id", "tier", "verified", "auth_type", "live_testable"} <= set(data[0])


def test_filter_by_tier_and_live_testable():
    c = _client()
    direct_none = c.get("/api/mcp-servers?tier=direct-none").json()
    assert all(e["tier"] == "direct-none" for e in direct_none)
    live = c.get("/api/mcp-servers?live_testable=true").json()
    assert all(e["live_testable"] for e in live)
    assert {"aws-knowledge", "deepwiki", "cloudflare-docs"} <= {e["id"] for e in live}


def test_detail_includes_endpoint_tools_and_creds():
    r = _client().get("/api/mcp-servers/aws-knowledge")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["endpoint"] == "https://knowledge-mcp.global.api.aws"
    assert d["auth_type"] == "none"
    assert "search_documentation" in d["example_tools"]
    assert d["credentials_needed"]


def test_apikey_detail_carries_descriptor():
    d = _client().get("/api/mcp-servers/exa").json()
    assert d["tier"] == "direct-apikey"
    assert d["api_key_descriptor"]["location"] == "HEADER"
    assert d["api_key_descriptor"]["parameter_name"] == "x-api-key"


def test_unknown_id_is_404_not_disclosure():
    assert _client().get("/api/mcp-servers/does-not-exist").status_code == 404


def test_bad_slug_is_400():
    assert _client().get("/api/mcp-servers/BAD__SLUG!!").status_code == 400
