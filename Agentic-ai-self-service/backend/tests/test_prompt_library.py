"""Phase 3 Gap 3H — Prompt Management Library store + router + deploy-hook tests.

moto-backed DDB; FastAPI TestClient with get_caller_sub overridden. No live
AWS. Verifies store round-trip, versioning (add/promote/resolve), GSI owner
isolation, router CRUD, slug disambiguation (Bug 122), tenant isolation
(404 cross-tenant), ACL-drift (Bug 126), promote 409 on unknown version, the
consumer resolve path, the deploy-time resolution hook, and codegen safety of
a resolved multi-line body (EXEC against strands stubs — Bug 125).
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

moto = pytest.importorskip("moto")
from app.routers import prompts as prompts_router_mod  # noqa: E402
from app.services import prompt_library_store as pl_mod  # noqa: E402
from app.services.auth import get_caller_sub  # noqa: E402
from app.services.prompt_library_store import (  # noqa: E402
    DEFAULT_ORG_ID,
    PromptEntry,
    PromptLibraryStore,
    PromptVersion,
    new_prompt_version_id,
    slugify,
)
from moto import mock_aws  # noqa: E402


def _create_table() -> None:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="PromptLibrary",
        KeySchema=[
            {"AttributeName": "org_id", "KeyType": "HASH"},
            {"AttributeName": "prompt_name", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "prompt_name", "AttributeType": "S"},
            {"AttributeName": "owner_sub", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "owner_sub-prompt_name-index",
                "KeySchema": [
                    {"AttributeName": "owner_sub", "KeyType": "HASH"},
                    {"AttributeName": "prompt_name", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        _create_table()
        yield


@pytest.fixture
def store(aws: None) -> PromptLibraryStore:
    s = PromptLibraryStore(table_name="PromptLibrary", region="us-east-1")
    # Point the module singleton at the moto-backed store so the router uses it.
    pl_mod._prompt_library_store = s
    return s


def _client(caller: str) -> TestClient:
    app = FastAPI()
    app.include_router(prompts_router_mod.router)
    app.dependency_overrides[get_caller_sub] = lambda: caller
    return TestClient(app)


def _seed(store: PromptLibraryStore, *, owner: str, name: str, body: str) -> PromptEntry:
    vid = new_prompt_version_id()
    entry = PromptEntry(
        prompt_name=name,
        owner_sub=owner,
        display_name=name,
        versions=[PromptVersion(version_id=vid, body=body, created_by=owner)],
        default_version_id=vid,
    )
    return store.put(entry)


# ---------------------------------------------------------------------------
# slugify + id minting
# ---------------------------------------------------------------------------


def test_slugify_basic():
    assert slugify("Support Triage Prompt") == "support-triage-prompt"
    assert slugify("My!!!Prompt  v2") == "my-prompt-v2"
    assert slugify("") == "prompt"


def test_new_version_id_distinct():
    a, b = new_prompt_version_id(), new_prompt_version_id()
    assert a != b
    assert len(a) == 32 and all(c in "0123456789abcdef" for c in a)


# ---------------------------------------------------------------------------
# store round-trip + versioning
# ---------------------------------------------------------------------------


def test_store_put_get(store: PromptLibraryStore):
    _seed(store, owner="alice", name="greeter", body="Be polite.")
    loaded = store.get(DEFAULT_ORG_ID, "greeter")
    assert loaded is not None
    assert loaded.owner_sub == "alice"
    assert loaded.versions[0].body == "Be polite."
    assert loaded.default_version_id == loaded.versions[0].version_id


def test_add_version_appends_distinct_ids(store: PromptLibraryStore):
    _seed(store, owner="alice", name="p", body="v1")
    v2 = store.add_version(DEFAULT_ORG_ID, "p", "v2 body", "alice")
    v3 = store.add_version(DEFAULT_ORG_ID, "p", "v3 body", "alice")
    assert v2 != v3
    entry = store.get(DEFAULT_ORG_ID, "p")
    assert len(entry.versions) == 3
    # Default unchanged by add_version (still v1).
    assert entry.default_version_id == entry.versions[0].version_id


def test_add_version_missing_prompt_returns_none(store: PromptLibraryStore):
    assert store.add_version(DEFAULT_ORG_ID, "nope", "x", "alice") is None


def test_promote_flips_default(store: PromptLibraryStore):
    _seed(store, owner="alice", name="p", body="v1")
    v2 = store.add_version(DEFAULT_ORG_ID, "p", "v2 body", "alice")
    assert store.promote(DEFAULT_ORG_ID, "p", v2) is True
    assert store.get(DEFAULT_ORG_ID, "p").default_version_id == v2


def test_promote_unknown_version_returns_false(store: PromptLibraryStore):
    _seed(store, owner="alice", name="p", body="v1")
    assert store.promote(DEFAULT_ORG_ID, "p", "deadbeef") is False


def test_resolve_body_default_vs_explicit(store: PromptLibraryStore):
    _seed(store, owner="alice", name="p", body="v1 body")
    v2 = store.add_version(DEFAULT_ORG_ID, "p", "v2 body", "alice")
    # No version → default (v1).
    assert store.resolve_body(DEFAULT_ORG_ID, "p") == "v1 body"
    # Explicit version.
    assert store.resolve_body(DEFAULT_ORG_ID, "p", v2) == "v2 body"
    # Unknown version → None.
    assert store.resolve_body(DEFAULT_ORG_ID, "p", "nope") is None


def test_list_for_owner_gsi_isolation(store: PromptLibraryStore):
    _seed(store, owner="alice", name="a", body="x")
    _seed(store, owner="alice", name="b", body="x")
    _seed(store, owner="bob", name="c", body="x")
    assert {e.prompt_name for e in store.list_for_owner("alice")} == {"a", "b"}
    assert {e.prompt_name for e in store.list_for_owner("bob")} == {"c"}


# ---------------------------------------------------------------------------
# router: create + get + versions
# ---------------------------------------------------------------------------


def test_create_and_get(store: PromptLibraryStore):
    c = _client("alice")
    resp = c.post(
        "/api/prompts",
        json={
            "display_name": "Support Triage",
            "description": "routes tickets",
            "tags": ["support"],
            "body": "You triage support tickets.",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["prompt_name"] == "support-triage"
    assert body["is_owner"] is True
    assert len(body["versions"]) == 1
    assert body["default_version_id"] == body["versions"][0]["version_id"]

    got = c.get("/api/prompts/support-triage")
    assert got.status_code == 200
    assert got.json()["display_name"] == "Support Triage"


def test_add_version_and_promote_via_router(store: PromptLibraryStore):
    c = _client("alice")
    c.post("/api/prompts", json={"display_name": "P", "body": "v1"})
    add = c.post("/api/prompts/p/versions", json={"body": "v2"})
    assert add.status_code == 200, add.text
    v2 = add.json()["version_id"]
    # Default unchanged on add.
    assert add.json()["default_version_id"] != v2
    # Promote flips default.
    promo = c.post(f"/api/prompts/p/promote/{v2}")
    assert promo.status_code == 200
    assert promo.json()["default_version_id"] == v2
    assert c.get("/api/prompts/p").json()["default_version_id"] == v2


def test_update_metadata(store: PromptLibraryStore):
    c = _client("alice")
    c.post("/api/prompts", json={"display_name": "P", "body": "v1"})
    resp = c.put("/api/prompts/p", json={"description": "new desc", "tags": ["x"]})
    assert resp.status_code == 200
    assert resp.json()["description"] == "new desc"
    assert resp.json()["tags"] == ["x"]


def test_delete(store: PromptLibraryStore):
    c = _client("alice")
    c.post("/api/prompts", json={"display_name": "P", "body": "v1"})
    assert c.delete("/api/prompts/p").status_code == 200
    assert c.get("/api/prompts/p").status_code == 404


# ---------------------------------------------------------------------------
# Bug 122 — slug collision disambiguation
# ---------------------------------------------------------------------------


def test_create_slug_collision_disambiguates(store: PromptLibraryStore):
    _client("alice").post("/api/prompts", json={"display_name": "Dup", "body": "a"})
    bob_resp = _client("bob").post("/api/prompts", json={"display_name": "Dup", "body": "b"})
    assert bob_resp.status_code == 200
    assert bob_resp.json()["prompt_name"] != "dup"  # disambiguated
    # Alice's original is untouched + still owned by Alice with her body.
    alice_entry = store.get(DEFAULT_ORG_ID, "dup")
    assert alice_entry is not None
    assert alice_entry.owner_sub == "alice"
    assert alice_entry.versions[0].body == "a"


def test_owner_recreate_overwrites_in_place(store: PromptLibraryStore):
    alice = _client("alice")
    alice.post("/api/prompts", json={"display_name": "Mine", "body": "v1"})
    resp2 = alice.post("/api/prompts", json={"display_name": "Mine", "body": "v2"})
    assert resp2.json()["prompt_name"] == "mine"
    # Re-create replaces in place (single fresh version with the new body).
    entry = store.get(DEFAULT_ORG_ID, "mine")
    assert len(entry.versions) == 1
    assert entry.versions[0].body == "v2"


# ---------------------------------------------------------------------------
# Tenant isolation (404 cross-tenant on every mutate)
# ---------------------------------------------------------------------------


def test_cross_tenant_private_invisible(store: PromptLibraryStore):
    # In the default-org model there is no private tier, but a foreign caller
    # in a DIFFERENT org cannot see another org's prompt. We simulate by
    # seeding a prompt in a non-default org.
    vid = new_prompt_version_id()
    store.put(
        PromptEntry(
            org_id="other-org",
            prompt_name="secret",
            owner_sub="alice",
            display_name="secret",
            versions=[PromptVersion(version_id=vid, body="x", created_by="alice")],
            default_version_id=vid,
        )
    )
    # Bob (default-org) can't GET it (only owner or same-org).
    assert _client("bob").get("/api/prompts/secret").status_code == 404


def test_cross_tenant_mutations_404(store: PromptLibraryStore):
    _client("alice").post("/api/prompts", json={"display_name": "Alice P", "body": "a"})
    bob = _client("bob")
    # Org-visible: Bob can read it (consumer path), but cannot mutate.
    assert bob.get("/api/prompts/alice-p").status_code == 200
    assert bob.get("/api/prompts/alice-p").json()["is_owner"] is False
    assert bob.put("/api/prompts/alice-p", json={"description": "hax"}).status_code == 404
    assert bob.delete("/api/prompts/alice-p").status_code == 404
    assert bob.post("/api/prompts/alice-p/versions", json={"body": "hax"}).status_code == 404
    assert bob.post("/api/prompts/alice-p/promote/whatever").status_code == 404
    # Alice's prompt is intact + unchanged.
    assert store.get(DEFAULT_ORG_ID, "alice-p").description == ""
    assert len(store.get(DEFAULT_ORG_ID, "alice-p").versions) == 1


# ---------------------------------------------------------------------------
# Bug 126 — single-resource GET uses same authz as list
# ---------------------------------------------------------------------------


def test_acl_drift_get_matches_list(store: PromptLibraryStore):
    # Foreign-org private prompt: exists, but invisible on both list AND get.
    vid = new_prompt_version_id()
    store.put(
        PromptEntry(
            org_id="other-org",
            prompt_name="foreign",
            owner_sub="alice",
            display_name="foreign",
            versions=[PromptVersion(version_id=vid, body="x", created_by="alice")],
            default_version_id=vid,
        )
    )
    bob = _client("bob")
    listing = bob.get("/api/prompts?scope=all").json()
    assert all(e["prompt_name"] != "foreign" for e in listing)
    assert bob.get("/api/prompts/foreign").status_code == 404


# ---------------------------------------------------------------------------
# promote unknown version → 409
# ---------------------------------------------------------------------------


def test_promote_unknown_version_409(store: PromptLibraryStore):
    alice = _client("alice")
    alice.post("/api/prompts", json={"display_name": "P", "body": "v1"})
    resp = alice.post("/api/prompts/p/promote/deadbeefdeadbeef")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# resolve consumer path
# ---------------------------------------------------------------------------


def test_resolve_org_visible_for_non_owner(store: PromptLibraryStore):
    alice = _client("alice")
    alice.post("/api/prompts", json={"display_name": "Shared", "body": "shared body"})
    bob = _client("bob")
    resp = bob.get("/api/prompts/shared/resolve")
    assert resp.status_code == 200
    assert resp.json()["body"] == "shared body"


def test_resolve_explicit_version(store: PromptLibraryStore):
    alice = _client("alice")
    alice.post("/api/prompts", json={"display_name": "P", "body": "v1"})
    v2 = alice.post("/api/prompts/p/versions", json={"body": "v2"}).json()["version_id"]
    resp = alice.get(f"/api/prompts/p/resolve?version={v2}")
    assert resp.status_code == 200
    assert resp.json()["body"] == "v2"


def test_resolve_foreign_org_404(store: PromptLibraryStore):
    vid = new_prompt_version_id()
    store.put(
        PromptEntry(
            org_id="other-org",
            prompt_name="foreign",
            owner_sub="alice",
            display_name="foreign",
            versions=[PromptVersion(version_id=vid, body="x", created_by="alice")],
            default_version_id=vid,
        )
    )
    assert _client("bob").get("/api/prompts/foreign/resolve").status_code == 404


# ---------------------------------------------------------------------------
# Deploy-time resolution hook
# ---------------------------------------------------------------------------


class _Cfg:
    """Minimal stand-in for RuntimeConfig (has a mutable system_prompt)."""

    def __init__(self, system_prompt):
        self.system_prompt = system_prompt


def test_hook_rewrites_uri_ref(store: PromptLibraryStore):
    from app.services.prompt_resolver import resolve_system_prompt

    _seed(store, owner="alice", name="shared", body="resolved body here")
    cfg = _Cfg("prompt://shared")
    resolve_system_prompt(cfg, "alice")
    assert cfg.system_prompt == "resolved body here"


def test_hook_rewrites_uri_ref_with_version(store: PromptLibraryStore):
    from app.services.prompt_resolver import resolve_system_prompt

    alice = _client("alice")
    alice.post("/api/prompts", json={"display_name": "P", "body": "v1"})
    v2 = alice.post("/api/prompts/p/versions", json={"body": "v2 body"}).json()["version_id"]
    cfg = _Cfg(f"prompt://p@{v2}")
    resolve_system_prompt(cfg, "alice")
    assert cfg.system_prompt == "v2 body"


def test_hook_rewrites_dict_ref(store: PromptLibraryStore):
    from app.services.prompt_resolver import resolve_system_prompt

    _seed(store, owner="alice", name="shared", body="dict resolved")
    cfg = _Cfg({"promptId": "shared"})
    resolve_system_prompt(cfg, "alice")
    assert cfg.system_prompt == "dict resolved"


def test_hook_leaves_inline_string_untouched(store: PromptLibraryStore):
    from app.services.prompt_resolver import resolve_system_prompt

    original = "You are a helpful assistant.\nBe concise."
    cfg = _Cfg(original)
    resolve_system_prompt(cfg, "alice")
    assert cfg.system_prompt == original  # byte-identical


def test_hook_missing_ref_keeps_original(store: PromptLibraryStore):
    from app.services.prompt_resolver import resolve_system_prompt

    cfg = _Cfg("prompt://does-not-exist")
    resolve_system_prompt(cfg, "alice")
    # No exception; original ref string preserved (deploy never hard-fails).
    assert cfg.system_prompt == "prompt://does-not-exist"


def test_hook_foreign_ref_keeps_original(store: PromptLibraryStore):
    from app.services.prompt_resolver import resolve_system_prompt

    vid = new_prompt_version_id()
    store.put(
        PromptEntry(
            org_id="other-org",
            prompt_name="foreign",
            owner_sub="alice",
            display_name="foreign",
            versions=[PromptVersion(version_id=vid, body="secret", created_by="alice")],
            default_version_id=vid,
        )
    )
    cfg = _Cfg("prompt://foreign")
    resolve_system_prompt(cfg, "bob")  # bob in default-org can't see other-org
    assert cfg.system_prompt == "prompt://foreign"  # leak prevented


# ---------------------------------------------------------------------------
# Codegen safety — a resolved multi-line body with triple quotes (Bug 125)
# ---------------------------------------------------------------------------


def _install_strands_stubs():
    """Minimal strands + bedrock_agentcore stubs so a generated agent module
    can be exec'd to verify symbol resolution (mirrors test_hitl_codegen)."""
    strands = types.ModuleType("strands")

    class Agent:
        def __init__(self, *a, **k):
            self.kwargs = k

        def __call__(self, *a, **k):
            return "stub-response"

    def tool(f=None, **k):
        return f if f else (lambda g: g)

    strands.Agent = Agent
    strands.tool = tool

    smodels = types.ModuleType("strands.models")

    class BedrockModel:
        def __init__(self, *a, **k):
            pass

    smodels.BedrockModel = BedrockModel
    strands.models = smodels

    sbedrock = types.ModuleType("strands.models.bedrock")
    sbedrock.BedrockModel = BedrockModel
    smodels.bedrock = sbedrock

    bac = types.ModuleType("bedrock_agentcore")
    bacr = types.ModuleType("bedrock_agentcore.runtime")

    class App:
        def entrypoint(self, f):
            return f

        def run(self):
            pass

    bacr.BedrockAgentCoreApp = App
    bac.runtime = bacr

    mods = {
        "strands": strands,
        "strands.models": smodels,
        "strands.models.bedrock": sbedrock,
        "bedrock_agentcore": bac,
        "bedrock_agentcore.runtime": bacr,
    }
    for n, m in mods.items():
        sys.modules[n] = m


def test_resolved_body_with_triple_quotes_codegen_safe():
    """A resolved multi-line body containing triple-quotes must be escaped by
    _escape_triple_quotes and produce an import-safe agent module."""
    from app.models.deployment_models import RuntimeConfig
    from app.services.code_generator import generate_agent_code

    nasty_body = 'Line1\n"""injection attempt"""\nLine3 with \\ backslash'
    cfg = RuntimeConfig(
        name="prompt_lib_t",
        model={"modelId": "us.anthropic.claude-sonnet-5"},
        systemPrompt=nasty_body,
        modelProvider="bedrock",
    )
    code = generate_agent_code(config=cfg, tools=[], observability_enabled=False)
    _install_strands_stubs()
    g: dict = {"__name__": "agent_under_test"}
    # EXEC, not just AST-parse — this is what catches an unescaped triple-quote
    # body breaking out of the string literal (Bug 125 class).
    exec(compile(code, "<agent.py>", "exec"), g)
    assert "injection attempt" in code  # the body made it into the source
