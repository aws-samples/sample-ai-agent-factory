"""Gateway deployment and cleanup for AgentCore.

Uses pure boto3 APIs — no external CLI or starter toolkit dependencies.
Handles MCP Gateway creation, Lambda target deployment, Cognito OAuth
setup, JWT auth configuration, and resource cleanup.

Requirements: 5.3
"""

import fnmatch
import io
import ipaddress
import json
import logging
import os
import re
import socket
import time
import urllib.parse
import zipfile

import boto3

from app.services import codegen_templates
from app.services.aws_errors import is_error

logger = logging.getLogger(__name__)


def _safe_log_token(value: object, *, limit: int = 128) -> str:
    """Return a log-safe rendering of an identifier (resource/provider/connector
    NAME or ARN) for diagnostic logging.

    SECURITY (CodeQL py/clear-text-logging-sensitive-data): connector credential
    secrets are minted into Secrets Manager and the only things we ever log are
    NAMES/ARNs/ids — never the secret value. This helper makes that guarantee
    explicit and machine-checkable: it rebuilds the string from a restricted
    character class ([A-Za-z0-9_./:-]), which both strips anything unexpected and
    severs the taint flow from any secret-typed variable that shares the caller's
    scope (the flagged log args are names, not values).
    """
    s = "" if value is None else str(value)
    s = re.sub(r"[^A-Za-z0-9_./:-]", "", s)
    return s[:limit]


# ---------------------------------------------------------------------------
# SSRF guard for OIDC discovery + any operator-supplied URL we fetch
# ---------------------------------------------------------------------------


class _DiscoveryUrlInvalid(ValueError):
    """The supplied URL is structurally invalid (bad scheme, missing host, etc.)."""


class _DiscoveryUrlBlocked(ValueError):
    """The supplied URL points (after DNS resolution) at a disallowed network."""


# Networks we refuse to talk to. Built once at module import time.
# Covers loopback, link-local (IMDS at 169.254.169.254 + Lambda creds at 169.254.170.2),
# RFC1918 private space, CGNAT, multicast, "this network", and IPv4/IPv6 reserved space.
_DISALLOWED_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        # IPv4
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # RFC1918
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local (IMDS, Lambda creds)
        "172.16.0.0/12",  # RFC1918
        "192.0.0.0/24",  # IETF
        "192.0.2.0/24",  # TEST-NET-1
        "192.168.0.0/16",  # RFC1918
        "198.18.0.0/15",  # benchmark
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved (incl. 255.255.255.255)
        # IPv6
        "::1/128",  # loopback
        "::/128",  # unspecified
        "::ffff:0:0/96",  # IPv4-mapped (so an IPv4 RFC1918 mapped into v6 is also blocked
        # via the v4 check, but we keep this for belt-and-braces)
        "fc00::/7",  # ULA (private)
        "fe80::/10",  # link-local
        "ff00::/8",  # multicast
        "2001:db8::/32",  # documentation
    )
)


def _load_oidc_host_allowlist() -> tuple[str, ...] | None:
    """Return tuple of allowed host glob patterns from env, or None if no allowlist set.

    Env var: OIDC_DISCOVERY_HOST_ALLOWLIST=*.okta.com,*.auth0.com,*.amazoncognito.com
    """
    raw = os.environ.get("OIDC_DISCOVERY_HOST_ALLOWLIST", "").strip()
    if not raw:
        return None
    parts = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    return parts or None


def _host_matches_allowlist(host: str, allowlist: tuple[str, ...]) -> bool:
    host = host.lower()
    return any(fnmatch.fnmatchcase(host, pattern) for pattern in allowlist)


def _validate_discovery_url(url: str) -> str:
    """Validate that ``url`` is safe to fetch from a server-side context.

    Raises ``_DiscoveryUrlInvalid`` for structural problems and
    ``_DiscoveryUrlBlocked`` if any resolved IP falls in a disallowed network or the
    host is not on the operator-configured allowlist.

    Returns the validated URL on success (caller should use this verbatim with
    ``urlopen``). Note: a residual race remains where DNS could re-resolve to a
    private IP between this validation and the actual ``urlopen`` call; we mitigate
    by requiring a strict urlopen timeout in the caller. To eliminate the race
    entirely, one would have to issue the HTTP request against a pinned IP with
    SNI/Host overrides — out of scope here.
    """
    if not url or not isinstance(url, str):
        raise _DiscoveryUrlInvalid("OIDC discovery URL is empty")

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise _DiscoveryUrlInvalid(f"OIDC discovery URL must use https scheme (got '{parsed.scheme}')")
    host = parsed.hostname
    if not host:
        raise _DiscoveryUrlInvalid("OIDC discovery URL has no host component")

    allowlist = _load_oidc_host_allowlist()
    if allowlist is not None and not _host_matches_allowlist(host, allowlist):
        raise _DiscoveryUrlBlocked(f"OIDC discovery host '{host}' is not on OIDC_DISCOVERY_HOST_ALLOWLIST")

    # Resolve every A/AAAA record under a strict timeout so an attacker cannot stall
    # us on DNS to keep a half-validated socket alive.
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5)
    try:
        try:
            infos = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except (TimeoutError, socket.gaierror, OSError) as e:
            raise _DiscoveryUrlBlocked(f"OIDC discovery URL host '{host}' could not be resolved: {e}") from e
    finally:
        socket.setdefaulttimeout(prev_timeout)

    if not infos:
        raise _DiscoveryUrlBlocked(f"OIDC discovery URL host '{host}' returned no DNS records")

    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        # IPv6 sockaddr can carry a scope id like "fe80::1%eth0" — strip it.
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as e:
            raise _DiscoveryUrlBlocked(f"OIDC discovery URL resolved to unparseable IP '{ip_str}': {e}") from e
        for net in _DISALLOWED_NETWORKS:
            # ip_address(v4) in ip_network(v6) raises TypeError, so guard on family.
            if ip_obj.version != net.version:
                continue
            if ip_obj in net:
                raise _DiscoveryUrlBlocked(f"OIDC discovery URL resolves to disallowed IP ({ip_str} in {net})")

    return url


def _validate_outbound_url(url: str, allowlist_hosts: tuple[str, ...] | None = None) -> str:
    """Validate any user-supplied outbound URL (e.g. a connector OpenAPI spec URL).

    Generalizes :func:`_validate_discovery_url` (same https-only + DNS-resolved
    private/IMDS denylist + optional operator allowlist via env). When
    *allowlist_hosts* is provided (e.g. a connector's vetted hosts), the URL's host
    must additionally match one of those globs. Returns the validated URL.
    """
    validated = _validate_discovery_url(url)
    if allowlist_hosts:
        host = (urllib.parse.urlparse(validated).hostname or "").lower()
        if not any(fnmatch.fnmatchcase(host, pat.lower()) for pat in allowlist_hosts):
            raise _DiscoveryUrlBlocked(
                f"Connector spec host '{host}' is not in the connector allowlist {list(allowlist_hosts)}"
            )
    return validated


# ---------------------------------------------------------------------------
# Response key helpers
# ---------------------------------------------------------------------------


def _resolve_gateway_tool_actions(agentcore_ctrl, gateway_id: str, timeout: int = 180) -> tuple[list, int]:
    """Return (qualified Cedar action names, expected_tool_count) for a gateway,
    waiting up to *timeout*s for EACH target to be truly SYNCED into the gateway's
    servable MCP tool plane.

    Bug 134/race-A: `inlinePayload` is the CONFIGURED schema (echoed back the
    instant the target exists) — it does NOT prove the gateway has synced those
    tools into the plane the agent discovers via tools/list. The authoritative
    per-target signal is `lastSynchronizedAt` (only on get_gateway_target, not on
    list_gateway_targets items). We synchronize, then poll each target until
    status==READY AND lastSynchronizedAt has advanced past its pre-sync value, so
    the manifest the Cedar policy is built from == the plane the agent will
    discover. We also return how many tools the gateway CONFIGURED so the policy
    step can fail-closed on a partial (synced < configured) plane.
    """
    import time as _t

    def _list_target_ids() -> list:
        try:
            resp = agentcore_ctrl.list_gateway_targets(gatewayIdentifier=gateway_id, maxResults=50)
            items = resp.get("items", resp.get("gatewayTargetSummaries", []))
            return [
                (t.get("name", ""), t.get("targetId") or t.get("gatewayTargetId"))
                for t in items
                if t.get("name") and (t.get("targetId") or t.get("gatewayTargetId"))
            ]
        except Exception as e:  # noqa: BLE001
            logger.warning("list_gateway_targets failed (will retry): %s", e)
            return []

    def _configured_tools(detail: dict) -> list:
        tc = detail.get("targetConfiguration", {}) or {}
        mcp = tc.get("mcp", {}) or {}
        schema = (mcp.get("lambda", {}) or {}).get("toolSchema", {}) or {}
        return schema.get("inlinePayload", []) or []

    # Snapshot pre-sync timestamps so we can require lastSynchronizedAt to ADVANCE
    # (a target may carry a stale sync time from a prior deploy of a reused gw).
    pre_sync = {}
    for _tname, tid in _list_target_ids():
        try:
            d = agentcore_ctrl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=tid)
            pre_sync[tid] = d.get("lastSynchronizedAt")
        except Exception:  # noqa: BLE001
            pre_sync[tid] = None

    try:
        agentcore_ctrl.synchronize_gateway_targets(gatewayIdentifier=gateway_id)
    except Exception as e:  # noqa: BLE001
        logger.info("synchronize_gateway_targets (non-fatal) for %s: %s", gateway_id, e)

    deadline = _t.time() + timeout
    actions = []
    expected = 0
    while _t.time() < deadline:
        actions = []
        expected = 0
        all_synced = True
        ids = _list_target_ids()
        if not ids:
            all_synced = False
        for tname, tid in ids:
            try:
                detail = agentcore_ctrl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=tid)
            except Exception:  # noqa: BLE001
                all_synced = False
                continue
            tools = _configured_tools(detail)
            expected += len(tools)
            tstatus = (detail.get("status") or "").upper()
            synced_at = detail.get("lastSynchronizedAt")
            # Readiness depends on the target TYPE:
            #  - INLINE-payload Lambda targets declare their tools inline, so they
            #    are servable as soon as status==READY. They NEVER get a
            #    lastSynchronizedAt (that timestamp is only set for targets whose
            #    tool list is CRAWLED — OpenAPI specs / external MCP servers).
            #    Requiring lastSynchronizedAt here was the bug: an inline-Lambda
            #    target stays READY with lastSynchronizedAt=None forever, so the
            #    poll always timed out at 0/N (verified live).
            #  - CRAWLED targets (no inlinePayload) ARE only servable once
            #    lastSynchronizedAt is present and (for reused gateways) advanced.
            is_inline = bool(tools)
            if is_inline:
                target_synced = tstatus == "READY"
            else:
                target_synced = (
                    tstatus in ("READY", "ACTIVE") and synced_at is not None and synced_at != pre_sync.get(tid)
                )
            if not target_synced:
                all_synced = False
                continue
            for tool in tools:
                nm = tool.get("name")
                if nm:
                    actions.append(f"{tname}___{nm}")
        # Done when every configured target is synced AND every configured tool
        # is present in the action list.
        if ids and all_synced and len(actions) == expected and expected > 0:
            logger.warning("Gateway %s tool plane synced: %d/%d tools", gateway_id, len(actions), expected)
            return actions, expected
        _t.sleep(5)

    logger.warning(
        "Gateway %s tool plane not fully synced within %ds; %d/%d tools synced",
        gateway_id,
        timeout,
        len(actions),
        expected,
    )
    return actions, expected


def _get_targets_from_response(response: dict) -> list:
    """Extract targets list from list_gateway_targets response.

    The API may return the list under different keys depending on SDK version.
    """
    return response.get("items", response.get("targets", response.get("gatewayTargetSummaries", [])))


def _get_gateways_from_response(response: dict) -> list:
    """Extract gateways list from list_gateways response."""
    return response.get("items", response.get("gateways", response.get("gatewaySummaries", [])))


def _list_all_gateways(agentcore_ctrl) -> list:
    """List EVERY gateway, following pagination.

    The conflict-recovery path matches an existing gateway by name; a single
    unpaginated ``list_gateways()`` silently misses gateways past the first
    page, so a redeploy into a busy account fails to find its own gateway and
    raises "exists but not found via list". Follow nextToken to completion.
    """
    gateways: list = []
    next_token = None
    while True:
        resp = agentcore_ctrl.list_gateways(nextToken=next_token) if next_token else agentcore_ctrl.list_gateways()
        gateways.extend(_get_gateways_from_response(resp))
        next_token = resp.get("nextToken")
        if not next_token:
            return gateways


# ---------------------------------------------------------------------------
# Boto3 wrapper helpers
# ---------------------------------------------------------------------------


def _create_lambda_client(region: str):
    return boto3.client("lambda", region_name=region)


def _create_iam_client():
    return boto3.client("iam")


def _create_cognito_client(region: str):
    return boto3.client("cognito-idp", region_name=region)


def _create_agentcore_control_client(region: str):
    return boto3.client("bedrock-agentcore-control", region_name=region)


def _create_agentcore_client(region: str):
    return boto3.client("bedrock-agentcore", region_name=region)


def _create_secrets_client(region: str):
    return boto3.client("secretsmanager", region_name=region)


# ---------------------------------------------------------------------------
# Connector credentials: Secrets Manager + AgentCore credential providers
# ---------------------------------------------------------------------------
#
# SaaS connectors authenticate outbound calls via an AgentCore credential
# provider (API key or OAuth2 client-credentials). The raw secret is stored ONCE
# in our own Secrets Manager secret (owner-scoped name) and the provider is
# created with apiKeySecretSource/clientSecretSource="EXTERNAL" referencing that
# secret — so the raw value never lands in DynamoDB, canvas JSON, or logs.


# Provider names are derived from the connector + deployment so teardown can find
# them. AgentCore provider names must match ^[a-zA-Z0-9_-]+$ and are <=64 chars.
def _sanitize_provider_name(raw: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]", "-", raw)[:64]
    return name or "connector-cred"


def _put_connector_secret(region: str, owner_sub: str, payload: dict) -> str:
    """Create a Secrets Manager secret holding a connector credential payload.

    Name pattern: ``agentcore-connector/{safe_owner}/{uuid}``. *payload* is a JSON
    object such as ``{"apiKey": "..."}`` or ``{"clientSecret": "..."}``. Returns the
    secret ARN. The raw value is never logged.
    """
    import uuid as _uuid

    safe_owner = re.sub(r"[^a-zA-Z0-9_-]", "-", (owner_sub or "anon"))[:48]
    resource_name = f"agentcore-connector/{safe_owner}/{_uuid.uuid4().hex[:12]}"
    sm = _create_secrets_client(region)
    resp = sm.create_secret(
        Name=resource_name,
        SecretString=json.dumps(payload),
        Description="AgentCore SaaS connector credential (auto-managed)",
    )
    # SECURITY (CodeQL py/clear-text-logging-sensitive-data): log a CONSTANT only
    # — never the generated name or the payload. The caller gets the ARN.
    logger.info("Created connector credential resource")
    return resp["ARN"]


def _ensure_api_key_credential_provider(
    agentcore_ctrl,
    name: str,
    *,
    secret_arn: str,
    json_key: str = "apiKey",
) -> str:
    """Create (or reuse) an API-key credential provider backed by our own secret.

    Returns the credential provider ARN. Idempotent: on conflict the existing
    provider is looked up and its ARN returned.
    """
    provider_name = _sanitize_provider_name(name)
    try:
        resp = agentcore_ctrl.create_api_key_credential_provider(
            name=provider_name,
            apiKeySecretConfig={"secretId": secret_arn, "jsonKey": json_key},
            apiKeySecretSource="EXTERNAL",
        )
        arn = resp.get("credentialProviderArn") or resp.get("apiKeyCredentialProviderArn", "")
        # SECURITY (CodeQL py/clear-text-logging-sensitive-data): log a constant;
        # provider_name shares scope with the secret arn/config and is taint-flagged.
        logger.info("Created API-key credential provider")
        return arn
    except Exception as e:  # noqa: BLE001
        # "already exists" fallback kept deliberately: the service reports an
        # existing provider as a ValidationException whose message says
        # "already exists" (verified live in cfn_provider), not only as a
        # ConflictException — the code check alone would miss it.
        if is_error(e, "ConflictException") or "already exists" in str(e):
            try:
                got = agentcore_ctrl.get_api_key_credential_provider(name=provider_name)
                return got.get("credentialProviderArn") or got.get("apiKeyCredentialProviderArn", "")
            except Exception:  # noqa: BLE001
                # SECURITY: constant only — provider_name is taint-flagged (see above).
                logger.debug("API-key provider conflict lookup failed; re-raising original error")
        raise


# Phase 3 (Loom) OBO — on-behalf-of token-exchange grant types (RFC 8693 /
# RFC 7523) and their AgentCore client-auth pairing. Verified against the live
# bedrock-agentcore-control service model (boto3 1.43.8):
#   TOKEN_EXCHANGE (RFC 8693, e.g. Okta) → CLIENT_SECRET_BASIC + actorTokenContent NONE
#   JWT_AUTHORIZATION_GRANT (RFC 7523, e.g. Entra ID) → CLIENT_SECRET_POST
_OBO_GRANT_TYPES = ("TOKEN_EXCHANGE", "JWT_AUTHORIZATION_GRANT")
_OBO_CLIENT_AUTH = {
    "TOKEN_EXCHANGE": "CLIENT_SECRET_BASIC",
    "JWT_AUTHORIZATION_GRANT": "CLIENT_SECRET_POST",
}


