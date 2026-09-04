"""The registry provider seam — Workstream B.

The load-bearing property of this workstream is that it is ADDITIVE:
``test_registry_store.py`` and ``test_registry_rbac.py`` must pass with no edits,
because with the default ``dynamodb`` provider the router's call path is the same
one it used before the seam existed. These tests cover the seam itself and the
LiteLLM backend, and pin the three traps that make a pluggable governance backend
dangerous:

1. ``RegistryEntry.status`` DEFAULTS to ``"approved"``, so a provider that
   projects an external catalog into that model gets approval for free.
2. ``RegistryQueryFailed`` must be the SAME class object across import paths, or
   the deploy gate's ``except Exception`` swallows it and the gate fails OPEN.
3. ``DynamoRegistryProvider`` must resolve the store INSIDE each method, or it
   snapshots whichever singleton existed first.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
from collections.abc import Iterator

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from app.routers import registry as registry_router_mod  # noqa: E402
from app.routers.registry import caller_is_admin  # noqa: E402
from app.services import registry_providers as rp  # noqa: E402
from app.services import registry_store as rs_mod  # noqa: E402
from app.services.auth import get_caller_sub  # noqa: E402
from app.services.registry_providers import litellm as lgl  # noqa: E402
from app.services.registry_providers.base import (  # noqa: E402
    SOURCE_LITELLM,
    SOURCE_PLATFORM,
    RegistryProvider,
    UnsupportedRegistryOperation,
)
from app.services.registry_providers.dynamo import DynamoRegistryProvider  # noqa: E402
from app.services.registry_store import RegistryEntry, RegistryStore  # noqa: E402
from moto import mock_aws  # noqa: E402

_BACKEND = pathlib.Path(__file__).resolve().parents[1]

CALLER = "sub-dev-1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _create_table() -> None:
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName="AgentRegistry",
        KeySchema=[
            {"AttributeName": "org_id", "KeyType": "HASH"},
            {"AttributeName": "agent_slug", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "agent_slug", "AttributeType": "S"},
            {"AttributeName": "owner_sub", "AttributeType": "S"},
            {"AttributeName": "visibility", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "owner_sub-agent_slug-index",
                "KeySchema": [
                    {"AttributeName": "owner_sub", "KeyType": "HASH"},
                    {"AttributeName": "agent_slug", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "visibility-agent_slug-index",
                "KeySchema": [
                    {"AttributeName": "visibility", "KeyType": "HASH"},
                    {"AttributeName": "agent_slug", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def store() -> Iterator[RegistryStore]:
    with mock_aws():
        _create_table()
        s = RegistryStore(table_name="AgentRegistry", region="us-east-1")
        rs_mod._registry_store = s
        yield s
        rs_mod._registry_store = None


def _entry(slug: str, owner: str = CALLER, **kw) -> RegistryEntry:
    defaults = dict(
        agent_slug=slug,
        owner_sub=owner,
        display_name=slug,
        canvas_snapshot={"nodes": []},
        status="approved",
    )
    defaults.update(kw)
    return RegistryEntry(**defaults)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_default_provider_is_dynamodb_when_nothing_is_configured(monkeypatch):
    """The setting row is unreachable in a unit test (conftest strips creds), and
    an unreachable settings table must mean 'the behavior that existed before',
    not an exception and not a half-configured backend."""
    monkeypatch.delenv("REGISTRY_PROVIDER", raising=False)
    assert rp.default_registry_provider() == "dynamodb"
    assert isinstance(rp.get_registry_provider(), DynamoRegistryProvider)


def test_env_override_selects_the_provider(monkeypatch):
    monkeypatch.setenv("REGISTRY_PROVIDER", "litellm")
    assert rp.default_registry_provider() == "litellm"
    assert isinstance(rp.get_registry_provider(), lgl.LiteLLMRegistryProvider)


def test_an_unrecognized_setting_falls_back_to_dynamodb(monkeypatch):
    monkeypatch.setenv("REGISTRY_PROVIDER", "postgres")
    assert rp.default_registry_provider() == "dynamodb"


def test_set_default_registry_provider_rejects_an_unknown_backend():
    with pytest.raises(ValueError, match="must be one of"):
        rp.set_default_registry_provider("mysql")


def test_the_provider_is_not_module_cached(monkeypatch):
    """A new object per call. Caching would ignore a setting change until the
    Lambda recycled, and would break any test that flips the setting mid-run."""
    monkeypatch.delenv("REGISTRY_PROVIDER", raising=False)
    assert rp.get_registry_provider() is not rp.get_registry_provider()


def test_both_providers_satisfy_the_protocol():
    assert isinstance(DynomoOrDynamo := DynamoRegistryProvider(), RegistryProvider)
    assert isinstance(lgl.LiteLLMRegistryProvider(), RegistryProvider)
    assert DynomoOrDynamo.capabilities().provider == "dynamodb"


# ---------------------------------------------------------------------------
# The Dynamo provider is a pass-through
# ---------------------------------------------------------------------------


def test_dynamo_provider_is_behaviorally_identical_to_the_store(store):
    provider = DynamoRegistryProvider()
    provider.put(_entry("alpha"))

    assert provider.get("default-org", "alpha").display_name == "alpha"
    assert [e.agent_slug for e in provider.list_for_org("default-org")] == ["alpha"]
    assert [e.agent_slug for e in provider.list_for_owner(CALLER)] == ["alpha"]
    provider.increment_usage("default-org", "alpha")
    assert provider.get("default-org", "alpha").usage_count == 1
    assert provider.update("default-org", "alpha", {"description": "d"}).description == "d"
    assert provider.delete("default-org", "alpha") is True
    assert provider.get("default-org", "alpha") is None

    # Same results as calling the store directly.
    store.put(_entry("beta", status="pending"))
    assert [e.agent_slug for e in provider.list_pending("default-org")] == ["beta"]


def test_dynamo_provider_resolves_the_store_inside_every_method(store):
    """The ~30 RBAC/store tests install their moto store by assigning to the
    module singleton. A provider that captured it in __init__ would snapshot
    whichever instance existed first and silently ignore that assignment."""
    provider = DynamoRegistryProvider()
    provider.put(_entry("gamma"))
    assert provider.get("default-org", "gamma") is not None

    # Swap the singleton out from under the already-constructed provider.
    class _Sentinel:
        def get(self, org_id, slug):
            return "swapped"

    rs_mod._registry_store = _Sentinel()
    assert provider.get("default-org", "gamma") == "swapped"


def test_dynamo_capabilities_declare_nothing_read_only():
    caps = DynamoRegistryProvider().capabilities()
    assert caps.read_only_sources == ()
    assert caps.is_read_only(_entry("x")) is False
    assert all(
        (caps.supports_publish, caps.supports_update, caps.supports_delete, caps.supports_review, caps.supports_clone)
    )


def test_a_platform_publish_is_always_platform_sourced(store):
    """A client cannot claim external provenance to dodge the mutation guards."""
    saved = DynamoRegistryProvider().put(_entry("delta", source=SOURCE_LITELLM))
    assert saved.source == SOURCE_PLATFORM


# ---------------------------------------------------------------------------
# The three traps
# ---------------------------------------------------------------------------


def test_registry_entry_status_still_defaults_to_approved():
    """Not a wish — a fact this workstream has to work around. If this ever
    changes, the explicit status= on the LiteLLM projection can be revisited; while
    it holds, any projection that omits status silently hands out approval."""
    assert RegistryEntry(agent_slug="s", owner_sub="o", display_name="d").status == "approved"


def test_the_litellm_projection_sets_status_explicitly():
    """AST guard, not a value check. A value check passes whether the "approved"
    came from the projection or from the model default — which is precisely the
    ambiguity that makes the default dangerous. This asserts the code says so."""
    src = (_BACKEND / "src" / "app" / "services" / "registry_providers" / "litellm.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "_project")
    call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "RegistryEntry")
    kwargs = {k.arg for k in call.keywords}
    assert "status" in kwargs, "_project must set status explicitly, never inherit the 'approved' default"
    assert "source" in kwargs, "_project must mark provenance so the router can refuse mutations"


def test_registry_query_failed_is_the_same_class_across_import_paths():
    """If the LiteLLM backend raised its OWN RegistryQueryFailed, the deploy gate
    — which catches the one from aws_agent_registry — would miss it, and the
    surrounding `except Exception` in deployment_handler would log a warning and
    LET THE DEPLOY THROUGH. A governance gate that opens on error is not a gate."""
    from app.services.aws_agent_registry import RegistryQueryFailed as FromAws

    assert lgl.RegistryQueryFailed is FromAws


def test_the_litellm_module_does_not_define_its_own_query_failure():
    src = (_BACKEND / "src" / "app" / "services" / "registry_providers" / "litellm.py").read_text()
    names = [n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef)]
    assert "RegistryQueryFailed" not in names


# ---------------------------------------------------------------------------
# LiteLLM projection
# ---------------------------------------------------------------------------

_SERVERS = [
    {"server_id": "s1", "alias": "GitHub MCP", "description": "issues", "tools": [{"name": "list_issues"}]},
    {"server_id": "s2", "mcp_info": {"server_name": "jira"}},
    {"server_id": "s3", "alias": "retired", "enabled": False},
]


@pytest.fixture
def litellm_configured(monkeypatch):
    monkeypatch.setattr(
        lgl,
        "get_litellm_registry_config",
        lambda: {"base_url": "https://litellm.example.com", "api_key_ref": "arn:x", "verified": True},
    )
    monkeypatch.setattr(lgl, "_read_api_key", lambda ref: "sk-test")
    monkeypatch.setattr(lgl, "_get_json", lambda url, key, servers=None: {"data": _SERVERS})
    return None


def test_projection_maps_server_name_and_alias_into_the_catalog_shape(litellm_configured):
    entries = lgl.LiteLLMRegistryProvider()._projected()
    assert set(entries) == {"github-mcp", "jira"}, "disabled servers must not be projected at all"
    gh = entries["github-mcp"]
    assert gh.display_name == "GitHub MCP"
    assert gh.description == "issues"
    assert gh.source == SOURCE_LITELLM
    assert gh.status == "approved"
    assert "list_issues" in gh.tags
    assert gh.canvas_snapshot == {}


def test_a_projected_row_is_owned_by_a_sentinel_no_caller_can_match(litellm_configured):
    """owner_sub is checked with `entry.owner_sub == caller_sub` in the visibility
    helper, so an empty or guessable owner would hand a caller ownership of a
    governed catalog entry."""
    gh = lgl.LiteLLMRegistryProvider()._projected()["github-mcp"]
    assert gh.owner_sub == lgl.LITELLM_OWNER_SENTINEL
    assert ":" in gh.owner_sub, "a Cognito sub is a UUID and can never contain a colon"


def test_server_enablement_honours_several_field_spellings():
    assert lgl._server_is_enabled({}) is True, "absent flag means enabled — do not hide the whole catalog"
    assert lgl._server_is_enabled({"enabled": False}) is False
    assert lgl._server_is_enabled({"is_enabled": False}) is False
    assert lgl._server_is_enabled({"disabled": True}) is False
    assert lgl._server_is_enabled({"status": "disabled"}) is False
    assert lgl._server_is_enabled({"status": "healthy"}) is True


def test_an_unreadable_catalog_raises_instead_of_returning_an_empty_one(monkeypatch):
    monkeypatch.setattr(lgl, "get_litellm_registry_config", lambda: None)
    with pytest.raises(lgl.RegistryQueryFailed, match="not configured"):
        lgl.list_litellm_servers()


def test_a_401_from_litellm_names_the_key_not_the_url(monkeypatch):
    import urllib.error

    monkeypatch.setattr(
        lgl,
        "get_litellm_registry_config",
        lambda: {"base_url": "https://l.example.com", "api_key_ref": "", "verified": True},
    )
    monkeypatch.setattr(lgl, "_read_api_key", lambda ref: "")

    def _boom(*a, **kw):
        raise urllib.error.HTTPError("u", 401, "no", {}, None)

    monkeypatch.setattr(lgl, "_get_json", _boom)
    with pytest.raises(lgl.RegistryQueryFailed, match="rejected the virtual key"):
        lgl.list_litellm_servers()


# ---------------------------------------------------------------------------
# LiteLLM writes: sidecar, collisions, read-only projections
# ---------------------------------------------------------------------------


def test_sidecar_rows_and_projections_are_merged(store, litellm_configured):
    provider = lgl.LiteLLMRegistryProvider()
    provider.put(_entry("my-own-agent"))
    slugs = {e.agent_slug for e in provider.list_for_org("default-org")}
    assert slugs == {"my-own-agent", "github-mcp", "jira"}


def test_publishing_over_a_projected_slug_is_refused(store, litellm_configured):
    """Defence in depth beneath the router. `publish` disambiguates before it ever
    gets here (see the router test below), so this is the backstop for any other
    caller: writing a sidecar row onto a projected slug would shadow the governed
    catalog entry in every listing — impersonation of a row nobody owns."""
    with pytest.raises(lgl.RegistryEntryConflict, match="already names an MCP server"):
        lgl.LiteLLMRegistryProvider().put(_entry("github-mcp"))


def test_the_router_maps_a_conflict_to_409_rather_than_500(store, monkeypatch):
    """The backstop above is only useful if it surfaces as a client error. A bare
    ValueError escaping a handler is a 500 and reads as a platform fault."""

    class _Conflicting:
        def capabilities(self):
            return lgl.LiteLLMRegistryProvider().capabilities()

        def get(self, org_id, slug):
            return None

        def put(self, entry):
            raise lgl.RegistryEntryConflict("'x' already names an MCP server; choose a different name.")

    monkeypatch.setattr(registry_router_mod, "get_registry_provider", lambda: _Conflicting())
    resp = _client().post("/api/registry", json={"display_name": "x", "canvas_snapshot": {}})
    assert resp.status_code == 409
    assert "choose a different" in resp.json()["detail"]


def test_a_preexisting_sidecar_row_wins_over_a_projection(store, litellm_configured):
    """put() refuses collisions, so one can only exist from before the backend was
    switched on. The sidecar row must stay reachable rather than vanish behind a
    read-only projection its owner cannot touch."""
    store.put(_entry("jira", display_name="my jira agent"))
    entries = {e.agent_slug: e for e in lgl.LiteLLMRegistryProvider().list_for_org("default-org")}
    assert entries["jira"].display_name == "my jira agent"
    assert entries["jira"].source == SOURCE_PLATFORM
    assert len([e for e in entries.values() if e.agent_slug == "jira"]) == 1


def test_mutating_a_projection_raises_unsupported_not_a_silent_noop(store, litellm_configured):
    provider = lgl.LiteLLMRegistryProvider()
    with pytest.raises(UnsupportedRegistryOperation, match="no write API"):
        provider.update("default-org", "github-mcp", {"description": "x"})
    with pytest.raises(UnsupportedRegistryOperation, match="no write API"):
        provider.delete("default-org", "github-mcp")


def test_increment_usage_on_a_projection_is_a_silent_skip(store, litellm_configured):
    """Telemetry must never fail a clone; there is simply no row to bump."""
    lgl.LiteLLMRegistryProvider().increment_usage("default-org", "github-mcp")


def test_pending_queue_and_mine_and_public_are_sidecar_only(store, litellm_configured):
    provider = lgl.LiteLLMRegistryProvider()
    store.put(_entry("waiting", status="pending"))
    # A projection can never be pending: LiteLLM has no review state, so a row that
    # cannot be approved must not sit in an admin's queue forever.
    assert [e.agent_slug for e in provider.list_pending("default-org")] == ["waiting"]
    assert [e.agent_slug for e in provider.list_for_owner(CALLER)] == ["waiting"]
    assert provider.list_public() == []


def test_litellm_capabilities_mark_only_projections_read_only():
    caps = lgl.LiteLLMRegistryProvider().capabilities()
    assert caps.read_only_sources == (SOURCE_LITELLM,)
    assert caps.is_read_only(_entry("a", source=SOURCE_LITELLM)) is True
    # Sidecar rows keep the FULL workflow. A blanket "review is unsupported" would
    # strand every published agent at pending, invisible to non-owners forever.
    assert caps.is_read_only(_entry("a")) is False
    assert caps.supports_review is True


# ---------------------------------------------------------------------------
# Router behavior under each backend
# ---------------------------------------------------------------------------


def _client(caller: str = CALLER, admin: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(registry_router_mod.router)
    app.dependency_overrides[get_caller_sub] = lambda: caller
    app.dependency_overrides[caller_is_admin] = lambda: admin
    return TestClient(app, raise_server_exceptions=False)


def test_the_error_decorator_keeps_dependency_injection_working(store):
    """functools.wraps + inspect.signature following __wrapped__ is what makes the
    decorator invisible to FastAPI. If that broke, every handler would 422 on its
    injected caller_sub rather than fail visibly here."""
    resp = _client().get("/api/registry")
    assert resp.status_code == 200


def test_router_reports_provenance_and_read_only(store, litellm_configured, monkeypatch):
    monkeypatch.setenv("REGISTRY_PROVIDER", "litellm")
    rows = {r["agent_slug"]: r for r in _client().get("/api/registry").json()}
    assert rows["github-mcp"]["source"] == "litellm"
    assert rows["github-mcp"]["read_only"] is True


def test_router_marks_platform_rows_mutable_even_under_litellm(store, litellm_configured, monkeypatch):
    monkeypatch.setenv("REGISTRY_PROVIDER", "litellm")
    _client().post(
        "/api/registry",
        json={"display_name": "mine", "canvas_snapshot": {"nodes": []}},
    )
    rows = {r["agent_slug"]: r for r in _client().get("/api/registry").json()}
    assert rows["mine"]["source"] == "platform"
    assert rows["mine"]["read_only"] is False


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", "/api/registry/github-mcp"),
        ("delete", "/api/registry/github-mcp"),
        ("post", "/api/registry/github-mcp/clone"),
        ("post", "/api/registry/github-mcp/approve"),
        ("post", "/api/registry/github-mcp/reject"),
    ],
)
def test_unsupported_operations_are_501_not_a_silent_degradation(store, litellm_configured, monkeypatch, method, path):
    monkeypatch.setenv("REGISTRY_PROVIDER", "litellm")
    client = _client(admin=True)
    kwargs = {"json": {"description": "x"}} if method == "put" else {}
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 501, resp.text
    detail = resp.json()["detail"]
    assert "LiteLLM" in detail, "a 501 must name where the truth lives, not just refuse"
    # Caught live in Frankfurt: approve/reject passed the verb as "approved here"
    # while _assert_mutable already appends " here", so the refusal read "cannot be
    # approved here here." The message is the entire product of a 501 — it is the
    # only thing telling an admin to go change it in LiteLLM — so a garbled one is
    # a real defect, and each verb reads as prose exactly once.
    # Scoped to the verb clause — the trailing capability notes legitimately say
    # "read-only here", so a whole-message count would fail on correct prose.
    clause = detail.split(". ")[0]
    assert "here here" not in clause, clause
    assert clause.count(" here") == 1, clause


def test_publishing_a_name_a_governed_server_holds_does_not_shadow_it(store, litellm_configured, monkeypatch):
    """The router's pre-existing cross-owner disambiguation (Bug 122) already covers
    this: a projected entry's owner is the sentinel, so it reads as "another owner
    holds this slug" and publish suffixes rather than overwrites. That is the better
    outcome than the provider's 409 — the developer still gets their agent, and the
    governed catalog row keeps its name."""
    monkeypatch.setenv("REGISTRY_PROVIDER", "litellm")
    resp = _client().post("/api/registry", json={"display_name": "GitHub MCP", "canvas_snapshot": {}})
    assert resp.status_code == 200
    assert resp.json()["agent_slug"] != "github-mcp"
    assert resp.json()["source"] == "platform"

    rows = {r["agent_slug"]: r for r in _client().get("/api/registry").json()}
    assert rows["github-mcp"]["source"] == "litellm", "the governed row must survive intact"
    assert rows["github-mcp"]["read_only"] is True


def test_an_unreadable_catalog_is_503_not_a_short_list(store, monkeypatch):
    """The failure mode this prevents: LiteLLM is down, the listing quietly returns
    only the sidecar rows, and the UI shows a plausible-looking catalog that is
    missing every governed MCP server."""
    monkeypatch.setenv("REGISTRY_PROVIDER", "litellm")
    monkeypatch.setattr(lgl, "get_litellm_registry_config", lambda: None)
    resp = _client().get("/api/registry")
    assert resp.status_code == 503
    assert "partial catalog" in resp.json()["detail"]


def test_the_default_backend_leaves_every_operation_available(store):
    """The additive guarantee, asserted rather than assumed."""
    client = _client(admin=True)
    assert client.post("/api/registry", json={"display_name": "a", "canvas_snapshot": {}}).status_code == 200
    assert client.post("/api/registry/a/approve").status_code == 200
    assert client.put("/api/registry/a", json={"description": "d"}).status_code == 200
    assert client.post("/api/registry/a/clone").status_code == 200
    assert client.delete("/api/registry/a").status_code == 200


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------


def test_litellm_config_never_returns_the_key(store, litellm_configured, monkeypatch):
    monkeypatch.setenv("REGISTRY_PROVIDER", "litellm")
    body = _client().get("/api/registry/litellm-config").json()
    assert body["provider"] == "litellm"
    assert body["api_key_ref"] == "arn:x"
    assert "api_key" not in body
    assert "sk-test" not in json.dumps(body)


def test_a_private_base_url_is_refused_with_400_and_no_secret_is_minted(store, monkeypatch):
    """Unreachable is tolerable (a private LiteLLM is reachable from the VPC-mode
    runtime); pointing the CONTROL PLANE at RFC1918 or link-local space is not."""
    monkeypatch.setattr(
        lgl, "put_registry_secret", lambda *a, **kw: pytest.fail("must not mint a secret for a rejected URL")
    )
    resp = _client(admin=True).post(
        "/api/registry/litellm-config",
        json={"base_url": "http://169.254.169.254/", "api_key": "sk-x"},
    )
    assert resp.status_code == 400
    assert "LiteLLM registry base URL" in resp.json()["detail"]
    assert "OIDC" not in resp.json()["detail"], "the shared guard must not misname the subject"


def test_config_requires_admin(store):
    resp = _client(admin=False).post(
        "/api/registry/litellm-config",
        json={"base_url": "https://l.example.com", "api_key": "sk-x"},
    )
    assert resp.status_code == 403


def test_a_foreign_secret_arn_is_refused(store, monkeypatch):
    """Without this a tenant could point the registry at any secret in the account
    and have the control plane read it back for them.

    The SSRF guard runs first and resolves DNS, which no unit test should depend
    on, so it is stubbed to isolate the ref check."""
    from app.services import gateway_deployer

    monkeypatch.setattr(gateway_deployer, "_validate_outbound_url", lambda url, **kw: url)
    resp = _client(admin=True).post(
        "/api/registry/litellm-config",
        json={"base_url": "https://litellm.example.com", "api_key_ref": "arn:aws:secretsmanager:::secret:prod/db"},
    )
    assert resp.status_code == 400
    assert "agentcore-registry/" in resp.json()["detail"]


def test_the_ssrf_guard_runs_before_anything_else_is_touched(store, monkeypatch):
    """Ordering is the control: a base URL that fails the guard must be rejected
    before the key is read, the proxy is probed, or a secret is minted."""
    from app.services import gateway_deployer

    def _reject(url, **kw):
        raise ValueError(f"{kw.get('label', '')} nope")

    monkeypatch.setattr(gateway_deployer, "_validate_outbound_url", _reject)
    monkeypatch.setattr(lgl, "validate_secret_ref", lambda r: pytest.fail("ref checked before the URL"))
    monkeypatch.setattr(lgl, "probe_litellm_registry", lambda *a: pytest.fail("probed a rejected URL"))
    monkeypatch.setattr(lgl, "put_registry_secret", lambda *a, **kw: pytest.fail("minted for a rejected URL"))

    resp = _client(admin=True).post(
        "/api/registry/litellm-config",
        json={"base_url": "https://whatever.example.com", "api_key": "sk-x"},
    )
    assert resp.status_code == 400
    assert "LiteLLM registry base URL" in resp.json()["detail"]


def test_the_secret_namespace_is_its_own(monkeypatch):
    """Not agentcore-connector/ — that namespace is swept by per-deployment
    teardown, and a registry credential outlives every deployment."""
    assert lgl.SECRET_NAMESPACE == "agentcore-registry/"
    assert "connector" not in lgl.SECRET_NAMESPACE
    assert "provider" not in lgl.SECRET_NAMESPACE


def test_the_iam_grant_covers_the_new_namespace():
    src = (_BACKEND.parent / "infra" / "stacks" / "platform" / "lambdas.py").read_text()
    assert "secret:agentcore-registry/*" in src


def test_an_unreachable_but_public_proxy_saves_as_unverified(monkeypatch):
    """Fail-closed here would make a private self-hosted LiteLLM unusable."""

    def _boom(*a, **kw):
        raise TimeoutError("no route")

    monkeypatch.setattr(lgl, "_get_json", _boom)
    probe = lgl.probe_litellm_registry("https://litellm.internal.example.com", "sk-x")
    assert probe["reachable"] is False
    assert "unverified" in probe["detail"]


def test_a_rejected_key_is_a_hard_error_not_an_unverified_save(monkeypatch):
    import urllib.error

    def _boom(*a, **kw):
        raise urllib.error.HTTPError("u", 403, "no", {}, None)

    monkeypatch.setattr(lgl, "_get_json", _boom)
    with pytest.raises(ValueError, match="rejected the virtual key"):
        lgl.probe_litellm_registry("https://litellm.example.com", "sk-bad")


def test_a_wrong_base_url_is_a_hard_error(monkeypatch):
    import urllib.error

    def _boom(*a, **kw):
        raise urllib.error.HTTPError("u", 404, "no", {}, None)

    monkeypatch.setattr(lgl, "_get_json", _boom)
    with pytest.raises(ValueError, match="no /v1/mcp/server route"):
        lgl.probe_litellm_registry("https://wrong.example.com", "sk-x")


def test_litellm_servers_route_distinguishes_disabled_from_absent(store, litellm_configured, monkeypatch):
    monkeypatch.setenv("REGISTRY_PROVIDER", "litellm")
    body = _client().get("/api/registry/litellm-servers").json()
    by_name = {s["name"]: s for s in body["servers"]}
    assert by_name["retired"]["enabled"] is False
    assert by_name["GitHub MCP"]["enabled"] is True
    assert by_name["GitHub MCP"]["slug"] == "github-mcp"


def test_the_literal_litellm_routes_precede_the_slug_route():
    """FastAPI matches in declaration order: a literal path declared after
    /{slug} is swallowed by the path parameter. Caught live for the /aws-* routes.
    """
    src = (_BACKEND / "src" / "app" / "routers" / "registry.py").read_text()
    slug_at = src.index('@router.get("/{slug}"')
    for literal in ("/litellm-config", "/litellm-servers"):
        assert src.index(f'"{literal}"') < slug_at, f"{literal} must be declared before /{{slug}}"
