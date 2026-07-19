"""SaaS connector catalog for AgentCore Gateway OpenAPI targets.

A connector is a curated, deployable Gateway *target* that exposes a third-party
SaaS API (Jira, Asana, Slack, GitHub, Salesforce, ...) as MCP tools an agent can
call. Connectors follow the same ``tool -> gateway -> runtime`` wiring as built-in
tools; the gateway crawls the connector's OpenAPI spec and serves its operations as
tools, authenticating outbound calls via an AgentCore credential provider.

This module is the single source of truth for the catalog (mirrors the
``GATEWAY_TOOL_SCHEMAS`` registry in ``gateway_deployer.py``). It is pure data plus
small lookup helpers — NO boto3 here. Credential-provider creation and target
deployment live in ``gateway_deployer.py``.

Auth scope (v1): API key + 2-legged OAuth (client-credentials) only. No 3-legged /
per-user consent yet.
"""

from __future__ import annotations

# Sentinel id for a user-supplied OpenAPI/MCP connector (no curated catalog entry).
GENERIC_CONNECTOR_ID = "generic_openapi"

# Outbound credential placement for API-key connectors.
CredentialLocation = str  # "HEADER" | "QUERY_PARAMETER"

# Supported outbound auth methods a connector entry may advertise.
AUTH_API_KEY = "api_key"
AUTH_OAUTH2_CC = "oauth2_cc"  # OAuth2 client-credentials (2-legged)

# Hosts the vendor OpenAPI specs are published from. This is DISTINCT from a
# connector's ``allowlist_hosts`` (which is the SSRF allowlist for the runtime
# API host, e.g. app.asana.com). The spec is FETCHED from a different,
# vendor-controlled documentation host (most publish on GitHub raw), so the
# spec-fetch SSRF check uses ``spec_host_allowlist`` — NOT the API allowlist.
# Bug: conflating the two rejected every branded connector that fetches its
# catalog spec_url ("Connector spec host 'raw.githubusercontent.com' is not in
# the connector allowlist ['app.asana.com']").
_VENDOR_SPEC_HOSTS = ["raw.githubusercontent.com"]


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
#
# Each entry documents how to deploy that SaaS as an OpenAPI Gateway target:
#   id                        stable connector id (matches frontend palette toolId)
#   label                     human label
#   icon                      emoji/icon hint for the palette
#   spec_url                  default OpenAPI spec URL (None => user must supply)
#   auth_methods              ordered list of supported auth methods
#   oauth_vendor              AgentCore create_oauth2_credential_provider vendor enum,
#                             or None when the vendor has no first-class enum
#                             (then CustomOauth2 is used, requiring a discovery_url)
#   credential_location       default placement for API-key auth (HEADER/QUERY_PARAMETER)
#   credential_parameter_name default header/query param name for the key
#   credential_prefix         default value prefix (e.g. "Bearer") or "" for none
#   allowlist_hosts           glob hosts the spec_url / API base is allowed to resolve to
#   default_scopes            default OAuth scopes
#   doc_url                   where a user gets credentials
#
# IMPORTANT: there is NO "Asana" OAuth vendor in the AgentCore enum, so Asana is
# API-key (Personal Access Token) only in v1.