def _ensure_oauth2_credential_provider(
    agentcore_ctrl,
    name: str,
    *,
    vendor: str,
    client_id: str,
    client_secret_arn: str,
    json_key: str = "clientSecret",
    discovery_url: str | None = None,
    delegation_mode: str = "m2m",
    obo_grant_type: str | None = None,
) -> str:
    """Create (or reuse) an OAuth2 credential provider for a connector.

    *vendor* is an AgentCore vendor enum (e.g. ``AtlassianOauth2``,
    ``GithubOauth2``, or ``CustomOauth2``). For branded vendors the config key is
    derived as ``{vendorLower}ProviderConfig``; for ``CustomOauth2`` a
    ``discovery_url`` is required. The client secret is referenced from our own
    Secrets Manager secret (``clientSecretSource="EXTERNAL"``). Returns the
    credential provider ARN. Idempotent on conflict.

    Phase 3 (Loom) OBO: when ``delegation_mode="obo"`` the provider is configured
    for on-behalf-of token exchange (RFC 8693) so the agent calls downstream
    services AS THE END-USER, preserving the delegation chain and least-privilege
    — rather than a shared machine-to-machine identity. OBO requires the
    ``CustomOauth2`` vendor (the branded vendor configs don't expose the
    exchange config) and a token-exchange-capable IdP (Entra/Okta/Auth0/OIDC).
    """
    from app.services.connectors import vendor_config_key

    provider_name = _sanitize_provider_name(name)
    config_key = vendor_config_key(vendor)

    _obo = str(delegation_mode or "m2m").lower() == "obo"
    if _obo and vendor != "CustomOauth2":
        # OBO exchange config only exists on customOauth2ProviderConfig.
        raise ValueError("OBO delegation requires the CustomOauth2 vendor (custom OIDC provider)")

    if vendor == "CustomOauth2":
        if not discovery_url:
            raise ValueError("CustomOauth2 connector requires a discovery_url")
        provider_config = {
            "oauthDiscovery": {"discoveryUrl": discovery_url},
            "clientId": client_id,
            "clientSecretConfig": {"secretId": client_secret_arn, "jsonKey": json_key},
            "clientSecretSource": "EXTERNAL",
        }
        if _obo:
            grant = (obo_grant_type or "TOKEN_EXCHANGE").upper()
            if grant not in _OBO_GRANT_TYPES:
                raise ValueError(f"obo_grant_type must be one of {_OBO_GRANT_TYPES}")
            provider_config["clientAuthenticationMethod"] = _OBO_CLIENT_AUTH[grant]
            obo_cfg: dict = {"grantType": grant}
            if grant == "TOKEN_EXCHANGE":
                # RFC 8693: no actor token — the user's subject token carries identity.
                obo_cfg["tokenExchangeGrantTypeConfig"] = {"actorTokenContent": "NONE"}
            provider_config["onBehalfOfTokenExchangeConfig"] = obo_cfg
    else:
        provider_config = {
            "clientId": client_id,
            "clientSecretConfig": {"secretId": client_secret_arn, "jsonKey": json_key},
            "clientSecretSource": "EXTERNAL",
        }

    try:
        resp = agentcore_ctrl.create_oauth2_credential_provider(
            name=provider_name,
            credentialProviderVendor=vendor,
            oauth2ProviderConfigInput={config_key: provider_config},
        )
        # SECURITY (CodeQL py/clear-text-logging-sensitive-data): constant only.
        logger.info("Created OAuth2 credential provider")
        return resp["credentialProviderArn"]
    except Exception as e:  # noqa: BLE001
        # "already exists" fallback kept: existing providers can surface as a
        # ValidationException with an "already exists" message, not only a
        # ConflictException (see _ensure_api_key_credential_provider).
        if is_error(e, "ConflictException") or "already exists" in str(e):
            try:
                got = agentcore_ctrl.get_oauth2_credential_provider(name=provider_name)
                return got.get("credentialProviderArn", "")
            except Exception:  # noqa: BLE001
                # SECURITY: constant only — provider_name is taint-flagged.
                logger.debug("OAuth2 provider conflict lookup failed; re-raising original error")
        raise


# ---------------------------------------------------------------------------
# Lambda code constants (embedded Lambda source for gateway targets)
# ---------------------------------------------------------------------------

# Standalone customer-support demo Lambda. Canonical source lives in
# app/services/codegen_templates/customer_support_lambda.py.
CUSTOMER_SUPPORT_LAMBDA_CODE = codegen_templates.load_impl("customer_support_lambda")

CUSTOMER_SUPPORT_TOOLS_SCHEMA = {
    "inlinePayload": [
        {
            "name": "check_order_status",
            "description": "Check the status of a customer order by order ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "The order ID to look up",
                    }
                },
                "required": ["order_id"],
            },
        },
        {
            "name": "lookup_customer",
            "description": "Look up customer information by email address.",
            "inputSchema": {
                "type": "object",
                "properties": {"email": {"type": "string", "description": "Customer email address"}},
                "required": ["email"],
            },
        },
        {
            "name": "search_knowledge_base",
            "description": "Search the knowledge base for support articles.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
        },
        {
            "name": "get_return_policy",
            "description": "Get the return policy for a product category.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "product_category": {
                        "type": "string",
                        "description": "Product category",
                    }
                },
                "required": ["product_category"],
            },
        },
    ]
}

# Dynamic tools Lambda (search/wikipedia/weather/fetch + customer-support demo
# handlers + dispatcher). Canonical source lives in app/services/codegen_templates/
# (dynamic_tools_impl.py + customer_support_impl.py + dynamic_tools_handler.py).
DYNAMIC_TOOLS_LAMBDA_CODE = codegen_templates.dynamic_tools_lambda_source()

GATEWAY_TOOL_SCHEMAS: dict[str, dict] = {
    "duckduckgo_search": {
        "name": "duckduckgo_search",
        "description": "Search the web using DuckDuckGo.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "wikipedia_search": {
        "name": "wikipedia_search",
        "description": "Search Wikipedia and return an article summary.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "weather_api": {
        "name": "get_weather",
        "description": "Get current weather for a location.",
        "inputSchema": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    },
    "web_page_fetcher": {
        "name": "fetch_webpage",
        "description": "Fetch and extract text content from a webpage URL.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    # Customer support tools (from 05-blueprints/customer-support-agent-with-agentcore)
    "get_order": {
        "name": "get_order",
        "description": "Look up order details by order ID. Returns order items, status, dates, and total.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order ID (e.g. ORD-12345)",
                }
            },
            "required": ["order_id"],
        },
    },
    "get_customer": {
        "name": "get_customer",
        "description": "Look up customer information and order summary by customer ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "The customer ID (e.g. CUST-001)",
                }
            },
            "required": ["customer_id"],
        },
    },
    "list_orders": {
        "name": "list_orders",
        "description": "List orders for a customer by customer ID. Returns order summaries sorted by date.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "The customer ID"},
                "limit": {
                    "type": "integer",
                    "description": "Max orders to return (default 10)",
                },
            },
            "required": ["customer_id"],
        },
    },
    "process_refund": {
        "name": "process_refund",
        "description": "Process a refund for an order. Validates amount against order total.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "The order ID to refund"},
                "amount": {"type": "number", "description": "Refund amount in dollars"},
                "reason": {"type": "string", "description": "Reason for the refund"},
            },
            "required": ["order_id", "amount", "reason"],
        },
    },
    "knowledge_base": {
        "name": "knowledge_base_query",
        "description": "Search the knowledge base to answer questions using Retrieval Augmented Generation (RAG).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The question to answer from the knowledge base"},
            },
            "required": ["query"],
        },
    },
}


# ---------------------------------------------------------------------------
# Knowledge Base Tool Lambda
# ---------------------------------------------------------------------------

# Knowledge Base RAG query Lambda. Canonical source lives in
# app/services/codegen_templates/kb_lambda.py.
KNOWLEDGE_BASE_LAMBDA_TEMPLATE = codegen_templates.load_impl("kb_lambda")


def create_knowledge_base_lambda(
    region: str,
    gateway_role_arn: str,
    kb_id: str,
    foundation_model_arn: str,
    deployment_id: str,
) -> str:
    """Create a per-deployment Lambda that queries a Bedrock Knowledge Base."""
    iam_client = _create_iam_client()
    lambda_client = _create_lambda_client(region)

    suffix = deployment_id[:8]
    role_name = f"AgentCoreKBToolRole-{suffix}"
    fn_name = f"AgentCore-KBTool-{suffix}"

    role_arn = _ensure_lambda_role(iam_client, role_name, "Role for KB tool Lambda")

    # Attach Bedrock retrieve permissions
    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName="BedrockKBAccess",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "bedrock:Retrieve",
                            "bedrock:RetrieveAndGenerate",
                            "bedrock:InvokeModel",
                        ],
                        "Resource": "*",
                    }
                ],
            }
        ),
    )

    zip_bytes = _create_lambda_zip(KNOWLEDGE_BASE_LAMBDA_TEMPLATE)

    # Create or update Lambda with environment variables
    try:
        resp = lambda_client.create_function(
            FunctionName=fn_name,
            Runtime="python3.13",
            Role=role_arn,
            Handler="lambda_function.lambda_handler",
            Code={"ZipFile": zip_bytes},
            Description=f"KB Query tool for deployment {deployment_id}",
            Timeout=30,
            MemorySize=256,
            Environment={
                "Variables": {
                    "KNOWLEDGE_BASE_ID": kb_id,
                    "FOUNDATION_MODEL_ARN": foundation_model_arn,
                },
            },
        )
        lambda_arn = resp["FunctionArn"]
        if gateway_role_arn:
            # Bug 168: prune dangling-principal statements left by deleted gateway
            # roles before adding ours — a policy with an orphaned principal makes
            # AddPermission reject every call ("invalid principal").
            _prune_orphaned_lambda_permissions(lambda_client, fn_name)
            # IAM propagation race (Bug 149): retry on invalid-principal until the
            # freshly-created gateway role is resolvable by lambda:AddPermission.
            _last = None
            for _att in range(8):
                try:
                    lambda_client.add_permission(
                        FunctionName=fn_name,
                        StatementId="AllowAgentCoreInvoke",
                        Action="lambda:InvokeFunction",
                        Principal=gateway_role_arn,
                    )
                    _last = None
                    break
                except lambda_client.exceptions.ResourceConflictException:
                    _last = None
                    break
                except lambda_client.exceptions.InvalidParameterValueException as e:
                    if "principal" not in str(e).lower():
                        raise
                    _last = e
                    time.sleep(8)
            if _last is not None:
                raise _last
    except lambda_client.exceptions.ResourceConflictException:
        # Update existing
        lambda_client.update_function_code(FunctionName=fn_name, ZipFile=zip_bytes)
        lambda_client.update_function_configuration(
            FunctionName=fn_name,
            Environment={
                "Variables": {
                    "KNOWLEDGE_BASE_ID": kb_id,
                    "FOUNDATION_MODEL_ARN": foundation_model_arn,
                },
            },
        )
        resp = lambda_client.get_function(FunctionName=fn_name)
        lambda_arn = resp["Configuration"]["FunctionArn"]

    # Wait for Active state
    for _ in range(30):
        fn = lambda_client.get_function(FunctionName=fn_name)
        if fn["Configuration"]["State"] == "Active":
            break
        time.sleep(2)

    return lambda_arn


# ---------------------------------------------------------------------------
# Schema sanitization
# ---------------------------------------------------------------------------

# The Gateway CreateGatewayTarget API only allows these keys in JSON Schema
# property definitions. AI-generated schemas often include extras like
# "default", "enum", "examples", "format", "minimum", "maximum", etc.
_ALLOWED_SCHEMA_KEYS = {"type", "properties", "required", "items", "description"}


def _sanitize_gateway_schema(schema: dict) -> dict:
    """Recursively strip unsupported keys from a JSON Schema for the Gateway API."""
    if not isinstance(schema, dict):
        return schema

    cleaned = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            # Recurse into each property definition
            cleaned["properties"] = {
                prop_name: _sanitize_gateway_schema(prop_def) for prop_name, prop_def in value.items()
            }
        elif key == "items" and isinstance(value, dict):
            cleaned["items"] = _sanitize_gateway_schema(value)
        elif key in _ALLOWED_SCHEMA_KEYS:
            cleaned[key] = value
        # else: drop the unsupported key (default, enum, format, etc.)

    return cleaned


# ---------------------------------------------------------------------------
# Lambda creation helpers
# ---------------------------------------------------------------------------


