"""Phase A — SaaS connector catalog + gateway-target deploy tests.

Two layers:
  1. Pure-unit tests over ``app.services.connectors`` (the Phase A catalog +
     lookup helpers — distinct from the Phase 3 ``connectors_catalog`` module):
     catalog lookups, the generic sentinel, OAuth vendor mapping, and the
     asana = API-key-only ``supports_auth`` rule.
  2. ``gateway_deployer._deploy_connector_targets`` builds the EXACT boto3
     ``targetConfiguration`` + ``credentialProviderConfigurations`` shapes
     (API_KEY and OAUTH) the live service expects. boto3
     is fully mocked (MagicMock control client + patched secret/spec helpers),
     following the test_gateway_deployer_ssrf.py patch-the-helper style.

No AWS, no moto — the catalog has no AWS dependency, and the deploy path is
exercised against a fake control-plane client.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "src")

from app.services.connectors import (  # noqa: E402
    AUTH_API_KEY,
    AUTH_OAUTH2_CC,
    CONNECTOR_CATALOG,
    GENERIC_CONNECTOR_ID,
    get_connector,
    is_generic,
    known_connector_ids,
    oauth_vendor_for,
    supports_auth,
    vendor_config_key,
)

# ---------------------------------------------------------------------------
# Catalog data contract
# ---------------------------------------------------------------------------


def test_catalog_has_expected_ids():
    assert set(CONNECTOR_CATALOG.keys()) == {
        "jira",
        "asana",
        "slack",
        "github",
        "salesforce",
    }


def test_each_entry_id_matches_its_key():
    for cid, entry in CONNECTOR_CATALOG.items():
        assert entry["id"] == cid
        # Every curated entry advertises at least one supported auth method.
        assert entry["auth_methods"]
        assert set(entry["auth_methods"]) <= {AUTH_API_KEY, AUTH_OAUTH2_CC}


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def test_get_connector_known_and_unknown():
    assert get_connector("github")["id"] == "github"
    assert get_connector("does-not-exist") is None
    # The generic sentinel intentionally has no catalog entry.
    assert get_connector(GENERIC_CONNECTOR_ID) is None


def test_known_connector_ids_includes_generic_sentinel():
    ids = known_connector_ids()
    assert GENERIC_CONNECTOR_ID in ids
    assert {"jira", "asana", "slack", "github", "salesforce"} <= set(ids)
    # Sentinel is in addition to the five curated ids.
    assert len(ids) == len(CONNECTOR_CATALOG) + 1


# ---------------------------------------------------------------------------
# Generic sentinel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", GENERIC_CONNECTOR_ID])
def test_is_generic_true_for_sentinel_and_empty(value):
    assert is_generic(value) is True


@pytest.mark.parametrize("value", ["jira", "asana", "slack", "github", "salesforce"])
def test_is_generic_false_for_curated(value):
    assert is_generic(value) is False


def test_generic_supports_both_auth_methods():
    assert supports_auth(GENERIC_CONNECTOR_ID, AUTH_API_KEY) is True
    assert supports_auth(GENERIC_CONNECTOR_ID, AUTH_OAUTH2_CC) is True


def test_generic_has_no_oauth_vendor():
    # Generic carries no catalog default vendor; the deployer falls back to
    # CustomOauth2 (with a user-supplied discovery_url).
    assert oauth_vendor_for(GENERIC_CONNECTOR_ID) is None


# ---------------------------------------------------------------------------
# supports_auth — asana is API-key only
# ---------------------------------------------------------------------------


def test_asana_supports_api_key_only():
    assert supports_auth("asana", AUTH_API_KEY) is True
    # Asana has no first-class OAuth vendor enum -> oauth2_cc unsupported in v1.
    assert supports_auth("asana", AUTH_OAUTH2_CC) is False


def test_supports_auth_unknown_connector_is_false():
    assert supports_auth("not-a-connector", AUTH_API_KEY) is False


@pytest.mark.parametrize(
    "connector_id,method",
    [
        ("jira", AUTH_OAUTH2_CC),
        # Jira now ALSO supports api_key: the deploy path pre-computes
        # base64(email:token) and the provider sends it with the static "Basic "
        # prefix — valid Jira auth. (Both api_key and oauth2 are offered.)
        ("jira", AUTH_API_KEY),
        ("github", AUTH_OAUTH2_CC),
        ("github", AUTH_API_KEY),
        ("slack", AUTH_OAUTH2_CC),
        ("salesforce", AUTH_OAUTH2_CC),
    ],
)
def test_curated_supports_advertised_methods(connector_id, method):
    assert supports_auth(connector_id, method) is True


def test_jira_api_key_uses_basic_prefix():
    """Jira api-key IS supported via pre-computed base64(email:token) sent with a
    static 'Basic ' prefix (not Bearer). The catalog advertises api_key and the
    credential prefix is Basic."""
    from app.services.connectors import get_connector

    assert supports_auth("jira", AUTH_API_KEY) is True
    assert get_connector("jira")["credential_prefix"] == "Basic"


# ---------------------------------------------------------------------------
# OAuth vendor mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "connector_id,vendor",
    [
        ("jira", "AtlassianOauth2"),
        ("slack", "SlackOauth2"),
        ("github", "GithubOauth2"),
        ("salesforce", "SalesforceOauth2"),
    ],
)
def test_oauth_vendor_for_branded(connector_id, vendor):
    assert oauth_vendor_for(connector_id) == vendor


def test_oauth_vendor_for_asana_is_none():
    # No first-class vendor -> None (API-key only).
    assert oauth_vendor_for("asana") is None


@pytest.mark.parametrize(
    "vendor,key",
    [
        ("AtlassianOauth2", "atlassianOauth2ProviderConfig"),
        ("GithubOauth2", "githubOauth2ProviderConfig"),
        ("SlackOauth2", "slackOauth2ProviderConfig"),
        ("SalesforceOauth2", "salesforceOauth2ProviderConfig"),
        ("CustomOauth2", "customOauth2ProviderConfig"),
    ],
)
def test_vendor_config_key_mapping(vendor, key):
    assert vendor_config_key(vendor) == key


def test_vendor_config_key_requires_vendor():
    with pytest.raises(ValueError):
        vendor_config_key("")


# ---------------------------------------------------------------------------
# deploy path — _deploy_connector_targets builds the boto3 shapes
# ---------------------------------------------------------------------------


def _fake_ctrl() -> MagicMock:
    """A MagicMock bedrock-agentcore-control client that returns READY targets
    and stable credential-provider ARNs (mirrors the live response shapes)."""
    ctrl = MagicMock()
    ctrl.create_api_key_credential_provider.return_value = {
        "credentialProviderArn": "arn:aws:bedrock-agentcore:us-west-2:1:apikey/p",
    }
    ctrl.create_oauth2_credential_provider.return_value = {
        "credentialProviderArn": "arn:aws:bedrock-agentcore:us-west-2:1:oauth/p",
    }
    ctrl.create_gateway_target.return_value = {"targetId": "tgt-1"}
    ctrl.get_gateway_target.return_value = {"status": "READY"}
    return ctrl


def test_deploy_connector_target_api_key_builds_correct_shapes():
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    connectors = [
        {
            "connector_id": "github",
            "auth_method": "api_key",
            "secret_arn": "arn:aws:secretsmanager:us-west-2:1:secret:agentcore-connector/o/x",
            "spec_inline": '{"openapi": "3.0.0"}',
            "credential_location": "HEADER",
            "credential_parameter_name": "Authorization",
            "credential_prefix": "Bearer",
        }
    ]

    result = gd._deploy_connector_targets(ctrl, "gw-1", "us-west-2", connectors, owner_sub="o")

    # API-key provider created against our EXTERNAL secret (jsonKey=apiKey).
    ctrl.create_api_key_credential_provider.assert_called_once()
    akp = ctrl.create_api_key_credential_provider.call_args.kwargs
    assert akp["apiKeySecretSource"] == "EXTERNAL"
    assert akp["apiKeySecretConfig"]["jsonKey"] == "apiKey"
    assert akp["apiKeySecretConfig"]["secretId"] == connectors[0]["secret_arn"]
    # No oauth provider for an API-key connector.
    ctrl.create_oauth2_credential_provider.assert_not_called()

    # Gateway target: OpenAPI inline payload + API_KEY credential provider config.
    ctrl.create_gateway_target.assert_called_once()
    params = ctrl.create_gateway_target.call_args.kwargs
    assert params["gatewayIdentifier"] == "gw-1"
    assert params["targetConfiguration"]["mcp"]["openApiSchema"]["inlinePayload"] == ('{"openapi": "3.0.0"}')
    cpc = params["credentialProviderConfigurations"]
    assert len(cpc) == 1
    assert cpc[0]["credentialProviderType"] == "API_KEY"
    akcp = cpc[0]["credentialProvider"]["apiKeyCredentialProvider"]
    assert akcp["providerArn"].endswith("apikey/p")
    assert akcp["credentialParameterName"] == "Authorization"
    assert akcp["credentialLocation"] == "HEADER"
    assert akcp["credentialPrefix"] == "Bearer"

    # Cleanup handles returned. Even when the secret_arn is supplied (SFN path),
    # the consumed secret must be tracked so teardown deletes it (no orphan).
    assert result["credential_provider_names"]
    assert result["secret_arns"] == [connectors[0]["secret_arn"]]


def test_deploy_connector_target_oauth2_cc_builds_correct_shapes():
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    connectors = [
        {
            "connector_id": "github",
            "auth_method": "oauth2_cc",
            "secret_arn": "arn:aws:secretsmanager:us-west-2:1:secret:agentcore-connector/o/y",
            "spec_inline": '{"openapi": "3.0.0"}',
            "client_id": "abc123",
            "scopes": ["repo", "read:org"],
        }
    ]

    result = gd._deploy_connector_targets(ctrl, "gw-1", "us-west-2", connectors, owner_sub="o")

    # OAuth2 provider created with the branded vendor + its config key, EXTERNAL
    # client secret referencing our secret (jsonKey=clientSecret).
    ctrl.create_oauth2_credential_provider.assert_called_once()
    op = ctrl.create_oauth2_credential_provider.call_args.kwargs
    assert op["credentialProviderVendor"] == "GithubOauth2"
    cfg = op["oauth2ProviderConfigInput"]["githubOauth2ProviderConfig"]
    assert cfg["clientId"] == "abc123"
    assert cfg["clientSecretSource"] == "EXTERNAL"
    assert cfg["clientSecretConfig"]["jsonKey"] == "clientSecret"
    assert cfg["clientSecretConfig"]["secretId"] == connectors[0]["secret_arn"]
    # No api-key provider for an oauth connector.
    ctrl.create_api_key_credential_provider.assert_not_called()

    # Gateway target: OpenAPI inline payload + OAUTH credential provider config.
    params = ctrl.create_gateway_target.call_args.kwargs
    cpc = params["credentialProviderConfigurations"]
    assert len(cpc) == 1
    assert cpc[0]["credentialProviderType"] == "OAUTH"
    ocp = cpc[0]["credentialProvider"]["oauthCredentialProvider"]
    assert ocp["providerArn"].endswith("oauth/p")
    assert ocp["grantType"] == "CLIENT_CREDENTIALS"
    assert ocp["scopes"] == ["repo", "read:org"]

    assert result["credential_provider_names"]


def test_deploy_connector_mints_secret_from_raw_value_on_direct_path():
    """Direct-deploy path: a raw secret_value is minted here (never echoed) and
    the resulting ARN is recorded for teardown."""
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    minted_arn = "arn:aws:secretsmanager:us-west-2:1:secret:agentcore-connector/o/minted"
    connectors = [
        {
            "connector_id": "asana",
            "auth_method": "api_key",
            "secret_value": "super-secret-PAT",
            "spec_inline": '{"openapi": "3.0.0"}',
        }
    ]

    with patch.object(gd, "_put_connector_secret", return_value=minted_arn) as put:
        result = gd._deploy_connector_targets(ctrl, "gw-1", "us-west-2", connectors, owner_sub="o")

    # Secret minted with the api_key jsonKey payload; raw value never returned.
    put.assert_called_once()
    payload = put.call_args.args[2]
    assert payload == {"apiKey": "super-secret-PAT"}
    assert result["secret_arns"] == [minted_arn]
    # The provider references the freshly minted ARN.
    akp = ctrl.create_api_key_credential_provider.call_args.kwargs
    assert akp["apiKeySecretConfig"]["secretId"] == minted_arn


def test_deploy_generic_oauth_falls_back_to_custom_vendor_with_discovery():
    """A generic connector with oauth2_cc and no branded vendor uses CustomOauth2
    and requires/forwards the discovery_url."""
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    connectors = [
        {
            "connector_id": GENERIC_CONNECTOR_ID,
            "auth_method": "oauth2_cc",
            "secret_arn": "arn:aws:secretsmanager:us-west-2:1:secret:agentcore-connector/o/z",
            "spec_url": "https://api.example.com/openapi.json",
            "client_id": "cid",
            "discovery_url": "https://issuer/.well-known/openid-configuration",
        }
    ]

    with patch.object(gd, "_fetch_openapi_spec", return_value='{"openapi": "3.0.0"}') as fetch:
        gd._deploy_connector_targets(ctrl, "gw-1", "us-west-2", connectors, owner_sub="o")

    fetch.assert_called_once()
    op = ctrl.create_oauth2_credential_provider.call_args.kwargs
    assert op["credentialProviderVendor"] == "CustomOauth2"
    cfg = op["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
    assert cfg["oauthDiscovery"]["discoveryUrl"] == ("https://issuer/.well-known/openid-configuration")


# ---------------------------------------------------------------------------
# Production-readiness: partial-failure rollback + credential_location validation
# ---------------------------------------------------------------------------


def test_deploy_connector_rollback_on_midloop_failure():
    """If connector N fails, the providers + secrets created for connectors 0..N-1
    must be rolled back (best-effort delete) so nothing orphans on a failed deploy
    whose gateway_result is never persisted."""
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    # First connector deploys fine; second raises during target creation.
    ctrl.create_gateway_target.side_effect = [
        {"targetId": "tgt-1"},
        RuntimeError("boom on connector 2"),
    ]
    connectors = [
        {
            "connector_id": "github",
            "auth_method": "api_key",
            "secret_arn": "arn:aws:secretsmanager:us-west-2:1:secret:agentcore-connector/o/a",
            "spec_inline": '{"openapi": "3.0.0"}',
        },
        {
            "connector_id": "asana",
            "auth_method": "api_key",
            "secret_arn": "arn:aws:secretsmanager:us-west-2:1:secret:agentcore-connector/o/b",
            "spec_inline": '{"openapi": "3.0.0"}',
        },
    ]

    sm = MagicMock()
    with patch.object(gd, "_create_secrets_client", return_value=sm):
        with pytest.raises(RuntimeError, match="boom on connector 2"):
            gd._deploy_connector_targets(ctrl, "gw-1", "us-west-2", connectors, owner_sub="o")

    # The first connector's provider was rolled back (delete attempted) and its
    # consumed secret force-deleted — no orphan.
    assert ctrl.delete_oauth2_credential_provider.called or ctrl.delete_api_key_credential_provider.called
    assert sm.delete_secret.called
    deleted_secret_ids = {c.kwargs.get("SecretId") for c in sm.delete_secret.call_args_list}
    assert connectors[0]["secret_arn"] in deleted_secret_ids


@pytest.mark.parametrize("bad", ["body", "cookie", "HEADERS", "queryparam"])
def test_connector_config_rejects_bad_credential_location(bad):
    """A bad credential_location is a clean 422 at the API boundary, not a
    mid-deploy boto3 ValidationException."""
    from app.models.components import ConnectorConfig
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ConnectorConfig(connector_id="github", auth_method="api_key", credential_location=bad)


@pytest.mark.parametrize(
    "good,expected", [("header", "HEADER"), ("Query_Parameter", "QUERY_PARAMETER"), ("HEADER", "HEADER")]
)
def test_connector_config_normalizes_credential_location(good, expected):
    from app.models.components import ConnectorConfig

    cfg = ConnectorConfig(connector_id="github", auth_method="api_key", credential_location=good)
    assert cfg.credential_location == expected


def test_typed_provider_deleter_picks_correct_api():
    """Live-caught bug: delete_oauth2_credential_provider on an API_KEY provider
    returns success WITHOUT deleting it. The 'TYPE:name' record must route to the
    correct deleter — API_KEY -> delete_api_key_credential_provider only."""
    from app.services import gateway_deployer as gd

    ctrl = MagicMock()
    ok, msg = gd._delete_connector_credential_provider(ctrl, "API_KEY:acc-github-0")
    assert ok
    ctrl.delete_api_key_credential_provider.assert_called_once_with(name="acc-github-0")
    ctrl.delete_oauth2_credential_provider.assert_not_called()

    ctrl2 = MagicMock()
    ok2, _ = gd._delete_connector_credential_provider(ctrl2, "OAUTH:acc-jira-1")
    assert ok2
    ctrl2.delete_oauth2_credential_provider.assert_called_once_with(name="acc-jira-1")
    ctrl2.delete_api_key_credential_provider.assert_not_called()


def test_connector_providers_recorded_with_type_prefix():
    """deploy result records providers as 'TYPE:name' so teardown is unambiguous."""
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    connectors = [
        {
            "connector_id": "github",
            "auth_method": "api_key",
            "secret_arn": "arn:aws:secretsmanager:us-west-2:1:secret:agentcore-connector/o/x",
            "spec_inline": '{"openapi": "3.0.0"}',
        }
    ]
    result = gd._deploy_connector_targets(ctrl, "gw-1", "us-west-2", connectors, owner_sub="o")
    assert result["credential_provider_names"] == ["API_KEY:acc-github-0"]


# ---------------------------------------------------------------------------
# Bug A — catalog spec_url is fetched against the SPEC-HOST allowlist
# (raw.githubusercontent.com), NOT the API-host allowlist (app.asana.com).
#
# The earlier tests all pass spec_inline, which bypasses the fetch+allowlist
# path entirely — that shortcut is exactly why Bug A was not caught: a real
# catalog connector with NO inline spec fetches its spec_url from a vendor doc
# host (GitHub raw), but the SSRF check was using the API allowlist and rejected
# every branded connector with "spec host 'raw.githubusercontent.com' is not in
# the connector allowlist ['app.asana.com']". These tests exercise the REAL
# fetch path (only the network urlopen is stubbed) so the regression can't slip
# back in.
# ---------------------------------------------------------------------------


def test_catalog_connector_spec_fetched_against_spec_host_not_api_host():
    """An asana connector with no inline spec must fetch its catalog spec_url
    using the spec-host allowlist (raw.githubusercontent.com) — NOT the API
    allowlist (app.asana.com). Goes through the real _fetch_openapi_spec; only
    the network call (_validate_outbound_url) is observed."""
    from app.services import gateway_deployer as gd

    ctrl = _fake_ctrl()
    connectors = [
        {
            "connector_id": "asana",
            "auth_method": "api_key",
            "secret_arn": "arn:aws:secretsmanager:us-west-2:1:secret:agentcore-connector/o/a",
            # NO spec_inline — forces the catalog spec_url fetch path.
        }
    ]

    captured = {}

    def _fake_validate(url, allowlist_hosts=None):
        captured["url"] = url
        captured["allowlist"] = list(allowlist_hosts) if allowlist_hosts else None
        return url

    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            return b'{"openapi": "3.0.0"}'

    with (
        patch.object(gd, "_validate_outbound_url", side_effect=_fake_validate),
        patch.object(gd.urllib.request, "urlopen", return_value=_Resp()),
    ):
        gd._deploy_connector_targets(ctrl, "gw-1", "us-west-2", connectors, owner_sub="o")

    # The spec was fetched from the GitHub raw host...
    assert captured["url"] == CONNECTOR_CATALOG["asana"]["spec_url"]
    # Compare the parsed HOST exactly (not a substring check, which CodeQL flags
    # as py/incomplete-url-substring-sanitization).
    from urllib.parse import urlparse as _urlparse

    assert _urlparse(captured["url"]).hostname == "raw.githubusercontent.com"
    # ...and validated against the SPEC-host allowlist, NOT the API allowlist.
    assert captured["allowlist"] == ["raw.githubusercontent.com"]
    assert "app.asana.com" not in (captured["allowlist"] or [])


def test_catalog_connectors_with_default_spec_url_have_spec_host_allowlist():
    """Every catalog connector that ships a default spec_url must also declare a
    spec_host_allowlist (the doc host), so the spec fetch never falls back to the
    API allowlist. Connectors with spec_url=None (jira/salesforce — user supplies)
    are exempt."""
    for cid, entry in CONNECTOR_CATALOG.items():
        if entry.get("spec_url"):
            assert entry.get("spec_host_allowlist"), f"{cid} ships a default spec_url but no spec_host_allowlist"


# ---------------------------------------------------------------------------
# Bug 185 — oversized connector specs (GitHub ~12.5MB) are slimmed to fit the
# AgentCore 10MB target-spec cap WITHOUT dropping operations.
# ---------------------------------------------------------------------------


def test_slim_openapi_spec_preserves_operations_and_shrinks():
    import json as _json

    from app.services import gateway_deployer as gd

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Big", "description": "keep-info-desc"},
        "externalDocs": {"url": "http://docs", "description": "y" * 5000},
        "x-github": {"big": "z" * 9000},
        "components": {"responses": {"nf": {"description": "Not found", "content": {}}}},
        "paths": {
            "/things": {
                "get": {
                    "operationId": "listThings",
                    "summary": "List things",
                    "description": "keep-op-desc",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {"example": {"a": "b" * 9000}, "examples": {"e": "c" * 9000}}
                            },
                        }
                    },
                },
                "post": {"operationId": "createThing", "responses": {"200": {"description": "ok"}}},
            }
        },
    }
    raw = _json.dumps(spec)
    slim = gd._slim_openapi_spec(raw)
    assert len(slim.encode()) < len(raw.encode())  # shrank
    s = _json.loads(slim)
    # both operations preserved with their operationIds
    assert s["paths"]["/things"]["get"]["operationId"] == "listThings"
    assert s["paths"]["/things"]["post"]["operationId"] == "createThing"
    # Bug 185b: descriptions are REQUIRED on Response Objects and must be KEPT
    # (stripping them produced an invalid spec that served 0 tools).
    assert s["components"]["responses"]["nf"]["description"] == "Not found"
    assert s["paths"]["/things"]["get"]["responses"]["200"]["description"] == "ok"
    assert s["info"]["description"] == "keep-info-desc"
    assert s["paths"]["/things"]["get"]["description"] == "keep-op-desc"
    # size-heavy samples / vendor extensions removed
    assert "externalDocs" not in s
    assert "x-github" not in s
    ct = s["paths"]["/things"]["get"]["responses"]["200"]["content"]["application/json"]
    assert "example" not in ct and "examples" not in ct


def test_build_openapi_schema_slims_when_over_s3_cap(monkeypatch):
    import json as _json

    from app.services import gateway_deployer as gd

    # Build a spec just over the slim target via a giant description.
    big = {
        "openapi": "3.0.0",
        "info": {"title": "B"},
        "paths": {
            "/x": {
                "get": {
                    "operationId": "getX",
                    "description": "d" * (gd._S3_SPEC_SLIM_TARGET + 1000),
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    raw = _json.dumps(big)
    assert len(raw.encode()) > gd._S3_SPEC_SLIM_TARGET

    captured = {}
    monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", "test-bucket")

    class _S3:
        def put_object(self, **kw):
            captured["bytes"] = len(kw["Body"])
            return {}

    monkeypatch.setattr(gd.boto3, "client", lambda *a, **k: _S3())
    block = gd._build_openapi_schema(raw, connector_id="github", region="us-east-1")
    assert "s3" in block  # staged, not inlined
    # The staged object was slimmed below the 10MB cap.
    assert captured["bytes"] < gd._MAX_S3_SPEC_BYTES


# ---------------------------------------------------------------------------
# Bug 189b — drop gateway-unsupported media types so real SaaS specs (GitHub)
# validate instead of failing with "MediaType ... is not supported".
# ---------------------------------------------------------------------------


def test_sanitize_openapi_drops_unsupported_media_types():
    import json as _json

    from app.services import gateway_deployer as gd

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "x"},
        "paths": {
            "/x": {
                "get": {
                    "operationId": "getX",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {"schema": {"type": "object"}},
                                "application/scim+json": {"schema": {"type": "object"}},
                                "text/html": {"schema": {"type": "string"}},
                            },
                        },
                        "400": {
                            "description": "bad",
                            "content": {
                                "application/vnd.github.diff": {"schema": {"type": "string"}},
                            },
                        },
                    },
                }
            }
        },
    }
    out = _json.loads(gd._sanitize_openapi_for_gateway(_json.dumps(spec)))
    r200 = out["paths"]["/x"]["get"]["responses"]["200"]
    # supported kept, unsupported dropped
    assert set(r200["content"].keys()) == {"application/json"}
    # 400 had ONLY unsupported -> content removed, but description (required) kept
    r400 = out["paths"]["/x"]["get"]["responses"]["400"]
    assert "content" not in r400
    assert r400["description"] == "bad"
    # operation preserved
    assert out["paths"]["/x"]["get"]["operationId"] == "getX"


def test_sanitize_drops_operations_using_oneof():
    """Bug 189c: the gateway rejects 'oneOf' schemas; drop just those operations
    (keep the rest of the connector working)."""
    import json as _json

    from app.services import gateway_deployer as gd

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "x"},
        "paths": {
            "/keep": {"get": {"operationId": "keep", "responses": {"200": {"description": "ok"}}}},
            "/drop": {
                "post": {
                    "operationId": "drop",
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"oneOf": [{"type": "string"}, {"type": "integer"}]}}
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }
    out = _json.loads(gd._sanitize_openapi_for_gateway(_json.dumps(spec)))
    assert "/keep" in out["paths"] and out["paths"]["/keep"]["get"]["operationId"] == "keep"
    # the oneOf operation (and now-empty path) is removed
    assert "/drop" not in out["paths"]
    assert "oneOf" not in _json.dumps(out["paths"])


def test_sanitize_drops_requestbody_when_only_unsupported_media(monkeypatch):
    """Bug 189b follow-up: if a requestBody has ONLY unsupported media types,
    sanitizing removes its content -> the requestBody would be invalid
    (requestBody requires content). The whole requestBody must be dropped."""
    import json as _json

    from app.services import gateway_deployer as gd

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "x"},
        "paths": {
            "/markdown/raw": {
                "post": {
                    "operationId": "render",
                    "requestBody": {"required": True, "content": {"text/x-markdown": {"schema": {"type": "string"}}}},
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    out = _json.loads(gd._sanitize_openapi_for_gateway(_json.dumps(spec)))
    op = out["paths"]["/markdown/raw"]["post"]
    assert "requestBody" not in op  # dropped (was content-less after sanitize)
    assert op["operationId"] == "render"  # operation preserved


def test_build_openapi_schema_sanitizes_inline_spec(monkeypatch):
    import json as _json

    from app.services import gateway_deployer as gd

    spec = _json.dumps(
        {
            "openapi": "3.0.0",
            "info": {"title": "x"},
            "paths": {
                "/x": {
                    "get": {
                        "operationId": "g",
                        "responses": {
                            "200": {"description": "ok", "content": {"application/scim+json": {"schema": {}}}}
                        },
                    }
                }
            },
        }
    )
    block = gd._build_openapi_schema(spec, connector_id="github", region="us-east-1")
    # small spec -> inline; the inline payload must have the unsupported type stripped
    assert "inlinePayload" in block
    assert "scim+json" not in block["inlinePayload"]


def test_cap_openapi_operations_limits_count():
    """Bug 189d: cap operations so the gateway tool-plane can materialize them."""
    import json as _json

    from app.services import gateway_deployer as gd

    paths = {
        f"/p{i}": {"get": {"operationId": f"op{i}", "responses": {"200": {"description": "ok"}}}} for i in range(50)
    }
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "x"},
        "paths": paths,
        "components": {"schemas": {"S": {"type": "object"}}},
    }
    out = _json.loads(gd._cap_openapi_operations(_json.dumps(spec), max_ops=10))
    ops = sum(1 for p, ms in out["paths"].items() for m in ms if m in ("get", "post", "put", "delete", "patch"))
    assert ops == 10
    assert "schemas" in out["components"]  # components preserved
    # under the cap -> unchanged string
    small = _json.dumps(
        {
            "openapi": "3.0.0",
            "info": {"title": "x"},
            "paths": {"/a": {"get": {"operationId": "a", "responses": {"200": {"description": "ok"}}}}},
        }
    )
    assert gd._cap_openapi_operations(small, max_ops=10) == small


def test_sanitize_rewrites_operationids_for_bedrock_tool_names():
    """Bug 191: operationIds become tool names <target>___<opId> which Bedrock
    requires to match [a-zA-Z0-9_-]+ and be <=64 chars. Rewrite slashes/long ids."""
    import json as _json
    import re as _re

    from app.services import gateway_deployer as gd

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "x"},
        "paths": {
            "/a": {"get": {"operationId": "meta/root", "responses": {"200": {"description": "ok"}}}},
            "/b": {
                "get": {
                    "operationId": "actions/this-is-a-really-really-really-long-operation-id-exceeding-limit",
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/c": {"get": {"operationId": "meta/root", "responses": {"200": {"description": "ok"}}}},
        },
    }
    out = _json.loads(gd._sanitize_openapi_for_gateway(_json.dumps(spec)))
    ids = [
        op["operationId"]
        for pth, ms in out["paths"].items()
        for m, op in ms.items()
        if isinstance(op, dict) and "operationId" in op
    ]
    for i in ids:
        assert _re.fullmatch(r"[a-zA-Z0-9_-]+", i), i
        assert len(i) <= 44, i
    assert len(ids) == len(set(ids))  # de-duplicated (two meta/root -> distinct)