CONNECTOR_CATALOG: dict[str, dict] = {
    "jira": {
        "id": "jira",
        "label": "Jira",
        "icon": "🪧",
        "spec_url": None,  # Atlassian spec is site-scoped; user supplies their cloud spec/base
        # A Jira/Atlassian API TOKEN authenticates via HTTP Basic
        # base64(email:token). The AgentCore api-key provider can't COMPUTE that
        # per request, BUT the caller can pre-compute base64(email:token) at
        # deploy time and store THAT as the api-key value, with the static prefix
        # "Basic " — the provider then sends `Authorization: Basic <b64>` on every
        # call, which IS valid Jira auth. So api_key IS offered (the deploy path
        # builds the base64 from email+token). OAuth2 (AtlassianOauth2) remains
        # available for 2LO where the access token is a Bearer credential.
        "auth_methods": [AUTH_API_KEY, AUTH_OAUTH2_CC],
        "oauth_vendor": "AtlassianOauth2",
        "credential_location": "HEADER",
        "credential_parameter_name": "Authorization",
        # Default prefix is "Basic " for the api-key (pre-computed base64) path.
        # The OAuth2 path overrides to a Bearer access token at runtime.
        "credential_prefix": "Basic",
        "allowlist_hosts": ["*.atlassian.net", "api.atlassian.com"],
        "default_scopes": ["read:jira-work", "write:jira-work"],
        "doc_url": "https://developer.atlassian.com/cloud/jira/platform/rest/v3/",
    },
    "asana": {
        "id": "asana",
        "label": "Asana",
        "icon": "🅰️",
        "spec_url": "https://raw.githubusercontent.com/Asana/openapi/master/defs/asana_oas.yaml",
        "spec_host_allowlist": _VENDOR_SPEC_HOSTS,
        # Asana has no first-class OAuth vendor enum -> PAT (API key) only in v1.
        "auth_methods": [AUTH_API_KEY],
        "oauth_vendor": None,
        "credential_location": "HEADER",
        "credential_parameter_name": "Authorization",
        "credential_prefix": "Bearer",
        "allowlist_hosts": ["app.asana.com"],
        "default_scopes": [],
        "doc_url": "https://developers.asana.com/docs/personal-access-token",
    },
    "slack": {
        "id": "slack",
        "label": "Slack",
        "icon": "💬",
        "spec_url": "https://raw.githubusercontent.com/slackapi/slack-api-specs/master/web-api/slack_web_openapi_v2.json",
        "spec_host_allowlist": _VENDOR_SPEC_HOSTS,
        "auth_methods": [AUTH_OAUTH2_CC, AUTH_API_KEY],
        "oauth_vendor": "SlackOauth2",
        "credential_location": "HEADER",
        "credential_parameter_name": "Authorization",
        "credential_prefix": "Bearer",
        "allowlist_hosts": ["slack.com", "*.slack.com"],
        "default_scopes": ["chat:write", "channels:read"],
        "doc_url": "https://api.slack.com/authentication/oauth-v2",
    },
    "github": {
        "id": "github",
        "label": "GitHub",
        "icon": "🐙",
        "spec_url": "https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json",
        "spec_host_allowlist": _VENDOR_SPEC_HOSTS,
        "auth_methods": [AUTH_API_KEY, AUTH_OAUTH2_CC],
        "oauth_vendor": "GithubOauth2",
        "credential_location": "HEADER",
        "credential_parameter_name": "Authorization",
        "credential_prefix": "Bearer",
        "allowlist_hosts": ["api.github.com"],
        "default_scopes": ["repo", "read:org"],
        "doc_url": "https://docs.github.com/en/authentication",
    },
    "salesforce": {
        "id": "salesforce",
        "label": "Salesforce",
        "icon": "☁️",
        "spec_url": None,  # instance-scoped; user supplies their org spec/base
        "auth_methods": [AUTH_OAUTH2_CC, AUTH_API_KEY],
        "oauth_vendor": "SalesforceOauth2",
        "credential_location": "HEADER",
        "credential_parameter_name": "Authorization",
        "credential_prefix": "Bearer",
        "allowlist_hosts": ["*.salesforce.com", "*.force.com", "*.my.salesforce.com"],
        "default_scopes": ["api", "refresh_token"],
        "doc_url": "https://help.salesforce.com/s/articleView?id=sf.connected_app_overview.htm",
    },
}


def get_connector(connector_id: str) -> dict | None:
    """Return the catalog entry for *connector_id*, or None if unknown.

    The GENERIC sentinel intentionally has no catalog entry — a generic connector
    is fully described by the user-supplied config (spec + auth), so callers should
    treat ``connector_id == GENERIC_CONNECTOR_ID`` as "no catalog defaults".
    """
    return CONNECTOR_CATALOG.get(connector_id)


def is_generic(connector_id: str | None) -> bool:
    """True when the connector is the user-supplied generic OpenAPI/MCP connector."""
    return not connector_id or connector_id == GENERIC_CONNECTOR_ID


def known_connector_ids() -> list[str]:
    """All curated connector ids plus the generic sentinel (for validation)."""
    return [*CONNECTOR_CATALOG.keys(), GENERIC_CONNECTOR_ID]


def supports_auth(connector_id: str, auth_method: str) -> bool:
    """Whether *connector_id* supports *auth_method*.

    The generic connector supports both methods (the user supplies everything).
    """
    if is_generic(connector_id):
        return auth_method in (AUTH_API_KEY, AUTH_OAUTH2_CC)
    entry = CONNECTOR_CATALOG.get(connector_id)
    if not entry:
        return False
    return auth_method in entry.get("auth_methods", [])


def oauth_vendor_for(connector_id: str) -> str | None:
    """Return the AgentCore OAuth2 vendor enum for *connector_id*.

    Returns None for the generic connector and for curated connectors that have no
    first-class vendor (those must use CustomOauth2 with a discovery URL).
    """
    if is_generic(connector_id):
        return None
    entry = CONNECTOR_CATALOG.get(connector_id)
    return entry.get("oauth_vendor") if entry else None


def vendor_config_key(vendor: str) -> str:
    """Map an OAuth2 vendor enum to its create_oauth2_credential_provider config key.

    e.g. ``AtlassianOauth2`` -> ``atlassianOauth2ProviderConfig``,
    ``GithubOauth2`` -> ``githubOauth2ProviderConfig``,
    ``CustomOauth2`` -> ``customOauth2ProviderConfig``.

    The rule (verified against the live service model): lowercase the first letter
    of the vendor name and append ``ProviderConfig``.
    """
    if not vendor:
        raise ValueError("vendor is required")
    return vendor[0].lower() + vendor[1:] + "ProviderConfig"