def _create_lambda_zip(code: str) -> bytes:
    """Create an in-memory zip file containing a single lambda_function.py."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lambda_function.py", code)
    buf.seek(0)
    return buf.read()


def _ensure_lambda_role(iam_client, role_name: str, description: str) -> str:
    """Create or reuse an IAM role for a Lambda function. Returns the role ARN."""
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        resp = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description=description,
        )
        role_arn = resp["Role"]["Arn"]
        iam_client.attach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
        logger.info("Created IAM role: %s", role_arn)
        time.sleep(10)
    except iam_client.exceptions.EntityAlreadyExistsException:
        role_arn = iam_client.get_role(RoleName=role_name)["Role"]["Arn"]
        logger.info("Reusing existing IAM role: %s", role_arn)
    return role_arn


def _wait_lambda_updatable(lambda_client, function_name: str, timeout: int = 90) -> None:
    """Block until *function_name* is in a state that accepts an update.

    A Lambda mid-create/mid-update has State=Pending or LastUpdateStatus=InProgress;
    update_function_code/configuration then throws ResourceConflictException. We
    poll until State=Active AND LastUpdateStatus != InProgress so concurrent
    gateway deploys serialize cleanly on the shared singleton tool Lambda.
    """
    import time as _t

    deadline = _t.time() + timeout
    while _t.time() < deadline:
        try:
            cfg = lambda_client.get_function(FunctionName=function_name)["Configuration"]
        except Exception:  # noqa: BLE001
            return
        state = cfg.get("State", "Active")
        last = cfg.get("LastUpdateStatus", "Successful")
        if state == "Active" and last != "InProgress":
            return
        _t.sleep(3)


def _prune_orphaned_lambda_permissions(lambda_client, function_name: str) -> int:
    """Remove resource-policy statements whose principal IAM role is gone (Bug 168).

    A shared tool Lambda accumulates one statement per gateway role. When a prior
    gateway's role is deleted on teardown, its statement lingers with a dangling
    principal. A policy holding a dangling principal makes lambda:AddPermission
    reject EVERY subsequent call with "The provided principal was invalid" — which
    bricks all future gateway deploys that reuse this Lambda. We read the policy,
    and for each ``AllowAgentCoreInvoke-<role>`` statement whose role no longer
    exists in IAM, remove that statement. Returns the number pruned. Best-effort:
    any error is swallowed (the caller still attempts its add + retry).
    """
    try:
        pol_raw = lambda_client.get_policy(FunctionName=function_name).get("Policy")
    except Exception as e:  # noqa: BLE001
        # ResourceNotFound => the function simply has no resource policy yet:
        # nothing to prune, genuinely benign.
        if is_error(e, "ResourceNotFoundException", "ResourceNotFound"):
            return 0
        # Anything else (notably AccessDenied when the caller role lacks
        # lambda:GetPolicy) means the prune is INERT — it can never remove a
        # dangling principal, so the reused-Lambda AddPermission failure would
        # silently return. Surface it loudly (Defect A) instead of swallowing.
        logger.warning(
            "Orphan-permission prune could not read the policy of %s (%s); "
            "dangling gateway-role principals will NOT be cleaned — check that "
            "the deploy role has lambda:GetPolicy on function:AgentCore*",
            function_name,
            type(e).__name__,
        )
        return 0
    try:
        statements = json.loads(pol_raw).get("Statement", []) or []
    except Exception:  # noqa: BLE001
        return 0

    iam = _create_iam_client()
    pruned = 0
    for st in statements:
        sid = st.get("Sid") or ""
        # Only touch the per-gateway-role invoke grants we manage.
        if not sid.startswith("AllowAgentCoreInvoke-"):
            continue
        role_name = sid[len("AllowAgentCoreInvoke-") :]
        if not role_name:
            continue
        try:
            iam.get_role(RoleName=role_name)
            continue  # role still exists — keep the statement
        except Exception as e:  # noqa: BLE001
            if not is_error(e, "NoSuchEntity", "NoSuchEntityException"):
                # Unknown IAM error — don't risk removing a valid grant.
                logger.debug("get_role(%s) failed with a non-NoSuchEntity error; keeping statement", role_name)
                continue
        # Role is gone: remove the dangling statement.
        try:
            lambda_client.remove_permission(FunctionName=function_name, StatementId=sid)
            pruned += 1
            logger.info(
                "Pruned orphaned invoke permission %s from %s (role deleted)",
                sid,
                function_name,
            )
        except Exception:  # noqa: BLE001 — best-effort by contract (caller retries its add)
            logger.debug("Could not prune permission %s from %s", sid, function_name, exc_info=True)
    return pruned


def _create_or_update_lambda(
    lambda_client,
    function_name: str,
    role_arn: str,
    zip_bytes: bytes,
    description: str,
    gateway_role_arn: str | None = None,
) -> str:
    """Create or update a Lambda function. Returns the function ARN.

    These tool Lambdas (AgentCoreCustomerSupportTools / AgentCoreDynamicTools) are
    SHARED SINGLETONS reused across every gateway deploy. Each gateway has its OWN
    execution role (AgentCoreGateway-<gatewayId>), and the gateway invokes the
    Lambda using that role — so the Lambda's resource policy MUST grant
    lambda:InvokeFunction to EVERY gateway role that uses it, not just the first
    one that created the function.

    Bug 134/stability: previously the invoke permission was added ONLY on the
    create path. The 2nd+ gateway hit ResourceConflictException (function exists),
    updated the code, and NEVER added its own role to the policy — so its
    gateway could not invoke the Lambda, the gateway served 0 tools over MCP, and
    the agent's tools/list came up empty (the "works on run #1, 0 tools on run
    #2/#3" flake — same target config, different gateway role missing from the
    Lambda policy). Fix: ALWAYS add the per-gateway-role permission (unique
    StatementId per role), on both create and reuse paths.
    """
    try:
        resp = lambda_client.create_function(
            FunctionName=function_name,
            Runtime="python3.13",
            Role=role_arn,
            Handler="lambda_function.lambda_handler",
            Code={"ZipFile": zip_bytes},
            Description=description,
            Timeout=30,
            MemorySize=256,
        )
        lambda_arn = resp["FunctionArn"]
    except lambda_client.exceptions.ResourceConflictException:
        # The shared singleton tool Lambda (AgentCoreDynamicTools etc.) already
        # exists. Concurrent deploys race here: if ANOTHER deploy is mid-update the
        # function is in Pending/InProgress and update_function_code throws
        # ResourceConflictException ("resource ... is currently in the following
        # state: Pending"). Wait for it to settle, then retry the update. Verified
        # live: two parallel gateway deploys collided on AgentCoreDynamicTools.
        _wait_lambda_updatable(lambda_client, function_name)
        for _attempt in range(8):
            try:
                lambda_client.update_function_code(FunctionName=function_name, ZipFile=zip_bytes)
                break
            except lambda_client.exceptions.ResourceConflictException:
                _wait_lambda_updatable(lambda_client, function_name)
                time.sleep(3)
        resp = lambda_client.get_function(FunctionName=function_name)
        lambda_arn = resp["Configuration"]["FunctionArn"]

    # ALWAYS grant the invoking gateway role (idempotent, per-role StatementId) so
    # a shared Lambda reused by a NEW gateway still authorizes that gateway.
    if gateway_role_arn:
        # Bug 168 (caught live 2026-06-25): this tool Lambda is SHARED by name
        # across deployments and accumulates one resource-policy statement per
        # gateway role. When a prior gateway's role is later DELETED (teardown),
        # its statement is left behind referencing a now-deleted role — AWS
        # stores it as an orphaned unique principal id (AROA...). A resource
        # policy carrying a dangling principal makes lambda:AddPermission reject
        # EVERY subsequent call with "The provided principal was invalid" (even a
        # valid account/role/service principal — proven live on a fresh fn the
        # same call succeeds). So before adding our statement, PRUNE any existing
        # statement whose principal role no longer exists. This unbricks the
        # shared Lambda's policy under create/delete churn (the real cause of
        # "no tool targets could be deployed", mis-attributed to Bug 149).
        _prune_orphaned_lambda_permissions(lambda_client, function_name)
        # StatementId must be unique per principal + match ^[A-Za-z0-9-_]+$.
        role_name = gateway_role_arn.rsplit("/", 1)[-1]
        stmt_id = re.sub(r"[^A-Za-z0-9_-]", "-", f"AllowAgentCoreInvoke-{role_name}")[:100]
        # IAM propagation race (Bug 149): a freshly-created gateway role may not yet
        # be visible to lambda:AddPermission, which validates the principal exists and
        # rejects with InvalidParameterValueException "The provided principal was
        # invalid." The fixed 10s post-create sleep is variable and often
        # insufficient under create/delete churn (this passed early in a run then
        # began failing). Retry with backoff so the principal becomes resolvable.
        last_exc = None
        for attempt in range(8):
            try:
                lambda_client.add_permission(
                    FunctionName=function_name,
                    StatementId=stmt_id,
                    Action="lambda:InvokeFunction",
                    Principal=gateway_role_arn,
                )
                logger.info("Granted %s invoke on %s", role_name, function_name)
                last_exc = None
                break
            except lambda_client.exceptions.ResourceConflictException:
                last_exc = None
                break  # this gateway role is already permitted — fine
            except lambda_client.exceptions.InvalidParameterValueException as e:
                if "principal" not in str(e).lower():
                    raise
                last_exc = e
                logger.warning(
                    "add_permission principal not yet propagated (attempt %d/8): %s",
                    attempt + 1,
                    str(e)[:160],
                )
                time.sleep(8)
        if last_exc is not None:
            raise last_exc

    for _ in range(30):
        fn = lambda_client.get_function(FunctionName=function_name)
        if fn["Configuration"]["State"] == "Active":
            break
        time.sleep(2)

    return lambda_arn


def create_dynamic_gateway_lambda(region: str, gateway_role_arn: str) -> str:
    """Create a Lambda function with dynamic tools for the MCP gateway."""
    iam_client = _create_iam_client()
    lambda_client = _create_lambda_client(region)
    role_arn = _ensure_lambda_role(
        iam_client,
        "AgentCoreDynamicToolsLambdaRole",
        "Role for AgentCore Dynamic Tools Lambda",
    )
    zip_bytes = _create_lambda_zip(DYNAMIC_TOOLS_LAMBDA_CODE)
    return _create_or_update_lambda(
        lambda_client,
        "AgentCoreDynamicTools",
        role_arn,
        zip_bytes,
        "Dynamic tools for AgentCore Gateway",
        gateway_role_arn,
    )


def create_customer_support_lambda(region: str, gateway_role_arn: str) -> str:
    """Create a Lambda function with customer support tools for the MCP gateway."""
    iam_client = _create_iam_client()
    lambda_client = _create_lambda_client(region)
    role_arn = _ensure_lambda_role(
        iam_client,
        "AgentCoreCustomerSupportLambdaRole",
        "Role for AgentCore Customer Support Lambda",
    )
    zip_bytes = _create_lambda_zip(CUSTOMER_SUPPORT_LAMBDA_CODE)
    return _create_or_update_lambda(
        lambda_client,
        "AgentCoreCustomerSupportTools",
        role_arn,
        zip_bytes,
        "Customer Support tools for AgentCore Gateway",
        gateway_role_arn,
    )


# ---------------------------------------------------------------------------
# Cognito OAuth setup (pure boto3, replaces starter toolkit)
# ---------------------------------------------------------------------------


def _create_cognito_oauth(cognito_client, gateway_name: str, region: str) -> dict:
    """Create Cognito User Pool + App Client for gateway OAuth.

    Returns dict with authorizer_config and client_info.
    """
    pool_name = f"AgentCore-{gateway_name}"

    # Create User Pool
    pool_resp = cognito_client.create_user_pool(
        PoolName=pool_name,
        AutoVerifiedAttributes=[],
        UsernameAttributes=["email"],
        Policies={
            "PasswordPolicy": {
                "MinimumLength": 8,
                "RequireUppercase": True,
                "RequireLowercase": True,
                "RequireNumbers": True,
                "RequireSymbols": False,
            }
        },
    )
    user_pool_id = pool_resp["UserPool"]["Id"]
    logger.info("Created Cognito User Pool: %s", user_pool_id)

    # Create resource server for scoped access
    resource_id = f"agentcore-{gateway_name}"
    scope_name = "invoke"
    try:
        cognito_client.create_resource_server(
            UserPoolId=user_pool_id,
            Identifier=resource_id,
            Name=f"AgentCore Gateway {gateway_name}",
            Scopes=[{"ScopeName": scope_name, "ScopeDescription": "Invoke gateway"}],
        )
    except Exception as e:
        logger.warning("Resource server creation: %s", e)

    # Create domain for token endpoint
    domain_name = f"agentcore-{gateway_name}-{user_pool_id.split('_')[-1][:8]}".lower()
    domain_name = re.sub(r"[^a-z0-9-]", "-", domain_name)[:63]
    try:
        cognito_client.create_user_pool_domain(
            Domain=domain_name,
            UserPoolId=user_pool_id,
        )
    except Exception as e:
        logger.warning("Domain creation: %s", e)

    # Create App Client with client_credentials grant
    full_scope = f"{resource_id}/{scope_name}"
    client_resp = cognito_client.create_user_pool_client(
        UserPoolId=user_pool_id,
        ClientName=f"{gateway_name}-client",
        GenerateSecret=True,
        AllowedOAuthFlowsUserPoolClient=True,
        AllowedOAuthFlows=["client_credentials"],
        AllowedOAuthScopes=[full_scope],
        SupportedIdentityProviders=["COGNITO"],
    )
    client_id = client_resp["UserPoolClient"]["ClientId"]
    client_secret = client_resp["UserPoolClient"]["ClientSecret"]

    token_endpoint = f"https://{domain_name}.auth.{region}.amazoncognito.com/oauth2/token"

    authorizer_config = {
        "customJWTAuthorizer": {
            "discoveryUrl": f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/openid-configuration",
            "allowedClients": [client_id],
        }
    }

    client_info = {
        "user_pool_id": user_pool_id,
        "client_id": client_id,
        "client_secret": client_secret,
        "token_endpoint": token_endpoint,
        "scope": full_scope,
    }

    return {
        "authorizer_config": authorizer_config,
        "client_info": client_info,
    }


# ---------------------------------------------------------------------------
# Cognito token helper
# ---------------------------------------------------------------------------


def _create_external_oauth_config(identity_config: dict, region: str) -> dict:
    """Create authorizer config for external IDP (Okta, Azure AD, Auth0, custom OIDC).

    No Cognito resources are created. Uses the user-provided discovery URL and credentials.
    Returns dict with authorizer_config and client_info.
    """
    provider = identity_config.get("provider", "custom")
    client_id = identity_config.get("clientId", identity_config.get("client_id", ""))
    client_secret = identity_config.get("clientSecretRef", identity_config.get("client_secret_ref", ""))
    discovery_url = identity_config.get("discoveryUrl", identity_config.get("discovery_url", ""))
    scopes = identity_config.get("scopes", [])
    audience = identity_config.get("audience", "")

    # Derive token_endpoint from discovery document.
    # The URL came from operator-supplied identity_config; validate it before any
    # DNS-aware HTTP call so we can't be tricked into hitting the IMDS endpoint
    # (169.254.169.254), Lambda credentials endpoint (169.254.170.2), or any
    # private-network host. See _validate_discovery_url() for the policy.
    token_endpoint = ""
    if discovery_url:
        import urllib.request

        # Raises _DiscoveryUrlInvalid / _DiscoveryUrlBlocked (both ValueError) on
        # any policy violation. We deliberately do NOT swallow these — a half-
        # configured gateway with a bad discovery_url is worse than a failed deploy.
        validated_url = _validate_discovery_url(discovery_url)

        req = urllib.request.Request(validated_url, headers={"Accept": "application/json"})
        try:
            # Strict 10s timeout so an attacker can't burn CPU by stalling us on
            # the actual fetch. The host has been validated above; the residual
            # DNS-rebinding race window is bounded by this timeout.
            with (
                urllib.request.urlopen(req, timeout=10) as resp
            ):  # nosemgrep: dynamic-urllib-use-detected -- URL validated by _validate_discovery_url (scheme=https + IP denylist + optional allowlist)
                discovery_doc = json.loads(resp.read().decode())
                token_endpoint = discovery_doc.get("token_endpoint", "")
        except Exception as e:
            # Re-raise: a transient discovery-doc fetch failure must fail the
            # deploy, not silently produce a gateway with empty token_endpoint.
            raise RuntimeError(f"Failed to fetch OIDC discovery document from {validated_url}: {e}") from e

    authorizer_config = {
        "customJWTAuthorizer": {
            "discoveryUrl": discovery_url,
            "allowedClients": [client_id] if client_id else [],
        }
    }
    if audience:
        authorizer_config["customJWTAuthorizer"]["allowedAudiences"] = [audience]

    client_info = {
        "provider": provider,
        "client_id": client_id,
        "client_secret": client_secret,
        "token_endpoint": token_endpoint,
        "scope": " ".join(scopes) if scopes else "",
        "discovery_url": discovery_url,
    }

    return {
        "authorizer_config": authorizer_config,
        "client_info": client_info,
    }


def get_cognito_token(client_info: dict) -> str:
    """Get OAuth access token using client_credentials grant. Supports Cognito and external IDPs."""
    import urllib.parse

    import urllib3

    client_id = client_info.get("client_id", "")
    client_secret = client_info.get("client_secret", "")
    token_endpoint = client_info.get("token_endpoint", "")
    scope = client_info.get("scope", "")

    if not client_id or not token_endpoint:
        raise RuntimeError("Missing client_id or token_endpoint in gateway config")

    try:
        import certifi

        http = urllib3.PoolManager(ca_certs=certifi.where())
    except ImportError:
        http = urllib3.PoolManager()

    form_data = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
        }
    )

    resp = http.request(
        "POST",
        token_endpoint,
        body=form_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10.0,
    )

    if resp.status == 200:
        return json.loads(resp.data.decode())["access_token"]
    raise RuntimeError(f"Token request failed: {resp.status} {resp.data.decode()}")


def _count_served_tools(gateway_url: str, client_info: dict) -> int:
    """Return how many tools the gateway ACTUALLY serves over MCP tools/list.

    Bug 134: a Lambda gateway target can reach status=READY with a fully
    configured inlinePayload, yet the gateway's MCP plane serves an EMPTY tool
    list (a service-side propagation flake confirmed live — identical configs,
    one gateway serves tools forever, another serves 0 forever). The ONLY
    client-observable truth is the gateway's own tools/list. We probe it with the
    gateway's M2M token (the same path the agent uses) so deploy-time readiness
    means "the agent will see tools", not just "status READY". Returns -1 on a
    transport/auth error (caller treats as not-yet-ready, keeps polling).
    """
    import urllib3

    try:
        import certifi

        http = urllib3.PoolManager(ca_certs=certifi.where())
    except ImportError:
        http = urllib3.PoolManager()
    try:
        token = get_cognito_token(client_info)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        resp = http.request(
            "POST",
            gateway_url,
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=20.0,
        )
        if resp.status != 200:
            return -1
        text = resp.data.decode()
        # streamable-http may wrap the JSON in SSE "data: " frames
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    tools = obj.get("result", {}).get("tools")
                    if tools is not None:
                        return len(tools)
                except Exception:  # noqa: BLE001
                    continue
        try:
            return len(json.loads(text).get("result", {}).get("tools", []))
        except Exception:  # noqa: BLE001
            return -1
    except Exception as e:  # noqa: BLE001
        logger.info("tools/list probe error (will retry): %s", str(e)[:120])
        return -1


def _qualified_tools_from_served(gateway_url: str, client_info: dict) -> list:
    """Return the gateway's served tool names from a live MCP tools/list probe.

    Used for connector (OpenAPI) targets, whose tools are crawled rather than
    declared inline — the control plane reports 0 configured tools, so the served
    plane is the only source of the fully-qualified action names.
    """
    import urllib3

    try:
        import certifi

        http = urllib3.PoolManager(ca_certs=certifi.where())
    except ImportError:
        http = urllib3.PoolManager()
    try:
        token = get_cognito_token(client_info)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        resp = http.request(
            "POST",
            gateway_url,
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=20.0,
        )
        if resp.status != 200:
            return []
        text = resp.data.decode()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    tools = obj.get("result", {}).get("tools")
                    if tools is not None:
                        return [t.get("name") for t in tools if t.get("name")]
                except Exception:  # noqa: BLE001
                    continue
        try:
            tools = json.loads(text).get("result", {}).get("tools", [])
            return [t.get("name") for t in tools if t.get("name")]
        except Exception:  # noqa: BLE001
            return []
    except Exception as e:  # noqa: BLE001
        logger.info("qualified-tools probe error: %s", str(e)[:120])
        return []


def _wait_for_gateway_to_serve_tools(gateway_url: str, client_info: dict, expected: int, timeout: int = 90) -> int:
    """Poll the gateway's MCP tools/list until it serves >= 1 tool (ideally
    `expected`), or *timeout*. Returns the served count (0 if it never serves).
    This is the authoritative deploy-time readiness signal — it matches exactly
    what the deployed agent will discover.
    """
    import time as _t

    deadline = _t.time() + timeout
    served = 0
    while _t.time() < deadline:
        served = _count_served_tools(gateway_url, client_info)
        if served >= expected and expected > 0:
            logger.warning("Gateway serves %d/%d tools over MCP", served, expected)
            return served
        if served > 0:
            logger.warning("Gateway serves %d tools over MCP (expected %d)", served, expected)
        _t.sleep(8)
    logger.warning("Gateway served %d/%d tools within %ds", max(served, 0), expected, timeout)
    return max(served, 0)


# ---------------------------------------------------------------------------
# JWT auth configuration
# ---------------------------------------------------------------------------


def configure_jwt_auth(runtime_id: str, gateway_config: dict, region: str) -> dict:
    """Configure JWT auth on a deployed runtime for header forwarding."""
    client_info = gateway_config.get("client_info", {})
    provider = client_info.get("provider", "cognito")
    client_id = client_info.get("client_id", "")

    if provider == "cognito" or not provider:
        user_pool_id = client_info.get("user_pool_id", "")
        if not user_pool_id or not client_id:
            return {"success": False, "error": "Missing user_pool_id or client_id"}
        discovery_url = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/openid-configuration"
    else:
        # External IDP: use provided discovery URL
        discovery_url = client_info.get("discovery_url", "")
        if not discovery_url or not client_id:
            return {
                "success": False,
                "error": "Missing discovery_url or client_id for external IDP",
            }

    try:
        agentcore_client = _create_agentcore_control_client(region)
        get_resp = agentcore_client.get_agent_runtime(agentRuntimeId=runtime_id)

        authorizer_config = {
            "customJWTAuthorizer": {
                "discoveryUrl": discovery_url,
                "allowedClients": [client_id],
            }
        }
        if client_info.get("audience"):
            authorizer_config["customJWTAuthorizer"]["allowedAudiences"] = [client_info["audience"]]

        update_params = {
            "agentRuntimeId": runtime_id,
            "agentRuntimeArtifact": get_resp.get("agentRuntimeArtifact", {}),
            "roleArn": get_resp.get("roleArn", ""),
            "networkConfiguration": get_resp.get("networkConfiguration", {}),
            "protocolConfiguration": get_resp.get("protocolConfiguration", {"serverProtocol": "HTTP"}),
            "requestHeaderConfiguration": {"requestHeaderAllowlist": ["Authorization"]},
            "authorizerConfiguration": authorizer_config,
        }
        env_vars = get_resp.get("environmentVariables")
        if env_vars:
            update_params["environmentVariables"] = env_vars

        agentcore_client.update_agent_runtime(**update_params)

        for _ in range(30):
            time.sleep(10)
            status_resp = agentcore_client.get_agent_runtime(agentRuntimeId=runtime_id)
            status = status_resp.get("status", "")
            if status in ("READY", "ACTIVE"):
                break
            if "FAILED" in status:
                return {
                    "success": False,
                    "error": f"Runtime entered {status} after JWT update",
                }

        return {"success": True, "message": "JWT auth configured on runtime"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Gateway deployment (pure boto3)
# ---------------------------------------------------------------------------


def _cleanup_old_cognito_pool(gw_detail: dict, cognito_client) -> None:
    """Extract the old Cognito user pool ID from a gateway's authorizer config and delete it."""
    try:
        auth_cfg = gw_detail.get("authorizerConfiguration", {})
        jwt_cfg = auth_cfg.get("customJWTAuthorizer", {})
        discovery_url = jwt_cfg.get("discoveryUrl", "")
        # Format: https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/...
        # Parse with urlparse and validate the host EXACTLY (netloc) rather than a
        # substring/endswith check on the raw URL — a substring like "amazonaws.com"
        # can appear at an arbitrary position (py/incomplete-url-substring-sanitization).
        from urllib.parse import urlparse as _urlparse

        _parsed = _urlparse(discovery_url)
        _host = _parsed.hostname or ""
        if _host.startswith("cognito-idp.") and _host.endswith(".amazonaws.com"):
            # path is /{pool_id}/.well-known/... — pool_id is the first segment.
            _segments = [s for s in _parsed.path.split("/") if s]
            old_pool_id = _segments[0] if _segments else ""
            if old_pool_id and "_" in old_pool_id:
                pool_detail = cognito_client.describe_user_pool(UserPoolId=old_pool_id)
                domain = pool_detail.get("UserPool", {}).get("Domain")
                if domain:
                    cognito_client.delete_user_pool_domain(UserPoolId=old_pool_id, Domain=domain)
                cognito_client.delete_user_pool(UserPoolId=old_pool_id)
                logger.info("Cleaned up old Cognito pool: %s", old_pool_id)
    except Exception as e:
        logger.warning("Could not clean up old Cognito pool: %s", e)


def _wait_for_gateway(agentcore_ctrl, gateway_id: str, timeout: int = 120) -> dict:
    """Poll until gateway is READY or timeout."""
    for _ in range(timeout // 5):
        gw = agentcore_ctrl.get_gateway(gatewayIdentifier=gateway_id)
        status = gw.get("status", "")
        if status == "READY":
            return gw
        if "FAILED" in status:
            raise RuntimeError(f"Gateway entered {status}")
        time.sleep(5)
    raise RuntimeError(f"Gateway {gateway_id} did not become READY in {timeout}s")


def _create_gateway_target_with_retry(
    agentcore_ctrl,
    gateway_id: str,
    target_name: str,
    create_params: dict,
    max_retries: int = 5,
) -> dict | None:
    """Create a gateway target with retry logic. Reuses existing target on conflict."""
    for attempt in range(max_retries):
        try:
            target = agentcore_ctrl.create_gateway_target(**create_params)
            logger.info("Gateway target created: %s", target.get("targetId"))
            for _ in range(30):
                t = agentcore_ctrl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target["targetId"])
                if t.get("status") == "READY":
                    break
                time.sleep(2)
            return target
        except Exception as e:
            err_str = str(e)
            # If the target already exists, look it up and reuse it.
            # "already exists" message fallback kept: conflicts can surface as a
            # ValidationException whose message says "already exists".
            if is_error(e, "ConflictException") or "already exists" in err_str:
                logger.info("Gateway target '%s' already exists, reusing", target_name)
                try:
                    targets_resp = agentcore_ctrl.list_gateway_targets(gatewayIdentifier=gateway_id)
                    for t in _get_targets_from_response(targets_resp):
                        if t.get("name") == target_name:
                            logger.info("Reusing existing target: %s", t.get("targetId"))
                            return t
                except Exception as list_err:
                    logger.warning("Could not list targets: %s", list_err)
                # If we couldn't find it, just return None (non-fatal)
                return None
            elif "not ready" in err_str.lower() and attempt < max_retries - 1:
                time.sleep(10 * (attempt + 1))
            else:
                raise
    return None


