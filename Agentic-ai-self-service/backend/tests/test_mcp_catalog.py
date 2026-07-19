"""Tests for the external MCP-server catalog + Gateway target-param assembly.

Two layers:
  1. Pure catalog-data invariants (services/mcp_catalog).
  2. The external-MCP Gateway target-param builder (services/gateway_deployer
     .build_external_mcp_target_params) — verifies each integration tier maps to
     the correct credentialProviderConfiguration, and adapter tiers are rejected.

No AWS: the API-key provider creation is stubbed with a fake agentcore_ctrl.
Grounded in the live bedrock-agentcore-control model (boto3 1.43.8): an
mcpServer target needs only `endpoint`; credential providers are optional.
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "src")

from app.services.gateway_deployer import build_external_mcp_target_params  # noqa: E402
from app.services.mcp_catalog import (  # noqa: E402
    MCP_SERVERS,
    get_mcp_server,
    list_by_tier,
    live_testable_servers,
)

VALID_TIERS = {"direct-none", "direct-apikey", "direct-oauth", "adapter-3lo", "adapter-stdio"}
VALID_AUTH = {"none", "api_key", "oauth2_client_credentials", "oauth2_3lo", "iam_sigv4"}


class _FakeCtrl:
    """Minimal fake agentcore_ctrl for API-key provider creation."""

    def __init__(self):
        self.created = []

    def create_api_key_credential_provider(self, **kw):
        self.created.append(kw)
        return {"credentialProviderArn": "arn:aws:...:provider/fake-apikey"}

    def get_api_key_credential_provider(self, **kw):
        return {"credentialProviderArn": "arn:aws:...:provider/fake-apikey"}


# ---------------------------------------------------------------------------
# Catalog data invariants
# ---------------------------------------------------------------------------


def test_catalog_nonempty_and_well_formed():
    assert len(MCP_SERVERS) >= 20
    for sid, e in MCP_SERVERS.items():
        assert e["id"] == sid
        assert e["tier"] in VALID_TIERS, f"{sid} bad tier {e['tier']}"
        assert e["auth_type"] in VALID_AUTH, f"{sid} bad auth {e['auth_type']}"
        assert e["verified"] in {"live", "docs", "community"}
        # direct-* tiers must carry a concrete https endpoint (no {placeholders}
        # allowed ONLY where the catalog explicitly templates a host).
        if e["tier"].startswith("direct") and e["endpoint"] is not None:
            assert e["endpoint"].startswith("https://")


def test_apikey_entries_carry_a_descriptor():
    for e in list_by_tier("direct-apikey"):
        assert "api_key_descriptor" in e, f"{e['id']} missing api_key_descriptor"
        d = e["api_key_descriptor"]
        assert d["location"] in {"HEADER", "QUERY_PARAMETER"}
        assert d["parameter_name"]


def test_live_testable_are_no_auth_or_optional_key():
    # Everything flagged live_testable must be reachable WITHOUT vendor creds:
    # either no auth, or an api_key tier whose free tier works keyless.
    lt = {e["id"] for e in live_testable_servers()}
    assert {"aws-knowledge", "deepwiki", "cloudflare-docs"} <= lt
    for e in live_testable_servers():
        assert e["auth_type"] in {"none", "api_key"}


def test_get_and_copy_semantics():
    a = get_mcp_server("aws-knowledge")
    a["display_name"] = "MUTATED"
    b = get_mcp_server("aws-knowledge")
    assert b["display_name"] != "MUTATED"  # deep-copied on read
    assert get_mcp_server("does-not-exist") is None


# ---------------------------------------------------------------------------
# Target-param assembly per tier
# ---------------------------------------------------------------------------


def test_tier1_no_auth_omits_credential_provider():
    entry = get_mcp_server("aws-knowledge")
    params = build_external_mcp_target_params(
        _FakeCtrl(),
        gateway_id="gw",
        target_name="mcp-aws-knowledge",
        catalog_entry=entry,
        endpoint=entry["endpoint"],
    )
    assert params["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == entry["endpoint"]
    # No credential provider for a Tier-1 no-auth target.
    assert "credentialProviderConfigurations" not in params


def test_tier2_api_key_header_builds_apikey_provider():
    ctrl = _FakeCtrl()
    entry = get_mcp_server("exa")  # x-api-key header
    params = build_external_mcp_target_params(
        ctrl,
        gateway_id="gw",
        target_name="mcp-exa",
        catalog_entry=entry,
        endpoint=entry["endpoint"],
        secret_arn="arn:secret:exa",
    )
    cfg = params["credentialProviderConfigurations"][0]
    assert cfg["credentialProviderType"] == "API_KEY"
    ak = cfg["credentialProvider"]["apiKeyCredentialProvider"]
    assert ak["credentialLocation"] == "HEADER"
    assert ak["credentialParameterName"] == "x-api-key"
    assert "credentialPrefix" not in ak  # no prefix for x-api-key
    assert ctrl.created  # provider was created


def test_tier2_query_param_and_prefix_variants():
    # Tavily uses a query parameter.
    tav = build_external_mcp_target_params(
        _FakeCtrl(),
        gateway_id="gw",
        target_name="mcp-tavily",
        catalog_entry=get_mcp_server("tavily"),
        endpoint=get_mcp_server("tavily")["endpoint"],
        secret_arn="arn:secret:tav",
    )
    ak = tav["credentialProviderConfigurations"][0]["credentialProvider"]["apiKeyCredentialProvider"]
    assert ak["credentialLocation"] == "QUERY_PARAMETER"
    assert ak["credentialParameterName"] == "tavilyApiKey"

    # Firecrawl uses Authorization: Bearer <key> (prefix present).
    fc = build_external_mcp_target_params(
        _FakeCtrl(),
        gateway_id="gw",
        target_name="mcp-firecrawl",
        catalog_entry=get_mcp_server("firecrawl"),
        endpoint=get_mcp_server("firecrawl")["endpoint"],
        secret_arn="arn:secret:fc",
    )
    ak = fc["credentialProviderConfigurations"][0]["credentialProvider"]["apiKeyCredentialProvider"]
    assert ak["credentialParameterName"] == "Authorization"
    assert ak["credentialPrefix"] == "Bearer "


def test_tier2_requires_secret_arn():
    with pytest.raises(RuntimeError, match="API key"):
        build_external_mcp_target_params(
            _FakeCtrl(),
            gateway_id="gw",
            target_name="mcp-exa",
            catalog_entry=get_mcp_server("exa"),
            endpoint=get_mcp_server("exa")["endpoint"],
        )


def test_tier3_oauth_client_credentials():
    entry = get_mcp_server("databricks")
    params = build_external_mcp_target_params(
        _FakeCtrl(),
        gateway_id="gw",
        target_name="mcp-databricks",
        catalog_entry=entry,
        endpoint="https://myws.cloud.databricks.com/api/2.0/mcp/sql",
        oauth_provider_arn="arn:aws:...:provider/dbx",
        oauth_scopes=["sql"],
    )
    cfg = params["credentialProviderConfigurations"][0]
    assert cfg["credentialProviderType"] == "OAUTH"
    assert cfg["credentialProvider"]["oauthCredentialProvider"]["providerArn"].endswith("dbx")
    assert cfg["credentialProvider"]["oauthCredentialProvider"]["scopes"] == ["sql"]


def test_tier3_iam_sigv4_uses_gateway_role():
    entry = get_mcp_server("aws-mcp")
    params = build_external_mcp_target_params(
        _FakeCtrl(),
        gateway_id="gw",
        target_name="mcp-aws",
        catalog_entry=entry,
        endpoint=entry["endpoint"],
    )
    assert params["credentialProviderConfigurations"][0]["credentialProviderType"] == "GATEWAY_IAM_ROLE"


def test_adapter_tiers_are_rejected():
    for sid in ("notion", "atlassian", "aws-labs-stdio", "brave-search"):
        with pytest.raises(ValueError, match="adapter"):
            entry = get_mcp_server(sid)
            build_external_mcp_target_params(
                _FakeCtrl(),
                gateway_id="gw",
                target_name=f"mcp-{sid}",
                catalog_entry=entry,
                endpoint="https://example.com/mcp",
            )


def test_non_https_endpoint_rejected():
    with pytest.raises(ValueError, match="https"):
        build_external_mcp_target_params(
            _FakeCtrl(),
            gateway_id="gw",
            target_name="mcp-x",
            catalog_entry=get_mcp_server("aws-knowledge"),
            endpoint="http://insecure/mcp",
        )


# ---------------------------------------------------------------------------
# Orchestration helper: _deploy_external_mcp_targets (the deploy-path wiring)
# ---------------------------------------------------------------------------

from app.services import gateway_deployer as gd  # noqa: E402
from app.services.gateway_deployer import _fill_endpoint_placeholders  # noqa: E402


def test_fill_endpoint_placeholders_ok_and_missing_and_injection():
    assert (
        _fill_endpoint_placeholders("https://{store_domain}/api/mcp", {"store_domain": "shop.myshopify.com"})
        == "https://shop.myshopify.com/api/mcp"
    )
    with pytest.raises(RuntimeError, match="store_domain"):
        _fill_endpoint_placeholders("https://{store_domain}/api/mcp", {})
    with pytest.raises(RuntimeError, match="Invalid value"):
        _fill_endpoint_placeholders("https://{h}/mcp", {"h": "evil.com/../x"})  # path escape
    with pytest.raises(RuntimeError, match="Invalid value"):
        _fill_endpoint_placeholders("https://{h}/mcp", {"h": "http://evil"})  # scheme injection


def _patch_target_capture(monkeypatch):
    """Capture CreateGatewayTarget params instead of calling AWS."""
    captured = []
    monkeypatch.setattr(
        gd,
        "_create_gateway_target_with_retry",
        lambda ctrl, gw, name, params: captured.append({"name": name, "params": params}) or {"targetId": "t-1"},
    )
    monkeypatch.setattr(
        gd, "_put_connector_secret", lambda region, owner, payload: "arn:aws:secretsmanager:...:secret:fake"
    )
    return captured


def test_deploy_external_mcp_tier1_no_auth(monkeypatch):
    captured = _patch_target_capture(monkeypatch)
    out = gd._deploy_external_mcp_targets(
        _FakeCtrl(),
        "gw-1",
        "us-east-1",
        [{"server_id": "aws-knowledge"}],
        owner_sub="alice",
    )
    assert len(captured) == 1
    tc = captured[0]["params"]["targetConfiguration"]["mcp"]["mcpServer"]
    assert tc["endpoint"] == "https://knowledge-mcp.global.api.aws"
    assert "credentialProviderConfigurations" not in captured[0]["params"]  # Tier 1
    assert out["secret_arns"] == []


def test_deploy_external_mcp_tier2_mints_secret_and_provider(monkeypatch):
    captured = _patch_target_capture(monkeypatch)
    out = gd._deploy_external_mcp_targets(
        _FakeCtrl(),
        "gw-1",
        "us-east-1",
        [{"server_id": "exa", "secret_value": "sk-test-123"}],
        owner_sub="alice",
    )
    assert len(captured) == 1
    # secret minted from raw key; API_KEY credential provider attached
    assert out["secret_arns"] == ["arn:aws:secretsmanager:...:secret:fake"]
    cfgs = captured[0]["params"]["credentialProviderConfigurations"]
    assert cfgs[0]["credentialProviderType"] == "API_KEY"


def test_deploy_external_mcp_tier2_missing_key_raises(monkeypatch):
    _patch_target_capture(monkeypatch)
    with pytest.raises(RuntimeError, match="API key"):
        gd._deploy_external_mcp_targets(
            _FakeCtrl(),
            "gw-1",
            "us-east-1",
            [{"server_id": "exa"}],
            owner_sub="alice",
        )


def test_deploy_external_mcp_placeholder_endpoint(monkeypatch):
    captured = _patch_target_capture(monkeypatch)
    gd._deploy_external_mcp_targets(
        _FakeCtrl(),
        "gw-1",
        "us-east-1",
        [{"server_id": "shopify-storefront", "endpoint_vars": {"store_domain": "shop.myshopify.com"}}],
        owner_sub="alice",
    )
    assert (
        captured[0]["params"]["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"]
        == "https://shop.myshopify.com/api/mcp"
    )


def test_deploy_external_mcp_unknown_id_raises(monkeypatch):
    _patch_target_capture(monkeypatch)
    with pytest.raises(RuntimeError, match="Unknown MCP server id"):
        gd._deploy_external_mcp_targets(
            _FakeCtrl(),
            "gw-1",
            "us-east-1",
            [{"server_id": "does-not-exist"}],
            owner_sub="alice",
        )


def test_deploy_external_mcp_custom_endpoint_no_auth(monkeypatch):
    """Custom (non-catalog) endpoint: raw https URL + auth_type=none wires a
    Tier-1 mcpServer target with no credential provider."""
    captured = _patch_target_capture(monkeypatch)
    out = gd._deploy_external_mcp_targets(
        _FakeCtrl(),
        "gw-1",
        "us-east-1",
        [{"endpoint": "https://example.com/mcp", "auth_type": "none", "name": "My MCP"}],
        owner_sub="alice",
    )
    assert len(captured) == 1
    tc = captured[0]["params"]["targetConfiguration"]["mcp"]["mcpServer"]
    assert tc["endpoint"] == "https://example.com/mcp"
    assert "credentialProviderConfigurations" not in captured[0]["params"]
    assert out["secret_arns"] == []


def test_deploy_external_mcp_custom_endpoint_api_key(monkeypatch):
    """Custom endpoint with auth_type=api_key mints a secret + API_KEY provider."""
    captured = _patch_target_capture(monkeypatch)
    out = gd._deploy_external_mcp_targets(
        _FakeCtrl(),
        "gw-1",
        "us-east-1",
        [{"endpoint": "https://example.com/mcp", "auth_type": "api_key", "secret_value": "sk-x"}],
        owner_sub="alice",
    )
    assert out["secret_arns"] == ["arn:aws:secretsmanager:...:secret:fake"]
    assert captured[0]["params"]["credentialProviderConfigurations"][0]["credentialProviderType"] == "API_KEY"


def test_deploy_external_mcp_custom_endpoint_rejects_non_https(monkeypatch):
    """A custom endpoint must be https (SSRF/scheme guard)."""
    _patch_target_capture(monkeypatch)
    with pytest.raises(Exception, match="https|scheme|URL"):
        gd._deploy_external_mcp_targets(
            _FakeCtrl(),
            "gw-1",
            "us-east-1",
            [{"endpoint": "http://insecure.example.com/mcp", "auth_type": "none"}],
            owner_sub="alice",
        )


def test_deploy_external_mcp_custom_endpoint_rejects_private_host(monkeypatch):
    """A custom endpoint resolving to a private/metadata host is blocked."""
    _patch_target_capture(monkeypatch)
    with pytest.raises(Exception, match="(?i)block|private|disallow|link-local|metadata|network"):
        gd._deploy_external_mcp_targets(
            _FakeCtrl(),
            "gw-1",
            "us-east-1",
            [{"endpoint": "https://169.254.169.254/latest/meta-data", "auth_type": "none"}],
            owner_sub="alice",
        )
