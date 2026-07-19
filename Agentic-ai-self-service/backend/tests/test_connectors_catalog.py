"""Phase 3 Gap 3E — pre-built connector catalog tests.

Pure-unit tests on the catalog data + helpers, plus a FastAPI TestClient over
the connectors router with ``get_caller_sub`` overridden (no AWS, no moto
needed — the catalog has no AWS dependency at all).

Scope reminder: this gap is catalog-only. The ``tool_schemas`` describe gateway
targets to advertise; per-connector Lambda execution is an explicit follow-up
and is intentionally NOT covered here.
"""

from __future__ import annotations

import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from app.routers.connectors import router as connectors_router  # noqa: E402
from app.services.auth import _LOCAL_DEV_SUB, get_caller_sub  # noqa: E402
from app.services.connectors_catalog import (  # noqa: E402
    ALLOWED_SCHEMA_KEYS,
    CONNECTORS,
    get_connector,
    list_connectors,
)

EXPECTED_IDS = {
    "slack",
    "github",
    "jira",
    "notion",
    "salesforce",
    "google_drive",
    "gmail",
    "confluence",
    "pagerduty",
    "hubspot",
    "stripe",
    "sendgrid",
}

REQUIRED_KEYS = {
    "id",
    "display_name",
    "icon",
    "category",
    "auth_type",
    "credential_schema",
    "tool_schemas",
    "capabilities",
}

import re  # noqa: E402

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


# ---------------------------------------------------------------------------
# Catalog data contract
# ---------------------------------------------------------------------------


def test_all_expected_connectors_present():
    assert set(CONNECTORS.keys()) == EXPECTED_IDS
    assert len(CONNECTORS) == 12


def test_each_connector_has_required_keys_and_valid_fields():
    for cid, entry in CONNECTORS.items():
        assert set(entry.keys()) >= REQUIRED_KEYS, f"{cid} missing keys"
        # id matches its dict key and the slug regex
        assert entry["id"] == cid
        assert _SLUG_RE.match(cid), f"{cid} not a valid slug"
        assert entry["auth_type"] in {"oauth", "api_key"}, f"{cid} bad auth_type"
        assert isinstance(entry["display_name"], str) and entry["display_name"]
        assert isinstance(entry["icon"], str) and entry["icon"]
        assert isinstance(entry["category"], str) and entry["category"]
        assert isinstance(entry["capabilities"], list) and entry["capabilities"]
        assert isinstance(entry["tool_schemas"], list) and entry["tool_schemas"]
        assert isinstance(entry["credential_schema"], dict)


def _assert_allowed_keys_recursive(schema, ctx: str):
    """Recursively assert a JSON-Schema dict only uses Bug-10 allowed keys."""
    assert isinstance(schema, dict), f"{ctx}: schema not a dict"
    illegal = set(schema.keys()) - ALLOWED_SCHEMA_KEYS
    assert not illegal, f"{ctx}: illegal schema keys {illegal} (Bug 10 regression)"
    props = schema.get("properties")
    if props is not None:
        assert isinstance(props, dict), f"{ctx}: properties not a dict"
        for pname, pschema in props.items():
            _assert_allowed_keys_recursive(pschema, f"{ctx}.properties.{pname}")
    items = schema.get("items")
    if items is not None:
        _assert_allowed_keys_recursive(items, f"{ctx}.items")


def test_tool_schemas_only_use_bug10_allowed_keys():
    """Guards against the Bug 10 regression (enum/default/format/etc.)."""
    for cid, entry in CONNECTORS.items():
        for ts in entry["tool_schemas"]:
            assert "name" in ts and ts["name"], f"{cid}: tool missing name"
            assert "description" in ts and ts["description"], f"{cid}: tool missing description"
            assert "inputSchema" in ts, f"{cid}: tool {ts.get('name')} missing inputSchema"
            _assert_allowed_keys_recursive(ts["inputSchema"], f"{cid}.{ts['name']}.inputSchema")
        # credential_schema is also advertised-shaped; hold it to the same rule.
        _assert_allowed_keys_recursive(entry["credential_schema"], f"{cid}.credential_schema")


def test_tool_names_within_bug11_qualified_limit():
    """When later registered as <connector>___<tool> gateway targets, the
    qualified name must stay under AgentCore's 64-char limit (Bug 11)."""
    for cid, entry in CONNECTORS.items():
        for ts in entry["tool_schemas"]:
            qualified = f"{cid}___{ts['name']}"
            assert len(qualified) <= 64, f"{qualified} exceeds 64 chars"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_get_connector_returns_entry_or_none():
    slack = get_connector("slack")
    assert slack is not None and slack["id"] == "slack"
    assert get_connector("does-not-exist") is None


def test_get_connector_returns_a_copy_not_the_module_object():
    a = get_connector("slack")
    a["display_name"] = "MUTATED"
    b = get_connector("slack")
    assert b["display_name"] == "Slack"  # original untouched


def test_list_connectors_returns_all():
    entries = list_connectors()
    assert {e["id"] for e in entries} == EXPECTED_IDS


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(connectors_router)
    app.dependency_overrides[get_caller_sub] = lambda: _LOCAL_DEV_SUB
    return TestClient(app)


def test_list_endpoint_returns_summaries(client: TestClient):
    resp = client.get("/api/connectors")
    assert resp.status_code == 200
    body = resp.json()
    assert {c["id"] for c in body} == EXPECTED_IDS
    # Summary must NOT leak tool internals or credential schema.
    for c in body:
        assert "tool_schemas" not in c
        assert "credential_schema" not in c
        assert set(c.keys()) == {
            "id",
            "display_name",
            "icon",
            "category",
            "auth_type",
            "capabilities",
        }


def test_detail_endpoint_returns_full_entry(client: TestClient):
    resp = client.get("/api/connectors/slack")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "slack"
    assert body["credential_schema"]
    assert isinstance(body["tool_schemas"], list) and body["tool_schemas"]
    assert body["tool_schemas"][0]["name"]


def test_detail_unknown_id_returns_404(client: TestClient):
    resp = client.get("/api/connectors/nonexistent")
    assert resp.status_code == 404


def test_detail_invalid_slug_returns_400(client: TestClient):
    resp = client.get("/api/connectors/Invalid!Slug")
    assert resp.status_code == 400


def test_endpoints_declare_auth_dependency():
    """Catalog is auth-gated even though it is not owner-scoped."""
    from fastapi.params import Depends as DependsParam

    for route in connectors_router.routes:
        dependants = getattr(route, "dependant", None)
        # Every connectors route must carry the get_caller_sub dependency.
        dep_calls = []
        for dep in getattr(dependants, "dependencies", []):
            dep_calls.append(dep.call)
        assert get_caller_sub in dep_calls, f"{route.path} missing auth dependency"
    # Sanity: the import shape we rely on exists.
    assert DependsParam is not None