def _build_gateway_role_policy() -> dict:
    """Build the IAM policy document for gateway roles with scoped permissions."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:*",
                    "agent-credential-provider:*",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:ListFoundationModels",
                    "bedrock:GetFoundationModel",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": "arn:aws:iam::*:role/AgentCore*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "secretsmanager:CreateSecret",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:DeleteSecret",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": "lambda:InvokeFunction",
                "Resource": "arn:aws:lambda:*:*:function:AgentCore*",
            },
        ],
    }


def _fetch_openapi_spec(spec_url: str, allowlist_hosts: list | None = None) -> str:
    """Fetch a connector's OpenAPI spec from *spec_url* and return it as a string.

    The URL is validated (https-only, private/IMDS denylist, connector allowlist)
    before any network call. Caller passes the result to the Gateway target as
    ``openApiSchema.inlinePayload``.
    """
    import urllib.request

    validated = _validate_outbound_url(spec_url, tuple(allowlist_hosts) if allowlist_hosts else None)
    req = urllib.request.Request(validated, headers={"Accept": "application/json, application/yaml, text/yaml"})
    with (
        urllib.request.urlopen(req, timeout=30) as resp
    ):  # nosemgrep: dynamic-urllib-use-detected -- URL validated by _validate_outbound_url (https + IP denylist + host allowlist)
        return resp.read().decode("utf-8", errors="replace")


# AgentCore CreateGatewayTarget caps the inline openApiSchema payload (the API
# rejects very large inline specs). Real SaaS specs blow past it (GitHub ~12MB,
# Asana ~3MB, Slack ~1.2MB), so anything over this threshold is staged to S3 and
# referenced via openApiSchema.s3.uri instead of inlinePayload. 100KB is a safe,
# conservative inline ceiling.
_MAX_INLINE_SPEC_BYTES = 100 * 1024
# AgentCore ALSO caps the S3-staged spec object at 10 MB ("The provided S3 object
# exceeds the maximum allowed size of 10 MB"). GitHub's published OpenAPI is ~12.5
# MB (all variants > 10 MB), so staging alone isn't enough — we slim the spec
# (drop description/examples/docs, which the gateway crawler doesn't need to emit
# tools) when it approaches the cap. Keep a safety margin below 10 MB.
_MAX_S3_SPEC_BYTES = 10 * 1024 * 1024
_S3_SPEC_SLIM_TARGET = int(9.5 * 1024 * 1024)


def _slim_openapi_spec(spec_str: str) -> str:
    """Strip non-essential, size-heavy fields from an OpenAPI spec so it fits the
    AgentCore 10 MB target-spec cap, WITHOUT dropping any operations/tools AND
    WITHOUT producing an invalid spec.

    Removes ``example``, ``examples``, ``externalDocs`` and vendor ``x-*``
    extensions recursively — pure documentation/samples the gateway crawler does
    not need to expose operations as tools.

    Bug 185b (caught live): an earlier version also stripped ``description``,
    which broke validation because the OpenAPI spec REQUIRES ``description`` on
    Response Objects (``components.responses.*`` / ``responses.<code>``). The
    gateway rejected the slimmed GitHub spec with "attribute
    components.responses.<x>.description is missing" and served 0 tools. So
    descriptions are now PRESERVED. On GitHub this still drops ~12.5MB -> ~4.6MB,
    comfortably under the cap. Best-effort: returns the original on parse failure.
    """
    try:
        spec = json.loads(spec_str)
    except Exception:  # noqa: BLE001
        return spec_str

    def _is_x_ext(key: str) -> bool:
        return isinstance(key, str) and key.startswith("x-")

    _DROP = {"example", "examples", "externalDocs"}

    def prune(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in _DROP or _is_x_ext(k):
                    continue
                out[k] = prune(v)
            return out
        if isinstance(node, list):
            return [prune(i) for i in node]
        return node

    slimmed = prune(spec)
    return json.dumps(slimmed, separators=(",", ":"))


# AgentCore's gateway OpenAPI crawler only accepts these request/response media
# types; any other (GitHub uses application/scim+json, application/vnd.github.*,
# text/html, application/octet-stream, ...) is rejected with "MediaType <x> is not
# supported in response" -> the target fails validation and serves 0 tools.
_GATEWAY_SUPPORTED_MEDIA_TYPES = {
    "application/json",
    "application/xml",
    "multipart/form-data",
    "application/x-www-form-urlencoded",
}


def _sanitize_openapi_for_gateway(spec_str: str) -> str:
    """Drop ``content`` media types the AgentCore gateway does not support, so a
    real-world SaaS spec (GitHub) validates instead of failing with 100+
    "MediaType ... is not supported" errors and serving 0 tools (Bug 189b).

    Only prunes the *media-type keys* inside ``content`` objects (request bodies +
    responses); operations, parameters, and schemas are untouched. A ``content``
    that becomes empty is removed entirely (a Response with no content is valid;
    it still needs its required ``description``, which is left intact). Best-effort:
    returns the original on parse failure.
    """
    try:
        spec = json.loads(spec_str)
    except Exception:  # noqa: BLE001
        return spec_str

    changed = False

    def walk(node):
        nonlocal changed
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "content" and isinstance(v, dict):
                    kept = {mt: walk(mv) for mt, mv in v.items() if mt in _GATEWAY_SUPPORTED_MEDIA_TYPES}
                    if len(kept) != len(v):
                        changed = True
                    # Drop an empty content map so the parent (response/requestBody)
                    # stays valid without unsupported-only content.
                    if kept:
                        out[k] = kept
                    continue
                out[k] = walk(v)
            # A requestBody REQUIRES `content`; if sanitizing removed all of its
            # media types the requestBody is now invalid ("requestBody.content is
            # missing"). Signal removal so the operation drops the whole
            # requestBody (the operation stays valid — body just becomes optional).
            if "requestBody" in out and isinstance(out["requestBody"], dict) and "content" not in out["requestBody"]:
                del out["requestBody"]
                changed = True
            return out
        if isinstance(node, list):
            return [walk(i) for i in node]
        return node

    result = walk(spec)

    # Bug 189c — the gateway crawler also rejects operations whose request/response
    # SCHEMAS use ``oneOf`` ("schema with oneOf is currently not supported"). These
    # can't be auto-rewritten without changing semantics, so DROP just those
    # operations (GitHub: ~30 of 1194) rather than failing the whole connector. The
    # vast majority of operations remain usable.
    paths = result.get("paths")
    if isinstance(paths, dict):
        _METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}

        def _uses_oneof(node) -> bool:
            if isinstance(node, dict):
                if "oneOf" in node:
                    return True
                return any(_uses_oneof(v) for v in node.values())
            if isinstance(node, list):
                return any(_uses_oneof(i) for i in node)
            return False

        dropped_ops = 0
        for path, item in list(paths.items()):
            if not isinstance(item, dict):
                continue
            for method in list(item.keys()):
                if method.lower() in _METHODS and _uses_oneof(item[method]):
                    del item[method]
                    dropped_ops += 1
                    changed = True
            # Remove a path that has no operations left.
            if not any(m.lower() in _METHODS for m in item):
                del paths[path]
        if dropped_ops:
            logger.info("Dropped %d operation(s) using unsupported 'oneOf' schemas", dropped_ops)

        # Bug 191 — the gateway derives each tool name as
        # ``<target>___<operationId>`` and Bedrock Converse requires tool names to
        # match ``[a-zA-Z0-9_-]+`` and be <= 64 chars. GitHub's operationIds ALL
        # contain '/' (e.g. "meta/root", "actions/get-...-for-enterprise") and
        # many exceed the budget, so EVERY invoke fails with a ValidationException
        # ("toolSpec.name failed to satisfy constraint"). Rewrite each operationId
        # to a compliant, de-duplicated slug (<=44 chars, leaving room for the
        # ~16-char target prefix + 4 padding under the 64 cap).
        _seen_ids: set = set()
        _OPID_MAX = 44

        def _slug(op_id: str) -> str:
            s = re.sub(r"[^a-zA-Z0-9_-]", "_", op_id)
            if len(s) > _OPID_MAX:
                s = s[:_OPID_MAX]
            base = s or "op"
            cand = base
            i = 1
            while cand in _seen_ids:
                suffix = f"_{i}"
                cand = base[: _OPID_MAX - len(suffix)] + suffix
                i += 1
            _seen_ids.add(cand)
            return cand

        renamed = 0
        for item in paths.values():
            if not isinstance(item, dict):
                continue
            for method, op in item.items():
                if method.lower() in _METHODS and isinstance(op, dict):
                    oid = op.get("operationId")
                    if isinstance(oid, str):
                        new_oid = _slug(oid)
                        if new_oid != oid:
                            op["operationId"] = new_oid
                            renamed += 1
                            changed = True
        if renamed:
            logger.info("Rewrote %d operationId(s) to satisfy Bedrock tool-name constraints", renamed)

    # Only re-serialize when we actually dropped something — otherwise return the
    # original string verbatim (preserves formatting + avoids needless rewrites).
    if not changed:
        return spec_str
    return json.dumps(result, separators=(",", ":"))


# The gateway tool-plane cannot materialize an unbounded number of operations: a
# very large OpenAPI target (GitHub ~1145 ops) syncs 0 tools and the deploy fails
# the "serves N tools" gate. Cap the operation count so the gateway can actually
# serve a usable subset. The agent further narrows this to MAX_GATEWAY_TOOLS at
# invoke time; this cap is the gateway-side ceiling. Override via
# MAX_CONNECTOR_OPERATIONS.
_MAX_CONNECTOR_OPERATIONS = int(os.environ.get("MAX_CONNECTOR_OPERATIONS", "80"))


def _cap_openapi_operations(spec_str: str, *, max_ops: int) -> str:
    """Keep at most *max_ops* operations in an OpenAPI spec (deterministic by path
    then method), pruning the rest so the gateway can materialize its tool plane.

    Components/schemas are left intact (operations may $ref them). Best-effort:
    returns the original on parse failure or when already under the cap.
    """
    try:
        spec = json.loads(spec_str)
    except Exception:  # noqa: BLE001
        return spec_str
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return spec_str
    _METHODS = ("get", "post", "put", "delete", "patch", "head", "options", "trace")
    total = sum(1 for _p, item in paths.items() if isinstance(item, dict) for m in item if m.lower() in _METHODS)
    if total <= max_ops:
        return spec_str
    kept = 0
    new_paths: dict = {}
    for path in sorted(paths.keys()):
        item = paths[path]
        if not isinstance(item, dict):
            continue
        new_item = {}
        for key, val in item.items():
            if key.lower() in _METHODS:
                if kept < max_ops:
                    new_item[key] = val
                    kept += 1
                # else drop this operation
            else:
                new_item[key] = val  # path-level params, etc.
        # only keep the path if it retained >=1 operation
        if any(k.lower() in _METHODS for k in new_item):
            new_paths[path] = new_item
    spec["paths"] = new_paths
    logger.info("Capped connector spec operations %d -> %d (gateway tool-plane limit)", total, kept)
    return json.dumps(spec, separators=(",", ":"))


def _build_openapi_schema(spec_str: str, *, connector_id: str, region: str) -> dict:
    """Return the gateway ``openApiSchema`` block, inlining small specs and
    staging large ones to the artifacts S3 bucket (``s3.uri``)."""
    # Always strip media types the gateway can't crawl (Bug 189b) — applies to
    # inline AND staged specs. Only rewrites the string if something changed.
    _san = _sanitize_openapi_for_gateway(spec_str)
    if _san != spec_str:
        logger.info("Sanitized connector '%s' spec (dropped unsupported media types)", _safe_log_token(connector_id))
        spec_str = _san

    # Cap operation count so the gateway can materialize its tool plane (Bug 189d).
    _capped = _cap_openapi_operations(spec_str, max_ops=_MAX_CONNECTOR_OPERATIONS)
    if _capped != spec_str:
        spec_str = _capped

    if len(spec_str.encode("utf-8")) <= _MAX_INLINE_SPEC_BYTES:
        return {"inlinePayload": spec_str}

    # Slim oversized specs so the S3 object fits AgentCore's 10 MB target cap.
    if len(spec_str.encode("utf-8")) > _S3_SPEC_SLIM_TARGET:
        slim = _slim_openapi_spec(spec_str)
        before, after = len(spec_str.encode("utf-8")), len(slim.encode("utf-8"))
        if after < before:
            logger.info(
                "Slimmed connector '%s' spec %d -> %d bytes to fit the 10 MB cap",
                connector_id,
                before,
                after,
            )
            spec_str = slim

    bucket = os.environ.get("ARTIFACTS_BUCKET_NAME", "")
    if not bucket:
        # No artifacts bucket wired (e.g. unit context) — fall back to inline and
        # let the API surface the size error rather than silently dropping tools.
        logger.warning(
            "Spec for connector '%s' is %d bytes (>inline cap) but ARTIFACTS_BUCKET_NAME "
            "is unset; falling back to inlinePayload (may fail).",
            connector_id,
            len(spec_str),
        )
        return {"inlinePayload": spec_str}

    import uuid as _uuid

    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", connector_id or "generic")[:32]
    key = f"connector-specs/{safe}/{_uuid.uuid4().hex[:12]}.json"
    boto3.client("s3", region_name=region).put_object(
        Bucket=bucket,
        Key=key,
        Body=spec_str.encode("utf-8"),
        ContentType="application/json",
    )
    account_id = os.environ.get("AWS_ACCOUNT_ID", "")
    s3_block: dict = {"uri": f"s3://{bucket}/{key}"}
    if account_id:
        s3_block["bucketOwnerAccountId"] = account_id
    logger.info("Staged connector '%s' spec (%d bytes) to s3://%s/%s", connector_id, len(spec_str), bucket, key)
    return {"s3": s3_block}


def _delete_connector_credential_provider(agentcore_ctrl, entry: str) -> tuple[bool, str]:
    """Delete one connector credential provider. Returns (deleted, message).

    *entry* is "TYPE:name" (TYPE in {API_KEY, OAUTH}) for providers created by the
    current code, or a bare "name" for older persisted records. The TYPE prefix is
    REQUIRED for correctness: delete_oauth2_credential_provider on an API_KEY
    provider returns success WITHOUT deleting it (verified live), so a bare name is
    handled by trying a deleter and VERIFYING the provider is actually gone before
    declaring success.
    """
    if ":" in entry:
        ptype, name = entry.split(":", 1)
    else:
        ptype, name = "", entry

    def _is_gone(get_fn) -> bool:
        try:
            get_fn(name=name)
            return False
        except Exception as e:  # noqa: BLE001
            return is_error(e, "ResourceNotFoundException", "NotFoundException")

    if ptype == "API_KEY":
        try:
            agentcore_ctrl.delete_api_key_credential_provider(name=name)
            return True, f"API_KEY credential provider {name} deleted"
        except Exception as e:  # noqa: BLE001
            if is_error(e, "ResourceNotFoundException", "NotFoundException"):
                return True, f"Credential provider {name} already gone"
            return False, f"API_KEY provider {name} delete error: {e}"
    if ptype == "OAUTH":
        try:
            agentcore_ctrl.delete_oauth2_credential_provider(name=name)
            return True, f"OAUTH credential provider {name} deleted"
        except Exception as e:  # noqa: BLE001
            if is_error(e, "ResourceNotFoundException", "NotFoundException"):
                return True, f"Credential provider {name} already gone"
            return False, f"OAUTH provider {name} delete error: {e}"

    # Untyped (legacy): try BOTH, but verify the matching get reports gone.
    for deleter, getter in (
        ("delete_api_key_credential_provider", "get_api_key_credential_provider"),
        ("delete_oauth2_credential_provider", "get_oauth2_credential_provider"),
    ):
        try:
            getattr(agentcore_ctrl, deleter)(name=name)
        except Exception as e:  # noqa: BLE001
            if is_error(e, "ResourceNotFoundException", "NotFoundException"):
                continue
        # Verify the provider of THIS type is actually gone.
        if _is_gone(getattr(agentcore_ctrl, getter)):
            return True, f"Credential provider {name} deleted"
    return False, f"Credential provider {name} could not be confirmed deleted"


def _deploy_connector_targets(
    agentcore_ctrl,
    gateway_id: str,
    region: str,
    connectors: list[dict],
    owner_sub: str = "",
) -> dict:
    """Deploy SaaS connectors as OpenAPI Gateway targets with credential providers.

    Each connector dict carries: connector_id, auth_method
    ("api_key"|"oauth2_cc"), EITHER secret_arn (already minted) OR secret_value
    (raw — minted here and never returned), spec_url/spec_inline, scopes,
    credential_location/parameter_name/prefix, oauth_vendor, discovery_url.

    Returns {"credential_provider_names": [...], "secret_arns": [...]} so teardown
    can delete everything created here. On a mid-loop failure, partial resources
    are rolled back (best-effort) before re-raising.
    """
    created_providers: list[str] = []
    created_secrets: list[str] = []
    created_spec_s3_uris: list[str] = []

    def _rollback_partial() -> None:
        """Best-effort delete of providers/secrets/specs created before a mid-loop failure.

        On a failed connector deploy the gateway_result is never persisted, so the
        caller cannot tear these down later — roll back here to avoid orphaning a
        credential provider or a Secrets Manager secret holding a raw credential.
        """
        for entry in created_providers:
            _delete_connector_credential_provider(agentcore_ctrl, entry)
        if created_secrets:
            sm = _create_secrets_client(region)
            for _sidx, sarn in enumerate(created_secrets):
                try:
                    sm.delete_secret(SecretId=sarn, ForceDeleteWithoutRecovery=True)
                except Exception:  # noqa: BLE001 — best-effort rollback
                    # Log NOTHING from the secret-bearing scope (no ARN/value):
                    # CodeQL py/clear-text-logging taints created_secrets; a
                    # positional index is enough to correlate with the create log.
                    logger.warning("Rollback: could not delete connector secret #%d", _sidx)
        for uri in created_spec_s3_uris:
            _delete_spec_s3_object(uri, region)

    try:
        _deploy_connector_targets_inner(
            agentcore_ctrl,
            gateway_id,
            region,
            connectors,
            owner_sub,
            created_providers,
            created_secrets,
            created_spec_s3_uris,
        )
    except Exception:
        logger.error("Connector deploy failed mid-loop; rolling back partial resources")
        _rollback_partial()
        raise

    return {
        "credential_provider_names": created_providers,
        "secret_arns": created_secrets,
        "spec_s3_uris": created_spec_s3_uris,
    }


def _delete_spec_s3_object(uri: str, region: str) -> None:
    """Best-effort delete of an s3://bucket/key connector-spec object."""
    if not uri.startswith("s3://"):
        return
    try:
        rest = uri[len("s3://") :]
        bucket, _, key = rest.partition("/")
        if bucket and key:
            boto3.client("s3", region_name=region).delete_object(Bucket=bucket, Key=key)
    except Exception:  # noqa: BLE001 — best-effort by contract (docstring)
        logger.debug("Could not delete connector spec object %s", _safe_log_token(uri), exc_info=True)


def _deploy_connector_targets_inner(
    agentcore_ctrl,
    gateway_id: str,
    region: str,
    connectors: list[dict],
    owner_sub: str,
    created_providers: list,
    created_secrets: list,
    created_spec_s3_uris: list,
) -> None:
    """Per-connector deploy loop. Accumulates created provider names + secret ARNs
    into the caller's lists so a mid-loop failure can be rolled back."""
    from app.services.connectors import (
        AUTH_API_KEY,
        AUTH_OAUTH2_CC,
        get_connector,
        oauth_vendor_for,
    )

    for idx, conn in enumerate(connectors or []):
        connector_id = conn.get("connector_id") or conn.get("connectorId") or ""
        auth_method = conn.get("auth_method") or conn.get("authMethod") or AUTH_API_KEY
        catalog = get_connector(connector_id) or {}

        # Resolve the spec: explicit inline > explicit url > catalog default url.
        spec_inline = conn.get("spec_inline") or conn.get("specContent")
        spec_url = conn.get("spec_url") or conn.get("specUrl") or catalog.get("spec_url")
        # The SPEC-FETCH allowlist is the host the OpenAPI doc is downloaded from
        # (e.g. raw.githubusercontent.com), which is DIFFERENT from the API host
        # allowlist (catalog['allowlist_hosts'], e.g. app.asana.com). Use the
        # catalog's spec_host_allowlist when present; for a catalog DEFAULT spec_url
        # (vendor-vetted by us) fall back to that URL's own host so the built-in
        # connectors always fetch. A user-supplied custom spec_url with no
        # spec_host_allowlist is still SSRF-guarded (https + private-IP denylist via
        # _validate_outbound_url) even with no host allowlist.
        from urllib.parse import urlparse as _urlparse

        spec_allowlist = conn.get("spec_host_allowlist") or catalog.get("spec_host_allowlist")
        if not spec_allowlist and spec_url and spec_url == catalog.get("spec_url"):
            _h = _urlparse(spec_url).hostname
            spec_allowlist = [_h] if _h else None
        if not spec_inline:
            if not spec_url:
                raise RuntimeError(f"Connector '{connector_id}' has no OpenAPI spec (provide spec_url or spec_inline)")
            spec_inline = _fetch_openapi_spec(spec_url, spec_allowlist)

        # Mint the secret here if the caller passed a raw value (direct-deploy path);
        # the SFN path mints earlier and passes secret_arn. Never echo the raw value.
        secret_arn = conn.get("secret_arn") or conn.get("secretArn") or ""
        raw_secret = conn.get("secret_value") or conn.get("secretValue")
        if not secret_arn and raw_secret:
            payload_key = "clientSecret" if auth_method == AUTH_OAUTH2_CC else "apiKey"
            secret_arn = _put_connector_secret(region, owner_sub, {payload_key: raw_secret})
        # Track every secret this connector CONSUMES (minted here OR supplied by the
        # SFN path) so teardown deletes it. Without this the SFN-minted secret holding
        # the raw API key / OAuth client secret would be orphaned on delete.
        if secret_arn and secret_arn not in created_secrets:
            created_secrets.append(secret_arn)

        safe_conn = re.sub(r"[^a-zA-Z0-9-]", "-", connector_id or "generic")[:24]
        provider_name = f"acc-{safe_conn}-{idx}"
        target_name = f"conn-{safe_conn}-{idx}"[:48]
        # Record the provider as "TYPE:name" so teardown calls the CORRECT deleter.
        # (Verified live: delete_oauth2_credential_provider on an API_KEY provider
        # returns success WITHOUT deleting it — trial-and-error delete silently
        # orphans the provider. The type prefix removes the guesswork.)
        provider_type = "OAUTH" if auth_method == AUTH_OAUTH2_CC else "API_KEY"

        if auth_method == AUTH_OAUTH2_CC:
            vendor = (
                conn.get("oauth_vendor") or conn.get("oauthVendor") or oauth_vendor_for(connector_id) or "CustomOauth2"
            )
            discovery_url = conn.get("discovery_url") or conn.get("discoveryUrl")
            # Phase 3 (Loom) OBO — carry delegation mode + grant type from the
            # connector payload so the credential provider is minted for
            # on-behalf-of token exchange when requested.
            delegation_mode = conn.get("delegation_mode") or conn.get("delegationMode") or "m2m"
            obo_grant_type = conn.get("obo_grant_type") or conn.get("oboGrantType")
            provider_arn = _ensure_oauth2_credential_provider(
                agentcore_ctrl,
                provider_name,
                vendor=vendor,
                client_id=conn.get("client_id") or conn.get("clientId") or "",
                client_secret_arn=secret_arn,
                discovery_url=discovery_url,
                delegation_mode=delegation_mode,
                obo_grant_type=obo_grant_type,
            )
            # OBO fix (Loom-study 0.3): the credential PROVIDER is minted for
            # on-behalf-of exchange when delegation_mode=obo, but the target's
            # oauthCredentialProvider previously ALWAYS requested CLIENT_CREDENTIALS
            # — so the downstream call ran as the shared M2M identity, never as the
            # end user. The OAuthCredentialProvider.grantType enum is
            # {CLIENT_CREDENTIALS, AUTHORIZATION_CODE, TOKEN_EXCHANGE}; OBO must
            # request TOKEN_EXCHANGE so AgentCore Identity performs the RFC 8693
            # exchange and the downstream token carries the user's identity+scopes.
            _target_grant = "TOKEN_EXCHANGE" if str(delegation_mode).lower() == "obo" else "CLIENT_CREDENTIALS"
            cred_cfg = {
                "credentialProviderType": "OAUTH",
                "credentialProvider": {
                    "oauthCredentialProvider": {
                        "providerArn": provider_arn,
                        "scopes": conn.get("scopes") or catalog.get("default_scopes") or [],
                        "grantType": _target_grant,
                    }
                },
            }
        else:  # API_KEY
            if not secret_arn:
                raise RuntimeError(f"Connector '{connector_id}' api_key auth requires a secret_arn or secret_value")
            provider_arn = _ensure_api_key_credential_provider(agentcore_ctrl, provider_name, secret_arn=secret_arn)
            cred_cfg = {
                "credentialProviderType": "API_KEY",
                "credentialProvider": {
                    "apiKeyCredentialProvider": {
                        "providerArn": provider_arn,
                        "credentialParameterName": conn.get("credential_parameter_name")
                        or conn.get("credentialParameterName")
                        or catalog.get("credential_parameter_name")
                        or "Authorization",
                        "credentialLocation": conn.get("credential_location")
                        or conn.get("credentialLocation")
                        or catalog.get("credential_location")
                        or "HEADER",
                    }
                },
            }
            prefix = (
                conn.get("credential_prefix")
                if conn.get("credential_prefix") is not None
                else (
                    conn.get("credentialPrefix")
                    if conn.get("credentialPrefix") is not None
                    else catalog.get("credential_prefix")
                )
            )
            if prefix:
                cred_cfg["credentialProvider"]["apiKeyCredentialProvider"]["credentialPrefix"] = prefix

        created_providers.append(f"{provider_type}:{provider_name}")

        openapi_schema = _build_openapi_schema(spec_inline, connector_id=connector_id or "generic", region=region)
        # If the spec was staged to S3, remember the key so teardown deletes it.
        _s3_uri = openapi_schema.get("s3", {}).get("uri", "")
        if _s3_uri:
            created_spec_s3_uris.append(_s3_uri)
        create_params = {
            "gatewayIdentifier": gateway_id,
            "name": target_name,
            "targetConfiguration": {"mcp": {"openApiSchema": openapi_schema}},
            "credentialProviderConfigurations": [cred_cfg],
        }
        _create_gateway_target_with_retry(agentcore_ctrl, gateway_id, target_name, create_params)
        logger.info(
            "Connector '%s' deployed as OpenAPI gateway target %s",
            _safe_log_token(connector_id),
            _safe_log_token(target_name),
        )


def deploy_gateway(
    gateway_config: dict,
    region: str,
    template_id: str | None = None,
    gateway_tools: list | None = None,
    identity_config: dict | None = None,
    custom_tools: list[dict] | None = None,
    mcp_server_runtime_arn: str | None = None,
    mcp_oauth: dict | None = None,
    knowledge_base_result: dict | None = None,
    deployment_id: str | None = None,
    gateway_retry: int = 0,
    connectors: list[dict] | None = None,
    external_mcp_servers: list[dict] | None = None,
    owner_sub: str = "",
) -> dict:
    """Deploy a Gateway using pure boto3 APIs.

    Args:
        gateway_config: Gateway configuration dict with ``name``.
        region: AWS region.
        template_id: Optional template identifier.
        gateway_tools: Tool IDs to deploy as Lambda targets.
        identity_config: Optional identity provider config (for external IDPs).
        custom_tools: Optional list of AI-generated custom tool definitions.

    Returns:
        Dict with ``success``, ``gateway_url``, ``gateway_id``, ``gateway_name``,
        ``client_info``, ``lambda_function_name``.
    """
    try:
        gateway_tools = gateway_tools or []
        custom_tools = custom_tools or []
        connectors = connectors or []
        external_mcp_servers = external_mcp_servers or []
        agentcore_ctrl = _create_agentcore_control_client(region)
        cognito_client = _create_cognito_client(region)

        raw_name = gateway_config.get("name", "AgentCoreGateway")
        gateway_name = re.sub(r"[^a-zA-Z0-9-]", "-", raw_name)[:48]
        if not gateway_name or not gateway_name[0].isalnum():
            gateway_name = "gw-" + gateway_name

        # Step 1: Create authorizer (Cognito or external IDP)
        identity_config = identity_config or {}
        # Treat empty credentials as "auto-create Cognito" (e.g. template 3 sends empty clientId)
        if identity_config and not (identity_config.get("clientId") or identity_config.get("client_id") or "").strip():
            identity_config = {}
        provider = identity_config.get("provider", "cognito")
        if provider and provider != "cognito":
            logger.info(
                "Creating external %s authorizer for gateway '%s'",
                provider,
                gateway_name,
            )
            cognito_response = _create_external_oauth_config(identity_config, region)
        else:
            logger.info("Creating Cognito authorizer for gateway '%s'", gateway_name)
            cognito_response = _create_cognito_oauth(cognito_client, gateway_name, region)

        # Step 1b: Create gateway IAM role
        iam_client = _create_iam_client()
        gw_role_name = f"AgentCoreGateway-{gateway_name}"
        gw_trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
        try:
            role_resp = iam_client.create_role(
                RoleName=gw_role_name,
                AssumeRolePolicyDocument=json.dumps(gw_trust_policy),
                Description=f"Gateway role for {gateway_name}",
            )
            gw_role_arn = role_resp["Role"]["Arn"]
            # SECURITY: Scope Lambda invoke to AgentCore-prefixed functions only,
            # and limit bedrock-agentcore actions to gateway-specific operations.
            sts_client = boto3.client("sts")
            sts_client.get_caller_identity()  # validate credentials
            _gw_policy_doc = _build_gateway_role_policy()
            iam_client.put_role_policy(
                RoleName=gw_role_name,
                PolicyName="GatewayLambdaInvoke",
                PolicyDocument=json.dumps(_gw_policy_doc),
            )
            time.sleep(10)
        except iam_client.exceptions.EntityAlreadyExistsException:
            gw_role_arn = iam_client.get_role(RoleName=gw_role_name)["Role"]["Arn"]
            # Update the policy to ensure it has latest permissions (critical for MCP patterns)
            _gw_policy_doc = _build_gateway_role_policy()
            iam_client.put_role_policy(
                RoleName=gw_role_name,
                PolicyName="GatewayLambdaInvoke",
                PolicyDocument=json.dumps(_gw_policy_doc),
            )
            logger.info("Updated gateway role %s with latest permissions", gw_role_name)

        # Step 2: Create or reuse gateway
        gateway = None
        try:
            gw_resp = agentcore_ctrl.create_gateway(
                name=gateway_name,
                roleArn=gw_role_arn,
                protocolType="MCP",
                authorizerType="CUSTOM_JWT",
                authorizerConfiguration=cognito_response["authorizer_config"],
            )
            gateway = {
                "gatewayId": gw_resp["gatewayId"],
                "gatewayUrl": gw_resp.get("gatewayUrl", ""),
                "gatewayArn": gw_resp.get("gatewayArn", ""),
                "roleArn": gw_resp.get("roleArn", ""),
            }
            logger.info("Created gateway: %s", gateway["gatewayId"])
            # Wait for gateway to be ready
            gw_ready = _wait_for_gateway(agentcore_ctrl, gateway["gatewayId"])
            gateway["gatewayUrl"] = gw_ready.get("gatewayUrl", gateway["gatewayUrl"])
            gateway["roleArn"] = gw_ready.get("roleArn", gateway["roleArn"])
        except Exception as create_err:
            err_str = str(create_err)
            # "already exists" message fallback kept (ValidationException shape).
            if is_error(create_err, "ConflictException") or "already exists" in err_str:
                logger.info("Gateway '%s' already exists, looking up", gateway_name)
                for gw in _list_all_gateways(agentcore_ctrl):
                    if gw.get("name") == gateway_name:
                        gw_id = gw["gatewayId"]
                        gw_detail = agentcore_ctrl.get_gateway(gatewayIdentifier=gw_id)
                        gateway = {
                            "gatewayId": gw_id,
                            "gatewayUrl": gw_detail.get("gatewayUrl", ""),
                            "gatewayArn": gw_detail.get("gatewayArn", ""),
                            "roleArn": gw_detail.get("roleArn", gw.get("roleArn", "")),
                        }
                        # Clean up old Cognito pool before updating with new one
                        _cleanup_old_cognito_pool(gw_detail, cognito_client)
                        # Update authorizer config to match new Cognito user pool
                        try:
                            agentcore_ctrl.update_gateway(
                                gatewayIdentifier=gw_id,
                                name=gw_detail.get("name", gateway_name),
                                roleArn=gw_detail.get("roleArn", gw_role_arn),
                                protocolType=gw_detail.get("protocolType", "MCP"),
                                authorizerType="CUSTOM_JWT",
                                authorizerConfiguration=cognito_response["authorizer_config"],
                            )
                            logger.info("Updated gateway %s authorizer config", gw_id)
                            _wait_for_gateway(agentcore_ctrl, gw_id)
                        except Exception as update_err:
                            logger.warning("Could not update gateway authorizer: %s", update_err)
                        logger.info("Reusing gateway %s, url=%s", gw_id, gateway["gatewayUrl"])
                        break
                if gateway is None:
                    raise RuntimeError(f"Gateway '{gateway_name}' exists but not found via list") from create_err
            else:
                raise

        lambda_function_name = "AgentCoreLambdaTestFunction"

        # Step 3: Create gateway targets based on template
        # Customer support tools need their own Lambda (not DynamicTools)
        _CUSTOMER_SUPPORT_TEMPLATES = {
            "customer-support-assistant",
            "customer-support-blueprint",
        }
        _CUSTOMER_TOOL_IDS = {
            "get_order",
            "get_customer",
            "list_orders",
            "process_refund",
        }
        _has_customer_tools = bool(set(gateway_tools or []) & _CUSTOMER_TOOL_IDS)

        if template_id in _CUSTOMER_SUPPORT_TEMPLATES or _has_customer_tools:
            custom_lambda_arn = create_customer_support_lambda(region, gateway.get("roleArn", ""))
            lambda_function_name = "AgentCoreCustomerSupportTools"
            create_params = {
                "gatewayIdentifier": gateway["gatewayId"],
                "name": "CustomerSupportTools",
                "targetConfiguration": {
                    "mcp": {
                        "lambda": {
                            "lambdaArn": custom_lambda_arn,
                            "toolSchema": CUSTOMER_SUPPORT_TOOLS_SCHEMA,
                        }
                    }
                },
                "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
            }
            _create_gateway_target_with_retry(
                agentcore_ctrl,
                gateway["gatewayId"],
                "CustomerSupportTools",
                create_params,
            )

        elif template_id == "strands-gateway-agent" or gateway_tools:
            # Deploy DynamicTools Lambda with tool schemas.
            # If gateway_tools is specified, only deploy those specific tools.
            # Otherwise (template with no specific tools), deploy all available tools.
            dynamic_lambda_arn = create_dynamic_gateway_lambda(region, gateway.get("roleArn", ""))
            lambda_function_name = "AgentCoreDynamicTools"
            if gateway_tools:
                schemas = [GATEWAY_TOOL_SCHEMAS[tid] for tid in gateway_tools if tid in GATEWAY_TOOL_SCHEMAS]
            else:
                schemas = list(GATEWAY_TOOL_SCHEMAS.values())
            # Only create DynamicTools target if we have valid schemas (custom tool IDs won't match)
            if schemas:
                create_params = {
                    "gatewayIdentifier": gateway["gatewayId"],
                    "name": "DynamicTools",
                    "targetConfiguration": {
                        "mcp": {
                            "lambda": {
                                "lambdaArn": dynamic_lambda_arn,
                                "toolSchema": {"inlinePayload": schemas},
                            }
                        }
                    },
                    "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
                }
                _create_gateway_target_with_retry(agentcore_ctrl, gateway["gatewayId"], "DynamicTools", create_params)
            else:
                logger.info(
                    "No predefined tool schemas matched gateway_tools=%s, skipping DynamicTools target",
                    gateway_tools,
                )

        # Step 3b: Create MCP Server Runtime target (if provided)
        if mcp_server_runtime_arn:
            from urllib.parse import quote

            # Build HTTPS endpoint URL from ARN
            encoded_arn = quote(mcp_server_runtime_arn, safe="")
            mcp_endpoint_url = (
                f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
            )
            logger.info(
                "Creating MCP Server Runtime target: %s -> %s",
                mcp_server_runtime_arn,
                mcp_endpoint_url,
            )

            # MCP targets require OAUTH credential provider.
            # mcp_oauth contains Cognito credentials created by the MCP server step
            # (same pool used for the runtime's JWT authorizer).
            if not mcp_oauth:
                raise RuntimeError("mcp_oauth credentials required for MCP server target")

            mcp_discovery_url = mcp_oauth["discovery_url"]
            mcp_client_id = mcp_oauth["client_id"]
            mcp_client_secret = mcp_oauth["client_secret"]
            mcp_full_scope = mcp_oauth["scope"]

            # Register OAuth2 credential provider with AgentCore
            cred_provider_resp = agentcore_ctrl.create_oauth2_credential_provider(
                name=f"mcp-cred-{gateway_name}",
                credentialProviderVendor="CustomOauth2",
                oauth2ProviderConfigInput={
                    "customOauth2ProviderConfig": {
                        "oauthDiscovery": {
                            "discoveryUrl": mcp_discovery_url,
                        },
                        "clientId": mcp_client_id,
                        "clientSecret": mcp_client_secret,
                    }
                },
            )
            mcp_cred_provider_arn = cred_provider_resp["credentialProviderArn"]
            logger.info(
                "Created OAuth2 credential provider: %s", mcp_cred_provider_arn
            )  # nosemgrep: python-logger-credential-disclosure -- logs resource ARN, not secret

            mcp_target_params = {
                "gatewayIdentifier": gateway["gatewayId"],
                "name": "MCPServerRuntime",
                "targetConfiguration": {
                    "mcp": {
                        "mcpServer": {
                            "endpoint": mcp_endpoint_url,
                        }
                    }
                },
                "credentialProviderConfigurations": [
                    {
                        "credentialProviderType": "OAUTH",
                        "credentialProvider": {
                            "oauthCredentialProvider": {
                                "providerArn": mcp_cred_provider_arn,
                                "scopes": [mcp_full_scope],
                            }
                        },
                    }
                ],
            }
            _create_gateway_target_with_retry(
                agentcore_ctrl,
                gateway["gatewayId"],
                "MCPServerRuntime",
                mcp_target_params,
            )
            logger.info("MCP Server Runtime target created, waiting for target to become READY...")

            # MCP targets often fail initially due to IAM propagation delay.
            # Poll status and retry with update if FAILED.
            gw_id = gateway["gatewayId"]
            target_ready = False
            for attempt in range(8):
                time.sleep(15)
                targets_resp = agentcore_ctrl.list_gateway_targets(gatewayIdentifier=gw_id)
                targets_list = _get_targets_from_response(targets_resp)
                mcp_target = next(
                    (t for t in targets_list if t.get("name") == "MCPServerRuntime"),
                    None,
                )
                if not mcp_target:
                    logger.info(
                        "MCP target not found yet (attempt %d), raw keys: %s",
                        attempt + 1,
                        list(targets_resp.keys()),
                    )
                    continue
                tid = mcp_target.get("targetId", "")
                status = mcp_target.get("status", "")
                logger.info("MCP target status (attempt %d): %s", attempt + 1, status)
                if status == "READY":
                    target_ready = True
                    break
                if status in ("FAILED", "UPDATE_UNSUCCESSFUL") and tid:
                    logger.info("Retrying MCP target via update (attempt %d)...", attempt + 1)
                    try:
                        agentcore_ctrl.update_gateway_target(
                            gatewayIdentifier=gw_id,
                            targetId=tid,
                            name="MCPServerRuntime",
                            targetConfiguration=mcp_target_params["targetConfiguration"],
                            credentialProviderConfigurations=mcp_target_params["credentialProviderConfigurations"],
                        )
                    except Exception as update_err:
                        logger.warning(
                            "MCP target update failed (attempt %d): %s",
                            attempt + 1,
                            update_err,
                        )
            # Final check after all retries
            if not target_ready:
                time.sleep(20)
                final_resp = agentcore_ctrl.list_gateway_targets(gatewayIdentifier=gw_id)
                final_targets = _get_targets_from_response(final_resp)
                mcp_final = next(
                    (t for t in final_targets if t.get("name") == "MCPServerRuntime"),
                    None,
                )
                if mcp_final and mcp_final.get("status") == "READY":
                    target_ready = True
                    logger.info("MCP target reached READY on final check")
                else:
                    final_status = mcp_final.get("status", "NOT_FOUND") if mcp_final else "NOT_FOUND"
                    logger.warning(
                        "MCP target did not reach READY after retries (final status: %s), proceeding anyway",
                        final_status,
                    )

            lambda_function_name = "MCPServerRuntime"
            logger.info("MCP Server Runtime target created for gateway %s", gw_id)

        # Step 4: Deploy custom AI-generated tools as individual Gateway Targets
        custom_tool_lambdas = []
        custom_tool_roles = []
        for custom_tool in custom_tools:
            tool_name = custom_tool.get("toolName", custom_tool.get("tool_name", ""))
            lambda_code = custom_tool.get("lambdaCode", custom_tool.get("lambda_code", ""))
            description = custom_tool.get("description", "")
            input_schema = custom_tool.get("inputSchema", custom_tool.get("input_schema", {}))

            if not tool_name or not lambda_code:
                continue

            # SECURITY: Validate custom tool code before deploying to Lambda.
            # The tool_tester validates during testing, but users can skip testing
            # and deploy directly. This prevents arbitrary code execution.
            from app.services.tool_tester import _validate_code_safety

            is_safe, safety_error = _validate_code_safety(lambda_code)
            if not is_safe:
                logger.warning("Skipping unsafe custom tool '%s': %s", tool_name, safety_error)
                continue

            safe_name = re.sub(r"[^a-zA-Z0-9-]", "-", tool_name)[:48]
            fn_name = f"AgentCore-CustomTool-{safe_name}"
            try:
                iam_client_ct = _create_iam_client()
                role_arn_ct = _ensure_lambda_role(
                    iam_client_ct,
                    f"AgentCoreCustomToolRole-{safe_name}",
                    f"Role for custom tool {tool_name}",
                )
                zip_bytes_ct = _create_lambda_zip(lambda_code)
                custom_lambda_arn = _create_or_update_lambda(
                    _create_lambda_client(region),
                    fn_name,
                    role_arn_ct,
                    zip_bytes_ct,
                    f"AI-generated tool: {tool_name}",
                    gateway.get("roleArn", ""),
                )
                custom_tool_lambdas.append(fn_name)
                custom_tool_roles.append(f"AgentCoreCustomToolRole-{safe_name}")

                # Sanitize inputSchema — the Gateway API only allows specific keys
                # in property definitions. AI-generated schemas often include extra
                # keys like "default", "enum", "examples" that cause validation errors.
                sanitized_schema = _sanitize_gateway_schema(
                    input_schema if input_schema else {"type": "object", "properties": {}}
                )

                # Gateway returns tool names as "{TargetName}___{ToolName}" to Bedrock.
                # Bedrock Converse API has a 64-char limit on tool names.
                # Compute max target name length: 64 - 3 ("___") - len(tool_name)
                max_target_len = 64 - 3 - len(tool_name)
                if max_target_len < 3:
                    max_target_len = 3  # absolute minimum
                target_name = f"CT-{safe_name}"[:max_target_len]

                tool_schema = {
                    "inlinePayload": [
                        {
                            "name": tool_name,
                            "description": description,
                            "inputSchema": sanitized_schema,
                        }
                    ]
                }
                ct_params = {
                    "gatewayIdentifier": gateway["gatewayId"],
                    "name": target_name,
                    "targetConfiguration": {
                        "mcp": {
                            "lambda": {
                                "lambdaArn": custom_lambda_arn,
                                "toolSchema": tool_schema,
                            }
                        }
                    },
                    "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
                }
                _create_gateway_target_with_retry(agentcore_ctrl, gateway["gatewayId"], target_name, ct_params)
                logger.info("Custom tool '%s' deployed as gateway target", tool_name)
            except Exception as ct_err:
                logger.error("Failed to deploy custom tool '%s': %s", tool_name, ct_err)

        # Deploy Knowledge Base tool Lambda if KB was configured
        kb_lambda_name = ""
        if knowledge_base_result and knowledge_base_result.get("kb_id"):
            try:
                kb_id = knowledge_base_result["kb_id"]
                kb_model_arn = knowledge_base_result.get("foundation_model_arn", "")
                dep_id = deployment_id or "unknown"
                kb_lambda_arn = create_knowledge_base_lambda(
                    region,
                    gateway.get("roleArn", ""),
                    kb_id,
                    kb_model_arn,
                    dep_id,
                )
                kb_lambda_name = f"AgentCore-KBTool-{dep_id[:8]}"
                kb_schema = GATEWAY_TOOL_SCHEMAS["knowledge_base"]
                kb_target_name = f"KBTool-{dep_id[:8]}"
                kb_target_params = {
                    "gatewayIdentifier": gateway["gatewayId"],
                    "name": kb_target_name,
                    "targetConfiguration": {
                        "mcp": {
                            "lambda": {
                                "lambdaArn": kb_lambda_arn,
                                "toolSchema": {"inlinePayload": [kb_schema]},
                            }
                        }
                    },
                    "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
                }
                _create_gateway_target_with_retry(
                    agentcore_ctrl, gateway["gatewayId"], kb_target_name, kb_target_params
                )
                logger.info("Knowledge Base tool deployed as gateway target: %s", kb_target_name)
            except Exception as kb_err:
                logger.error("Failed to deploy KB tool: %s", kb_err)

        # Step 4c: Deploy SaaS connectors as OpenAPI gateway targets (with their
        # API-key / OAuth2 credential providers). Capture provider + secret refs so
        # teardown can delete them.
        connector_credential_providers: list[str] = []
        connector_secret_arns: list[str] = []
        connector_spec_s3_uris: list[str] = []
        if connectors:
            conn_result = _deploy_connector_targets(
                agentcore_ctrl,
                gateway["gatewayId"],
                region,
                connectors,
                owner_sub=owner_sub,
            )
            connector_credential_providers = conn_result["credential_provider_names"]
            connector_secret_arns = conn_result["secret_arns"]
            connector_spec_s3_uris = conn_result.get("spec_s3_uris", [])

        # Step 4d: Wire external MCP catalog servers as `mcpServer` gateway targets
        # (Tier 1 no-auth / Tier 2 API-key / Tier 3 OAuth-CC / SigV4). Their
        # credential providers + secrets are captured for teardown alongside the
        # connector refs (same cleanup path).
        mcp_credential_providers: list[str] = []
        mcp_secret_arns: list[str] = []
        if external_mcp_servers:
            mcp_result = _deploy_external_mcp_targets(
                agentcore_ctrl,
                gateway["gatewayId"],
                region,
                external_mcp_servers,
                owner_sub=owner_sub,
            )
            mcp_credential_providers = mcp_result["credential_provider_names"]
            mcp_secret_arns = mcp_result["secret_arns"]
            # Fold into the connector teardown refs so DELETE cleans them up.
            connector_credential_providers = connector_credential_providers + mcp_credential_providers
            connector_secret_arns = connector_secret_arns + mcp_secret_arns

        # Step 4e: Deploy explicit gateway_config.targets of the NON-MCP families
        # (openapi / lambda / smithy) as individual gateway targets on THIS same
        # gateway. This is what lets a user wire e.g. two MCP servers + a Lambda +
        # an OpenAPI spec to one gateway node: the mcp_server entries flow through
        # external_mcp_servers above, and the rest flow here. Each gets a unique
        # name. mcp_server entries present in `targets` are skipped by the helper
        # (already handled) to avoid double-deploy.
        config_targets = gateway_config.get("targets") or []
        _config_target_families = {(t or {}).get("type") for t in config_targets}
        _has_crawled_config_target = bool(_config_target_families & {"openapi", "smithy"})
        if config_targets:
            _deploy_config_targets(
                agentcore_ctrl,
                gateway["gatewayId"],
                region,
                config_targets,
                gateway_role_arn=gateway.get("roleArn", ""),
            )

        # Step 5: Synchronize NON-LAMBDA targets (OpenAPI / external MCP) so their
        # tools are crawled into the servable MCP plane. Bug 134:
        # synchronize_gateway_targets REQUIRES a targetIdList (the old call omitted
        # it → silent failure) AND rejects LAMBDA targets ("Target type LAMBDA is
        # not supported for synchronization") — Lambda targets serve their inline
        # tools directly. Connector (OpenAPI) targets MUST be crawled regardless of
        # the semantic-search toggle, so we always sync the non-lambda set whenever
        # any crawled target exists (or semantic search was explicitly requested).
        if (
            connectors
            or external_mcp_servers
            or mcp_server_runtime_arn
            or _has_crawled_config_target
            or gateway_config.get("semanticSearchEnabled")
        ):
            try:
                _sync_ids = []
                for _t in _get_targets_from_response(
                    agentcore_ctrl.list_gateway_targets(gatewayIdentifier=gateway["gatewayId"])
                ):
                    _tid = _t.get("targetId") or _t.get("gatewayTargetId")
                    _tc = (_t.get("targetConfiguration", {}) or {}).get("mcp", {}) or {}
                    # crawled targets = NOT lambda (openApiSchema / mcpServer)
                    if _tid and "lambda" not in _tc:
                        _sync_ids.append(_tid)
                if _sync_ids:
                    logger.warning(
                        "Synchronizing %d non-lambda target(s) on gateway %s", len(_sync_ids), gateway["gatewayId"]
                    )
                    agentcore_ctrl.synchronize_gateway_targets(
                        gatewayIdentifier=gateway["gatewayId"], targetIdList=_sync_ids
                    )
            except Exception as sync_err:
                logger.warning("Gateway target sync (non-fatal): %s", sync_err)

        # Bug 138: if the caller asked for tools (built-in and/or custom) but the
        # gateway ended up with ZERO targets, the agent would deploy against an
        # empty gateway and break at first invocation ("returned 0 tools ...
        # gateway wiring is broken"). This happens when an AI-generated spec
        # passes an unknown built-in toolId (no schema match → DynamicTools target
        # skipped) or every custom tool failed validation. FAIL LOUDLY here with a
        # message the user can act on, instead of silently shipping a dead gateway.
        tools_requested = bool(gateway_tools) or bool(custom_tools)
        if tools_requested:
            try:
                _existing_targets = _get_targets_from_response(
                    agentcore_ctrl.list_gateway_targets(gatewayIdentifier=gateway["gatewayId"])
                )
            except Exception:  # noqa: BLE001
                _existing_targets = []
            if not _existing_targets:
                _known = sorted(GATEWAY_TOOL_SCHEMAS.keys())
                _unknown = [t for t in (gateway_tools or []) if t not in GATEWAY_TOOL_SCHEMAS]
                detail = ""
                if _unknown:
                    detail = (
                        f" None of the requested tools {_unknown} are built-in "
                        f"tools (valid: {_known}). A custom tool needs lambdaCode + "
                        "an inputSchema to be deployable."
                    )
                raise RuntimeError(
                    "Gateway was created but no tool targets could be deployed, so "
                    "the agent would have no tools." + detail
                )

        # Bug 134: the policy step needs the gateway ARN + the fully-qualified
        # tool action names ("{TargetName}___{tool}") to generate schema-valid
        # Cedar. The control plane's get_gateway returns the ARN; the target
        # manifests give the tool names. Resolve them here (the gateway+targets
        # are freshly created, so this is the authoritative point) and return
        # them so policy_step doesn't have to re-query a possibly-unsynced gateway.
        gateway_arn = gateway.get("gatewayArn", "")
        if not gateway_arn:
            try:
                _gw = agentcore_ctrl.get_gateway(gatewayIdentifier=gateway["gatewayId"])
                gateway_arn = _gw.get("gatewayArn", "")
            except Exception:  # noqa: BLE001 — optional enrichment; policy_step tolerates a missing ARN
                logger.debug("Could not resolve gateway ARN for %s", gateway["gatewayId"], exc_info=True)
        qualified_tools, expected_tool_count = _resolve_gateway_tool_actions(agentcore_ctrl, gateway["gatewayId"])

        gateway_url = gateway.get("gatewayUrl", "")
        client_info = cognito_response["client_info"]

        # Connectors (OpenAPI targets) declare NO inline tools and CANNOT be synced
        # (verified live: SynchronizeGatewayTargets rejects OPEN_API_SCHEMA), so
        # _resolve_gateway_tool_actions reports expected_tool_count==0 for them even
        # though the gateway crawls the spec and DOES serve operations. The
        # control-plane is silent here, so the authoritative readiness signal is the
        # live MCP tools/list probe. Require the connector gateway to serve >=1 tool;
        # fail closed otherwise (never ship a connector gateway that serves nothing).
        if connectors and expected_tool_count == 0:
            served = _wait_for_gateway_to_serve_tools(gateway_url, client_info, expected=1, timeout=120)
            if served < 1:
                # Surface the REAL reason: a FAILED OpenAPI target carries an
                # actionable statusReason (e.g. "Invalid OpenAPI schema: ...items
                # is missing"). Without this the user only sees the generic
                # "0 tools" message and can't fix their spec. (Verified live: an
                # array schema missing `items` FAILs the target silently.)
                target_reasons = []
                try:
                    for _t in _get_targets_from_response(
                        agentcore_ctrl.list_gateway_targets(gatewayIdentifier=gateway["gatewayId"])
                    ):
                        _tid = _t.get("targetId") or _t.get("gatewayTargetId")
                        if not _tid:
                            continue
                        _d = agentcore_ctrl.get_gateway_target(gatewayIdentifier=gateway["gatewayId"], targetId=_tid)
                        if (_d.get("status") or "").upper() == "FAILED":
                            _r = _d.get("statusReasons") or _d.get("statusReason") or "unknown"
                            target_reasons.append(f"{_tid}: {_r}")
                except Exception:  # noqa: BLE001 — diagnostics enrichment only; the deploy still fails below
                    logger.debug("Could not collect FAILED-target reasons", exc_info=True)
                detail = (
                    f" Target failure(s): {target_reasons}"
                    if target_reasons
                    else " No target reported FAILED — likely the AgentCore empty-tool-plane "
                    "provisioning flake (retry the deploy)."
                )
                # Tear down the dead gateway (targets + gateway + cred providers +
                # secrets) before aborting. Otherwise the same-named gateway lingers
                # and the NEXT deploy reuses the broken one (verified live: a stuck
                # conn-github-gw was reused across runs and kept serving 0 tools).
                try:
                    _abort_cfg = {
                        "gateway_id": gateway["gatewayId"],
                        "gateway_name": gateway_name,  # P-PLAT-TEARDOWN: so IAM role is cleaned
                        "client_info": client_info,
                        "connector_credential_providers": connector_credential_providers,
                        "connector_secret_arns": connector_secret_arns,
                        "connector_spec_s3_uris": connector_spec_s3_uris,
                    }
                    cleanup_gateway_resources("connector-abort", region, _abort_cfg)
                except Exception as _ce:  # noqa: BLE001
                    logger.warning("Abort-cleanup of dead connector gateway failed: %s", str(_ce)[:120])
                return {
                    "success": False,
                    "error": (
                        f"Gateway {gateway['gatewayId']} serves 0 tools over MCP after "
                        f"deploying connector OpenAPI target(s)." + detail
                    ),
                    "connector_credential_providers": connector_credential_providers,
                    "connector_secret_arns": connector_secret_arns,
                    "connector_spec_s3_uris": connector_spec_s3_uris,
                }
            # Backfill qualified_tools from the live plane so the policy step (if any)
            # has the connector's tool actions.
            qualified_tools = _qualified_tools_from_served(gateway_url, client_info)
            logger.warning("Connector gateway %s serves %d tool(s) over MCP", gateway["gatewayId"], served)

        # Bug 134 (THE stability fix): a Lambda gateway target can be status=READY
        # with a full inline schema yet the gateway's MCP plane serves an EMPTY
        # tool list — a confirmed AgentCore service-side provisioning flake with
        # NO control-plane signal and NO client action to force it (sync rejects
        # Lambda targets; recreate-target doesn't help). The ONLY deterministic
        # cure is to PROBE the gateway's real MCP tools/list (what the agent will
        # see) and, if it doesn't serve the configured tools, DELETE THE WHOLE
        # GATEWAY and retry from scratch — a fresh gateway usually provisions a
        # working tool plane. Bounded retries; if none serve, fail the deploy
        # (never ship a runtime against a 0-tool gateway).
        if expected_tool_count > 0:
            served = _wait_for_gateway_to_serve_tools(gateway_url, client_info, expected_tool_count, timeout=90)
            if served < expected_tool_count:
                if gateway_retry < 2:
                    logger.warning(
                        "Gateway %s serves %d/%d tools over MCP — likely the "
                        "AgentCore empty-tool-plane flake. Tearing it down and "
                        "recreating (attempt %d/3).",
                        gateway["gatewayId"],
                        served,
                        expected_tool_count,
                        gateway_retry + 2,
                    )
                    try:
                        for _t in _get_targets_from_response(
                            agentcore_ctrl.list_gateway_targets(gatewayIdentifier=gateway["gatewayId"])
                        ):
                            _tid = _t.get("targetId") or _t.get("gatewayTargetId")
                            if _tid:
                                agentcore_ctrl.delete_gateway_target(
                                    gatewayIdentifier=gateway["gatewayId"], targetId=_tid
                                )
                        time.sleep(5)
                        agentcore_ctrl.delete_gateway(gatewayIdentifier=gateway["gatewayId"])
                    except Exception as del_err:  # noqa: BLE001
                        logger.warning("Cleanup before gateway retry (non-fatal): %s", del_err)
                    time.sleep(8)
                    return deploy_gateway(
                        gateway_config,
                        region,
                        template_id=template_id,
                        gateway_tools=gateway_tools,
                        identity_config=identity_config,
                        custom_tools=custom_tools,
                        mcp_server_runtime_arn=mcp_server_runtime_arn,
                        mcp_oauth=mcp_oauth,
                        knowledge_base_result=knowledge_base_result,
                        deployment_id=deployment_id,
                        gateway_retry=gateway_retry + 1,
                    )
                # Exhausted retries — fail closed rather than ship a broken gateway.
                return {
                    "success": False,
                    "error": (
                        f"Gateway {gateway['gatewayId']} never served its tools over "
                        f"MCP ({served}/{expected_tool_count}) after 3 gateway recreations "
                        "— AgentCore empty-tool-plane provisioning flake. Aborting."
                    ),
                }
            # Servable plane confirmed — qualified_tools should reflect it.
            logger.warning("Gateway %s confirmed serving %d tools over MCP", gateway["gatewayId"], served)

        result = {
            "success": True,
            "gateway_url": gateway_url,
            "gateway_id": gateway["gatewayId"],
            "gateway_arn": gateway_arn,
            "gateway_name": gateway_name,
            "client_info": client_info,
            "lambda_function_name": lambda_function_name,
            "custom_tool_lambdas": custom_tool_lambdas,
            "custom_tool_roles": custom_tool_roles,
            "kb_lambda_name": kb_lambda_name,
            # Connector teardown refs: AgentCore credential providers + Secrets
            # Manager secrets + staged OpenAPI spec S3 objects created for SaaS
            # connectors on this gateway.
            "connector_credential_providers": connector_credential_providers,
            "connector_secret_arns": connector_secret_arns,
            "connector_spec_s3_uris": connector_spec_s3_uris,
            # Fully-qualified tool action names for Cedar policy generation, plus
            # how many tools the gateway CONFIGURED so the policy step can
            # fail-closed on a partial (synced < configured) tool plane.
            "qualified_tools": qualified_tools,
            "expected_tool_count": expected_tool_count,
        }
        # SECURITY (CodeQL py/clear-text-logging-sensitive-data): the `result`
        # dict nests client_info.client_secret, so it is taint-tracked — do NOT
        # read any field from it in a log call (even gateway_id). Log only the
        # tool count (an int) and a constant; the gateway id/arn are returned to
        # the caller and recorded in the deployment manifest for correlation.
        logger.info("Gateway deployed (%d tools)", len(qualified_tools))
        return result

    except Exception as e:
        logger.error("Gateway deployment failed: %s", e)
        # Defect D: a gateway-step failure AFTER the gateway/role/targets were
        # created but BEFORE the runtime exists leaks those resources — the
        # deployment manifest only records them on the success path, so nothing
        # ever tears them down (no runtime → no cleanup; no standalone delete
        # route). Best-effort release of whatever we provisioned before failing,
        # so a failed deploy leaves no orphan gateway/role/Lambda-grant behind.
        _leaked_gateway = locals().get("gateway") or {}
        _leaked_gw_id = _leaked_gateway.get("gatewayId") if isinstance(_leaked_gateway, dict) else None
        if _leaked_gw_id:
            try:
                _abort_cfg = {
                    "gateway_id": _leaked_gw_id,
                    "gateway_name": locals().get("gateway_name"),
                    "client_info": locals().get("client_info"),
                    "lambda_function_name": locals().get("lambda_function_name") or "AgentCoreLambdaTestFunction",
                    "custom_tool_lambdas": locals().get("custom_tool_lambdas") or [],
                    "custom_tool_roles": locals().get("custom_tool_roles") or [],
                    "connector_credential_providers": locals().get("connector_credential_providers") or [],
                    "connector_secret_arns": locals().get("connector_secret_arns") or [],
                    "connector_spec_s3_uris": locals().get("connector_spec_s3_uris") or [],
                }
                cleanup_gateway_resources("gateway-deploy-abort", region, _abort_cfg)
                logger.info("Released leaked gateway %s after failed deploy", _leaked_gw_id)
            except Exception as _ce:  # noqa: BLE001 — abort cleanup is best-effort
                logger.warning("Abort-cleanup after failed gateway deploy failed: %s", str(_ce)[:160])
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Gateway cleanup
# ---------------------------------------------------------------------------


# Tool Lambdas created once and reused by EVERY gateway deploy. They must never
# be unconditionally deleted on a per-gateway teardown (Defect C) — doing so
# bricks every other live gateway still wired to them. They are released by
# reference count instead (see _release_shared_tool_lambda).
_SHARED_TOOL_LAMBDAS = frozenset({"AgentCoreDynamicTools", "AgentCoreCustomerSupportTools"})


def _release_shared_tool_lambda(lambda_client, function_name: str, gateway_role_name: str | None) -> str:
    """Reference-counted teardown for a SHARED singleton tool Lambda (Defect C).

    Remove only THIS gateway's ``AllowAgentCoreInvoke-<role>`` statement, then
    delete the function ONLY when no such invoke statements remain (i.e. no other
    live gateway still depends on it). Returns a human-readable log line.

    A shared Lambda deleted while another gateway still uses it makes that
    gateway serve 0 tools (its MCP ``tools/list`` hits a missing function) — the
    "tear down A, B goes dead" failure the matrix run reproduced live.
    """
    if gateway_role_name:
        stmt_id = re.sub(r"[^A-Za-z0-9_-]", "-", f"AllowAgentCoreInvoke-{gateway_role_name}")[:100]
        try:
            lambda_client.remove_permission(FunctionName=function_name, StatementId=stmt_id)
        except Exception as e:  # noqa: BLE001 — statement may already be gone
            if not is_error(e, "ResourceNotFoundException", "ResourceNotFound"):
                logger.debug("remove_permission(%s) on shared lambda failed (continuing)", stmt_id, exc_info=True)

    # Count remaining per-gateway invoke grants. If any survive, another gateway
    # still needs the function — keep it. Also prune any now-orphaned statements
    # so the policy stays clean for the next deploy.
    try:
        _prune_orphaned_lambda_permissions(lambda_client, function_name)
        pol_raw = lambda_client.get_policy(FunctionName=function_name).get("Policy")
        statements = json.loads(pol_raw).get("Statement", []) or []
    except Exception as e:  # noqa: BLE001
        if is_error(e, "ResourceNotFoundException", "ResourceNotFound"):
            # AMBIGUOUS: GetPolicy raises the SAME ResourceNotFoundException for
            # "function doesn't exist" AND "function exists but its resource
            # policy is empty" — which is exactly the refcount-zero state after
            # the last gateway's grant was removed above (verified live: the
            # Lambda leaked as Active while teardown reported "already absent").
            # Disambiguate with get_function: if the function still exists, an
            # empty policy means zero grants remain → this WAS the last gateway,
            # fall through to the delete below.
            try:
                lambda_client.get_function(FunctionName=function_name)
            except Exception:  # noqa: BLE001 — genuinely gone (or unreadable: keep out)
                return f"Shared tool Lambda {function_name} already absent"
            statements = []
        else:
            # Can't read the policy (e.g. GetPolicy denied): DON'T risk deleting
            # a Lambda other gateways may need. Leave it in place.
            return f"Shared tool Lambda {function_name} kept (policy unreadable: {type(e).__name__})"

    remaining = [s for s in statements if (s.get("Sid") or "").startswith("AllowAgentCoreInvoke-")]
    if remaining:
        return f"Shared tool Lambda {function_name} kept ({len(remaining)} other gateway grant(s) remain)"
    try:
        lambda_client.delete_function(FunctionName=function_name)
        return f"Shared tool Lambda {function_name} deleted (last gateway released it)"
    except Exception as e:  # noqa: BLE001
        if is_error(e, "ResourceNotFoundException"):
            return f"Shared tool Lambda {function_name} already absent"
        return f"Shared tool Lambda delete error: {e}"


def cleanup_gateway_resources(runtime_id: str, region: str, gateway_config: dict | None = None) -> list[str]:
    """Clean up all gateway resources: targets, Lambda, gateway, and Cognito."""
    cleanup_log: list[str] = []

    if gateway_config is None:
        return ["No gateway config provided"]

    gateway_id = gateway_config.get("gateway_id")
    client_info = gateway_config.get("client_info")
    lambda_name = gateway_config.get("lambda_function_name", "AgentCoreLambdaTestFunction")

    if not gateway_id:
        return ["No gateway_id in config"]

    # Delete gateway targets
    try:
        agentcore_ctrl = _create_agentcore_control_client(region)
        targets = _get_targets_from_response(agentcore_ctrl.list_gateway_targets(gatewayIdentifier=gateway_id))
        for target in targets:
            tid = target.get("targetId")
            if tid:
                agentcore_ctrl.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=tid)
                cleanup_log.append(f"Target {tid} deleted")
    except Exception as e:
        cleanup_log.append(f"Target cleanup error: {e}")

    # Delete gateway (wait briefly for target deletion to propagate)
    try:
        time.sleep(3)
        agentcore_ctrl.delete_gateway(gatewayIdentifier=gateway_id)
        cleanup_log.append(f"Gateway {gateway_id} deleted")
    except Exception as e:
        cleanup_log.append(f"Gateway delete error: {e}")

    # Delete Cognito resources (only if provider is Cognito)
    if client_info:
        idp_provider = client_info.get("provider", "cognito")
        if idp_provider == "cognito" or not idp_provider:
            try:
                cognito_client = _create_cognito_client(region)
                user_pool_id = client_info.get("user_pool_id")
                client_id_val = client_info.get("client_id")
                if user_pool_id:
                    # Delete domain first (required before pool deletion)
                    try:
                        pool_detail = cognito_client.describe_user_pool(UserPoolId=user_pool_id)
                        domain = pool_detail.get("UserPool", {}).get("Domain")
                        if domain:
                            cognito_client.delete_user_pool_domain(UserPoolId=user_pool_id, Domain=domain)
                    except Exception:  # noqa: BLE001 — pool delete below still surfaces real failures
                        # No client_info-derived value in the log: that dict also
                        # holds client_secret, so CodeQL py/clear-text-logging
                        # taints user_pool_id/client_id too. Message only.
                        logger.debug("Cognito domain delete failed (continuing)", exc_info=True)
                    if client_id_val:
                        try:
                            cognito_client.delete_user_pool_client(UserPoolId=user_pool_id, ClientId=client_id_val)
                        except Exception:  # noqa: BLE001 — client is cascade-deleted with the pool anyway
                            logger.debug("Cognito client delete failed (continuing)", exc_info=True)
                    cognito_client.delete_user_pool(UserPoolId=user_pool_id)
                    cleanup_log.append(f"Cognito pool {user_pool_id} deleted")
            except Exception as e:
                cleanup_log.append(f"Cognito cleanup error: {e}")
        else:
            cleanup_log.append(f"External IDP ({idp_provider}) — no Cognito cleanup needed")

    # Delete Lambda function. SHARED singleton tool Lambdas
    # (AgentCoreDynamicTools / AgentCoreCustomerSupportTools) are reused by every
    # gateway, so they are released by reference count — NOT unconditionally
    # deleted (Defect C: deleting one kills every other live gateway wired to it).
    # A per-gateway auto-created Lambda (AgentCoreLambdaTestFunction-* etc.) is not
    # shared and is deleted outright.
    lambda_client = None
    try:
        lambda_client = _create_lambda_client(region)
        if lambda_name in _SHARED_TOOL_LAMBDAS:
            gw_name = gateway_config.get("gateway_name")
            gw_role_name = f"AgentCoreGateway-{gw_name}" if gw_name else None
            cleanup_log.append(_release_shared_tool_lambda(lambda_client, lambda_name, gw_role_name))
        else:
            lambda_client.delete_function(FunctionName=lambda_name)
            cleanup_log.append(f"Lambda {lambda_name} deleted")
    except Exception as e:
        if not is_error(e, "ResourceNotFoundException"):
            cleanup_log.append(f"Lambda delete error: {e}")

    # Delete custom tool Lambdas
    custom_tool_lambdas = gateway_config.get("custom_tool_lambdas", [])
    for fn_name in custom_tool_lambdas:
        try:
            if not lambda_client:
                lambda_client = _create_lambda_client(region)
            lambda_client.delete_function(FunctionName=fn_name)
            cleanup_log.append(f"Custom tool Lambda {fn_name} deleted")
        except Exception as e:
            if not is_error(e, "ResourceNotFoundException"):
                cleanup_log.append(f"Custom tool Lambda delete error: {e}")

    # Delete custom tool IAM roles
    custom_tool_roles = gateway_config.get("custom_tool_roles", [])
    for role_name in custom_tool_roles:
        try:
            iam_client = _create_iam_client()
            # Detach managed policies before role deletion
            attached = iam_client.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", [])
            for policy in attached:
                iam_client.detach_role_policy(RoleName=role_name, PolicyArn=policy["PolicyArn"])
            iam_client.delete_role(RoleName=role_name)
            cleanup_log.append(f"IAM role {role_name} deleted")
        except Exception as e:
            if not is_error(e, "NoSuchEntity", "NoSuchEntityException"):
                cleanup_log.append(f"IAM role cleanup error for {role_name}: {e}")

    # Delete the gateway's own execution role (AgentCoreGateway-<gateway_name>).
    # P-PLAT-TEARDOWN: failed deploys leave orphaned IAM roles because the gateway
    # step creates the role early (before targets) but only records resources at
    # the END on success. On failure the role never gets into the manifest. Delete
    # it explicitly here so abort-cleanup (line ~3143) catches it.
    gw_name = gateway_config.get("gateway_name")
    if gw_name:
        gw_role_name = f"AgentCoreGateway-{gw_name}"
        try:
            iam_client = _create_iam_client()
            # Role may have inline + attached policies; detach all before delete.
            for pn in iam_client.list_role_policies(RoleName=gw_role_name).get("PolicyNames", []):
                iam_client.delete_role_policy(RoleName=gw_role_name, PolicyName=pn)
            for ap in iam_client.list_attached_role_policies(RoleName=gw_role_name).get("AttachedPolicies", []):
                iam_client.detach_role_policy(RoleName=gw_role_name, PolicyArn=ap["PolicyArn"])
            iam_client.delete_role(RoleName=gw_role_name)
            cleanup_log.append(f"Gateway IAM role {gw_role_name} deleted")
        except Exception as e:  # noqa: BLE001
            if not is_error(e, "NoSuchEntity", "NoSuchEntityException"):
                cleanup_log.append(f"Gateway IAM role cleanup error: {e}")

    # Delete SaaS connector credential providers (API-key OR OAuth2 — try both,
    # since the stored name doesn't record the type). Non-fatal per item.
    connector_providers = gateway_config.get("connector_credential_providers", [])
    if connector_providers:
        try:
            agentcore_ctrl = agentcore_ctrl  # reuse if defined above
        except NameError:  # pragma: no cover
            agentcore_ctrl = _create_agentcore_control_client(region)
        for provider_entry in connector_providers:
            _ok, _msg = _delete_connector_credential_provider(agentcore_ctrl, provider_entry)
            cleanup_log.append(_msg)

    # Delete connector secrets from Secrets Manager (force, no recovery window).
    connector_secret_arns = gateway_config.get("connector_secret_arns", [])
    if connector_secret_arns:
        sm_client = _create_secrets_client(region)
        for secret_arn in connector_secret_arns:
            try:
                sm_client.delete_secret(SecretId=secret_arn, ForceDeleteWithoutRecovery=True)
                cleanup_log.append(f"Connector secret {secret_arn} deleted")
            except Exception as e:  # noqa: BLE001
                if not is_error(e, "ResourceNotFoundException"):
                    cleanup_log.append(f"Connector secret delete error: {e}")

    # Delete staged OpenAPI spec objects (large connector specs routed to S3).
    for uri in gateway_config.get("connector_spec_s3_uris", []):
        _delete_spec_s3_object(uri, region)
        cleanup_log.append(f"Connector spec object {uri} deleted")

    return cleanup_log


# ---------------------------------------------------------------------------
# External MCP-server Gateway targets (Tiers 1-3 of docs/MCP_GATEWAY_INTEGRATION)
# ---------------------------------------------------------------------------
#
# Unlike the platform-deployed Runtime-MCP target (which builds its endpoint from
# an AgentCore Runtime ARN and authenticates with the platform's own Cognito),
# these functions wire an ARBITRARY EXTERNAL remote MCP endpoint from the MCP
# catalog (services/mcp_catalog.py) as a `mcp.mcpServer` target, selecting the
# outbound credential provider from the entry's `auth_type`:
#
#   none                       → no credential provider (Tier 1)
#   api_key                    → API_KEY provider (header/query/bearer)   (Tier 2)
#   oauth2_client_credentials  → OAUTH provider, CLIENT_CREDENTIALS grant (Tier 3)
#   iam_sigv4                  → GATEWAY_IAM_ROLE (SigV4 outbound)         (Tier 3)
#
# `adapter-3lo` / `adapter-stdio` tiers are NOT handled here — they require the
# platform to host an MCP proxy on Runtime first (that adapter is then wired via
# the existing `mcp_server_runtime_arn` path). Passing such an entry raises.
#
# Verified against the live bedrock-agentcore-control model (boto3 1.43.8):
# McpServerTargetConfiguration requires only `endpoint`; credentialProvider-
# Configurations is OPTIONAL, so a no-auth target is valid.


def _mcp_api_key_cred_config(provider_arn: str, descriptor: dict) -> dict:
    """Build an API_KEY credentialProviderConfiguration from a catalog descriptor.

    ``descriptor`` = {location: HEADER|QUERY_PARAMETER, parameter_name, prefix}.
    """
    api_key_cfg: dict = {
        "providerArn": provider_arn,
        "credentialParameterName": descriptor.get("parameter_name") or "Authorization",
        "credentialLocation": descriptor.get("location") or "HEADER",
    }
    prefix = descriptor.get("prefix")
    if prefix:
        api_key_cfg["credentialPrefix"] = prefix
    return {
        "credentialProviderType": "API_KEY",
        "credentialProvider": {"apiKeyCredentialProvider": api_key_cfg},
    }


def build_external_mcp_target_params(
    agentcore_ctrl,
    *,
    gateway_id: str,
    target_name: str,
    catalog_entry: dict,
    endpoint: str,
    secret_arn: str | None = None,
    oauth_provider_arn: str | None = None,
    oauth_scopes: list | None = None,
) -> dict:
    """Assemble CreateGatewayTarget params for an external MCP catalog entry.

    Pure w.r.t. AWS EXCEPT it may create an API_KEY credential provider (Tier 2)
    from ``secret_arn``. OAuth providers (Tier 3) are expected to be created by
    the caller and passed as ``oauth_provider_arn`` (they need client-id/secret
    wiring the caller owns). Raises for adapter-* tiers.
    """
    tier = catalog_entry.get("tier", "")
    auth_type = catalog_entry.get("auth_type", "none")

    if tier.startswith("adapter"):
        raise ValueError(
            f"MCP '{catalog_entry.get('id')}' is tier '{tier}' — it requires a hosted "
            "adapter (Runtime/container) and cannot be wired as a direct external target. "
            "See docs/MCP_GATEWAY_INTEGRATION.md."
        )
    if not re.match(r"^https://", endpoint or ""):
        raise ValueError(f"MCP endpoint must be an https:// URL, got: {endpoint!r}")

    params: dict = {
        "gatewayIdentifier": gateway_id,
        "name": target_name,
        # Gateway crawls tools/list dynamically — no mcpToolSchema required.
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": endpoint}}},
    }

    cred_configs: list = []
    if auth_type == "none":
        pass  # Tier 1 — no credential provider (valid per API model).
    elif auth_type == "api_key":
        if not secret_arn:
            raise RuntimeError(f"MCP '{catalog_entry.get('id')}' needs an API key — provide a secret_arn.")
        descriptor = catalog_entry.get("api_key_descriptor") or {}
        provider_arn = _ensure_api_key_credential_provider(agentcore_ctrl, f"mcp-{target_name}", secret_arn=secret_arn)
        cred_configs.append(_mcp_api_key_cred_config(provider_arn, descriptor))
    elif auth_type == "oauth2_client_credentials":
        if not oauth_provider_arn:
            raise RuntimeError(f"MCP '{catalog_entry.get('id')}' needs an OAuth provider ARN (client-credentials).")
        cred_configs.append(
            {
                "credentialProviderType": "OAUTH",
                "credentialProvider": {
                    "oauthCredentialProvider": {
                        "providerArn": oauth_provider_arn,
                        "scopes": oauth_scopes or [],
                    }
                },
            }
        )
    elif auth_type == "iam_sigv4":
        # SigV4 outbound signed by the gateway's own execution role.
        cred_configs.append({"credentialProviderType": "GATEWAY_IAM_ROLE"})
    else:
        raise ValueError(f"Unsupported MCP auth_type: {auth_type!r}")

    if cred_configs:
        params["credentialProviderConfigurations"] = cred_configs
    return params


def deploy_external_mcp_target(
    agentcore_ctrl,
    *,
    gateway_id: str,
    catalog_entry: dict,
    endpoint: str | None = None,
    secret_arn: str | None = None,
    oauth_provider_arn: str | None = None,
    oauth_scopes: list | None = None,
    target_name: str | None = None,
) -> dict | None:
    """Create a Gateway `mcpServer` target for an external MCP catalog entry.

    ``endpoint`` overrides the catalog endpoint (needed when the catalog URL has
    ``{placeholders}`` like a Databricks workspace or a Shopify store domain).
    Returns the created/reused target dict, or None if creation was non-fatally
    skipped. Target name is derived from the catalog id (kept short so the
    resulting ``<target>___<tool>`` qualified names stay under 64 chars).
    """
    ep = endpoint or catalog_entry.get("endpoint")
    name = target_name or _sanitize_provider_name(f"mcp-{catalog_entry.get('id', 'ext')}")[:48]
    params = build_external_mcp_target_params(
        agentcore_ctrl,
        gateway_id=gateway_id,
        target_name=name,
        catalog_entry=catalog_entry,
        endpoint=ep,
        secret_arn=secret_arn,
        oauth_provider_arn=oauth_provider_arn,
        oauth_scopes=oauth_scopes,
    )
    logger.info(
        "Creating external MCP target '%s' (tier=%s, auth=%s)",
        name,
        catalog_entry.get("tier"),
        catalog_entry.get("auth_type"),
    )
    return _create_gateway_target_with_retry(agentcore_ctrl, gateway_id, name, params)


def _fill_endpoint_placeholders(endpoint: str, endpoint_vars: dict) -> str:
    """Substitute ``{placeholder}`` tokens in a catalog endpoint from user input.

    Catalog endpoints like ``https://{store_domain}/api/mcp`` carry per-deploy
    placeholders the UI collects. Every ``{name}`` must be supplied, else the
    unresolved endpoint would fail the ``https://`` validation downstream. Values
    are lightly sanitized (no scheme, no path-escaping) so a value can't inject a
    different host segment.
    """
    filled = endpoint or ""
    for token in re.findall(r"\{([a-zA-Z0-9_]+)\}", endpoint or ""):
        val = str((endpoint_vars or {}).get(token, "")).strip()
        if not val:
            raise RuntimeError(f"External MCP endpoint needs a value for '{token}'.")
        # Placeholders are host/segment tokens (e.g. a store domain), never a
        # scheme or path — reject a value carrying "/", "://", or whitespace so it
        # can't rewrite the endpoint's host/path structure.
        if "://" in val or "/" in val or any(c in val for c in (" ", "\t", "\n")):
            raise RuntimeError(f"Invalid value for MCP placeholder '{token}'.")
        filled = filled.replace("{" + token + "}", val)
    return filled


def _deploy_external_mcp_targets(
    agentcore_ctrl,
    gateway_id: str,
    region: str,
    external_mcp_servers: list[dict],
    owner_sub: str = "",
) -> dict:
    """Wire external MCP catalog servers as Gateway ``mcpServer`` targets.

    Mirrors ``_deploy_connector_targets``: each selection dict carries
    ``server_id`` (catalog key), optional ``endpoint_vars`` (fills ``{...}``
    placeholders), optional ``secret_value`` (Tier-2 API key — minted here) or
    ``secret_arn`` (pre-minted), and optional ``oauth`` ``{client_id, client_secret,
    token_url, scopes}`` for Tier-3 client-credentials. ``adapter-*`` tiers are
    rejected up front (they need a hosted proxy).

    Returns ``{credential_provider_names, secret_arns}`` for teardown. Partial
    resources are rolled back best-effort on a mid-loop failure before re-raising.
    """
    from app.services.mcp_catalog import get_mcp_server

    created_providers: list[str] = []
    created_secrets: list[str] = []

    def _rollback_partial() -> None:
        for pname in created_providers:
            _delete_connector_credential_provider(agentcore_ctrl, pname)
        if created_secrets:
            sm = _create_secrets_client(region)
            for _sidx, sarn in enumerate(created_secrets):
                try:
                    sm.delete_secret(SecretId=sarn, ForceDeleteWithoutRecovery=True)
                except Exception:  # noqa: BLE001 — best-effort rollback
                    # No ARN/value in the log (CodeQL py/clear-text-logging taint).
                    logger.warning("Rollback: could not delete MCP-server secret #%d", _sidx)

    try:
        for sel in external_mcp_servers:
            server_id = (sel or {}).get("server_id") or (sel or {}).get("serverId")
            raw_endpoint = (sel or {}).get("endpoint") or (sel or {}).get("server_url") or (sel or {}).get("serverUrl")

            # CUSTOM endpoint path: the caller supplied a raw https MCP endpoint
            # not in the curated catalog. Synthesize an in-memory catalog entry
            # from the selection's own fields (endpoint + auth_type) instead of
            # a catalog lookup. The endpoint is SSRF-validated (https-only, DNS-
            # resolved, private/IMDS ranges blocked) exactly like the OpenAPI
            # spec-url path. Only the direct tiers are wireable (no adapter).
            if not server_id and raw_endpoint:
                custom_auth = (sel.get("auth_type") or sel.get("authType") or "none").lower()
                if custom_auth not in ("none", "api_key", "oauth2_client_credentials", "iam_sigv4"):
                    raise RuntimeError(
                        f"Custom MCP auth_type '{custom_auth}' is not a direct tier "
                        "(use none / api_key / oauth2_client_credentials / iam_sigv4)."
                    )
                validated_endpoint = _validate_outbound_url(raw_endpoint)
                server_id = "custom-" + re.sub(r"[^a-z0-9]+", "-", (sel.get("name") or "mcp").lower()).strip("-")[:32]
                entry = {
                    "id": server_id,
                    "display_name": sel.get("name") or "Custom MCP",
                    "endpoint": validated_endpoint,
                    "auth_type": custom_auth,
                    "tier": {
                        "none": "direct-none",
                        "api_key": "direct-apikey",
                        "oauth2_client_credentials": "direct-oauth",
                        "iam_sigv4": "direct-iam",
                    }[custom_auth],
                    "api_key_descriptor": sel.get("api_key_descriptor") or {},
                }
            else:
                if not server_id:
                    raise RuntimeError("External MCP selection needs a 'server_id' or a custom 'endpoint'.")
                entry = get_mcp_server(server_id)
                if entry is None:
                    raise RuntimeError(f"Unknown MCP server id: {server_id}")

            endpoint = _fill_endpoint_placeholders(
                entry.get("endpoint") or "", sel.get("endpoint_vars") or sel.get("endpointVars") or {}
            )
            auth_type = entry.get("auth_type", "none")

            secret_arn = sel.get("secret_arn") or sel.get("secretArn")
            oauth_provider_arn = None
            oauth_scopes = None

            # Tier 2 — mint an owner-scoped secret for a raw API key.
            if auth_type == "api_key" and not secret_arn:
                raw_key = sel.get("secret_value") or sel.get("secretValue")
                if not raw_key:
                    raise RuntimeError(f"MCP '{server_id}' needs an API key (secret_value).")
                secret_arn = _put_connector_secret(region, owner_sub, {"apiKey": raw_key})
                created_secrets.append(secret_arn)

            # Tier 3 — create the OAuth2 client-credentials provider from user creds.
            if auth_type == "oauth2_client_credentials":
                oauth = sel.get("oauth") or {}
                client_id = oauth.get("client_id") or oauth.get("clientId")
                client_secret = oauth.get("client_secret") or oauth.get("clientSecret")
                # A CustomOauth2 client-credentials provider resolves its token
                # endpoint from the IdP's OIDC discovery document.
                discovery_url = (
                    oauth.get("discovery_url")
                    or oauth.get("discoveryUrl")
                    or oauth.get("token_url")
                    or oauth.get("tokenUrl")
                )
                if not (client_id and client_secret and discovery_url):
                    raise RuntimeError(f"MCP '{server_id}' needs oauth {{client_id, client_secret, discovery_url}}.")
                cs_arn = _put_connector_secret(region, owner_sub, {"clientSecret": client_secret})
                created_secrets.append(cs_arn)
                oauth_provider_arn = _ensure_oauth2_credential_provider(
                    agentcore_ctrl,
                    f"mcp-{server_id}",
                    vendor="CustomOauth2",
                    client_id=client_id,
                    client_secret_arn=cs_arn,
                    discovery_url=discovery_url,
                )
                created_providers.append(_sanitize_provider_name(f"mcp-{server_id}"))
                oauth_scopes = oauth.get("scopes") or (entry.get("oauth_descriptor") or {}).get("scopes")

            # Tier-2's API-key provider is created inside deploy_external_mcp_target;
            # track its name so teardown can remove it (target_name → mcp-<id>).
            if auth_type == "api_key":
                created_providers.append(_sanitize_provider_name(f"mcp-mcp-{server_id}"))

            deploy_external_mcp_target(
                agentcore_ctrl,
                gateway_id=gateway_id,
                catalog_entry=entry,
                endpoint=endpoint,
                secret_arn=secret_arn,
                oauth_provider_arn=oauth_provider_arn,
                oauth_scopes=oauth_scopes,
            )
    except Exception:
        logger.error("External MCP deploy failed mid-loop; rolling back partial resources")
        _rollback_partial()
        raise

    return {"credential_provider_names": created_providers, "secret_arns": created_secrets}


def _grant_gateway_invoke_on_lambda(region: str, function_arn: str, gateway_role_arn: str) -> None:
    """Grant the gateway execution role ``lambda:InvokeFunction`` on a
    user-supplied Lambda ARN (idempotent, per-role StatementId).

    Mirrors the grant `_create_or_update_lambda` applies to our managed tool
    Lambdas, but targets a function we did NOT create (a raw ARN from a gateway
    ``lambda`` target). Best-effort on propagation races; a conflicting statement
    (already granted) is treated as success.
    """
    if not (function_arn and gateway_role_arn):
        return
    lambda_client = _create_lambda_client(region)
    role_name = gateway_role_arn.rsplit("/", 1)[-1]
    # PRUNE first: a prior (now-deleted) gateway role can leave a dangling
    # principal (AROA...) in this function's resource policy, which makes EVERY
    # subsequent add_permission reject with "The provided principal was invalid"
    # — even a valid one. This is the root cause of multi-gateway deploy
    # failures against a shared/reused Lambda (matches the managed-Lambda path).
    _prune_orphaned_lambda_permissions(lambda_client, function_arn)
    stmt_id = re.sub(r"[^A-Za-z0-9_-]", "-", f"AllowAgentCoreInvoke-{role_name}")[:100]
    for attempt in range(8):
        try:
            lambda_client.add_permission(
                FunctionName=function_arn,
                StatementId=stmt_id,
                Action="lambda:InvokeFunction",
                Principal=gateway_role_arn,
            )
            logger.info("Granted %s invoke on user Lambda %s", role_name, _safe_log_token(function_arn))
            return
        except lambda_client.exceptions.ResourceConflictException:
            return  # already permitted — fine
        except lambda_client.exceptions.InvalidParameterValueException as e:
            if "principal" not in str(e).lower() or attempt == 7:
                raise
            time.sleep(8)


def _default_lambda_tool_schema(function_arn: str) -> dict:
    """Build a generic single-tool schema for a bare Lambda ARN target.

    AgentCore's ``McpLambdaTargetConfiguration`` REQUIRES a ``toolSchema``, but a
    gateway ``lambda`` target only carries the function ARN. Derive a passthrough
    tool named after the function that accepts a free-form object payload so the
    target is valid and invocable.
    """
    fn_name = function_arn.rsplit(":function:", 1)[-1].split(":")[0] or "invoke"
    tool_name = re.sub(r"[^a-zA-Z0-9_-]", "_", fn_name)[:60] or "invoke"
    return {
        "inlinePayload": [
            {
                "name": tool_name,
                "description": f"Invoke the {fn_name} Lambda function.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            }
        ]
    }


def _openapi_target_cred_config(agentcore_ctrl, target: dict, base_name: str) -> dict | None:
    """Pick the outbound credential provider for an OpenAPI ``targets[]`` entry.

    OpenAPI targets are arbitrary external HTTP APIs, so ``GATEWAY_IAM_ROLE``
    (SigV4) is INVALID for them (Defect B). Valid options:

      * ``none`` / unset  → return ``None`` (public spec — omit the block).
      * ``api_key``       → API_KEY provider from a pre-minted ``secret_arn``.
      * ``oauth2_client_credentials`` → OAUTH provider from a pre-minted
        ``oauth_provider_arn``.

    The multi-target modal currently collects only the spec (public is the
    default), so ``None`` is the common path; the auth branches are honored when
    a richer payload supplies them (keeps parity with the connector OpenAPI path).
    """
    auth_type = (target.get("auth_type") or target.get("authType") or "none").lower()
    if auth_type in ("", "none"):
        return None
    if auth_type == "api_key":
        secret_arn = target.get("secret_arn") or target.get("secretArn")
        if not secret_arn:
            logger.warning(
                "OpenAPI target %s requests api_key auth but has no secret_arn; deploying as public",
                base_name,
            )
            return None
        provider_arn = _ensure_api_key_credential_provider(
            agentcore_ctrl, f"openapi-{base_name}", secret_arn=secret_arn
        )
        descriptor = {
            "parameter_name": target.get("credential_parameter_name") or target.get("credentialParameterName"),
            "location": target.get("credential_location") or target.get("credentialLocation"),
            "prefix": target.get("credential_prefix") or target.get("credentialPrefix"),
        }
        return _mcp_api_key_cred_config(provider_arn, descriptor)
    if auth_type in ("oauth2_client_credentials", "oauth"):
        provider_arn = target.get("oauth_provider_arn") or target.get("oauthProviderArn")
        if not provider_arn:
            logger.warning(
                "OpenAPI target %s requests oauth but has no oauth_provider_arn; deploying as public",
                base_name,
            )
            return None
        return {
            "credentialProviderType": "OAUTH",
            "credentialProvider": {
                "oauthCredentialProvider": {
                    "providerArn": provider_arn,
                    "scopes": target.get("scopes") or [],
                }
            },
        }
    logger.warning("OpenAPI target %s has unsupported auth_type %r; deploying as public", base_name, auth_type)
    return None


def _deploy_config_targets(
    agentcore_ctrl,
    gateway_id: str,
    region: str,
    targets: list[dict],
    gateway_role_arn: str = "",
    name_prefix: str = "cfgtgt",
) -> dict:
    """Deploy a mixed list of gateway ``targets`` (openapi / lambda / smithy) —
    all on the SAME gateway — creating one gateway target per entry.

    This is the multi-target counterpart to ``_deploy_connector_targets`` /
    ``_deploy_external_mcp_targets``. It handles the NON-MCP families (mcp_server
    entries are wired via ``external_mcp_servers``):

      * ``lambda``  → ``targetConfiguration.mcp.lambda`` (+ gateway-role invoke
        grant on the user-supplied ARN), using a generic passthrough toolSchema.
      * ``openapi`` → ``targetConfiguration.mcp.openApiSchema`` (inline / staged),
        mirroring the connector OpenAPI path (no credential provider — public /
        gateway-IAM specs; auth'd specs should use the connector path).
      * ``smithy``  → ``targetConfiguration.mcp.smithyModel`` from inline content.

    Each target gets a UNIQUE ``name`` (``<prefix>-<family>-<index>``) on the
    gateway. Returns ``{"target_names": [...]}`` for logging / assertions.
    Unknown / unsupported families are skipped with a warning (never fatal).
    """
    created_target_names: list[str] = []

    for idx, raw in enumerate(targets or []):
        target = raw or {}
        ttype = target.get("type") or target.get("target_type") or ""
        base_name = re.sub(r"[^a-zA-Z0-9-]", "-", f"{name_prefix}-{ttype or 'x'}-{idx}")[:48]

        if ttype == "lambda":
            function_arn = target.get("function_arn") or target.get("functionArn")
            if not function_arn:
                logger.warning("Gateway lambda target #%d has no function_arn; skipping", idx)
                continue
            if gateway_role_arn:
                _grant_gateway_invoke_on_lambda(region, function_arn, gateway_role_arn)
            tool_schema = (
                target.get("tool_schema") or target.get("toolSchema") or _default_lambda_tool_schema(function_arn)
            )
            create_params = {
                "gatewayIdentifier": gateway_id,
                "name": base_name,
                "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": function_arn, "toolSchema": tool_schema}}},
                "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
            }
            _create_gateway_target_with_retry(agentcore_ctrl, gateway_id, base_name, create_params)
            created_target_names.append(base_name)
            logger.info("Deployed lambda gateway target %s -> %s", base_name, _safe_log_token(function_arn))

        elif ttype == "openapi":
            spec_inline = target.get("spec_content") or target.get("specContent")
            spec_url = target.get("spec_url") or target.get("specUrl")
            if not spec_inline:
                if not spec_url:
                    logger.warning("Gateway openapi target #%d has no spec_url/spec_content; skipping", idx)
                    continue
                spec_inline = _fetch_openapi_spec(spec_url)
            openapi_schema = _build_openapi_schema(spec_inline, connector_id=base_name, region=region)
            create_params = {
                "gatewayIdentifier": gateway_id,
                "name": base_name,
                "targetConfiguration": {"mcp": {"openApiSchema": openapi_schema}},
            }
            # An OpenAPI target is an external HTTP API, NOT an AWS-native target:
            # GATEWAY_IAM_ROLE (SigV4) is invalid for it (AgentCore rejects with
            # "IamCredentialProvider is required for openApiSchema targets" — Defect
            # B). Valid providers are API_KEY / OAUTH, or none at all for a public
            # spec. The modal collects only the spec, so the default is public
            # (omit the block); api_key/oauth are honored if present in the payload.
            cred_cfg = _openapi_target_cred_config(agentcore_ctrl, target, base_name)
            if cred_cfg is not None:
                create_params["credentialProviderConfigurations"] = [cred_cfg]
            _create_gateway_target_with_retry(agentcore_ctrl, gateway_id, base_name, create_params)
            created_target_names.append(base_name)
            logger.info("Deployed openapi gateway target %s", base_name)

        elif ttype == "smithy":
            # AgentCore's smithyModel is an inline/staged API schema. The bare
            # model_name ('dynamodb') carries no schema, so require inline content
            # (model_content / spec_content) — skip loudly otherwise.
            model_content = (
                target.get("model_content")
                or target.get("modelContent")
                or target.get("spec_content")
                or target.get("specContent")
            )
            if not model_content:
                logger.warning(
                    "Gateway smithy target #%d ('%s') has no inline model content; skipping",
                    idx,
                    target.get("model_name") or target.get("modelName"),
                )
                continue
            create_params = {
                "gatewayIdentifier": gateway_id,
                "name": base_name,
                "targetConfiguration": {"mcp": {"smithyModel": {"inlinePayload": model_content}}},
                # Smithy models front AWS SDK services (e.g. DynamoDB): they ARE
                # AWS-native, so GATEWAY_IAM_ROLE (SigV4 signed by the gateway
                # execution role) is the correct provider here — unlike openapi.
                "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
            }
            _create_gateway_target_with_retry(agentcore_ctrl, gateway_id, base_name, create_params)
            created_target_names.append(base_name)
            logger.info("Deployed smithy gateway target %s", base_name)

        elif ttype == "mcp_server":
            # mcp_server entries are wired via external_mcp_servers (secret hygiene
            # + SSRF validation live there); ignore here to avoid double-deploy.
            continue

        else:
            logger.warning("Unknown gateway target family '%s' (#%d); skipping", ttype, idx)

    return {"target_names": created_target_names}
