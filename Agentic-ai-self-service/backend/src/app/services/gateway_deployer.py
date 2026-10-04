"""Gateway deployment and cleanup for AgentCore.

Uses pure boto3 APIs — no external CLI or starter toolkit dependencies.
Handles MCP Gateway creation, Lambda target deployment, Cognito OAuth
setup, JWT auth configuration, and resource cleanup.

Requirements: 5.3
"""

import contextvars
import fnmatch
import hashlib
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
from contextlib import contextmanager
from contextvars import ContextVar

import boto3

from app.services import codegen_templates
from app.services.aws_errors import error_code, is_error
from app.services.aws_pagination import list_all
from app.services.deletion_confirmation import DeletionFailedAfterAccept, wait_until_absent
from app.services.error_sanitizer import redact_secrets
from app.services.gateway_mutation_lock import (
    GatewayUpdateUnconfirmed,
    authorizer_is,
    gateway_mutation_lock,
    shared_lambda_lock,
)
from app.services.gateway_update import NO_CLIENT_ALLOWED, preserving_gateway_update
from app.services.iam_boundary import create_role_kwargs, ensure_role_boundary
from app.services.mcp_gateway_protocol import pinned_protocol_configuration
from app.services.naming import (
    deployment_scope_suffix,
    regional_iam_role_name,
    scoped_function_name,
    scoped_role_name,
)
from app.services.resource_ownership import (
    ACCESS_TAG_KEY,
    ACCESS_TAG_VALUE,
    CDK_PROJECT_TAG_KEY,
    OWNER_TAG_KEY,
    ForeignResourceError,
    ResourceAccessRefused,
    ResourceDeletionRefused,
    assert_agentcore_resource_owned,
    assert_resource_access_allowed,
    assert_this_deployment_may_mutate,
    can_this_deployment_mutate,
    delete_owned_credential_provider,
    delete_owned_iam_role,
    delete_owned_s3_object,
    is_owned_by_this_stack,
    owner_sub_hash,
    owner_tags,
    resource_is_missing,
    stack_id,
    tag_map,
)
from app.services.resource_tagging import (
    governed_tag_list,
    governed_tags,
)
from app.services.runtime_deployer import RUNTIME_LOG_RETENTION_DAYS

logger = logging.getLogger(__name__)

_GATEWAY_AWS_SESSION: ContextVar[object | None] = ContextVar(
    "gateway_aws_session",
    default=None,
)
_GATEWAY_ARTIFACT_BUCKET: ContextVar[str | None] = ContextVar(
    "gateway_artifact_bucket",
    default=None,
)
_GATEWAY_ARTIFACT_OWNER: ContextVar[str | None] = ContextVar(
    "gateway_artifact_owner",
    default=None,
)


@contextmanager
def gateway_aws_session(
    session,
    *,
    artifact_bucket: str | None = None,
    expected_bucket_owner: str | None = None,
):
    """Route this module's AWS clients through *session* for one deploy/cleanup.

    Gateway deployment predates the Step Functions cross-account client seam and
    historically created its own ambient boto3 clients. The step handler enters
    this context with ``step_clients.session_for_event(event)`` so every helper in
    this large module targets the same account and region without threading nine
    separate clients through the call graph.
    """
    session_token = _GATEWAY_AWS_SESSION.set(session)
    bucket_token = _GATEWAY_ARTIFACT_BUCKET.set(artifact_bucket)
    owner_token = _GATEWAY_ARTIFACT_OWNER.set(expected_bucket_owner)
    try:
        yield
    finally:
        _GATEWAY_ARTIFACT_OWNER.reset(owner_token)
        _GATEWAY_ARTIFACT_BUCKET.reset(bucket_token)
        _GATEWAY_AWS_SESSION.reset(session_token)


def _aws_client(service_name: str, **kwargs):
    session = _GATEWAY_AWS_SESSION.get()
    if session is not None:
        return session.client(service_name, **kwargs)
    return boto3.client(service_name, **kwargs)


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


_OIDC_ALLOWLIST_ENV = "OIDC_DISCOVERY_HOST_ALLOWLIST"
_OUTBOUND_ALLOWLIST_ENV = "OUTBOUND_HOST_ALLOWLIST"


def _load_host_allowlist(*env_vars: str) -> tuple[tuple[str, ...], str] | None:
    """First of *env_vars* that is set wins. Returns (host globs, var name) or None.

    The var name is returned so a rejection can name the variable that actually
    blocked the host rather than a variable the operator never set.
    """
    for var in env_vars:
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        parts = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
        if parts:
            return parts, var
    return None


def _load_oidc_host_allowlist() -> tuple[str, ...] | None:
    """Return tuple of allowed host glob patterns from env, or None if no allowlist set.

    Env var: OIDC_DISCOVERY_HOST_ALLOWLIST=*.okta.com,*.auth0.com,*.amazoncognito.com
    """
    found = _load_host_allowlist(_OIDC_ALLOWLIST_ENV)
    return found[0] if found else None


def _host_matches_allowlist(host: str, allowlist: tuple[str, ...]) -> bool:
    host = host.lower()
    return any(fnmatch.fnmatchcase(host, pattern) for pattern in allowlist)


def _validate_discovery_url(
    url: str,
    label: str = "OIDC discovery URL",
    allowlist_env: tuple[str, ...] = (_OIDC_ALLOWLIST_ENV,),
) -> str:
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

    *label* names the thing being validated in the raised messages. It defaults to
    the original wording so every pre-existing caller's message is unchanged. It
    exists because this guard is now reused for URLs that have nothing to do with
    OIDC (connector spec URLs, a LiteLLM gateway base URL), and a rejection that
    says "OIDC discovery URL" sends an operator to the wrong piece of config.

    *allowlist_env* is the same idea applied to the host allowlist. An operator who
    sets ``OIDC_DISCOVERY_HOST_ALLOWLIST=*.okta.com`` to pin their identity provider
    was also, silently, pinning every other outbound URL this guard validates — so a
    LiteLLM base URL got rejected for not being an Okta host. Non-OIDC callers pass
    ``(_OUTBOUND_ALLOWLIST_ENV, _OIDC_ALLOWLIST_ENV)``: the neutral variable wins
    when set, and the OIDC one is still honoured as the fallback. That ordering is
    deliberate — it means this change never loosens an existing deployment, it only
    gives an operator a way to state the two policies separately.
    """
    if not url or not isinstance(url, str):
        raise _DiscoveryUrlInvalid(f"{label} is empty")

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise _DiscoveryUrlInvalid(f"{label} must use https scheme (got '{parsed.scheme}')")
    host = parsed.hostname
    if not host:
        raise _DiscoveryUrlInvalid(f"{label} has no host component")

    found = _load_host_allowlist(*allowlist_env)
    if found is not None and not _host_matches_allowlist(host, found[0]):
        raise _DiscoveryUrlBlocked(f"{label} host '{host}' is not on {found[1]}")

    # Resolve every A/AAAA record under a strict timeout so an attacker cannot stall
    # us on DNS to keep a half-validated socket alive.
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5)
    try:
        try:
            infos = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except (TimeoutError, socket.gaierror, OSError) as e:
            raise _DiscoveryUrlBlocked(f"{label} host '{host}' could not be resolved: {e}") from e
    finally:
        socket.setdefaulttimeout(prev_timeout)

    if not infos:
        raise _DiscoveryUrlBlocked(f"{label} host '{host}' returned no DNS records")

    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        # IPv6 sockaddr can carry a scope id like "fe80::1%eth0" — strip it.
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as e:
            raise _DiscoveryUrlBlocked(f"{label} resolved to unparseable IP '{ip_str}': {e}") from e
        for net in _DISALLOWED_NETWORKS:
            # ip_address(v4) in ip_network(v6) raises TypeError, so guard on family.
            if ip_obj.version != net.version:
                continue
            if ip_obj in net:
                raise _DiscoveryUrlBlocked(f"{label} resolves to disallowed IP ({ip_str} in {net})")

    return url


def _validate_outbound_url(
    url: str,
    allowlist_hosts: tuple[str, ...] | None = None,
    label: str = "OIDC discovery URL",
) -> str:
    """Validate any user-supplied outbound URL (e.g. a connector OpenAPI spec URL).

    Generalizes :func:`_validate_discovery_url` (same https-only + DNS-resolved
    private/IMDS denylist + optional operator allowlist via env). When
    *allowlist_hosts* is provided (e.g. a connector's vetted hosts), the URL's host
    must additionally match one of those globs. Returns the validated URL.

    *label* is forwarded so a caller validating something that is not an OIDC
    discovery document gets a rejection message naming what it actually rejected.

    The host allowlist reads ``OUTBOUND_HOST_ALLOWLIST`` first and falls back to
    ``OIDC_DISCOVERY_HOST_ALLOWLIST``, so nothing an existing operator configured
    stops being enforced, but pinning an identity provider no longer implicitly
    pins every connector spec and LiteLLM base URL to that same host list.
    """
    validated = _validate_discovery_url(
        url,
        label=label,
        allowlist_env=(_OUTBOUND_ALLOWLIST_ENV, _OIDC_ALLOWLIST_ENV),
    )
    if allowlist_hosts:
        host = (urllib.parse.urlparse(validated).hostname or "").lower()
        if not any(fnmatch.fnmatchcase(host, pat.lower()) for pat in allowlist_hosts):
            raise _DiscoveryUrlBlocked(
                f"Connector spec host '{host}' is not in the connector allowlist {list(allowlist_hosts)}"
            )
    return validated


_AUTH_ENDPOINT_ALLOWLIST_ENV = "OAUTH_TOKEN_ENDPOINT_HOST_ALLOWLIST"


def validate_token_endpoint(url: str, label: str = "OAuth token endpoint") -> str:
    """Validate a URL we are about to send an OAuth **client secret** to.

    The discovery URL has been guarded since the DNS-rebinding fix; the token
    endpoint — one function away, and the only one of the two that carries a
    credential — was not guarded at all. It is not operator-typed, which is why it
    was missed: it arrives in the ``token_endpoint`` field of the OIDC discovery
    *document*, so whoever controls that document (a hostile or compromised IDP, or
    anyone who can serve the operator's discovery URL) chooses where the secret is
    POSTed. Reproduced before the fix: ``get_cognito_token`` delivered
    ``client_secret=…`` over plaintext HTTP to ``http://127.0.0.1:<port>/latest/
    meta-data/iam/security-credentials/``, i.e. exactly the host class
    ``_validate_discovery_url`` refuses.

    Same policy as every other outbound URL here (https only, plus the DNS-resolved
    loopback / link-local / RFC1918 / reserved denylist), which is what closes the
    plaintext leak and the pivot onto IMDS, the Lambda credentials endpoint, and any
    VPC-internal host.

    The host allowlist is read from its OWN variable and deliberately does NOT fall
    back to ``OIDC_DISCOVERY_HOST_ALLOWLIST`` / ``OUTBOUND_HOST_ALLOWLIST`` — the
    reason ``_validate_outbound_url`` needed a neutral variable in the first place,
    one step further. An operator who pins their IDP with
    ``OIDC_DISCOVERY_HOST_ALLOWLIST=*.okta.com`` has said nothing about token
    endpoints, and every Cognito gateway in the account mints its token against
    ``*.auth.<region>.amazoncognito.com`` from an endpoint this platform derived
    itself; honouring that pin here would have broken the entire default path while
    claiming to secure it. Unset means no host pinning, which is strictly tighter
    than the previous behaviour of no validation whatsoever.

    Raises ``_DiscoveryUrlInvalid`` / ``_DiscoveryUrlBlocked`` (both ``ValueError``).
    """
    return _validate_discovery_url(url, label=label, allowlist_env=(_AUTH_ENDPOINT_ALLOWLIST_ENV,))


def validate_token_endpoint_shape(url: str, label: str = "OAuth token endpoint") -> str:
    """Scheme + literal-IP check with **no DNS lookup**, for the bake-in paths.

    ``COGNITO_TOKEN_ENDPOINT`` / ``OAUTH_TOKEN_ENDPOINT`` are written into the agent
    runtime's environment (and into generated agent source), and the generated agent
    POSTs its own client secret there — inside the runtime, where none of this
    module's guards run. So the endpoint has to be checked before it is handed over.

    Deliberately weaker than :func:`validate_token_endpoint`, for two reasons that
    point the same way:

    * A DNS answer obtained at deploy time says nothing about the address the agent
      will resolve at invoke time, minutes to months later. Resolving here would
      look stronger while proving nothing extra about the runtime's request.
    * It would add a new DNS dependency to the default Cognito path of every
      gateway deploy, turning a transient resolver failure into a failed deployment
      of an endpoint this platform derived itself.

    What it does catch is the part that is decidable from the string and is fatal:
    a non-https scheme (the secret in cleartext) and a literal address inside the
    denylist (IMDS, the Lambda credentials endpoint, loopback, RFC1918). The
    DNS-resolving check still runs at the two places that actually open a socket
    with the secret in hand — ``get_cognito_token`` and, for an external IDP, the
    discovery-document hop in ``_create_external_oauth_config``.
    """
    if not url or not isinstance(url, str):
        raise _DiscoveryUrlInvalid(f"{label} is empty")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise _DiscoveryUrlInvalid(f"{label} must use https scheme (got '{parsed.scheme}')")
    host = parsed.hostname
    if not host:
        raise _DiscoveryUrlInvalid(f"{label} has no host component")
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        # A name, not a literal. Nothing further is decidable without DNS, and see
        # the docstring for why we do not ask.
        return url
    for net in _DISALLOWED_NETWORKS:
        if ip_obj.version == net.version and ip_obj in net:
            raise _DiscoveryUrlBlocked(f"{label} is a disallowed literal address ({host} in {net})")
    return url


# ---------------------------------------------------------------------------
# Response key helpers
# ---------------------------------------------------------------------------


def _list_all_gateway_targets(agentcore_ctrl, gateway_id: str) -> list[dict]:
    """Return every target on *gateway_id* across control-plane pages.

    Target lookup is used for both creation-conflict adoption and destructive
    retry/teardown. Missing a later page can therefore either create a duplicate
    target or delete a gateway while one of its children still exists.
    """
    return list_all(
        agentcore_ctrl,
        "list_gateway_targets",
        item_keys=("items", "targets", "gatewayTargetSummaries"),
        request={
            "gatewayIdentifier": gateway_id,
            "maxResults": 50,
        },
    )


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
            items = _list_all_gateway_targets(agentcore_ctrl, gateway_id)
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
    return list_all(
        agentcore_ctrl,
        "list_gateways",
        item_keys=("items", "gateways", "gatewaySummaries"),
        request={},
    )


# ---------------------------------------------------------------------------
# Boto3 wrapper helpers
# ---------------------------------------------------------------------------


def _create_lambda_client(region: str):
    return _aws_client("lambda", region_name=region)


def _create_logs_client(region: str):
    return _aws_client("logs", region_name=region)


def _create_iam_client():
    return _aws_client("iam")


def _create_cognito_client(region: str):
    return _aws_client("cognito-idp", region_name=region)


def _create_platform_cognito_client(pool_region: str):
    """A Cognito client for the platform's shared gateway-auth pool.

    CDK created that pool in the PLATFORM account, so only the platform Lambda's own
    credentials can add or remove a gateway's resource server and app client in it.
    This deliberately bypasses the bound target session: routing these calls
    through the deployment account's assumed role addresses a pool that doesn't
    exist there. Everything else, including the secret holding the copied client
    secret, stays on the target session.
    """
    return boto3.client("cognito-idp", region_name=pool_region)


def _create_agentcore_control_client(region: str):
    return _aws_client("bedrock-agentcore-control", region_name=region)


def _create_agentcore_client(region: str):
    return _aws_client("bedrock-agentcore", region_name=region)


def _create_secrets_client(region: str):
    return _aws_client("secretsmanager", region_name=region)


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


def _scoped_provider_name(raw: str, scope: str | None) -> str:
    """Provider name for *raw*, made unique to *scope* (the gateway it serves).

    **AgentCore credential providers live in ONE account-global token vault**,
    but every name this module derives comes from a catalog id, a connector id,
    or a user-typed target label — none of which are per-tenant. Without a scope,
    two different platform users who both wire, say, the ``exa`` catalog entry
    both land on the provider ``mcp-mcp-exa``. The second deploy takes the
    "already exists" branch, so that user's target authenticates with the FIRST
    user's API key while their own freshly minted secret is never read: a
    cross-tenant credential crossover, plus a rotation that silently no-ops.

    Verified on real AWS, which is the only reason we know: two deployments of
    the same custom MCP target minted two new secrets, and both reused a
    provider created by an earlier deployment whose ``lastUpdatedTime`` never
    moved off its ``createdTime``. A deliberately invalid key still produced a
    READY target, because the wrong key was never the one being sent.

    The scope is folded in as a short digest rather than appended raw: names are
    capped at 64 chars and a gateway id would push a long target name over,
    where a blind truncation could re-collide the very names we are separating.
    """
    if not scope:
        return _sanitize_provider_name(raw)
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:10]
    return _sanitize_provider_name(f"{_sanitize_provider_name(raw)[:52]}-{digest}")


def secret_binding_tags(owner_sub: str, deployment_id: str) -> list[dict]:
    """Tags that bind a minted secret to the principal and deploy that asked for it.

    The ``agentcore-connector/`` prefix identifies the PRODUCT and the
    ``AgentCoreStack`` tag identifies the STACK, and neither distinguishes one
    tenant's secret from another's inside the same stack. A teardown that deletes on
    prefix alone, or an authorization check that accepts an ARN because it starts
    with the prefix, therefore treats every tenant's credential as its own.

    ``owner_sub`` is hashed rather than stored: the binding only ever needs to be
    COMPARED, so the tag does not need to carry a user identifier that then shows up
    in `ListSecrets`, Config history and every cost report. A truncated sha256 is
    enough to make a collision infeasible for an attacker who cannot choose their own
    ``sub``.

    Empty inputs contribute no tag at all rather than a tag with an empty value, so
    "unbound" is distinguishable from "bound to nobody" by a reader that checks for
    the key's presence.
    """
    tags: list[dict] = []
    if owner_sub:
        tags.append({"Key": "OwnerSubHash", "Value": owner_sub_hash(owner_sub)})
    if deployment_id:
        tags.append({"Key": "DeploymentId", "Value": str(deployment_id)[:256]})
    return tags


class ConnectorSecretBindingError(ValueError):
    """A credential reference cannot be safely bound to this deployment."""


class ConnectorSecretDeletionRefused(RuntimeError):
    """Teardown could not prove that a secret belongs to this deployment."""


def _connector_owner_hash(owner_sub: str) -> str:
    return owner_sub_hash(owner_sub)


# The attribute a failed CreateSecret's exception carries the generated secret name
# under. See the except clause in _put_connector_secret.
SECRET_CANDIDATE_ATTR = "secret_candidate"  # pragma: allowlist secret

# Set by a caller that owns a durable manifest: called with (secret name, region)
# BEFORE each CreateSecret, and must raise if the row is not durable.
_SECRET_INTENT_JOURNAL: contextvars.ContextVar = contextvars.ContextVar("secret_intent_journal", default=None)


@contextmanager
def secret_intent_journal(record):
    """Journal every secret name this block may create, before it is created.

    A hard kill (Lambda timeout, OOM) between CreateSecret and the manifest row runs
    no handler, and the ARN never reaches anyone. Tag discovery cannot close that on
    its own: ListSecrets is eventually consistent, so an immediate empty result is
    not proof of absence. A pre-create row naming the exact secret is: teardown
    DescribeSecrets that name, deletes it if it exists (after re-proving its tags),
    and reads ResourceNotFound as absent. A name journaled for a create that never
    happened therefore costs one read.
    """
    token = _SECRET_INTENT_JOURNAL.set(record)
    try:
        yield
    finally:
        _SECRET_INTENT_JOURNAL.reset(token)


#: Tag stamped on every ``agentcore-connector/`` secret at mint time (F-01 b). The shared runtime
#: role is one principal for every tenant in ``shared`` identity mode and no IAM condition on it
#: can tell one tenant's secret from another's; what it CAN do is refuse the secrets of
#: deployments that never use it. Infra conditions the shared role's ``GetSecretValue`` on
#: ``aws:ResourceTag/IdentityMode=shared``, so a ``per_agent`` deployment's credentials (read by
#: its own role, by exact ARN) leave the shared role's reach entirely. Infra's ``aws:TagKeys``
#: allowlists for this prefix name this key literally; changing it is an infra change too.
IDENTITY_MODE_TAG_KEY = "IdentityMode"
IDENTITY_MODE_SHARED = "shared"
IDENTITY_MODE_PER_AGENT = "per_agent"

# The mode travels as a ContextVar, like ``_SECRET_INTENT_JOURNAL`` above and for the same
# reason: ``_put_connector_secret`` is reached through layers (deploy_gateway, the LiteLLM and
# MCP deployers, the runtime/KB staging helpers) that never see the identity config. It is set by
# the code that does -- the API's credential staging and the step handlers that mint -- and an
# unset context reads as ``shared``: that is ``IdentityConfig.mode``'s own default and the value
# that keeps a shared-mode runtime readable, so a path that forgets to set it degrades to today's
# behaviour rather than to a green deploy with a dead tool plane.
_CONNECTOR_IDENTITY_MODE: contextvars.ContextVar = contextvars.ContextVar("connector_identity_mode", default=None)


def identity_mode_of(identity_config) -> str:
    """``per_agent`` only when the request/event identity config opts in; otherwise ``shared``.

    Accepts the ``IdentityConfig`` model (API path), its ``model_dump`` dict (the SFN event's
    ``identity_config``) or ``None``. Any other value of ``mode`` is the default: ``shared`` is the
    platform's default identity mode and the only other member of the model's ``Literal``.
    """
    if isinstance(identity_config, dict):
        mode = identity_config.get("mode")
    else:
        mode = getattr(identity_config, "mode", None)
    return IDENTITY_MODE_PER_AGENT if mode == IDENTITY_MODE_PER_AGENT else IDENTITY_MODE_SHARED


@contextmanager
def connector_identity_mode(identity_config):
    """Stamp every connector secret minted in this block with the deployment's identity mode."""
    token = _CONNECTOR_IDENTITY_MODE.set(identity_mode_of(identity_config))
    try:
        yield
    finally:
        _CONNECTOR_IDENTITY_MODE.reset(token)


def current_connector_identity_mode() -> str:
    """The identity mode the enclosing deployment declared, or ``shared`` outside any."""
    return _CONNECTOR_IDENTITY_MODE.get() or IDENTITY_MODE_SHARED


def manifest_secret_journal(
    store, deployment_id: str, target_account_id: str | None = None, *, extra: dict | None = None
):
    """A secret_intent_journal recorder writing strict manifest rows to *store*.

    *extra* is merged into every row, e.g. the gateway step's graph tag."""

    def _record(name: str, region: str) -> None:
        row = {"type": "secret", "id": name, "region": region, "created_by_deployment": True, **(extra or {})}
        if target_account_id:
            row["account"] = target_account_id
        store.record_resource_strict(deployment_id, row)

    return _record


def _put_connector_secret(
    region: str,
    owner_sub: str,
    payload: dict | str,
    deployment_id: str = "",
    *,
    secrets_client=None,
    purpose: str = "connector-credential",
    resource_tags: dict | None = None,
) -> str:
    """Create a Secrets Manager secret holding a connector credential payload.

    Name pattern: ``agentcore-connector/{safe_owner}/{uuid}``. Connector callers
    pass a JSON object such as ``{"apiKey": "..."}``; runtime credential staging
    passes the source secret's raw string unchanged so provider keys and OTEL
    ``Header=Value`` payloads retain their format. Returns the secret ARN. The raw
    value is never logged.
    """
    import uuid as _uuid

    # The owner prefix comes from the shared helper below, so is_own_connector_secret
    # checks the SAME expression this mints. Two copies would diverge silently.
    resource_name = f"{connector_secret_owner_prefix(owner_sub)}{_uuid.uuid4().hex[:12]}"
    sm = secrets_client or _create_secrets_client(region)
    secret_string = json.dumps(payload) if isinstance(payload, dict) else str(payload)
    journal = _SECRET_INTENT_JOURNAL.get()
    if journal is not None:
        # Outside the try: a journal that cannot write stops the create, so nothing
        # exists that no durable record names.
        journal(resource_name, region)
    try:
        resp = sm.create_secret(
            Name=resource_name,
            SecretString=secret_string,
            Description="AgentCore deployment-bound credential (auto-managed)",
            # The agentcore-connector/ prefix marks the PRODUCT, not the deployment, so a
            # prefix sweep over that namespace deletes every deployment's connector
            # credentials in the account — the worst case being one customer teardown
            # destroying another live deployment's raw customer API keys. The owner tag
            # is what makes the sweep in cleanup.sh safe, and the binding tags are what
            # let a per-tenant check tell two secrets in the same stack apart.
            #
            # Governance tags reach a CREDENTIAL secret here, which is worth one line of
            # justification. ARCC cnt_QbLfysVKP69zGk uses a Secrets Manager TAG to mark the
            # secrets that are NOT credentials, so that rotation-health monitoring can filter
            # down to the ones that are; a caller-supplied key able to land that mark would
            # quietly remove this secret from rotation monitoring. It cannot: every governance
            # key is namespaced ("platform:"/"org:", enforced by stampable_governance_tags),
            # and the filter key that guidance uses is unnamespaced. Cost attribution and ABAC
            # (cnt_6gBImtb08AJqCB) are what these tags are for, and a VALUE is never read as
            # authorization here -- ownership stays on the two unnamespaced tags below.
            Tags=governed_tag_list(region, resource_tags, extra={"Purpose": purpose})
            + secret_binding_tags(owner_sub, deployment_id)
            # F-01 (b): the deployment's identity mode, so the shared runtime role's read grant
            # can be conditioned to shared-mode secrets only. See IDENTITY_MODE_TAG_KEY.
            + [{"Key": IDENTITY_MODE_TAG_KEY, "Value": current_connector_identity_mode()}],
        )
    except Exception as exc:
        # The name is generated here, so it is known even when the response is not. A
        # create whose response was lost (a read timeout, a dropped connection) may
        # still have made the secret, with this deployment's tags on it, and the ARN
        # it would have returned is then the one thing nobody has. The caller rolls
        # back by this name; delete_deployment_bound_secret re-proves the tags first,
        # so naming a secret that was never created is harmless. A hard kill runs no
        # handler at all, which is why teardown ALSO discovers secrets by tag
        # (discover_deployment_bound_secrets).
        setattr(exc, SECRET_CANDIDATE_ATTR, resource_name)
        raise
    # SECURITY (CodeQL py/clear-text-logging-sensitive-data): log a CONSTANT only
    # — never the generated name or the payload. The caller gets the ARN.
    logger.info("Created connector credential resource")
    return resp["ARN"]


_SECRETS_MANAGER_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z]+)?):secretsmanager:"
    r"(?P<region>[a-z0-9-]+):(?P<account>\d{12}):secret:(?P<name>[A-Za-z0-9/_+=.@-]+)$"
)


def secrets_manager_arn_location(secret_arn: str) -> tuple[str, str, str]:
    """Return ``(account, region, name)`` for one complete Secrets Manager ARN.

    A substring containing ``:secret:agentcore-provider/`` is classification, not
    validation. This parser anchors the whole ARN so a caller cannot smuggle a
    namespace-looking fragment into an unrelated identifier.
    """
    match = _SECRETS_MANAGER_ARN_RE.fullmatch(str(secret_arn or ""))
    if not match:
        raise ConnectorSecretBindingError("The credential reference is not a valid Secrets Manager ARN.")
    return match.group("account"), match.group("region"), match.group("name")


def stage_runtime_secret_for_deployment(
    *,
    source_secret_ref: str,
    source_namespace: str,
    purpose: str,
    owner_sub: str,
    deployment_id: str,
    target_region: str,
    source_secrets_client,
    target_secrets_client,
    trusted_platform_source: bool = False,
    resource_tags: dict | None = None,
) -> str:
    """Copy a provider/OTEL source secret into this deployment's lifecycle.

    The source ARN is never granted directly to the runtime. For a user-owned
    source, live tags must prove this exact stack and caller before its value is
    read. The value is then copied to ``agentcore-connector/`` in the deployment
    target account/region with exact stack, tenant, and deployment tags. Teardown
    already deletes that namespace only after re-reading those tags.

    ``trusted_platform_source`` is reserved for the exact platform OTEL secret read
    from operator-controlled SSM configuration. It bypasses caller ownership, but
    not full ARN parsing or namespace confinement.
    """
    if not deployment_id:
        raise ConnectorSecretBindingError("A deployment id is required before a runtime credential can be staged.")
    if not owner_sub and not trusted_platform_source:
        raise ConnectorSecretBindingError("The credential owner could not be proven.")

    _account, source_region, source_name = secrets_manager_arn_location(source_secret_ref)
    namespace = str(source_namespace or "").strip("/")
    if not namespace or not source_name.startswith(f"{namespace}/"):
        raise ConnectorSecretBindingError(
            f"The credential reference must be in the {namespace or 'approved'} namespace."
        )

    try:
        described = source_secrets_client.describe_secret(SecretId=source_secret_ref)
    except Exception as exc:  # noqa: BLE001
        raise ConnectorSecretBindingError(
            "The credential reference could not be described in its source account."
        ) from exc

    if not trusted_platform_source:
        tags = tag_map(described.get("Tags"))
        expected_owner = owner_sub_hash(owner_sub)
        owner_matches = tags.get("OwnerSubHash") == expected_owner or tags.get("owner_sub") == owner_sub
        if not owner_matches:
            raise ConnectorSecretBindingError(
                "The credential belongs to another caller or has no verifiable caller binding."
            )
        if tags.get(OWNER_TAG_KEY) != stack_id(source_region):
            raise ConnectorSecretBindingError(
                "The credential belongs to another platform stack or has no exact stack binding."
            )

    try:
        raw = source_secrets_client.get_secret_value(SecretId=source_secret_ref).get("SecretString")
    except Exception as exc:  # noqa: BLE001
        raise ConnectorSecretBindingError("The credential value could not be read from its source account.") from exc
    if not isinstance(raw, str) or not raw:
        raise ConnectorSecretBindingError("The credential must contain a non-empty text value.")

    return _put_connector_secret(
        target_region,
        owner_sub,
        raw,
        deployment_id,
        secrets_client=target_secrets_client,
        purpose=purpose,
        resource_tags=resource_tags,
    )


def stage_customer_secret_for_deployment(
    *,
    source_secret_ref: str,
    purpose: str,
    owner_sub: str,
    deployment_id: str,
    target_region: str,
    source_secrets_client,
    target_secrets_client,
    resource_tags: dict | None = None,
) -> str:
    """Copy an explicitly opted-in customer secret into this deployment.

    Knowledge Base credential fields historically flowed straight into a new
    Bedrock service-role policy.  That let an API caller turn any readable secret
    ARN into authority for Bedrock.  The source secret must now carry
    ``AgentCoreFlowsAccess=allow`` (and, when present, a matching
    ``OwnerSubHash``).  Its raw value is copied unchanged into an exact
    deployment-bound ``agentcore-connector/`` secret; only that copy reaches the
    Step Functions history and KB execution role.
    """
    if not deployment_id:
        raise ConnectorSecretBindingError(
            "A deployment id is required before a Knowledge Base credential can be staged."
        )

    _account, source_region, _name = secrets_manager_arn_location(source_secret_ref)
    try:
        described = source_secrets_client.describe_secret(SecretId=source_secret_ref)
    except Exception as exc:  # noqa: BLE001
        raise ConnectorSecretBindingError(
            "The Knowledge Base credential could not be described in its source account."
        ) from exc

    try:
        assert_resource_access_allowed(
            "Knowledge Base credential secret",
            described.get("Tags"),
            owner_sub=owner_sub,
            region=source_region,
            deployment_id=deployment_id,
            require_explicit_opt_in=True,
        )
    except ResourceAccessRefused as exc:
        raise ConnectorSecretBindingError(
            "The Knowledge Base credential owner has not authorized this platform "
            f"to use it. Tag the source secret {ACCESS_TAG_KEY}={ACCESS_TAG_VALUE}"
            " and redeploy."
        ) from exc

    try:
        raw = source_secrets_client.get_secret_value(SecretId=source_secret_ref).get("SecretString")
    except Exception as exc:  # noqa: BLE001
        raise ConnectorSecretBindingError(
            "The Knowledge Base credential value could not be read from its source account."
        ) from exc
    if not isinstance(raw, str) or not raw:
        raise ConnectorSecretBindingError("The Knowledge Base credential must contain a non-empty text value.")

    return _put_connector_secret(
        target_region,
        owner_sub,
        raw,
        deployment_id,
        secrets_client=target_secrets_client,
        purpose=purpose,
        resource_tags=resource_tags,
    )


def _is_platform_connector_secret(secret_arn: str) -> bool:
    """True when *secret_arn* is inside the platform-managed secret namespace.

    This is classification, not delete authority. Every tenant and deployment shares
    the ``agentcore-connector/`` namespace, so callers that reuse or delete a secret
    must additionally prove ownership from the live binding tags. The ARN's name
    segment is everything after ``:secret:``.
    """
    name = str(secret_arn or "").partition(":secret:")[2] or str(secret_arn or "")
    return name.startswith("agentcore-connector/")


def connector_secret_owner_prefix(owner_sub: str) -> str:
    """The ``agentcore-connector/{safe_owner}/`` prefix _put_connector_secret mints under.

    One function so the minting name and any ownership check derive from the same
    expression; two copies of this sanitization would silently diverge and the
    check would then reject every legitimate ARN.
    """
    safe_owner = re.sub(r"[^a-zA-Z0-9_-]", "-", (owner_sub or "anon"))[:48]
    return f"agentcore-connector/{safe_owner}/"


def is_own_connector_secret(secret_arn: str, owner_sub: str) -> bool:
    """True when *secret_arn* is a connector secret minted for THIS caller.

    ``_is_platform_connector_secret`` above answers a different question — "did the
    platform mint this, so may teardown delete it" — and is explicitly NOT a tenant
    check: every tenant's secrets share the ``agentcore-connector/`` prefix, so
    accepting an ARN because it carries that prefix treats every tenant's credential
    as the caller's own (see ``secret_binding_tags``).

    The name ``_put_connector_secret`` mints is
    ``agentcore-connector/{safe_owner}/{uuid}``, and the owner segment is fixed on the
    real secret at creation — a caller cannot rename someone else's secret to carry
    their own sub. Cognito subs are UUIDs, so the sanitize-and-truncate is injective
    over them and the prefix match is proof of ownership.

    This helper is intentionally only one ownership signal. The binding path also
    reads ``OwnerSubHash`` from ``DescribeSecret`` and rejects any conflict; the name
    fallback exists for legacy same-owner secrets minted before that tag was added.
    It must never be used by itself as delete authority.
    """
    if not owner_sub:
        # No caller identity means nothing can be proven. Fail closed.
        return False
    name = str(secret_arn or "").partition(":secret:")[2] or str(secret_arn or "")
    return name.startswith(connector_secret_owner_prefix(owner_sub))


def _read_connector_secret_key(secrets_client, secret_ref: str, payload_key: str) -> str:
    """Read one expected credential field without ever returning/logging its ref."""
    try:
        raw = secrets_client.get_secret_value(SecretId=secret_ref).get("SecretString")
        payload = json.loads(raw or "")
    except Exception as exc:  # noqa: BLE001
        raise ConnectorSecretBindingError(
            "The stored credential could not be read as a JSON secret. Supply the raw "
            "credential so the platform can create a fresh deployment-bound copy."
        ) from exc
    value = payload.get(payload_key) if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        raise ConnectorSecretBindingError(
            f"The stored credential does not contain a non-empty {payload_key} field. "
            "Supply the raw credential so the platform can create a fresh copy."
        )
    return value


def bind_connector_secret_for_deployment(
    *,
    region: str,
    owner_sub: str,
    deployment_id: str,
    payload_key: str,
    raw_value: str | None = None,
    secret_ref: str | None = None,
    secrets_client=None,
    resource_tags: dict | None = None,
) -> tuple[str, bool]:
    """Return an exact-current-deployment credential ARN.

    Raw input always wins and is minted into a new secret. A supplied reference
    is accepted in place only when its live tags prove the exact current stack and
    deployment. An older same-owner/legacy reference is read once and copied into
    a fresh current-deployment secret, preventing two deployments from sharing a
    credential that either teardown could otherwise delete. Foreign, conflicting,
    or unprovable references fail closed.

    Returns ``(arn, created)``. The caller must durably record every created ARN
    before starting or advancing an asynchronous deployment.
    """
    if not deployment_id:
        raise ConnectorSecretBindingError("A deployment id is required before a credential can be stored or reused.")
    if payload_key not in {"apiKey", "clientSecret"}:
        raise ConnectorSecretBindingError("Unsupported credential payload type.")

    sm = secrets_client or _create_secrets_client(region)
    if isinstance(raw_value, str) and raw_value:
        return (
            _put_connector_secret(
                region,
                owner_sub,
                {payload_key: raw_value},
                deployment_id,
                secrets_client=sm,
                resource_tags=resource_tags,
            ),
            True,
        )

    ref = str(secret_ref or "")
    if not ref:
        raise ConnectorSecretBindingError(
            "A credential value is required. Supply the raw credential so the platform "
            "can store a deployment-bound copy."
        )
    if not _is_platform_connector_secret(ref):
        raise ConnectorSecretBindingError(
            "The credential reference is outside the platform-managed secret namespace. "
            "Supply the raw credential so the platform can store a safe copy."
        )

    try:
        described = sm.describe_secret(SecretId=ref)
    except Exception as exc:  # noqa: BLE001
        raise ConnectorSecretBindingError(
            "The credential reference could not be described. Supply the raw credential "
            "so the platform can store a fresh copy."
        ) from exc

    tags = tag_map(described.get("Tags"))
    expected_stack = stack_id(region)
    stack_tag = tags.get(OWNER_TAG_KEY)
    deployment_tag = tags.get("DeploymentId")
    owner_hash = tags.get("OwnerSubHash")
    expected_owner_hash = _connector_owner_hash(owner_sub) if owner_sub else ""
    exact_current = stack_tag == expected_stack and deployment_tag == deployment_id

    if stack_tag and stack_tag != expected_stack:
        raise ConnectorSecretBindingError(
            "The credential belongs to another platform stack. Supply the raw credential "
            "so this stack can store its own copy."
        )
    if owner_hash and (not expected_owner_hash or owner_hash != expected_owner_hash):
        raise ConnectorSecretBindingError(
            "The credential belongs to another caller. Supply the raw credential so the "
            "platform can store your own copy."
        )

    owner_proven = bool(
        (expected_owner_hash and owner_hash == expected_owner_hash)
        or is_own_connector_secret(ref, owner_sub)
        # A freshly pre-bound anonymous credential is internal to this execution:
        # exact stack+deployment tags are stronger than a missing owner identity.
        or (exact_current and not owner_sub and not owner_hash)
    )
    if not owner_proven:
        raise ConnectorSecretBindingError(
            "The credential owner could not be proven. Supply the raw credential so the "
            "platform can store a deployment-bound copy."
        )

    value = _read_connector_secret_key(sm, ref, payload_key)
    if exact_current:
        # Return the CANONICAL ARN, never the caller's reference verbatim. ``ref`` may be a bare
        # SecretId -- ``_is_platform_connector_secret`` accepts one -- and this was the only
        # branch that could hand a bare name back to the caller, contradicting both this
        # function's own contract ("Returns ``(arn, created)``") and the manifest's: the API
        # records what this returns into ``recorded_secret_arns``, and a resource manifest row
        # keyed on a bare name carries neither the account nor the region that teardown needs to
        # find it again. It also made a prepared payload's staged-reference membership check
        # unsatisfiable, because a bare entry is not a well-formed ARN.
        #
        # ``described`` is already in hand from the DescribeSecret above, so this costs no extra
        # call, and the ARN it returns is by construction in this client's own account and region.
        canonical = str(described.get("ARN") or "")
        if not canonical:
            raise ConnectorSecretBindingError(
                "The credential reference could not be resolved to a canonical ARN. Supply the "
                "raw credential so the platform can store a fresh copy."
            )
        # Fails CLOSED on an unparseable ARN rather than passing it on: everything downstream
        # (the manifest, teardown, the runtime grant) treats this value as an authoritative ARN.
        secrets_manager_arn_location(canonical)
        return canonical, False

    # Same owner, but an older/legacy/partially-tagged secret. Copy rather than
    # share: deletion of either deployment must never break the other.
    return (
        _put_connector_secret(
            region,
            owner_sub,
            {payload_key: value},
            deployment_id,
            secrets_client=sm,
            resource_tags=resource_tags,
        ),
        True,
    )


def delete_deployment_bound_secret(
    *,
    region: str,
    deployment_id: str,
    secret_ref: str,
    secrets_client=None,
) -> bool:
    """Force-delete only a secret proven to belong to this exact deployment.

    Returns ``True`` when a delete was issued and ``False`` when the secret was
    already absent. Any missing/mismatched proof raises
    :class:`ConnectorSecretDeletionRefused`; a manifest row is a record of intent,
    not authority over whichever resource currently occupies that identifier.
    """
    if not deployment_id:
        raise ConnectorSecretDeletionRefused("Secret deletion refused because the deployment id is unavailable.")
    if not _is_platform_connector_secret(secret_ref):
        raise ConnectorSecretDeletionRefused(
            "Secret deletion refused because the resource is outside the platform namespace."
        )

    sm = secrets_client or _create_secrets_client(region)
    try:
        described = sm.describe_secret(SecretId=secret_ref)
    except Exception as exc:  # noqa: BLE001
        if is_error(exc, "ResourceNotFoundException", "NotFoundException") or "not found" in str(exc).lower():
            return False
        raise

    tags = tag_map(described.get("Tags"))
    if tags.get(OWNER_TAG_KEY) != stack_id(region) or tags.get("DeploymentId") != deployment_id:
        raise ConnectorSecretDeletionRefused(
            "Secret deletion refused because exact stack and deployment ownership could not be proven."
        )
    sm.delete_secret(SecretId=secret_ref, ForceDeleteWithoutRecovery=True)
    return True


# 100 per page: 5,000 secrets for ONE deployment id is far past anything a deploy mints.
_DISCOVERY_PAGE_BUDGET = 50


def discover_deployment_bound_secrets(*, deployment_id: str, secrets_client) -> list[str]:
    """ARNs of every platform-namespace secret tagged with this exact deployment id.

    A manifest row is written only after a create RETURNS. A create whose response
    was lost, or a step killed between the create and the row (a Lambda timeout, an
    OOM, SIGKILL), leaves a secret no row names, and no exception handler runs to
    report it. The tags are written by the create itself, atomically with the
    secret, so they are the one record that cannot be lost that way.

    Discovery is not authority: every ARN returned here still goes through
    delete_deployment_bound_secret, which re-reads the tags and refuses unless the
    stack and the deployment id both match. The ``tag-value`` filter matches the
    value under ANY key, so the exact ``DeploymentId`` tag is also checked here.
    """
    if not deployment_id:
        return []
    filters = [
        {"Key": "name", "Values": ["agentcore-connector/"]},
        {"Key": "tag-key", "Values": ["DeploymentId"]},
        {"Key": "tag-value", "Values": [deployment_id]},
    ]
    found: list[str] = []
    token = ""
    seen_tokens: set[str] = set()
    # Fails closed rather than looping: an unexpected response shape once made this
    # follow a truthy non-string token thousands of times a second. A raise here is
    # reported by both teardowns as "discovery incomplete", never as clean.
    for _page_number in range(_DISCOVERY_PAGE_BUDGET):
        kwargs: dict = {"Filters": filters, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        page = secrets_client.list_secrets(**kwargs)
        entries = page.get("SecretList") if isinstance(page, dict) else None
        if not isinstance(entries, list):
            raise RuntimeError("ListSecrets returned an unexpected response shape")
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError("ListSecrets returned an unexpected entry shape")
            arn = str(entry.get("ARN") or "")
            if (
                arn
                and _is_platform_connector_secret(arn)
                and tag_map(entry.get("Tags")).get("DeploymentId") == deployment_id
            ):
                found.append(arn)
        token = page.get("NextToken")
        if token is None or token == "":
            return found
        if not isinstance(token, str):
            raise RuntimeError("ListSecrets returned a non-string NextToken")
        if token in seen_tokens:
            raise RuntimeError("ListSecrets repeated a NextToken")
        seen_tokens.add(token)
    raise RuntimeError(f"ListSecrets did not finish within {_DISCOVERY_PAGE_BUDGET} pages")


def unrecorded_deployment_secret_rows(
    *,
    deployment_id: str,
    recorded_rows: list[dict],
    region: str,
    secrets_client_for,
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Manifest rows for this deployment's secrets that no recorded row names.

    Searched in the deployment region and in every region a recorded secret row
    names. Returns the rows and, per region whose discovery failed, ``(region, error
    type)``: a teardown must report that as incomplete, never as clean.
    """
    if not deployment_id:
        return [], []
    recorded: set[str] = set()
    regions = {region}
    for row in recorded_rows:
        if row.get("type") == "secret":
            rid = str(row.get("id") or "")
            recorded.update({rid, rid.partition(":secret:")[2]})
            if row.get("region"):
                regions.add(str(row["region"]))
    rows: list[dict] = []
    failures: list[tuple[str, str]] = []
    for secret_region in sorted(regions):
        try:
            arns = discover_deployment_bound_secrets(
                deployment_id=deployment_id, secrets_client=secrets_client_for(secret_region)
            )
        except Exception as exc:  # noqa: BLE001
            failures.append((secret_region, type(exc).__name__))
            continue
        for arn in arns:
            name = arn.partition(":secret:")[2]
            # The ARN's name carries a 6-character suffix ("-AbCdEf") the recorded
            # name, a rollback's candidate, does not.
            if arn in recorded or name in recorded or name[:-7] in recorded:
                continue
            rows.append({"type": "secret", "id": arn, "region": secret_region, "created_by_deployment": True})
    return rows, failures


def _ensure_api_key_credential_provider(
    agentcore_ctrl,
    name: str,
    *,
    secret_arn: str,
    json_key: str = "apiKey",
    scope: str | None = None,
    region: str | None = None,
    resource_tags: dict | None = None,
) -> str:
    """Create (or reuse) an API-key credential provider backed by our own secret.

    Returns the credential provider ARN. Idempotent: on conflict the existing
    provider is looked up and its ARN returned — but *reuse means reuse of the
    name, never of a stale credential*. If the existing provider points at a
    different secret it is repointed at ``secret_arn`` before its ARN is
    returned, because the alternative is an agent that keeps sending the key it
    was deployed with the first time and ignores every rotation after that.

    ``scope`` (the gateway id) namespaces the provider — see
    ``_scoped_provider_name`` for why an unscoped name is a cross-tenant
    credential problem, not just an untidy one.
    """
    provider_name = _scoped_provider_name(name, scope)
    try:
        resp = agentcore_ctrl.create_api_key_credential_provider(
            name=provider_name,
            apiKeySecretConfig={"secretId": secret_arn, "jsonKey": json_key},
            apiKeySecretSource="EXTERNAL",
            tags=governed_tags(region, resource_tags),
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
            except Exception:  # noqa: BLE001
                # SECURITY: constant only — provider_name is taint-flagged (see above).
                logger.debug("API-key provider conflict lookup failed; re-raising original error")
                raise e from None
            assert_agentcore_resource_owned(
                agentcore_ctrl,
                "api_key_credential_provider",
                provider_name,
                region,
            )
            arn = got.get("credentialProviderArn") or got.get("apiKeyCredentialProviderArn", "")
            secret_ref = got.get("apiKeySecretArn")
            existing = (secret_ref if isinstance(secret_ref, dict) else {}).get("secretArn") or ""
            if secret_arn and existing and existing != secret_arn:
                # The key changed (rotation, or a redeploy with a new value).
                # Reusing the name while leaving the OLD secret attached is how a
                # corrected key silently fails to take effect. Note this repoint is
                # OUTSIDE the lookup's try: a failure here must surface as itself,
                # not be folded back into the original "already exists" error.
                try:
                    agentcore_ctrl.update_api_key_credential_provider(
                        name=provider_name,
                        apiKeySecretConfig={"secretId": secret_arn, "jsonKey": json_key},
                        apiKeySecretSource="EXTERNAL",
                    )
                except Exception as ue:  # noqa: BLE001
                    # Deliberately fatal. Returning the ARN anyway would hand back a
                    # provider still bound to the previous secret, so the agent would
                    # authenticate with a key the deployer already knows is stale —
                    # the exact silent failure this branch exists to close.
                    raise RuntimeError(
                        "An API-key credential provider already exists for this gateway target but is "
                        f"bound to an older secret, and it could not be repointed ({type(ue).__name__}). "
                        "Deploying anyway would send the previous key. Grant "
                        "bedrock-agentcore:UpdateApiKeyCredentialProvider, or delete the provider and retry."
                    ) from ue
                # SECURITY: constant only — the ARNs are taint-flagged.
                logger.info("Repointed an existing API-key credential provider at the current secret")
            return arn
        raise


# Phase 3 (Loom) OBO — on-behalf-of token-exchange grant types (RFC 8693 /
# RFC 7523) and their AgentCore client-auth pairing. Verified against the live
# bedrock-agentcore-control service model (boto3 1.43.8):
#   TOKEN_EXCHANGE (RFC 8693, e.g. Okta) → CLIENT_SECRET_BASIC + actorTokenContent NONE
#   JWT_AUTHORIZATION_GRANT (RFC 7523, e.g. Entra ID) → CLIENT_SECRET_POST
_OBO_GRANT_TYPES = ("TOKEN_EXCHANGE", "JWT_AUTHORIZATION_GRANT")
_BASIC_CLIENT_AUTH = "CLIENT_SECRET_BASIC"
_OBO_CLIENT_AUTH = {
    "TOKEN_EXCHANGE": _BASIC_CLIENT_AUTH,
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
    scope: str | None = None,
    region: str | None = None,
    resource_tags: dict | None = None,
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

    # ``scope`` (the gateway id) namespaces the provider for the reason spelled out
    # in _scoped_provider_name — and it matters more here than for an API key,
    # since the shared credential would be an OAuth *client secret*.
    provider_name = _scoped_provider_name(name, scope)
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
            tags=governed_tags(region, resource_tags),
        )
        # SECURITY (CodeQL py/clear-text-logging-sensitive-data): constant only.
        logger.info("Created OAuth2 credential provider")
        return resp["credentialProviderArn"]
    except Exception as e:  # noqa: BLE001
        # "already exists" fallback kept: existing providers can surface as a
        # ValidationException with an "already exists" message, not only a
        # ConflictException (see _ensure_api_key_credential_provider).
        if is_error(e, "ConflictException") or "already exists" in str(e):
            # Only the LOOKUP may fall back to the original conflict error. The ownership
            # refusal and the repoint failure below are real, actionable errors and must
            # propagate as themselves: wrapped in this try they were re-raised as the bare
            # "already exists" with a DEBUG line, and a denied UpdateOauth2CredentialProvider
            # left the provider silently on the previous deployment's client (redeploy audit
            # 2026-09-28, row 3; the API-key sibling already keeps its repoint outside).
            try:
                got = agentcore_ctrl.get_oauth2_credential_provider(name=provider_name)
            except Exception as lookup_exc:  # noqa: BLE001
                # SECURITY: constant only — provider_name is taint-flagged.
                logger.debug("OAuth2 provider conflict lookup failed; re-raising original error")
                raise e from lookup_exc
            assert_agentcore_resource_owned(
                agentcore_ctrl,
                "oauth2_credential_provider",
                provider_name,
                region,
            )
            # Repoint at the config we were just asked for, for the same
            # reason the API-key path does: a reused name must not pin an
            # agent to the client id / secret of whoever deployed first.
            try:
                agentcore_ctrl.update_oauth2_credential_provider(
                    name=provider_name,
                    credentialProviderVendor=vendor,
                    oauth2ProviderConfigInput={config_key: provider_config},
                )
            except Exception as update_exc:  # noqa: BLE001
                # Name the service's reason (AccessDeniedException, ValidationException…)
                # rather than "ClientError": the operator has to know whether to grant
                # bedrock-agentcore:UpdateOauth2CredentialProvider or fix the config.
                update_reason = (
                    getattr(update_exc, "response", {}).get("Error", {}).get("Code") or type(update_exc).__name__
                )
                raise RuntimeError(
                    "An OAuth2 credential provider already exists for this gateway target, "
                    "but it could not be repointed at this deployment's client secret "
                    f"({update_reason}). Reusing it would silently authenticate "
                    "with the previous deployment's credential."
                ) from update_exc
            return got.get("credentialProviderArn", "")
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
    resource_tags: dict | None = None,
) -> str:
    """Create a per-deployment Lambda that queries a Bedrock Knowledge Base."""
    iam_client = _create_iam_client()
    lambda_client = _create_lambda_client(region)

    # Stack-scoped names (F-7d): AgentCore-<stack token>-KBTool-<deployment prefix>. The
    # token keeps a collision inside this stack (a redeploy of the same deployment id) or
    # foreign; the infra grants this stack exactly its own function prefix.
    # Digest of the FULL id, not deployment_id[:8]: two ids sharing eight hex characters used
    # to collapse onto one function and one role, with identical stack tags, so the second
    # deployment's redeploy was "owned" by the first's function (peer 82). The exact
    # DeploymentId tag, checked on every authorization below, is the second half of the fix.
    suffix = deployment_scope_suffix(deployment_id)
    role_name = scoped_role_name("KBTool", stack_id(region), suffix)
    fn_name = scoped_function_name("KBTool", stack_id(region), suffix)

    # Role ensure, policy attach and the function create/update all run under the FUNCTION's
    # lock (peer 3c): the role is the function's paired resource, and a redeploy racing a
    # teardown must not reconcile the role's policy while the other side deletes the pair.
    with shared_lambda_lock(region, fn_name):
        return _kb_role_and_function(
            iam_client,
            lambda_client,
            region,
            gateway_role_arn,
            kb_id,
            foundation_model_arn,
            deployment_id,
            role_name,
            fn_name,
            resource_tags=resource_tags,
        )


def _kb_role_and_function(
    iam_client,
    lambda_client,
    region: str,
    gateway_role_arn: str,
    kb_id: str,
    foundation_model_arn: str,
    deployment_id: str,
    role_name: str,
    fn_name: str,
    resource_tags: dict | None = None,
) -> str:
    """Role + policy + function for one KB tool; the function lock is held by the caller."""
    _role_outcome: dict[str, bool] = {}
    role_arn = _ensure_lambda_role(
        iam_client,
        role_name,
        "Role for KB tool Lambda",
        outcome=_role_outcome,
        region=region,
        # Exact per-deployment binding on the ROLE too (peer 3c): a same-stack name
        # collision must not reuse another deployment's role or rewrite its policy.
        extra_tags={"DeploymentId": deployment_id},
        resource_tags=resource_tags,
    )

    # Attach Bedrock retrieve permissions only to a role this deployment created or
    # can prove it owns. _ensure_lambda_role is strict by default, so a colliding
    # foreign role never reaches either PutRolePolicy OR CreateFunction/PassRole.
    #
    # F-7, and the sharper half of it: _ensure_lambda_role's already-exists branch is
    # deliberately adoptive AND deliberately non-mutating (no tag_role, no
    # put_role_policy) precisely so an identically-named foreign role is never modified.
    # This call then modified it anyway, one line later, which defeats that design in the
    # one place it matters -- and it is worse than a code overwrite, because it GRANTS
    # bedrock:Retrieve/RetrieveAndGenerate/InvokeModel on Resource "*" to a principal
    # belonging to somebody else. Role names here are AgentCoreKBToolRole-<first 8 hex of
    # the deployment id>, so the collision space is small enough to matter.
    if not (_role_outcome.get("created") or _role_outcome.get("owned")):
        # Defence against a future test double or helper regression that returns a
        # role without setting its proof. The production helper currently raises
        # before this point.
        raise ForeignResourceError(f"Refusing to create the KB tool Lambda under unowned IAM role {role_name}.")
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

    # Create or update Lambda with environment variables (the caller holds the function's
    # write lock, F-7d: authorize -> create/update -> AddPermission is one fenced sequence
    # against the teardown's authorize -> delete of the same name).
    return _create_or_update_kb_function(
        lambda_client,
        fn_name,
        role_arn,
        zip_bytes,
        kb_id,
        foundation_model_arn,
        deployment_id,
        gateway_role_arn,
        region,
        resource_tags=resource_tags,
    )


def _create_or_update_kb_function(
    lambda_client,
    fn_name: str,
    role_arn: str,
    zip_bytes: bytes,
    kb_id: str,
    foundation_model_arn: str,
    deployment_id: str,
    gateway_role_arn: str,
    region: str,
    resource_tags: dict | None = None,
) -> str:
    """The body of :func:`create_knowledge_base_lambda` past the role; lock held by the caller."""
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
            # See _create_or_update_lambda: tag at creation so the conflict branch has
            # evidence to read, and note that Tags= is authorized as lambda:TagResource.
            # DeploymentId is the per-deployment half of the proof (peer 82).
            Tags=governed_tags(region, resource_tags, extra={"DeploymentId": deployment_id}),
        )
        lambda_arn = resp["FunctionArn"]
    except lambda_client.exceptions.ResourceConflictException:
        # Update existing -- but only after proving we may. A redeploy of the SAME
        # deployment is the common case and passes on the owner tag; a different
        # deployment whose id shares these 8 hex characters is refused (F-7).
        #
        # F-7d: each of the two mutations is fenced on its own fresh, ownership-proven read
        # (RevisionId); see _create_or_update_lambda for why one authorization up front is
        # a race, not a proof.
        _wait_lambda_updatable(lambda_client, fn_name)
        current = _authorized_fresh_read(lambda_client, fn_name, region, required_tags={"DeploymentId": deployment_id})
        _retry_lambda_mutation(
            lambda_client,
            fn_name,
            lambda: lambda_client.update_function_code(
                FunctionName=fn_name,
                ZipFile=zip_bytes,
                RevisionId=current["Configuration"]["RevisionId"],
            ),
        )
        _wait_lambda_updatable(lambda_client, fn_name, require_success=True)
        current = _authorized_fresh_read(lambda_client, fn_name, region, required_tags={"DeploymentId": deployment_id})
        _retry_lambda_mutation(
            lambda_client,
            fn_name,
            lambda: lambda_client.update_function_configuration(
                FunctionName=fn_name,
                Environment={
                    "Variables": {
                        "KNOWLEDGE_BASE_ID": kb_id,
                        "FOUNDATION_MODEL_ARN": foundation_model_arn,
                    },
                },
                RevisionId=current["Configuration"]["RevisionId"],
            ),
        )
        _wait_lambda_updatable(lambda_client, fn_name, require_success=True)
        resp = lambda_client.get_function(FunctionName=fn_name)
        lambda_arn = resp["Configuration"]["FunctionArn"]

    # The invoke grant for THIS gateway's role, on both branches (peer 82): it used to be
    # added only inside the create branch under a static StatementId, so a redeploy onto an
    # existing KB function after the gateway (and its role) had been recreated repointed the
    # target at a function the new role could not invoke. Per-role StatementId, like the
    # shared functions', after pruning dangling principals (Bug 168) and with the
    # propagation retry (Bug 149).
    if gateway_role_arn:
        _prune_orphaned_lambda_permissions(lambda_client, fn_name)
        _gw_role_name = gateway_role_arn.rsplit("/", 1)[-1]
        _stmt_id = re.sub(r"[^A-Za-z0-9_-]", "-", f"AllowAgentCoreInvoke-{_gw_role_name}")[:100]
        _last = None
        for _att in range(8):
            try:
                lambda_client.add_permission(
                    FunctionName=fn_name,
                    StatementId=_stmt_id,
                    Action="lambda:InvokeFunction",
                    Principal=gateway_role_arn,
                )
                _last = None
                break
            except lambda_client.exceptions.ResourceConflictException:
                _last = None
                break  # this gateway role is already permitted
            except lambda_client.exceptions.InvalidParameterValueException as e:
                if "principal" not in str(e).lower():
                    raise
                _last = e
                time.sleep(8)
        if _last is not None:
            raise _last

    _wait_lambda_updatable(lambda_client, fn_name, require_success=True)

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


def _custom_tool_resource_names(
    tool_name: str,
    owner_sub: str,
    gateway_id: str,
    region: str,
) -> tuple[str, str, str, str]:
    """Return the Lambda, role, target-safe tool name and scope binding for one owner's gateway.

    The scope is the gateway because the TARGET is: it is named ``CT-<tool>`` on the
    gateway, so every deployment on one gateway already shares one target. F-66,
    measured live: with a per-deployment scope a redeploy created its own function,
    found the target taken, and reused it still pointing at the FIRST deployment's
    function. Deleting that first deployment then deleted the function the surviving
    deployment's tool invoked, and every call returned isError. Scoped to the gateway,
    the redeploy updates the one function in place and records it as adopted, so the
    teardown co-residency gate keeps it while any deployment on the gateway is live.
    Owners stay isolated twice: the owner is in the scope, and a gateway another
    owner's live deployment is on is refused before any target is touched (F-63).
    """
    if not gateway_id:
        raise RuntimeError(
            "A gateway id is required to scope custom-tool Lambda and IAM resources to the gateway that invokes them."
        )
    scope_material = f"{owner_sub or stack_id(region)}:{gateway_id}"
    binding = hashlib.sha256(scope_material.encode()).hexdigest()
    # Twelve hex of the digest in the NAME (48 bits, collision-resistant for one stack's
    # scopes; it was eight, i.e. 32 bits, and peer 5a showed two scopes colliding onto one
    # function and role), and the FULL digest as the ``ToolScope`` tag that is verified on
    # both the function and the role before any reuse. The name is never the sole binding.
    scope = binding[:12]
    # 20, not 31: the stack token (F-7d) and the longer scope digest now sit in the name, and
    # the digest must survive the 64-character clip, so the tool's own name gives up the room.
    safe_name = re.sub(r"[^a-zA-Z0-9-]", "-", tool_name)[:20]
    return (
        scoped_function_name("CustomTool", stack_id(region), f"{safe_name}-{scope}"),
        scoped_role_name("CustomTool", stack_id(region), f"{safe_name}-{scope}"),
        safe_name,
        binding,
    )


def _ensure_lambda_role(
    iam_client,
    role_name: str,
    description: str,
    outcome: dict[str, bool] | None = None,
    *,
    region: str | None = None,
    extra_tags: dict[str, str] | None = None,
    resource_tags: dict | None = None,
) -> str:
    """Create or reuse an IAM role for a Lambda function. Returns the role ARN.

    When *outcome* is passed it is filled in with ``created`` (this call made the role)
    and ``owned`` (an existing role whose tags prove it belongs to this deployment).
    *extra_tags* is the role's exact binding beyond the stack (a custom tool's
    ``ToolScope``): written at create and REQUIRED to match on reuse, so a role of this
    stack that belongs to another scope is a collision, refused like a foreign one.

    Reuse fails closed -- it RAISES ``ForeignResourceError`` -- with no exemption for any
    caller. Passing a foreign role to ``CreateFunction`` is
    itself a confused-deputy problem even if this helper does not mutate the role: the
    new function would execute with somebody else's permissions. The
    ``allow_unowned_reuse`` exception that used to exist for the two shared tool roles
    is gone (F-7d): its caller "proved" the binding by adopting an untagged function, so a
    foreign untagged function+role pair passed. Shared tool roles are now stack-scoped by
    name (``naming.scoped_role_name``), so a legacy unowned singleton is never this
    stack's role and there is nothing left to reuse.
    """
    if outcome is not None:
        outcome.clear()
        outcome["created"] = False
        outcome["owned"] = False
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
            # Ownership tags at CREATION, not later. These role names are
            # account-global singletons (AgentCoreDynamicToolsLambdaRole,
            # AgentCoreCustomerSupportLambdaRole), so a name is not proof of
            # ownership: the live test account holds an AgentCoreDynamicToolsLambdaRole
            # created months earlier by something else entirely. The tag is the only
            # thing that lets _release_shared_tool_lambda delete the role it created
            # while refusing to touch an identically-named foreign one — see
            # resource_ownership.is_owned_by_this_stack, which returns False for
            # untagged on purpose.
            Tags=governed_tag_list(region, resource_tags, extra=extra_tags),
            **create_role_kwargs(),
        )
        role_arn = resp["Role"]["Arn"]
        iam_client.attach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
        logger.info("Created IAM role: %s", role_arn)
        if outcome is not None:
            outcome["created"] = True
            outcome["owned"] = True
        time.sleep(10)
    except iam_client.exceptions.EntityAlreadyExistsException:
        # Already exists: reuse ONLY a role whose tags prove it is this stack's (and, when
        # a binding is required, this exact scope's); anything else raises (F-7d). This
        # branch used to be "deliberately adoptive" for the untagged
        # AgentCoreDynamicToolsLambdaRole the live account holds from months earlier;
        # that role is foreign, and the shared roles are stack-scoped by name now, so
        # nothing this stack creates can collide with it. No tag_role, no
        # put_role_policy here either way.
        _existing_role = iam_client.get_role(RoleName=role_name)["Role"]
        role_arn = _existing_role["Arn"]
        if can_this_deployment_mutate(_existing_role.get("Tags"), region):
            existing_tags = tag_map(_existing_role.get("Tags"))
            mismatch = {k: existing_tags.get(k) for k, v in (extra_tags or {}).items() if existing_tags.get(k) != v}
            if mismatch:
                # This stack's role, but another scope's (F-7d, peer 5a): a name collision
                # between two custom tools, not a role to reuse. Refused before PassRole.
                raise ForeignResourceError(
                    f"IAM role {role_name} belongs to this stack but is bound to "
                    + ", ".join(f"{k}={v or '<untagged>'}" for k, v in sorted(mismatch.items()))
                    + ", not to this tool's scope, so it will not be reused or passed to a function."
                ) from None
            if outcome is not None:
                outcome["owned"] = True
            # F-06: this stack's role, proven above; retrofit the permissions boundary.
            ensure_role_boundary(iam_client, role_name, role=_existing_role)
            logger.info("Reusing existing IAM role: %s", role_arn)
        else:
            # Untagged or foreign: refused before any function is created under it (F-7d).
            assert_this_deployment_may_mutate(
                f"IAM role {role_name}",
                _existing_role.get("Tags"),
                region,
            )
    return role_arn


def _wait_lambda_updatable(
    lambda_client,
    function_name: str,
    timeout: int = 90,
    *,
    require_success: bool = False,
) -> dict:
    """Block until *function_name* is in a state that accepts an update.

    A Lambda mid-create/mid-update has State=Pending or LastUpdateStatus=InProgress;
    update_function_code/configuration then throws ResourceConflictException. We
    poll until State=Active AND LastUpdateStatus != InProgress so concurrent
    gateway deploys serialize cleanly on the shared singleton tool Lambda.

    ``require_success`` distinguishes recovery from verification. Before issuing a
    new mutation, an Active function whose *previous* update failed is still ready
    for another attempt. After issuing our own mutation, that same status must fail
    the deployment rather than report a change Lambda rejected.
    """
    import time as _t

    deadline = _t.time() + timeout
    while _t.time() < deadline:
        cfg = lambda_client.get_function(FunctionName=function_name)["Configuration"]
        state = cfg.get("State", "Active")
        last = cfg.get("LastUpdateStatus", "Successful")
        if state == "Failed":
            reason = (
                cfg.get("StateReason")
                or cfg.get("LastUpdateStatusReason")
                or "Lambda reported a failed state without a reason"
            )
            raise RuntimeError(f"Lambda function {function_name} entered a failed state: {reason}")
        if last == "Failed" and require_success:
            reason = (
                cfg.get("LastUpdateStatusReason")
                or cfg.get("StateReason")
                or "Lambda reported a failed update without a reason"
            )
            raise RuntimeError(f"Lambda function {function_name} update failed: {reason}")
        if state == "Active" and last != "InProgress":
            return cfg
        _t.sleep(3)
    raise TimeoutError(f"Lambda function {function_name} did not become updateable within {timeout} seconds")


def _authorized_fresh_read(
    lambda_client, function_name: str, region: str | None, *, required_tags: dict[str, str] | None = None
) -> dict:
    """``GetFunction`` plus the ownership proof on THAT read, for one fenced mutation.

    Returns the GetFunction response; its ``Configuration.RevisionId`` is the fence the
    caller sends with the mutation. Raises ``ForeignResourceError`` when the function read
    here is not this deployment's -- which, between two mutations, means somebody else
    touched it and the second mutation must not happen. *required_tags* makes the proof
    per-deployment or per-scope (the KB tool, a custom tool), not merely per-stack.
    """
    current = lambda_client.get_function(FunctionName=function_name)
    _authorize_tool_function_replacement(lambda_client, function_name, region, required_tags=required_tags)
    if not (current.get("Configuration") or {}).get("RevisionId"):
        raise RuntimeError(f"GetFunction for {function_name} returned no RevisionId; refusing an unfenced update")
    return current


def _retry_lambda_mutation(
    lambda_client,
    function_name: str,
    operation,
    *,
    attempts: int = 8,
):
    """Run one Lambda mutation with bounded conflict recovery.

    A shared tool function can be updated by concurrent gateway deployments. AWS
    returns ResourceConflictException while either update is in flight. Exhausting
    the retry budget must re-raise the final conflict; otherwise the deploy records
    success even though its requested mutation never happened.
    """
    last_conflict = None
    for attempt in range(attempts):
        try:
            return operation()
        except lambda_client.exceptions.ResourceConflictException as exc:
            last_conflict = exc
            _wait_lambda_updatable(lambda_client, function_name)
            if attempt < attempts - 1:
                time.sleep(3)
    if last_conflict is not None:
        raise last_conflict
    raise RuntimeError(f"Lambda mutation for {function_name} had no attempts")


def _prune_orphaned_lambda_permissions(lambda_client, function_name: str) -> int:
    """Remove resource-policy statements whose principal IAM role is gone (Bug 168).

    A shared tool Lambda accumulates one statement per gateway role. When a prior
    gateway's role is deleted on teardown, its statement lingers with a dangling
    principal. A policy holding a dangling principal makes lambda:AddPermission
    reject EVERY subsequent call with "The provided principal was invalid" — which
    bricks all future gateway deploys that reuse this Lambda. We read the policy,
    and for each ``AllowAgentCoreInvoke-<role>`` statement that does not grant the
    role of that name AS IT EXISTS NOW, remove that statement. Returns the number
    pruned. Best-effort: any error is swallowed (the caller still attempts its add +
    retry).

    "Exists now" compares the statement's principal with the role's current ARN, not
    just the name. IAM rewrites a deleted role's principal to its unique id (AROA...),
    and a role recreated under the same name gets a new one, so a name check keeps a
    grant that authorizes nobody. Measured 2026-10-02: the deploy had just recreated
    AgentCoreGateway-support-gateway, the fixture Lambda still held that role's
    statement with principal AROASNV5YASAZHWFURF3I, the name check kept it, and the
    caller's add_permission met ResourceConflictException on the same StatementId.
    Every caller reads that conflict as "already permitted", so the new role was never
    granted and CreateGatewayTarget refused the target.
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
        principal = st.get("Principal")
        if isinstance(principal, dict):
            principal = principal.get("AWS")
        try:
            current_arn = iam.get_role(RoleName=role_name)["Role"].get("Arn")
        except Exception as e:  # noqa: BLE001
            if not is_error(e, "NoSuchEntity", "NoSuchEntityException"):
                # Unknown IAM error — don't risk removing a valid grant.
                logger.debug("get_role(%s) failed with a non-NoSuchEntity error; keeping statement", role_name)
                continue
            current_arn = None
        if current_arn and principal == current_arn:
            continue  # grants the role that exists now — keep the statement
        # The role is gone, or a role was recreated under its name and the statement
        # still names the deleted one's unique id: remove it so the caller's add lands.
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


def _authorize_tool_function_replacement(
    lambda_client, function_name: str, region: str | None = None, *, required_tags: dict[str, str] | None = None
) -> str:
    """Decide whether this deployment may overwrite *function_name*'s code (F-7, F-7d).

    Returns ``"owned"``; raises ``ForeignResourceError`` for everything else.

    Both tool-Lambda paths in this module reached ``update_function_code`` from a bare
    ``except ResourceConflictException``, i.e. they replaced the executable code of
    whatever function happened to hold the name. That is a bigger deal than clobbering
    a neighbour's tool. ARCC ``cnt_pXauQr9E6bKwke``: "lambda:UpdateFunctionCode updates
    the code run by the target Lambda function. A principal with permission to this API
    can update arbitrary code to the lambdas it has access to. This provides privilege
    escalation to the permissions assigned to the lambda functions." ``cnt_L4ZLZgjrCctfxl``
    lists it as named escalation pattern 3 — "IAM principal accesses role by updating
    Lambda function code ... to execute with permissions of the attached execution role".
    So the code we push runs under an execution role we did not choose.

    Two outcomes, and only two:

    1. The tags prove ours -> ``"owned"``.
    2. Anything else -> refuse, BEFORE TagResource, UpdateFunctionCode,
       UpdateFunctionConfiguration or AddPermission. "Anything else" is: a tag set naming
       another deployment; a tag set with no owner at all (a product marker alone proves
       nothing); no tags; and an unreadable tag set.

    Until F-7d the untagged case was a third outcome, "adopt, warn, backfill the tags", kept
    as a deliberate residual so installs predating the ownership tag kept their shared
    singleton. Peer d3 then produced the takeover in code: an existing untagged
    ``AgentCoreDynamicTools`` bound to an unowned role was adopted, tagged as ours, its code
    replaced and our gateway granted invoke -- our code running under a role nobody in this
    deployment chose. The residual is gone. What replaced the compatibility it bought is the
    NAME: every function this stack creates is ``AgentCore-<stack token>-...``
    (``naming.scoped_function_name``), so a legacy unscoped singleton is simply never this
    stack's function, and a collision on the scoped name is either ours (tagged at create)
    or foreign (refused). No adoption mode exists; an operator who wants a legacy function
    under this stack deletes it once nothing uses it and lets the next deploy recreate it.

    An AccessDenied on the tag READ is refused rather than treated as "untagged". That
    direction matters: the other way, a missing ``lambda:ListTags`` grant would make every
    function look untagged and the protection would be silently gone while every deploy
    still went green.
    """
    fn = lambda_client.get_function(FunctionName=function_name)
    function_arn = fn["Configuration"]["FunctionArn"]

    try:
        tags = lambda_client.list_tags(Resource=function_arn).get("Tags") or {}
    except Exception as exc:  # noqa: BLE001
        raise ForeignResourceError(
            f"Lambda function {function_name} already exists, and this deployment could not "
            f"read its tags to check whether it owns it ({type(exc).__name__}), so it will "
            f"not replace the function's code. Grant lambda:ListTags on this stack's "
            f"function prefix to the deploy role and retry."
        ) from exc

    mapped = tag_map(tags)
    if can_this_deployment_mutate(tags, region):
        # A per-deployment or per-scope function (the KB tool's DeploymentId, a custom
        # tool's ToolScope): the stack owning it is not enough, the exact binding must match.
        # Two deployments of one stack used to collapse onto one function name and pass here
        # as "owned" (peers 82 and 5a, F-7d). A name match without the exact binding is a
        # collision, and a collision is refused, never adopted.
        mismatch = {k: mapped.get(k) for k, v in (required_tags or {}).items() if mapped.get(k) != v}
        if not mismatch:
            return "owned"
        recorded = ", ".join(f"{k}={v or '<untagged>'}" for k, v in sorted(mismatch.items()))
        wanted = ", ".join(f"{k}={required_tags[k]}" for k in sorted(mismatch))
        raise ForeignResourceError(
            f"Lambda function {function_name} belongs to this stack but is bound to {recorded}, not "
            f"{wanted}, so this deployment will not replace its code, role or environment. "
            f"Per-deployment and per-scope tool functions are never shared; a name collision "
            f"between two of them is the defect to fix, not a function to adopt."
        )

    owner = mapped.get(OWNER_TAG_KEY) or mapped.get(CDK_PROJECT_TAG_KEY)
    if owner:
        raise ForeignResourceError(
            f"Lambda function {function_name} already exists and belongs to {owner}, not to "
            f"{stack_id(region)}, so this deployment will not replace its code -- doing so "
            f"would run this deployment's code under that function's execution role. Lambda "
            f"function names are account-global. To proceed, either deploy into a different "
            f"account, or -- if this really is a function of yours -- retag it "
            f"{OWNER_TAG_KEY}={stack_id(region)} and redeploy."
        )
    raise ForeignResourceError(
        f"Lambda function {function_name} already exists and carries no ownership tag, so this "
        f"deployment cannot prove it created it and will not replace its code, tag it, or grant "
        f"a gateway invoke on it: doing so would run this deployment's code under an execution "
        f"role it did not choose. Functions this stack creates are named with its own prefix "
        f"and tagged {OWNER_TAG_KEY}={stack_id(region)} at creation. If this function is a "
        f"leftover of a build that predates that, delete it once nothing uses it and redeploy; "
        f"the next deploy recreates it under this stack's name."
    )


def _create_or_update_lambda(
    lambda_client,
    function_name: str,
    role_arn: str,
    zip_bytes: bytes,
    description: str,
    gateway_role_arn: str | None = None,
    region: str | None = None,
    outcome: dict[str, bool] | None = None,
    extra_tags: dict[str, str] | None = None,
    resource_tags: dict | None = None,
) -> str:
    """Create or update a tool Lambda under its per-function write lock (F-7d).

    *extra_tags* is the function's exact binding beyond the stack (a custom tool's
    ``ToolScope``): written at create, and REQUIRED to match on every reuse.

    Every caller -- the two shared functions and the per-gateway custom tools -- goes
    through this wrapper, so the lock is taken for every create-or-update, and it spans
    authorize -> create/update -> AddPermission. A teardown of the same function takes the
    same lock (``_release_shared_tool_lambda``, and the direct deletes in
    ``cleanup_gateway_resources``), so neither side can interleave inside the other's
    authorize-then-mutate sequence. See :func:`_create_or_update_lambda_locked`.
    """
    lock_region = region or os.environ.get("APP_AWS_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
    with shared_lambda_lock(lock_region, function_name):
        return _create_or_update_lambda_locked(
            lambda_client,
            function_name,
            role_arn,
            zip_bytes,
            description,
            gateway_role_arn,
            region=region,
            outcome=outcome,
            extra_tags=extra_tags,
            resource_tags=resource_tags,
        )


def _create_or_update_lambda_locked(
    lambda_client,
    function_name: str,
    role_arn: str,
    zip_bytes: bytes,
    description: str,
    gateway_role_arn: str | None = None,
    region: str | None = None,
    outcome: dict[str, bool] | None = None,
    extra_tags: dict[str, str] | None = None,
    resource_tags: dict | None = None,
) -> str:
    """Create or update a Lambda function. Returns the function ARN. Lock held by the caller.

    When *outcome* is passed, ``outcome["created"]`` says whether this call made the
    function (``False``: it already existed and its code was replaced), as
    ``_ensure_lambda_role`` does for roles. A caller that must not put an adopted
    function in its abort inventory needs to know which happened.

    *region* exists only to make the ownership tag written here and the ownership tag
    READ by the two authorization gates compute the same ``stack_id``. It was omitted,
    so the create stamped ``owner_tags()`` (this Lambda's own ``AWS_REGION``) while
    teardown asks about the region it is tearing down. Same value for a same-region
    deploy, which is why nothing caught it; for a cross-region deploy the deployment
    Lambda runs in us-east-1 and stamps ``…-us-east-1`` on a function it just created
    in eu-central-1, so a teardown passing ``region="eu-central-1"`` would read its own
    function as foreign. Harmless while nothing consulted the tag on the delete path —
    which is exactly what the gate below changes.

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
    if outcome is not None:
        outcome.clear()
        outcome["created"] = False
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
            # Tag at CREATION, because the conflict branch below has to decide whether
            # it may overwrite this function's code and a name is not evidence (F-7).
            # create_function's Tags argument is authorized as lambda:TagResource on the
            # function being created, NOT as part of lambda:CreateFunction -- the third
            # time this repo has paid for that assumption (see
            # infra/tests/test_tool_sandbox_grant.py for the first two). There is no
            # retry-untagged fallback here on purpose: an untagged tool Lambda is the
            # defect, so failing the deploy loudly beats creating one.
            Tags=governed_tags(region, resource_tags, extra=extra_tags),
        )
        lambda_arn = resp["FunctionArn"]
        if outcome is not None:
            outcome["created"] = True
    except lambda_client.exceptions.ResourceConflictException:
        # The shared singleton tool Lambda (AgentCoreDynamicTools etc.) already
        # exists. Concurrent deploys race here: if ANOTHER deploy is mid-update the
        # function is in Pending/InProgress and update_function_code throws
        # ResourceConflictException ("resource ... is currently in the following
        # state: Pending"). Wait for it to settle, then retry the update. Verified
        # live: two parallel gateway deploys collided on AgentCoreDynamicTools.
        #
        # F-7: prove we may replace this function's code BEFORE replacing it. Raises
        # ForeignResourceError when the function belongs to another deployment.
        #
        # F-7d: authorization-to-use is a race unless each mutation is fenced. So for EACH
        # mutation: wait until updatable, take a FRESH GetFunction, re-prove ownership on
        # that read, and send its RevisionId. Lambda rejects the update with
        # PreconditionFailedException if anything changed the function in between; that
        # is raised, never retried as success, because the thing that changed it may have
        # been a teardown or another owner.
        _wait_lambda_updatable(lambda_client, function_name)
        current = _authorized_fresh_read(lambda_client, function_name, region, required_tags=extra_tags)
        configured_role = (current.get("Configuration") or {}).get("Role")
        if configured_role != role_arn:
            # Shared function names are regional but IAM role names are
            # account-global. Builds predating regional role isolation can leave a
            # non-home function attached to the unsuffixed home role. Ownership was
            # proven on THIS read; move only that owned function to the exact role
            # before replacing its code.
            _retry_lambda_mutation(
                lambda_client,
                function_name,
                lambda: lambda_client.update_function_configuration(
                    FunctionName=function_name,
                    Role=role_arn,
                    RevisionId=current["Configuration"]["RevisionId"],
                ),
            )
            _wait_lambda_updatable(
                lambda_client,
                function_name,
                require_success=True,
            )
            current = _authorized_fresh_read(lambda_client, function_name, region, required_tags=extra_tags)
            configured_role = (current.get("Configuration") or {}).get("Role")
            if configured_role != role_arn:
                raise RuntimeError(
                    f"Lambda function {function_name} still uses {configured_role!r} after "
                    f"requesting execution role {role_arn!r}"
                ) from None
        _retry_lambda_mutation(
            lambda_client,
            function_name,
            lambda: lambda_client.update_function_code(
                FunctionName=function_name,
                ZipFile=zip_bytes,
                RevisionId=current["Configuration"]["RevisionId"],
            ),
        )
        _wait_lambda_updatable(
            lambda_client,
            function_name,
            require_success=True,
        )
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

    _wait_lambda_updatable(
        lambda_client,
        function_name,
        require_success=True,
    )

    return lambda_arn


def create_dynamic_gateway_lambda(region: str, gateway_role_arn: str, resource_tags: dict | None = None) -> str:
    """Create a Lambda function with dynamic tools for the MCP gateway."""
    iam_client = _create_iam_client()
    lambda_client = _create_lambda_client(region)
    role_outcome: dict[str, bool] = {}
    # Stack-scoped role name (F-7d): a collision is this stack's own role or foreign, and
    # _ensure_lambda_role fails closed on foreign. No unowned-reuse path exists any more.
    role_arn = _ensure_lambda_role(
        iam_client,
        shared_tool_role_name("DynamicTools", region),
        "Role for AgentCore Dynamic Tools Lambda",
        outcome=role_outcome,
        region=region,
        resource_tags=resource_tags,
    )
    zip_bytes = _create_lambda_zip(DYNAMIC_TOOLS_LAMBDA_CODE)
    # _create_or_update_lambda takes the function's write lock (F-7d) for every caller.
    return _create_or_update_lambda(
        lambda_client,
        shared_tool_function_name("DynamicTools", region),
        role_arn,
        zip_bytes,
        "Dynamic tools for AgentCore Gateway",
        gateway_role_arn,
        region=region,
        resource_tags=resource_tags,
    )


def create_customer_support_lambda(region: str, gateway_role_arn: str, resource_tags: dict | None = None) -> str:
    """Create a Lambda function with customer support tools for the MCP gateway."""
    iam_client = _create_iam_client()
    lambda_client = _create_lambda_client(region)
    role_outcome: dict[str, bool] = {}
    role_arn = _ensure_lambda_role(
        iam_client,
        shared_tool_role_name("CustomerSupportTools", region),
        "Role for AgentCore Customer Support Lambda",
        outcome=role_outcome,
        region=region,
        resource_tags=resource_tags,
    )
    zip_bytes = _create_lambda_zip(CUSTOMER_SUPPORT_LAMBDA_CODE)
    return _create_or_update_lambda(
        lambda_client,
        shared_tool_function_name("CustomerSupportTools", region),
        role_arn,
        zip_bytes,
        "Customer Support tools for AgentCore Gateway",
        gateway_role_arn,
        region=region,
        resource_tags=resource_tags,
    )


def tool_function_log_group_name(function_name: str) -> str:
    """Where Lambda writes *function_name*'s logs. No tool function sets a LoggingConfig."""
    return f"/aws/lambda/{function_name}"


def govern_tool_function_log_group(function_name: str, region: str) -> str:
    """Create-or-adopt a tool Lambda's log group and apply the platform's retention.

    Lambda creates ``/aws/lambda/<function>`` itself on the first invocation, with no
    retention, and nothing governed or deleted it afterwards. Measured on the matrix account
    after every deployment had been deleted: ``AgentCore-<token>-DynamicTools`` and
    ``-CustomerSupportTools`` were gone, and both log groups were still there with no
    ``retentionInDays``, holding the tool calls of every gateway that had used them. The CFN
    export never had this gap: it declares a retention-bounded group for every function.

    The policy is the runtime DEFAULT group's (runtime_deployer.govern_default_runtime_log_group):
    the group gets RUNTIME_LOG_RETENTION_DAYS while the function lives, and teardown leaves
    it to expire, so deleting a deployment does not erase its audit trail. Deleting it with
    the function would not even be reliable: an invocation still in flight recreates a
    deleted group with no retention, as Lambda does after a CloudFormation stack delete.

    The deploy calls this before the function's gateway target exists, so a new function is
    governed before the gateway can invoke it, and only once the function is in the deploy's
    abort inventory, so a failure here is cleaned up like any later failure instead of
    orphaning the function. Fail-closed, for the runtime group's reason. Not tagged, for the
    same reason too: the manifest is the ownership authority, and logs:TagResource is not
    granted on the prefix.
    """
    log_group_name = tool_function_log_group_name(function_name)
    logs_client = _create_logs_client(region)
    try:
        logs_client.create_log_group(logGroupName=log_group_name)
    except Exception as exc:  # noqa: BLE001 -- narrowed to one AWS error code
        if not is_error(exc, "ResourceAlreadyExistsException"):
            raise
    logs_client.put_retention_policy(logGroupName=log_group_name, retentionInDays=RUNTIME_LOG_RETENTION_DAYS)
    logger.info("Tool Lambda log group %s governed with %d-day retention", log_group_name, RUNTIME_LOG_RETENTION_DAYS)
    return log_group_name


# ---------------------------------------------------------------------------
# Cognito OAuth setup (pure boto3, replaces starter toolkit)
# ---------------------------------------------------------------------------


def is_platform_owned_user_pool(pool_id: str) -> bool:
    """True when *pool_id* is the platform's shared gateway-auth pool.

    This pool is created by the platform CDK stack, is marked RETAIN there, and
    holds the app client of EVERY gateway deployed against the platform. Deleting it
    (or its hosted domain) would revoke every deployed agent's gateway access at
    once and cost >381s of domain reprovisioning to undo — so every teardown path
    that can delete a user pool must call this first.

    Defence in depth: the shared pool is also never RECORDED as a deletable
    ``cognito_user_pool`` manifest resource (gateway_step._record_gateway_resources),
    so the generic teardown never even sees it. This predicate guards the paths that
    derive a pool id from somewhere other than the manifest — e.g.
    ``_cleanup_old_cognito_pool``, which parses it out of a gateway's discoveryUrl.

    This answers exactly ONE question — "is this the shared pool?" — and callers that
    need to decide what they may DELETE must use :func:`classify_user_pool` instead.
    The distinction is load-bearing: ``cleanup_gateway_resources`` reads a ``True``
    here as "shared pool, so delete this gateway's app client and resource server
    INSIDE it", so a predicate that returned ``True`` for any pool it could not
    identify would delete an app client inside a stranger's pool. Two different
    unknowns (is it shared / may we delete it) cannot share one boolean.
    """
    shared = os.environ.get("GATEWAY_SHARED_USER_POOL_ID", "").strip()
    return bool(shared) and pool_id.strip() == shared


#: A pool we must never delete, but whose app client + resource server for THIS gateway
#: are ours to remove.
POOL_SHARED_EXACT = "SHARED_EXACT"
#: A pool this deployment created and may delete outright, domain first.
POOL_OWNED_BY_STACK = "OWNED_BY_STACK"
#: Anything we cannot prove we created. Zero mutating calls.
POOL_FOREIGN_OR_UNKNOWN = "FOREIGN_OR_UNKNOWN"


def resource_server_is_unused(pool_id: str, resource_server_id: str, cognito_client) -> bool:
    """True when no app client in *pool_id* can still be using *resource_server_id*.

    A resource server in the SHARED pool is keyed on the gateway NAME
    (``agentcore-<gateway_name>``) and ``create_resource_server`` treats AlreadyExists
    as success, so two deployments that picked the same gateway name share ONE resource
    server. Deleting it on the first teardown would revoke the co-resident gateway's
    scope — a name is not proof of ownership (F-7). This is the check that makes the
    delete safe, and teardown must call it AFTER deleting its own app client.

    It asks the question with ``ListUserPoolClients``, which returns ids and names only
    and NEVER a client secret. ``DescribeUserPoolClient`` would answer it directly by
    reading each client's ``AllowedOAuthScopes``, but that action authorizes on the
    POOL, not the client, so the only grant that works is "read every client secret in
    the shared pool" — held by the deploy-time roles on purpose
    (``infra/stacks/platform/cognito_client_secret_grant.py``) and deliberately not by
    any teardown role. Trading a namespace delete for that is the wrong trade.

    Names are sufficient because the scope and the client name are minted from the same
    ``gateway_name`` in a single function: resource server ``agentcore-X``, scope
    ``agentcore-X/invoke``, client ``X-client``. So a client that holds this resource
    server's scope is named ``X-client``, and the converse over-refuses rather than
    over-deletes: a hand-created ``X-client`` that holds no such scope leaves the
    resource server in place, which costs one orphan and revokes nothing.

    Fails CLOSED. A list call that errors, or a resource-server id that does not carry
    the expected prefix, returns False — the caller then skips the delete.
    """
    prefix = "agentcore-"
    rs_id = (resource_server_id or "").strip()
    if not rs_id.startswith(prefix) or not pool_id:
        return False
    expected_client_name = f"{rs_id[len(prefix) :]}-client"
    try:
        # Paginate. A co-resident client sitting on page 2 of an unpaginated read is
        # invisible, and "no clients found" is exactly the answer that authorizes the
        # delete — so a truncated list is a wrong delete, not a missed one.
        pagination_cursor = "NextToken"
        clients = list_all(
            cognito_client,
            "list_user_pool_clients",
            item_keys=("UserPoolClients",),
            request={"UserPoolId": pool_id, "MaxResults": 60},
            request_token=pagination_cursor,
            response_token=pagination_cursor,
        )
        if any(desc.get("ClientName") == expected_client_name for desc in clients):
            return False
    except Exception:  # noqa: BLE001 — unreadable client list must fail SAFE (keep the resource server)
        logger.debug("Could not list user-pool clients; leaving resource server in place", exc_info=True)
        return False
    return True


def classify_user_pool(pool_id: str, cognito_client=None) -> str:
    """Decide what a teardown path is allowed to do to *pool_id*.

    WHY THIS EXISTS AS A THREE-WAY ANSWER. Every teardown path used to ask only "is
    this the platform's shared pool?" and treat "no" as permission to delete the pool.
    That is fail-OPEN twice over:

    * ``is_platform_owned_user_pool`` reads ``GATEWAY_SHARED_USER_POOL_ID``, and that
      variable was set on the step Lambdas but NOT on the deployment Lambda — which
      runs two teardown paths (``deployment_handler``'s ``cognito_user_pool`` manifest
      branch, and ``cleanup_gateway_resources`` via the legacy inline delete).
      Confirmed live: the deployment Lambda's environment had no ``GATEWAY_*`` keys at
      all. The check at the ``cleanup_gateway_resources`` site is an if/elif, so
      ``False`` did not mean "skip" — it fell through to ``delete_user_pool_domain``
      plus ``delete_user_pool`` on the pool holding EVERY deployed gateway's app
      client, and CDK's ``RETAIN`` cannot undo an out-of-band API delete.
    * Even with the variable set, "not the shared pool" is not "ours". A pool id that
      reaches a delete path from a gateway's discoveryUrl or from a manifest row
      written by another deployment would have been deleted on the strength of not
      matching one id.

    So the question is inverted: a pool is deleted only when we can PROVE we created
    it, by the ``AgentCoreStack`` owner tag every backend-created pool is stamped with
    at ``create_user_pool(UserPoolTags=owner_tags(region))``. The shared pool, being a
    CDK construct, deliberately does not carry that tag — which is also why it must
    never be added to it (``resource_ownership.is_owned_by_this_stack`` would then
    report the one RETAINed pool as deletable). ARCC ``cnt_1vtvHlE7JwCaFm`` is the
    doctrine: a service acting on a resource it did not create verifies ownership
    first rather than inferring it.

    Args:
        cognito_client: the client to inspect tags with. Pass the SAME client the
            caller will delete through, so a cross-account teardown classifies the
            pool in the target account. Omitting it falls back to a client for the
            region encoded in the pool id, which is correct only for a home-account
            teardown.

    Returns:
        One of :data:`POOL_SHARED_EXACT`, :data:`POOL_OWNED_BY_STACK`,
        :data:`POOL_FOREIGN_OR_UNKNOWN`. An unreadable pool (AccessDenied, throttle,
        already gone) classifies as foreign, so the failure mode is a leaked pool an
        operator deletes by hand rather than an unrecoverable delete.
    """
    pid = (pool_id or "").strip()
    if not pid:
        return POOL_FOREIGN_OR_UNKNOWN
    if is_platform_owned_user_pool(pid):
        return POOL_SHARED_EXACT
    from app.services.resource_ownership import is_owned_by_this_stack

    pool_region = _pool_region(pid)
    try:
        client = cognito_client or _create_cognito_client(pool_region)
        tags = client.describe_user_pool(UserPoolId=pid).get("UserPool", {}).get("UserPoolTags") or {}
    except Exception:  # noqa: BLE001 — unreadable tags must fail SAFE (protect the pool)
        logger.debug("Could not read user-pool tags; classifying as foreign", exc_info=True)
        return POOL_FOREIGN_OR_UNKNOWN
    # The owner tag is compared against the POOL'S region, not the caller's. A pool is
    # stamped at creation with ``owner_tags(region)`` where region is the region the
    # pool is being created IN (gateway_deployer's create_user_pool), and stack_id
    # embeds that region. A teardown running in us-east-1 against a pool this same
    # deployment created in eu-central-1 would otherwise compare
    # `...-eu-central-1` against `...-us-east-1`, classify its OWN pool as foreign,
    # and leak one pool per cross-region deploy. The region is read off the pool id
    # itself, which is authoritative — Cognito encodes it there.
    return POOL_OWNED_BY_STACK if is_owned_by_this_stack(tags, pool_region) else POOL_FOREIGN_OR_UNKNOWN


def _pool_region(pool_id: str) -> str:
    """A Cognito pool id is ``{region}_{suffix}``, so the region is in the id itself.

    Used so the ownership check does not need a region threaded through every caller.
    Falls back to the process region for a malformed id.
    """
    head = (pool_id or "").split("_", 1)[0]
    # A region always contains at least two hyphens ("us-east-1"); anything else is
    # not a region and must not be handed to boto3 as one.
    if head.count("-") >= 2:
        return head
    return os.environ.get("APP_AWS_REGION") or os.environ.get("AWS_REGION") or "us-east-1"


def _mint_client_secret_ref(
    region: str, owner_sub: str, deployment_id: str, client_secret: str, resource_tags: dict | None = None
) -> str:
    """Move a freshly created app client's secret into its own Secrets Manager secret.

    WHY THIS EXISTS. The agent used to be handed ``COGNITO_USER_POOL_ID`` and call
    ``DescribeUserPoolClient`` itself at the moment of use. That is correct about the
    thing it was fixing — an env var is not a place a secret can live, because
    ``GetAgentRuntime`` returns a runtime's environment in plaintext — but it pays for
    it with an IAM grant that cannot be made safe: Cognito's IAM resource type is
    ``userpool``, with NO granularity below it, so any grant that lets an agent read
    its OWN client secret also lets it read every other client's secret in the same
    pool. In ``shared`` identity mode that is one pool holding every deployed
    gateway's client; in the dedicated-pool mode it is every pool carrying this
    stack's owner tag, which is every deployment in the stack. Measured live: the
    runtime role holds ``ListGateways``/``GetGateway`` on ``*``, ``get-gateway``
    returns the target's ``allowedClients`` and pool id, and
    ``describe-user-pool-client`` then returns that client's ``ClientSecret``.

    Secrets Manager scopes per resource ARN, so the same "resolve it at the moment of
    use" property survives while the grant becomes nameable — which is what lets
    identity mode ``per_agent`` provide real isolation instead of a shared pool-wide
    read. The secret is read here ONCE, from ``create_user_pool_client``'s own
    response, by a control-plane role that legitimately has it because it just
    created the client.

    Raises on failure rather than falling back to the pool id. A fallback would be a
    green deploy with a dead tool plane, which is precisely the failure mode this
    whole area already produced once (an unsatisfiable tag condition nobody noticed
    because nothing failed).
    """
    if not client_secret:
        raise RuntimeError(
            "Cognito returned no ClientSecret for the gateway app client, so the "
            "gateway cannot mint tokens. Refusing to continue with a gateway whose "
            "tool plane would be silently dead."
        )
    return _put_connector_secret(
        region, owner_sub, {"clientSecret": client_secret}, deployment_id, resource_tags=resource_tags
    )


def _compensate_cognito_attempt(
    cognito_client,
    pool_id: str,
    *,
    client_id: str = "",
    resource_id: str = "",
    resource_server_created: bool = False,
    domain: str = "",
    pool_created: bool = False,
    pool_region: str = "",
    scope: str = "",
    secret_candidate: str = "",
    secret_region: str = "",
    deployment_id: str = "",
) -> tuple[list[str], dict]:
    """Delete what ONE failed Cognito helper call created, newest first.

    The helpers raise before they return, so the deploy's abort handler never sees a
    ``cognito_response`` and cannot name the client or resource server they already
    made: without this, a failed ``create_user_pool_client`` or secret copy leaks the
    resource server, and a failed secret copy leaks a live app client. Only what this
    attempt created is touched: a resource server that already existed belongs to the
    gateway using it, and one we created is still re-checked for users, because a
    concurrent deploy of the same name may have attached its client in between.

    Returns ``(failures, leftover)``. ``failures`` holds error types only, for the
    caller to attach to the ORIGINAL exception, which is the one the operator needs to
    see. ``leftover`` names what is STILL in the pool after the rollback, in the
    ``client_info`` shape the manifest writer reads, so the normal teardown owns it:
    a note on an exception is read by a person, and a person is not a teardown. It
    holds ids only, never the client secret. Empty when nothing was left.
    """
    failures: list[str] = []
    left: dict = {}
    if secret_candidate:
        # Newest first. A CreateSecret whose response was lost may have made it; the
        # delete re-proves stack and deployment tags, and "absent" is success.
        try:
            delete_deployment_bound_secret(
                region=secret_region, deployment_id=deployment_id, secret_ref=secret_candidate
            )
        except Exception as e:  # noqa: BLE001
            failures.append(f"secret: {type(e).__name__}")
            # The manifest's secret row is keyed on this field, and a NAME is a valid
            # SecretId. The teardown arm re-proves the tags too.
            left["minted_client_secret_ref"] = secret_candidate
    if pool_created:
        # A pool this call created holds only this call's children, and deleting it
        # removes them; its domain must go first or Cognito refuses the pool delete.
        if domain:
            try:
                _delete_domain_and_wait(cognito_client, pool_id, domain)
            except Exception as e:  # noqa: BLE001
                failures.append(f"domain: {type(e).__name__}")
        try:
            cognito_client.delete_user_pool(UserPoolId=pool_id)
        except Exception as e:  # noqa: BLE001
            failures.append(f"user pool: {type(e).__name__}")
            # The pool row's teardown arm detaches the domain before the pool delete,
            # so one row recovers both.
            left["user_pool_id"] = pool_id
        return failures, ({"client_info": left} if left else {})
    if client_id:
        try:
            cognito_client.delete_user_pool_client(UserPoolId=pool_id, ClientId=client_id)
        except Exception as e:  # noqa: BLE001
            failures.append(f"app client: {type(e).__name__}")
            left["client_id"] = client_id
    resource_server_left = False
    if resource_server_created and resource_id:
        # After the client delete, so our own client does not count as a user.
        if not resource_server_is_unused(pool_id, resource_id, cognito_client):
            failures.append("resource server: left in place, still in use or unreadable")
            resource_server_left = True
        else:
            try:
                cognito_client.delete_resource_server(UserPoolId=pool_id, Identifier=resource_id)
            except Exception as e:  # noqa: BLE001
                failures.append(f"resource server: {type(e).__name__}")
                resource_server_left = True
    if resource_server_left:
        # The teardown arm re-proves the server unused before deleting it, so a
        # concurrent deploy that joined it keeps it.
        left["scope"] = scope or f"{resource_id}/invoke"
    if not left:
        return failures, {}
    return failures, {
        "client_info": {
            "user_pool_id": pool_id,
            "user_pool_region": pool_region or _pool_region(pool_id),
            "shared_pool": True,
            **left,
        },
        "resource_server_created": resource_server_left,
    }


def _resource_server_has_scope(cognito_client, pool_id: str, resource_id: str, scope_name: str) -> bool:
    """Whether *resource_id* exists in *pool_id* and defines *scope_name*. False on any error."""
    try:
        described = cognito_client.describe_resource_server(UserPoolId=pool_id, Identifier=resource_id)
    except Exception:  # noqa: BLE001 — an unreadable server is not a proven one
        return False
    scopes = (described.get("ResourceServer") or {}).get("Scopes") or []
    return any(s.get("ScopeName") == scope_name for s in scopes)


def _delete_domain_and_wait(cognito_client, pool_id: str, domain: str, attempts: int = 12) -> None:
    """DeleteUserPoolDomain returns before the domain detaches, and DeleteUserPool
    refuses a pool that still has one, so poll the pool until its Domain is gone."""
    cognito_client.delete_user_pool_domain(UserPoolId=pool_id, Domain=domain)
    for _ in range(attempts):
        if not (cognito_client.describe_user_pool(UserPoolId=pool_id).get("UserPool") or {}).get("Domain"):
            return
        time.sleep(5)
    raise TimeoutError(f"domain still attached after {attempts * 5}s")


# The attribute a failed Cognito helper's exception carries its leftover inventory
# under. deploy_gateway's abort reads it, because the helper raised before it could
# return a ``cognito_response``.
COGNITO_LEFTOVER_ATTR = "cognito_leftover"


def _raise_after_compensation(error: Exception, compensation: tuple[list[str], dict]) -> None:
    failures, leftover = compensation
    if failures:
        # Type names only: a botocore message can echo request parameters.
        error.add_note("Cognito rollback incomplete: " + "; ".join(failures))
        logger.warning("Cognito rollback after a failed gateway auth setup was incomplete: %s", "; ".join(failures))
    if leftover:
        setattr(error, COGNITO_LEFTOVER_ATTR, leftover)
    raise error


def _create_cognito_oauth_in_shared_pool(
    cognito_client,
    gateway_name: str,
    region: str,
    pool_id: str,
    domain_prefix: str,
    owner_sub: str = "",
    deployment_id: str = "",
    *,
    pool_region: str | None = None,
    resource_tags: dict | None = None,
) -> dict:
    """Mint this gateway's own resource server + app client in the platform's pool.

    The pool and its hosted domain belong to the platform stack
    (``infra/stacks/platform/gateway_auth_pool.py``) and already exist, so the token
    endpoint below is reachable IMMEDIATELY. That is the entire point: a hosted
    domain created during a deploy is unreachable for >381s (measured; and
    ``describe_user_pool_domain`` misreports ``ACTIVE`` after 4s), which is longer
    than the gateway step's 300s budget, so the deploy-time MCP ``tools/list`` probe
    could never authenticate and the tool plane was never verifiable.

    ISOLATION (ARCC cnt_PQjUx2msVXY1wU — unique credentials per entity): only the
    pool and the public hosted domain are shared. This gateway gets its OWN resource
    server (``agentcore-<gateway_name>``) and its OWN app client, and that client is
    configured with ONLY its own ``<resource_server>/invoke`` scope. Cognito refuses
    a ``client_credentials`` request for a scope a client is not configured for, so
    one gateway's credential cannot mint a token another gateway's authorizer would
    accept. Per-gateway isolation is identical to the dedicated-pool path.

    The gateway's ``customJWTAuthorizer`` additionally pins ``allowedClients`` to this
    client id, so a token minted by a DIFFERENT client in the same pool is rejected
    by the gateway even if it somehow carried the right scope. That is the property
    that makes sharing the pool safe, and it is why allowedClients must stay pinned
    to one client id here rather than being widened.
    """
    # ``region`` is the DEPLOYMENT region: the copied client secret belongs beside
    # the runtime that consumes it. The shared pool is a CDK resource in the platform
    # stack's HOME region, which may differ for a same-account regional deployment.
    # Cognito control-plane calls and issuer/token URLs must use the pool's region.
    pool_region = pool_region or _pool_region(pool_id)
    resource_id = f"agentcore-{gateway_name}"
    scope_name = "invoke"
    # Idempotent: a redeploy of the same gateway name reuses the resource server.
    # Creating it is the only way to know it exists (there is no upsert), and
    # AlreadyExists is the success case, not an error.
    resource_server_created = False
    try:
        cognito_client.create_resource_server(
            UserPoolId=pool_id,
            Identifier=resource_id,
            Name=f"AgentCore Gateway {gateway_name}",
            Scopes=[{"ScopeName": scope_name, "ScopeDescription": "Invoke gateway"}],
        )
        resource_server_created = True
    except Exception as e:
        # Reuse only what a read PROVES exists. Treating every create error as
        # "already there" carried AccessDenied, throttling and a malformed request on
        # into a client create for a scope nobody had defined.
        if not _resource_server_has_scope(cognito_client, pool_id, resource_id, scope_name):
            raise
        # Type only. A botocore error message can echo the request parameters, and
        # this request names the pool and the gateway. ARCC cnt_rHmO501l15qr2W.
        logger.warning(
            "Shared-pool resource server create returned %s; the existing one is reused",
            type(e).__name__,
        )

    full_scope = f"{resource_id}/{scope_name}"
    client_id = ""
    try:
        client_resp = cognito_client.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=f"{gateway_name}-client",
            GenerateSecret=True,
            AllowedOAuthFlowsUserPoolClient=True,
            AllowedOAuthFlows=["client_credentials"],
            AllowedOAuthScopes=[full_scope],
            SupportedIdentityProviders=["COGNITO"],
        )
        client_id = client_resp["UserPoolClient"]["ClientId"]
        # Read the secret out of the CREATE response, not with a follow-up
        # DescribeUserPoolClient: the value is already here, and a second call is a
        # second chance for an IAM or throttling failure to leave a client with no
        # reachable secret. See _mint_client_secret_ref for why it goes to Secrets
        # Manager rather than being resolvable from the pool id at runtime.
        client_secret_ref = _mint_client_secret_ref(
            region,
            owner_sub,
            deployment_id,
            client_resp["UserPoolClient"].get("ClientSecret", ""),
            resource_tags=resource_tags,
        )
    except Exception as e:
        _raise_after_compensation(
            e,
            _compensate_cognito_attempt(
                cognito_client,
                pool_id,
                client_id=client_id,
                resource_id=resource_id,
                resource_server_created=resource_server_created,
                pool_region=pool_region,
                scope=full_scope,
                secret_candidate=getattr(e, SECRET_CANDIDATE_ATTR, ""),
                secret_region=region,
                deployment_id=deployment_id,
            ),
        )

    token_endpoint = f"https://{domain_prefix}.auth.{pool_region}.amazoncognito.com/oauth2/token"
    logger.warning(
        "Gateway auth uses the shared platform pool (warm domain), so the token "
        "endpoint is reachable at once and the tool plane can be verified"
    )

    return {
        "authorizer_config": {
            "customJWTAuthorizer": {
                "discoveryUrl": (
                    f"https://cognito-idp.{pool_region}.amazonaws.com/{pool_id}/.well-known/openid-configuration"
                ),
                # Pinned to THIS gateway's client. See the isolation note above:
                # this is what stops another gateway's client in the same pool from
                # being accepted here.
                "allowedClients": [client_id],
            }
        },
        # Same shape, and the same deliberate omission of client_secret, as the
        # dedicated-pool path below — see the long note at its `client_info`.
        # `client_secret_ref` is a Secrets Manager ARN, i.e. a NAME for the
        # credential and not the credential, so it is safe on the surfaces this
        # dict reaches (SFN history, the DynamoDB item, GET /api/deploy/{id}) —
        # that is exactly the "share only the reference" shape ARCC
        # cnt_77BHvX7WzuG1X8 asks for. resolve_client_secret() dereferences it.
        "client_info": {
            "user_pool_id": pool_id,
            # Teardown and any later authorizer reconstruction must route the pool's
            # children back to the pool region, not to the runtime/gateway region.
            "user_pool_region": pool_region,
            "client_id": client_id,
            "client_secret_ref": client_secret_ref,
            # The SAME ref, published a second time under a key that means "this
            # deploy created it". Only this key may drive a delete. See the note on
            # the external-IDP client_info in _create_external_oauth_config: that path
            # fills client_secret_ref with the CUSTOMER's own secret, and the manifest
            # cannot tell the two apart from the ref alone.
            "minted_client_secret_ref": client_secret_ref,
            "token_endpoint": token_endpoint,
            "scope": full_scope,
            # Marks the pool as platform-owned so teardown deletes only this
            # gateway's app client and resource server, never the pool or the
            # domain — deleting either would revoke every other deployed agent's
            # gateway access and cost >381s to rebuild.
            "shared_pool": True,
        },
        # Outside client_info, so only this deploy's own abort reads it: on a redeploy
        # the resource server pre-dates the deploy and still belongs to the gateway.
        "resource_server_created": resource_server_created,
    }


def _create_cognito_oauth(
    cognito_client,
    gateway_name: str,
    region: str,
    owner_sub: str = "",
    deployment_id: str = "",
    resource_tags: dict | None = None,
) -> dict:
    """Create Cognito User Pool + App Client for gateway OAuth.

    Returns dict with authorizer_config and client_info.

    When the platform provides a shared gateway-auth pool (env
    ``GATEWAY_SHARED_USER_POOL_ID`` + ``GATEWAY_SHARED_USER_POOL_DOMAIN``, set by
    ``infra/stacks/platform/gateway_auth_pool.py``) this creates only a per-gateway
    resource server and app client inside it and reuses the pool's already-warm
    hosted domain. Otherwise it falls back to creating a whole pool and a fresh
    domain, which is the behaviour every older stack has.
    """
    shared_pool_id = os.environ.get("GATEWAY_SHARED_USER_POOL_ID", "").strip()
    shared_domain = os.environ.get("GATEWAY_SHARED_USER_POOL_DOMAIN", "").strip()
    if shared_pool_id and shared_domain:
        shared_pool_region = _pool_region(shared_pool_id)
        # Platform credentials even when the regions match: the pool lives in the
        # platform account, and a same-region target client is still the wrong account.
        return _create_cognito_oauth_in_shared_pool(
            _create_platform_cognito_client(shared_pool_region),
            gateway_name,
            region,
            shared_pool_id,
            shared_domain,
            owner_sub,
            deployment_id,
            pool_region=shared_pool_region,
            resource_tags=resource_tags,
        )

    pool_name = f"AgentCore-{gateway_name}"

    # Create User Pool. Tagged with the owning stack because the pool NAME derives
    # from the user's gateway name and carries no deployment identity, so a
    # name-prefix sweep over "AgentCore*" cannot tell this pool from another
    # deployment's — or another product's. cleanup.sh now gates on this tag.
    pool_resp = cognito_client.create_user_pool(
        PoolName=pool_name,
        AutoVerifiedAttributes=[],
        UsernameAttributes=["email"],
        UserPoolTags=governed_tags(region, resource_tags),
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
    domain_created: list[str] = []
    try:
        return _populate_dedicated_cognito_pool(
            cognito_client,
            user_pool_id,
            gateway_name,
            region,
            owner_sub,
            deployment_id,
            domain_created,
            resource_tags=resource_tags,
        )
    except Exception as e:
        _raise_after_compensation(
            e,
            _compensate_cognito_attempt(
                cognito_client,
                user_pool_id,
                domain=(domain_created or [""])[0],
                pool_created=True,
                secret_candidate=getattr(e, SECRET_CANDIDATE_ATTR, ""),
                secret_region=region,
                deployment_id=deployment_id,
            ),
        )


def _populate_dedicated_cognito_pool(
    cognito_client,
    user_pool_id: str,
    gateway_name: str,
    region: str,
    owner_sub: str,
    deployment_id: str,
    domain_created_out: list[str],
    resource_tags: dict | None = None,
) -> dict:
    """The children of a dedicated pool. Split out so its caller can roll the pool back."""

    # Create resource server for scoped access
    resource_id = f"agentcore-{gateway_name}"
    scope_name = "invoke"
    # Neither create is optional, so neither error is swallowed: the pool was created
    # a moment ago, so nothing in it can already exist, and a pool with no resource
    # server or no domain is a green deploy with a dead token endpoint. The caller
    # rolls the pool back.
    cognito_client.create_resource_server(
        UserPoolId=user_pool_id,
        Identifier=resource_id,
        Name=f"AgentCore Gateway {gateway_name}",
        Scopes=[{"ScopeName": scope_name, "ScopeDescription": "Invoke gateway"}],
    )

    # Create domain for token endpoint
    domain_name = f"agentcore-{gateway_name}-{user_pool_id.split('_')[-1][:8]}".lower()
    domain_name = re.sub(r"[^a-z0-9-]", "-", domain_name)[:63]
    cognito_client.create_user_pool_domain(
        Domain=domain_name,
        UserPoolId=user_pool_id,
    )
    domain_created_out.append(domain_name)

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
    # The secret is bound to no local beyond the call below, which moves it straight
    # into Secrets Manager. A DEDICATED pool holds only this gateway's client, so
    # "let the agent call DescribeUserPoolClient on its own pool" looks safe here —
    # but the grant that authorizes it is `userpool/*` conditioned on
    # aws:ResourceTag/AgentCoreStack, and that tag names the STACK, which every
    # deployment in the stack shares. So the dedicated path had the same cross-tenant
    # read as the shared path, reached through the tag condition instead of an exact
    # ARN. Both paths now hand over a per-deployment secret reference, which is what
    # allows the pool-wide grant to be dropped from the tenant-facing runtime role
    # altogether. See _mint_client_secret_ref.
    client_secret_ref = _mint_client_secret_ref(
        region,
        owner_sub,
        deployment_id,
        client_resp["UserPoolClient"].get("ClientSecret", ""),
        resource_tags=resource_tags,
    )

    token_endpoint = f"https://{domain_name}.auth.{region}.amazoncognito.com/oauth2/token"

    authorizer_config = {
        "customJWTAuthorizer": {
            "discoveryUrl": f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/openid-configuration",
            "allowedClients": [client_id],
        }
    }

    # NO `client_secret` KEY. This dict is the gateway step's RESULT, and the
    # result is not a private value: every Task in the deployment state machine
    # uses `ResultPath: "$"`, so the whole event is re-emitted into the execution
    # history at each state (measured: 89 copies of this one secret, 90-day
    # retention, readable with states:GetExecutionHistory); it is written to the
    # DynamoDB deployment item; and it is returned verbatim by the tenant-facing
    # GET /api/deploy/{id}. It was also injected as the runtime env var
    # COGNITO_CLIENT_SECRET, which GetAgentRuntime returns in plaintext.
    #
    # ARCC cnt_77BHvX7WzuG1X8: secure the secret and "only share the secret
    # reference". cnt_vtSS0S3iwKjSuk: the client secret stays on the server side.
    # cnt_WoQ0DhK7aQ78yE: anything returned in an API response also lands in
    # CloudWatch once execution logging is on.
    #
    # `client_secret_ref` is a Secrets Manager ARN — a NAME for the credential, not
    # the credential — so it is safe on all of those surfaces, and it is what
    # resolve_client_secret() dereferences. `user_pool_id` + `client_id` stay for the
    # gateway's authorizer and for the legacy DescribeUserPoolClient fallback that
    # deployments created before the ref existed still rely on.
    client_info = {
        "user_pool_id": user_pool_id,
        "client_id": client_id,
        "client_secret_ref": client_secret_ref,
        # See the shared-pool path above: same ref, second key, and only this key
        # authorises a delete.
        "minted_client_secret_ref": client_secret_ref,
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
    # A REFERENCE (a Secrets Manager secret name), not the secret. It used to be
    # stored under the key `client_secret`, and get_cognito_token sent that value
    # verbatim as the OAuth client_secret form field — so an external-IDP gateway
    # authenticated with the string "my/okta/secret" and could never get a token.
    # Keeping the name honest is what lets resolve_client_secret() dereference it.
    client_secret_ref = identity_config.get("clientSecretRef", identity_config.get("client_secret_ref", ""))
    discovery_url = identity_config.get("discoveryUrl", identity_config.get("discovery_url", ""))
    scopes = identity_config.get("scopes", [])
    audience = identity_config.get("audience", "")

    # Derive token_endpoint from discovery document.
    # The URL came from operator-supplied identity_config; validate it before any
    # DNS-aware HTTP call so we can't be tricked into hitting the IMDS endpoint
    # (169.254.169.254), Lambda credentials endpoint (169.254.170.2), or any
    # private-network host. See _validate_discovery_url() for the policy.
    missing_endpoint = ""
    token_endpoint = missing_endpoint
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
                urllib.request.urlopen(req, timeout=10) as resp  # nosec B310
            ):  # nosemgrep: dynamic-urllib-use-detected -- URL validated by _validate_discovery_url (scheme=https + IP denylist + optional allowlist)
                discovery_doc = json.loads(resp.read().decode())
                token_endpoint = discovery_doc.get("token_endpoint", "")
        except Exception as e:
            # Re-raise: a transient discovery-doc fetch failure must fail the
            # deploy, not silently produce a gateway with empty token_endpoint.
            raise RuntimeError(f"Failed to fetch OIDC discovery document from {validated_url}: {e}") from e

        if token_endpoint:
            # The discovery document is REMOTE CONTENT, so this field is chosen by
            # whoever serves it — not by the operator who typed the discovery URL.
            # Validating the URL we fetch and then trusting the URL it hands back is
            # exactly the confused-deputy shape (F-6): the guard above buys nothing
            # if the next hop is unchecked, and the next hop is the one that carries
            # client_secret. Failing here rather than at first use means the error
            # names the discovery document as the source.
            #
            # OUTSIDE the try above on purpose. Inside it, the `except Exception`
            # would rewrap a _DiscoveryUrlBlocked as "Failed to fetch OIDC discovery
            # document", telling the operator to check their network when the
            # document fetched perfectly and its CONTENT was refused.
            token_endpoint = validate_token_endpoint(
                token_endpoint,
                label=f"token_endpoint advertised by the OIDC discovery document at {validated_url}",
            )

    authorizer_config = {
        "customJWTAuthorizer": {
            "discoveryUrl": discovery_url,
            "allowedClients": [client_id] if client_id else [],
        }
    }
    if audience:
        # Singular: the model member is ``allowedAudience`` (a list). The plural
        # ``allowedAudiences`` it used to send is not a member, so botocore's param
        # validation rejected the whole CreateGateway and an audience-bound external
        # IdP could never deploy at all.
        authorizer_config["customJWTAuthorizer"]["allowedAudience"] = [audience]

    # NOTE the deliberate absence of `minted_client_secret_ref`. On this path
    # `client_secret_ref` is the CUSTOMER's OAuth client secret: it arrives in
    # identity_config (read above from clientSecretRef) and lives in their Secrets
    # Manager, usually shared by every agent that authenticates against this identity
    # provider. The platform created nothing here, so nothing here may be deleted.
    #
    # Measured live on 2026-09-21: the manifest recorded
    # {"type": "secret", "id": "<the customer's ref>"} from `client_secret_ref` alone,
    # and the teardown arm for a `secret` row is
    # delete_secret(..., ForceDeleteWithoutRecovery=True) with no ownership check. So
    # deleting ONE agent would have irrecoverably destroyed the IDP credential shared
    # by all of them — no 7-day recovery window, and the customer would have to
    # re-issue it in their IDP. See gateway_step._record_gateway_resources.
    client_info = {
        "provider": provider,
        "client_id": client_id,
        "client_secret_ref": client_secret_ref,
        "token_endpoint": token_endpoint,
        "scope": " ".join(scopes) if scopes else "",
        "discovery_url": discovery_url,
        # Kept so every later authorizer built from client_info (configure_jwt_auth)
        # binds the same audience instead of silently accepting any.
        "audience": audience,
    }

    return {
        "authorizer_config": authorizer_config,
        "client_info": client_info,
    }


def _region_from_client_info(client_info: dict) -> str:
    """Best-effort region for the resources named in ``client_info``.

    A Secrets Manager ARN is authoritative when resolving a referenced secret. A
    regional deployment may use the HOME-region shared Cognito pool while keeping
    its deployment-bound secret in the TARGET region, so preferring the pool would
    ask the wrong regional Secrets Manager endpoint. Without a secret reference, a
    Cognito user pool id is literally ``<region>_<suffix>`` and is the best source.
    The token endpoint
    (``https://<domain>.auth.<region>.amazoncognito.com/...``) is the fallback, and
    the ambient region the last resort.
    """
    secret_ref = client_info.get("client_secret_ref") or client_info.get("clientSecretRef") or ""
    secret_match = re.match(
        r"^arn:[^:]+:secretsmanager:([a-z0-9-]+):",
        str(secret_ref),
    )
    if secret_match:
        return secret_match.group(1)
    pool_id = client_info.get("user_pool_id", "") or ""
    if "_" in pool_id:
        return pool_id.split("_", 1)[0]
    m = re.search(r"\.auth\.([a-z0-9-]+)\.amazoncognito\.com", client_info.get("token_endpoint", "") or "")
    if m:
        return m.group(1)
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


def resolve_client_secret(
    client_info: dict,
    *,
    secrets_client=None,
    cognito_client=None,
) -> str:
    """Resolve the gateway's OAuth client secret at the moment of use.

    The secret is deliberately NOT carried in ``client_info`` (see
    _create_cognito_oauth): that dict is the gateway step's result, so it is
    re-emitted into the Step Functions execution history at every subsequent
    state, written to the DynamoDB deployment item, and returned by the
    tenant-facing GET /api/deploy/{id}. ARCC cnt_77BHvX7WzuG1X8 — secure the
    secret and share only the reference; cnt_vtSS0S3iwKjSuk — the client secret
    stays server-side; cnt_WoQ0DhK7aQ78yE — anything in an API response also
    reaches CloudWatch once execution logging is on.

    Three sources, in order:

    1. An inline ``client_secret``. Only deployments created BEFORE this change
       have one; honouring it keeps their gateways working. New Cognito gateways
       never set it.
    2. A ``client_secret_ref``/``clientSecretRef`` — a Secrets Manager reference.
       Either an external IDP's secret (models/components.py documents the field as
       "Reference to Secrets Manager", and the UI validates it against Secrets
       Manager's naming charset), or — for every Cognito gateway created since
       _mint_client_secret_ref — the platform's own per-deployment copy of the app
       client's secret. Before this, the REFERENCE was sent verbatim as the OAuth
       ``client_secret`` form field, so no external-IDP gateway could ever obtain a
       token.
    3. ``user_pool_id`` + ``client_id`` — re-read from Cognito with
       DescribeUserPoolClient. Now a LEGACY path only, for gateways deployed before
       source 2 covered Cognito: it requires a grant that cannot be scoped below the
       pool, so it necessarily reads every other gateway's client secret in that
       pool. Nothing new depends on it, and the tenant-facing runtime role no longer
       holds the grant it needs.

    Callers must treat the return value as a value that may only live in local
    memory: never log it, never put it in a step result, never set it as an env
    var.
    """
    inline = client_info.get("client_secret", "")
    if inline:
        return inline

    region = _region_from_client_info(client_info)

    ref = client_info.get("client_secret_ref") or client_info.get("clientSecretRef") or ""
    if ref:
        resp = (secrets_client or _create_secrets_client(region)).get_secret_value(SecretId=ref)
        raw = resp.get("SecretString") or ""
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return raw
        if isinstance(parsed, dict):
            for key in ("clientSecret", "client_secret", "secret", "value"):
                if parsed.get(key):
                    return str(parsed[key])
            # A JSON object with none of the expected keys is a misconfigured
            # secret, not a secret we should guess at.
            raise RuntimeError(
                f"Secret '{ref}' holds a JSON object with no clientSecret/client_secret/secret/value key"
            )
        return raw

    user_pool_id = client_info.get("user_pool_id", "")
    client_id = client_info.get("client_id", "")
    if user_pool_id and client_id:
        resp = (cognito_client or _create_cognito_client(region)).describe_user_pool_client(
            UserPoolId=user_pool_id,
            ClientId=client_id,
        )
        return resp["UserPoolClient"].get("ClientSecret", "")

    return ""


class TokenRequestError(RuntimeError):
    """A client_credentials token mint returned a non-200.

    Carries the HTTP status and NOTHING from the response body — see the raise
    site for why the body cannot be propagated.
    """

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Token request failed: HTTP {status}")


def get_cognito_token(client_info: dict) -> str:
    """Get OAuth access token using client_credentials grant. Supports Cognito and external IDPs."""
    import urllib.parse

    import urllib3

    client_id = client_info.get("client_id", "")
    token_endpoint = client_info.get("token_endpoint", "")
    scope = client_info.get("scope", "")

    if not client_id or not token_endpoint:
        raise RuntimeError("Missing client_id or token_endpoint in gateway config")

    # Validate the destination BEFORE resolving the secret, not just before sending
    # it. This is the point of use, and it is guarded in addition to
    # _create_external_oauth_config because client_info does not only come from
    # there: it is read back out of the DynamoDB deployment record on every
    # redeploy, test-runtime call and readiness probe, and a record written before
    # that guard existed (or by the runtime-import path) carries whatever endpoint
    # it carries. A guard that only runs where the value is first accepted is a
    # guard that every stored value bypasses. ARCC cnt_77BHvX7WzuG1X8 — the secret
    # goes only to the party it authenticates to.
    token_endpoint = validate_token_endpoint(token_endpoint)

    # Resolved here and held only in this frame — see resolve_client_secret.
    client_secret = resolve_client_secret(client_info)

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
    # Status ONLY, never the body. This exception is the one thing a caller can
    # report about a failed token mint, so it gets logged and wrapped — and the
    # body is an uncontrolled response from a token endpoint that, for an external
    # IDP (_create_external_oauth_config), is a third party we do not govern. An
    # OAuth error_description commonly echoes the request, and this request carries
    # client_secret in its form body. Per ARCC cnt_rHmO501l15qr2W (log only the
    # minimum) and cnt_4t7ISjfmXuDOav (logged data must not include credentials),
    # the status is the diagnosis and the body is the risk. The status is carried
    # as an attribute so callers can report it without parsing the message.
    raise TokenRequestError(resp.status)


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
            # Log the STATUS (an int, never the body — an MCP error body can echo
            # the request, and this probe sends a bearer token). Without this line
            # a 403 here was indistinguishable in the logs from a 200 carrying an
            # empty tool list, which is exactly how an auth failure came to be
            # reported as an "AgentCore empty-tool-plane provisioning flake".
            logger.warning("tools/list probe got HTTP %d (not 200) — treating as not-ready", resp.status)
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
                except (json.JSONDecodeError, AttributeError, TypeError):
                    continue
        try:
            return len(json.loads(text).get("result", {}).get("tools", []))
        except Exception:  # noqa: BLE001
            return -1
    except Exception as e:  # noqa: BLE001
        # WARNING, not INFO. Measured live: the deployed step Lambdas configure no
        # log level, so they inherit the Lambda runtime's root handler at WARNING
        # and every logger.info in this module is DISCARDED. In run df698a37 the
        # gateway was created (we have its id) yet "Created gateway: %s" produced
        # no line at all, and this branch — the only thing that can say WHY the
        # probe got nothing — was equally silent. That silence was then read as
        # evidence the probe had NOT failed, which is how a possible auth or
        # reachability failure came to be reported as an AgentCore service-side
        # empty-tool-plane flake. An invisible diagnostic is worse than none,
        # because its absence looks like information.
        #
        # Exception TYPE and, for a token mint, the STATUS — never str(e). A
        # TokenRequestError's message is already status-only by construction, but a
        # urllib3/ssl error's str() carries the full URL and a botocore error can
        # carry request parameters. Type + status is enough to separate the three
        # causes that matter here (cannot resolve/connect, token mint refused,
        # gateway refused the token) and is the same discipline as
        # cfn_provider/handler.py, which logs an exception type rather than its
        # message. ARCC cnt_rHmO501l15qr2W (log the minimum) /
        # cnt_4t7ISjfmXuDOav (never log credentials).
        _status = getattr(e, "status", None)
        logger.warning(
            "tools/list probe failed, treating as not-ready (will retry): %s%s",
            type(e).__name__,
            f" (token endpoint HTTP {_status})" if isinstance(_status, int) else "",
        )
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
                except (json.JSONDecodeError, AttributeError, TypeError):
                    continue
        try:
            tools = json.loads(text).get("result", {}).get("tools", [])
            return [t.get("name") for t in tools if t.get("name")]
        except Exception:  # noqa: BLE001
            return []
    except Exception as e:  # noqa: BLE001
        logger.info("qualified-tools probe error: %s", str(e)[:120])
        return []


_ADOPTED_REPROBE_ATTEMPTS = 2
_ADOPTED_REPROBE_PAUSE_SECONDS = 15.0


def _reprobe_adopted_gateway_tool_plane(
    gateway: dict,
    gateway_url: str,
    client_info: dict,
    expected_tool_count: int,
    probe: dict,
    diag: str,
) -> int:
    """Give an ADOPTED gateway the probe budget a created one gets through recreation.

    Returns the highest served count seen; the caller decides whether it is enough.
    Each round is the same 90 s probe as the first one, separated by a pause so a
    target that is still UPDATING can reach READY between rounds.
    """
    served = -1
    for attempt in range(1, _ADOPTED_REPROBE_ATTEMPTS + 1):
        logger.warning(
            "Gateway %s %s; it pre-dated this deployment, so re-probing in place (%d/%d)",
            gateway["gatewayId"],
            diag,
            attempt,
            _ADOPTED_REPROBE_ATTEMPTS,
        )
        time.sleep(_ADOPTED_REPROBE_PAUSE_SECONDS)
        served = _wait_for_gateway_to_serve_tools(
            gateway_url, client_info, expected_tool_count, timeout=90, probe=probe
        )
        if served >= expected_tool_count:
            logger.warning(
                "Gateway %s converged on re-probe %d: serving %d/%d tools",
                gateway["gatewayId"],
                attempt,
                served,
                expected_tool_count,
            )
            return served
    return served


def _wait_for_gateway_to_serve_tools(
    gateway_url: str, client_info: dict, expected: int, timeout: int = 90, probe: dict | None = None
) -> int:
    """Poll the gateway's MCP tools/list until it serves >= 1 tool (ideally
    `expected`), or *timeout*. Returns the served count (0 if it never serves).
    This is the authoritative deploy-time readiness signal — it matches exactly
    what the deployed agent will discover.

    If *probe* is given it is filled in with how the polling ended, because the
    returned count is lossy in a way that has already caused a misdiagnosis:
    ``_count_served_tools`` returns -1 for a non-200 or a transport error, and
    this function clamps that to 0, making "the gateway answered with an empty
    tool list" indistinguishable from "the gateway never answered us". Those have
    different causes (service-side provisioning vs auth/reachability) and
    different cures, so a caller that reports a cause MUST read *probe*:

      got_valid_response: True if any poll got a parseable tools array.
      last_status:        short human-readable note when it never did.
    """
    import time as _t

    if probe is not None:
        probe["got_valid_response"] = False
        probe["last_status"] = ""

    deadline = _t.time() + timeout
    served = 0
    while _t.time() < deadline:
        served = _count_served_tools(gateway_url, client_info)
        if probe is not None and served >= 0:
            # >= 0 means a 200 whose body parsed into a tools array. An empty
            # array is a real answer: the plane is up and serving nothing.
            probe["got_valid_response"] = True
        if served >= expected and expected > 0:
            logger.warning("Gateway serves %d/%d tools over MCP", served, expected)
            return served
        if served > 0:
            logger.warning("Gateway serves %d tools over MCP (expected %d)", served, expected)
        _t.sleep(8)
    if probe is not None and not probe["got_valid_response"]:
        probe["last_status"] = "every tools/list probe returned a non-200 or failed to connect"
    logger.warning(
        "Gateway served %d/%d tools within %ds (valid tools/list response seen: %s)",
        max(served, 0),
        expected,
        timeout,
        None if probe is None else probe["got_valid_response"],
    )
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
        pool_region = client_info.get("user_pool_region") or _pool_region(user_pool_id)
        discovery_url = (
            f"https://cognito-idp.{pool_region}.amazonaws.com/{user_pool_id}/.well-known/openid-configuration"
        )
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
            authorizer_config["customJWTAuthorizer"]["allowedAudience"] = [client_info["audience"]]

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


def _pool_id_from_authorizer(auth_cfg: dict) -> str:
    """Pool id out of a ``customJWTAuthorizer`` discoveryUrl, or ``""``.

    Parses with ``urlparse`` and validates the HOST exactly rather than doing a
    substring/endswith check on the raw URL — a substring like ``amazonaws.com`` can
    appear at an arbitrary position (py/incomplete-url-substring-sanitization). Kept
    in one function because two callers now need it and the validation is the
    security-relevant part, not the parsing.
    """
    from urllib.parse import urlparse as _urlparse

    discovery_url = ((auth_cfg or {}).get("customJWTAuthorizer") or {}).get("discoveryUrl", "")
    parsed = _urlparse(discovery_url)
    host = parsed.hostname or ""
    if not (host.startswith("cognito-idp.") and host.endswith(".amazonaws.com")):
        return ""
    # path is /{pool_id}/.well-known/... — pool_id is the first segment.
    segments = [s for s in parsed.path.split("/") if s]
    return segments[0] if segments else ""


def assert_gateway_is_adoptable(
    gateway_name: str,
    previous_auth: dict,
    existing_role_arn: str = "",
    owned_role_arn: str = "",
    cognito_client=None,
) -> str:
    """Refuse to adopt a same-named gateway this deployment cannot prove is its own.

    Peer finding F-10, sub-defect 4. ``create_gateway`` raising ``ConflictException``
    has two causes the exception cannot distinguish: this deployment is redeploying its
    own gateway, or something else in the account already holds that name. AgentCore
    gateway names are **account-global** and ours come straight from a user-chosen
    canvas field, so the second is reachable by typing a name. The recovery branch
    assumed the first and went on to ``update_gateway`` the stranger's gateway onto this
    deployment's Cognito authorizer -- which does not merely mislabel it, it stops the
    gateway validating the tokens its real owner's clients present. There is a live
    example in this very account: the foreign gateway ``omargw``, pinned to the foreign
    pool ``AgentCore-omargw``.

    This is F-7's shape and ARCC ``cnt_GURZvDLm6pRn1K`` ("Prevent S3 Bucket Sniping")
    gives the exit criterion verbatim for a globally-unique name: *before performing
    actions, ensure ownership has not changed*. See also ``cnt_1vtvHlE7JwCaFm`` -- a
    service acting on a resource it did not create verifies ownership first rather than
    inferring it.

    **The proof is not a resource tag, and that is deliberate.** The obvious
    implementation -- stamp ``owner_tags`` on create and read them back with
    ``bedrock-agentcore:ListTagsForResource`` -- has two problems these do not. First,
    every gateway that already exists predates the tag, so a tag-only check refuses each
    one on its next redeploy: fail-closed over an incomplete ownership table removes the
    feature instead of securing it. Second, both ``ListTagsForResource`` and
    ``TagResource`` measure ``implicitDeny`` for the gateway step role, and tag-on-create
    typically requires the latter, so adding tags could fail *creation* -- a new outage
    to fix an old bug. Both proofs below work on gateways created before this check
    existed and need no IAM action the deploy does not already hold.

    **Proof 1, the gateway's IAM role.** ``owned_role_arn`` is the gateway role this
    deployment holds by the time it calls ``create_gateway``, and it is ownership-proven
    on both routes that produce it: either ``create_role`` had just succeeded (so nothing
    older can reference it), or the ``EntityAlreadyExists`` branch cleared
    ``assert_this_deployment_may_mutate`` against the role's tags. A pre-existing gateway
    already pointing at that role was therefore created by a principal holding
    ``iam:PassRole`` on this deployment's own role -- this platform. This is the proof
    that makes an **external-OAuth** redeploy work: such a gateway has no Cognito
    authorizer at all, so proof 2 cannot speak for it, and a check with only proof 2
    would refuse every legitimate redeploy of one.

    **Proof 2, the gateway's authorizer.** A gateway this platform deployed with Cognito
    sign-in is pinned to a pool that is either the platform's shared gateway-auth pool or
    one this stack created and owner-tagged. The ``authorizerConfiguration`` is already
    in hand -- the caller fetched it with ``get_gateway`` and keeps it as the only record
    of the previous pool -- and :func:`classify_user_pool` already decides that question
    through ``describe_user_pool``, a grant proven live. This proof covers the case where
    the role has been renamed or recreated but the pool is still ours.

    Fail direction, chosen on consequence rather than symmetry: when neither proof
    answers -- an unreadable authorizer, a non-Cognito one on a gateway using a role that
    is not ours -- the deploy is refused. Refusing a legitimate redeploy costs an
    operator a retry or a rename; repointing a stranger's gateway breaks their tool plane
    and cannot be undone from here, because the previous ``authorizerConfiguration`` is
    gone the moment ``update_gateway`` succeeds.

    Returns the name of the proof that succeeded, for the caller to log -- so an
    adoption in production says *why* it was allowed, not merely that it happened.

    Raises:
        ForeignResourceError: when ownership cannot be proven. Callers must invoke this
            **before** assigning the ``gateway`` local that the abort handler reads via
            ``locals().get("gateway")`` -- otherwise this refusal would itself delete
            the gateway it is protecting.
    """
    # Proof 1. Exact compare on the full ARN: the account id is part of it, and a
    # suffix/substring compare on the role NAME would accept another account's
    # identically-named role reached through a cross-account reference.
    if existing_role_arn and owned_role_arn and existing_role_arn == owned_role_arn:
        return "GATEWAY_ROLE"

    # Proof 2.
    pool_id = _pool_id_from_authorizer(previous_auth)
    pool_class = classify_user_pool(pool_id, cognito_client=cognito_client) if pool_id else POOL_FOREIGN_OR_UNKNOWN
    if pool_class in (POOL_SHARED_EXACT, POOL_OWNED_BY_STACK):
        return f"AUTHORIZER_POOL:{pool_class}"

    # The pool id is named because it is the operator's only lead on whose gateway this
    # is, and it is not a secret -- a pool id appears in every gateway's public
    # discoveryUrl. No client id and no client secret appear here.
    whose = f"its sign-in is provided by user pool {pool_id}" if pool_id else "it has no Cognito sign-in configured"
    raise ForeignResourceError(
        f"A gateway named '{gateway_name}' already exists in this account and "
        f"{whose}, which this deployment cannot prove it created. AgentCore gateway "
        f"names are account-global. Refusing to repoint it, because doing so would "
        f"stop it validating the tokens its own callers present. Either rename the "
        f"gateway on the canvas, or delete the existing gateway if it is yours. The "
        f"existing gateway has been left exactly as it was."
    )


class GatewayAdoptionRefused(RuntimeError):
    """An existing gateway cannot be adopted; nothing on it was changed."""


def _adoption_authorizer(
    gateway_name: str,
    gateway_id: str,
    previous_auth: dict,
    new_auth: dict,
    *,
    owner_sub: str,
    gateway_consumers,
) -> dict:
    """The authorizer an adopted gateway gets: this deploy's client plus every live one.

    F-63, measured live: a second deployment that adopted a gateway by name replaced
    ``allowedClients`` with its own client and then deleted the first deployment's,
    so the first agent's runtime, which froze that client into its environment, got
    HTTP 400 at the token endpoint and lost every tool. Every version of an agent is
    its own runtime and the implied gateway is named after the agent, so this was
    every redeploy's previous version, not only a second agent. A client that a live
    deployment still holds therefore stays allowed, and so is not retired; it is
    revoked when that deployment is torn down: its teardown removes the client from
    ``allowedClients`` before deleting it (F-66b), since deleting it alone left
    already-issued tokens accepted until they expired.

    Tenancy, before any of that: the adoption proof is stack-level (our role, our
    pool), so it passes for every user of the stack. A gateway that another user's
    live deployment is on is refused here, above the update, so neither its
    authorizer nor its targets are touched. A gateway nobody's live deployment is on
    has no owner left to protect, and adopting it is the redeploy path.

    ``gateway_consumers(gateway_id, pool_id)`` lists the OTHER live deployments on the gateway,
    with their clients in the gateway authorizer's pool
    (``DeploymentStateStore.live_gateway_consumers``). Without it, or when it fails,
    the adoption is refused: guessing "no consumers" is what stranded the runtime.
    """
    consumers = _adoption_consumers(
        gateway_name, gateway_id, previous_auth, owner_sub=owner_sub, gateway_consumers=gateway_consumers
    )
    jwt = (new_auth or {}).get("customJWTAuthorizer") or {}
    new_clients = list(jwt.get("allowedClients") or [])
    previous_jwt = (previous_auth or {}).get("customJWTAuthorizer") or {}
    previous_clients = list(previous_jwt.get("allowedClients") or [])
    _rest = lambda j: {k: v for k, v in j.items() if k != "allowedClients"}  # noqa: E731
    if consumers and (_rest(previous_jwt) != _rest(jwt) or set(previous_auth or {}) != set(new_auth or {})):
        # One JWT authorizer serves every client it allows, so anything but the client
        # list changing under a live consumer changes that consumer too: another issuer
        # strands it, another audience, scope or claim rule changes what its tokens may
        # do. Compared exactly, not by Cognito pool id: every external IdP has no pool
        # id, so two different issuers would compare equal.
        raise GatewayAdoptionRefused(
            f"Gateway '{gateway_name}' is used by {len(consumers)} live deployment(s) whose sign-in "
            f"settings differ from this deploy's, so it was left unchanged."
        )
    if any(not c.get("client_ids") for c in consumers):
        # A live deployment whose client is not on record (a legacy or manifest-only
        # row) may hold any of them, so none is dropped. Over-keeping costs a client
        # that stays allowed until its deployment is torn down; under-keeping strands
        # a runtime.
        kept = previous_clients
    else:
        live = {cid for c in consumers for cid in c["client_ids"]}
        kept = [cid for cid in previous_clients if cid in live]
    allowed = list(dict.fromkeys(new_clients + kept))
    if len(allowed) > 1:
        # A revoked-only list (F-66d) admits nobody; a real client replaces the marker,
        # and the marker stays when nothing would be left, since empty means everyone.
        allowed = [cid for cid in allowed if cid != NO_CLIENT_ALLOWED]
    return {**new_auth, "customJWTAuthorizer": {**jwt, "allowedClients": allowed}}


def _adoption_consumers(
    gateway_name: str,
    gateway_id: str,
    previous_auth: dict,
    *,
    owner_sub: str,
    gateway_consumers,
) -> list[dict]:
    """The other live deployments on ``gateway_id``, or a refusal to adopt it.

    Refuses when the caller has no owner, when the consumers cannot be read, and when
    any of them belongs to someone else. Called before this deploy creates anything
    (the pre-flight in ``deploy_gateway``) and again just above the update.
    """
    if not owner_sub:
        raise GatewayAdoptionRefused(
            f"Gateway '{gateway_name}' already exists, and this deploy has no owner to check it "
            f"against, so it was left unchanged."
        )
    if gateway_consumers is None:
        raise GatewayAdoptionRefused(
            f"Gateway '{gateway_name}' already exists, and this deploy cannot check which live "
            f"deployments use it, so it was left unchanged."
        )
    try:
        consumers = list(gateway_consumers(gateway_id, _pool_id_from_authorizer(previous_auth)))
    except Exception as e:  # noqa: BLE001
        raise GatewayAdoptionRefused(
            f"Gateway '{gateway_name}' already exists, and the deployments that use it could not "
            f"be listed ({error_code(e) or type(e).__name__}), so it was left unchanged."
        ) from e
    if any(c.get("owner_sub") != owner_sub for c in consumers):
        # Says nothing about who: the other deployment is not this caller's to see.
        raise GatewayAdoptionRefused(
            f"Gateway name '{gateway_name}' is in use by another deployment you do not own. "
            f"Choose a different gateway name."
        )
    return consumers


def _retire_stale_gateway_clients(previous_auth: dict, new_auth: dict, cognito_client) -> None:
    """Delete the app client a re-adopted gateway *used* to be pinned to.

    Peer finding F-10. ``_create_cognito_oauth_in_shared_pool`` calls
    ``create_user_pool_client`` unconditionally, and Cognito permits duplicate client
    names, so every redeploy of a gateway minted a NEW client and left the previous
    one in the pool — live and able to mint a token for that gateway's scope. The
    manifest row was then overwritten with the new client id, so nothing named the old
    one and no teardown ever deleted it. Redeploys are routine, so the pool accumulated
    one live orphaned credential per redeploy, each with its own secret.

    ARCC ``cnt_QbLfysVKP69zGk`` (credential rotation) is explicit that rotation is only
    rotation if the old credential is actually invalidated: *"create canary testing to
    continuously verify that old credentials are properly invalidated"*. Minting a
    replacement and leaving the original usable is not rotation, it is duplication.

    The old client id is taken from the gateway's OWN ``allowedClients`` — which the
    caller already fetched via ``get_gateway`` — so this costs no extra API call, and
    it needs no ``ListUserPoolClients`` grant (measured: ``implicitDeny`` for the
    gateway step role, while ``DeleteUserPoolClient`` on the pool is ``allowed``).

    Ownership is proven before any delete, the F-7 way, because ``allowedClients``
    comes off a gateway this deployment did not necessarily create: the client is
    deleted only when it lives in a pool that is provably the shared platform pool or
    provably this stack's own. A client id read off a FOREIGN gateway names a client in
    a foreign pool, and deleting that would break a stranger's gateway.
    """
    try:
        pool_id = _pool_id_from_authorizer(previous_auth)
        if not pool_id or "_" not in pool_id:
            return
        previous_clients = ((previous_auth or {}).get("customJWTAuthorizer") or {}).get("allowedClients") or []
        keep = set(((new_auth or {}).get("customJWTAuthorizer") or {}).get("allowedClients") or [])
        stale = [c for c in previous_clients if c and c not in keep and c != NO_CLIENT_ALLOWED]
        if not stale:
            return
        pool_class = classify_user_pool(pool_id, cognito_client=cognito_client)
        if pool_class not in (POOL_SHARED_EXACT, POOL_OWNED_BY_STACK):
            logger.warning(
                "Not deleting %d stale app client(s): pool %s is not provably ours",
                len(stale),
                pool_id,
            )
            return
        for client_id in stale:
            try:
                cognito_client.delete_user_pool_client(UserPoolId=pool_id, ClientId=client_id)
                # WARNING, not INFO, and not a style choice: this module's logger is
                # left at NOTSET and Lambda's root logger sits at WARNING, so an INFO
                # record never reaches a handler. Measured on the deployed
                # acfe2e-p0920-step-gateway after a real redeploy: the log group held
                # 17 WARNING and 4 ERROR records and ZERO INFO. At INFO this line was
                # invisible in production, which left the *refusal* above (a warning)
                # visible while the destructive action it guards was not -- exactly
                # backwards for the one call here that revokes a live credential.
                logger.warning("Deleted stale gateway app client in pool %s", pool_id)
            except Exception as e:  # noqa: BLE001
                # Code or type ONLY. The botocore message for this call echoes the
                # request parameters, which name the pool and the client id, and this
                # is the credential being retired (ARCC cnt_rHmO501l15qr2W). The error
                # CODE is what an operator needs -- AccessDenied means a missing grant
                # and needs fixing, ResourceNotFound means the client was already gone
                # and needs nothing -- and it is not sensitive. `type(e).__name__`
                # alone degrades to a bare "ClientError" whenever the code is not
                # modelled in the service definition, which answers neither question.
                logger.warning("Could not delete stale app client: %s", error_code(e) or type(e).__name__)
    except Exception as e:  # noqa: BLE001
        logger.warning("Stale app-client retirement skipped: %s", error_code(e) or type(e).__name__)


def _cleanup_old_cognito_pool(gw_detail: dict, cognito_client) -> None:
    """Extract the old Cognito user pool ID from a gateway's authorizer config and delete it."""
    try:
        auth_cfg = gw_detail.get("authorizerConfiguration", {})
        # Format: https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/...
        old_pool_id = _pool_id_from_authorizer(auth_cfg)
        if old_pool_id:
            # The pool id here comes from a GATEWAY'S OWN discoveryUrl, i.e. from a
            # resource we are re-adopting and did not necessarily create. That makes it
            # attacker-influenced input to a delete, so ownership is proven before the
            # delete rather than merely checked against one known id.
            old_pool_class = classify_user_pool(old_pool_id, cognito_client=cognito_client)
            if old_pool_class == POOL_SHARED_EXACT:
                # STOP. Re-adopting a gateway whose authorizer points at the shared
                # platform pool would otherwise delete that pool here, taking every
                # other deployed agent's gateway credentials with it. The
                # per-gateway app client is cleaned up by the manifest teardown; the
                # pool and its warm hosted domain are platform-owned and permanent.
                logger.warning("Old Cognito pool is the shared platform gateway-auth pool — not deleting it")
                return
            if old_pool_class != POOL_OWNED_BY_STACK:
                # A pool reached from a foreign gateway's discoveryUrl. Deleting it
                # would destroy a stranger's identity provider on the strength of a URL
                # we read out of a resource we do not own.
                logger.warning("Old Cognito pool is not provably ours — not deleting it")
                return
            if old_pool_id and "_" in old_pool_id:
                pool_detail = cognito_client.describe_user_pool(UserPoolId=old_pool_id)
                domain = pool_detail.get("UserPool", {}).get("Domain")
                if domain:
                    cognito_client.delete_user_pool_domain(UserPoolId=old_pool_id, Domain=domain)
                cognito_client.delete_user_pool(UserPoolId=old_pool_id)
                # WARNING for the same measured reason as the app-client deletion above:
                # an INFO record from this module never reaches a handler in Lambda, so
                # deleting an entire user pool -- every identity in it -- left no trace
                # in production at all, while the two "not deleting it" refusals above
                # were both visible.
                logger.warning("Cleaned up old Cognito pool: %s", old_pool_id)
    except Exception as e:
        # The message is kept here, unlike the app-client failure above, and the
        # difference is deliberate. DeleteUserPool's botocore message echoes the pool
        # id -- which the success line beside it already logs -- and carries the one
        # actionable detail an operator needs ("It has a domain configured that should
        # be deleted first"); the code alone is InvalidParameterException, which says
        # nothing. No half of a credential appears in it, so there is nothing to trade
        # the diagnosis for.
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


class _TargetTerminalFailure(RuntimeError):
    """A gateway target reached FAILED, or never reached READY, after its create (F-74)."""


class GatewayTargetUnproven(RuntimeError):
    """A target this deploy needs exists, and we could not establish what it is (F-74b).

    Separate from ``_TargetTerminalFailure`` because that one means the service told us the
    target is broken, and this one means we could not find out. Both are fatal; only this one
    can be resolved by retrying once the control plane answers.
    """


class GatewayTargetFamilyConflict(RuntimeError):
    """A target already holds the name and is a DIFFERENT kind of target (F-74b).

    A Lambda target where this deploy wants an OpenAPI one (or the reverse) is not a stale
    copy of our own work; nothing we create ever changes a target's family, so the thing
    holding the name was configured by someone else. Replacing it would silently repoint
    whatever depends on it, and reusing it would serve a tool plane that is not this canvas.
    """


#: The ``targetConfiguration.mcp`` discriminator keys, one per kind of target this platform
#: creates. The family is the one field in the shape that our own code NEVER changes for a
#: given target: a redeploy can move a Lambda ARN, a schema or an endpoint, but a canvas node
#: that produced an ``openApiSchema`` target keeps producing one.
_TARGET_FAMILY_KEYS = ("lambda", "openApiSchema", "smithyModel", "mcpServer")

#: Exactly the configuration fields ``UpdateGatewayTarget`` replaces, and therefore exactly
#: what has to match before an update can be reported as applied. ``name`` and ``description``
#: are excluded deliberately: the name is the sharing KEY, and a differing description is not
#: a differing tool plane, so including it would manufacture conflicts.
_TARGET_REPLACE_KEYS = (
    "targetConfiguration",
    "credentialProviderConfigurations",
    "metadataConfiguration",
    "privateEndpoint",
)

# Lists at these model paths are sets for target identity purposes. Keep this
# allow-list deliberately narrow: other AgentCore target shapes contain ordered
# lists (for example an HTTP passthrough's compositeIdentifier), and recursively
# sorting every list would let a materially different replacement masquerade as
# the configuration we asked the service to apply.
#: Where the service applies a default the request may omit (see _normalize_target_replace_value).
_OAUTH_PROVIDER_PATH = ("credentialProviderConfigurations", "*", "credentialProvider", "oauthCredentialProvider")
_OAUTH_DEFAULT_GRANT_TYPE = "CLIENT_CREDENTIALS"

_TARGET_SET_LIKE_LIST_PATHS = (
    ("credentialProviderConfigurations",),
    (
        "credentialProviderConfigurations",
        "*",
        "credentialProvider",
        "oauthCredentialProvider",
        "scopes",
    ),
    ("metadataConfiguration", "allowedRequestHeaders"),
    ("metadataConfiguration", "allowedQueryParameters"),
    ("metadataConfiguration", "allowedResponseHeaders"),
    ("privateEndpoint", "managedVpcResource", "subnetIds"),
    ("privateEndpoint", "managedVpcResource", "securityGroupIds"),
    ("targetConfiguration", "mcp", "lambda", "toolSchema", "inlinePayload"),
    (
        "targetConfiguration",
        "mcp",
        "apiGateway",
        "apiGatewayToolConfiguration",
        "toolOverrides",
    ),
    (
        "targetConfiguration",
        "mcp",
        "apiGateway",
        "apiGatewayToolConfiguration",
        "toolFilters",
    ),
    (
        "targetConfiguration",
        "mcp",
        "apiGateway",
        "apiGatewayToolConfiguration",
        "toolFilters",
        "*",
        "methods",
    ),
    ("targetConfiguration", "mcp", "connector", "enabled"),
    ("targetConfiguration", "mcp", "connector", "configurations"),
    (
        "targetConfiguration",
        "mcp",
        "connector",
        "configurations",
        "*",
        "parameterOverrides",
    ),
)

#: A terminal target failure whose only cause is a not-yet-propagated Cognito auth domain is retried
#: (bounded); see _update_existing_gateway_target. The pattern is the service's own wording.
_OAUTH_RESOLVE_FAILURE_RE = re.compile(r"failed to resolve hostname|check the oauth setup", re.IGNORECASE)
_OAUTH_RESOLVE_RETRIES = 6
_OAUTH_RESOLVE_RETRY_SECONDS = 20.0

_TARGET_TERMINAL_FAILURE_STATUSES = frozenset(
    {
        # CREATE_FAILED has appeared in older service responses even though the
        # current botocore enum calls the general terminal state FAILED.
        "CREATE_FAILED",
        "FAILED",
        "SYNCHRONIZE_UNSUCCESSFUL",
        "UPDATE_UNSUCCESSFUL",
    }
)


def target_family(target_configuration: dict | None) -> str:
    """Return the ``targetConfiguration.mcp`` discriminator, or ``""`` if there is none."""
    mcp = (target_configuration or {}).get("mcp") or {}
    return next((key for key in _TARGET_FAMILY_KEYS if key in mcp), "")


def _target_path_matches(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    return len(path) == len(pattern) and all(
        expected == "*" or expected == actual for actual, expected in zip(path, pattern, strict=True)
    )


def _target_list_is_set_like(path: tuple[str, ...]) -> bool:
    if any(_target_path_matches(path, pattern) for pattern in _TARGET_SET_LIKE_LIST_PATHS):
        return True
    # JSON Schema's `required` is a set of property names. It can occur at any
    # depth below a Lambda tool's input/output schema, so a fixed wildcard path
    # would be both brittle and incomplete.
    return (
        len(path) > 5
        and path[:5] == ("targetConfiguration", "mcp", "lambda", "toolSchema", "inlinePayload")
        and path[-1] == "required"
    )


def _normalize_target_replace_value(value, *, path: tuple[str, ...]):
    """Canonicalize one target configuration value for request/read-back comparison.

    ``GetGatewayTarget`` returns the same modelled configuration fields accepted by
    ``UpdateGatewayTarget``, but the service is free to reorder collection members and to
    omit optional top-level empty containers. Those representation differences do not mean
    an update failed to land. Only model paths known to be set-like are sorted; all other
    lists preserve order so a changed ordered configuration cannot produce a false match.
    Nested empty containers are preserved because they can be union members or otherwise
    semantically meaningful. The normalized value is never rendered in an error message
    (inline OpenAPI/Smithy payloads are modelled as sensitive).
    """
    if value is None:
        return None
    if isinstance(value, dict):
        normalized: dict = {}
        for key, child in value.items():
            normalized_child = _normalize_target_replace_value(
                child,
                path=(*path, str(key)),
            )
            if normalized_child is None:
                continue
            normalized[str(key)] = normalized_child
        # The service fills a default the request may omit and echoes it back. Measured live
        # (2026-09-28): the MCP server target's OAuth credential block is sent without
        # ``grantType``; GetGatewayTarget returned it with ``grantType: CLIENT_CREDENTIALS``, so
        # an equality read-back could never confirm the update and every REDEPLOY of an OAuth
        # target was refused as unproven (the create path never compares). Filling the documented
        # default on BOTH sides keeps a requested grant type that the service did not apply
        # detectable, and adds nothing the service does not itself assert.
        if path == _OAUTH_PROVIDER_PATH and "grantType" not in normalized:
            normalized["grantType"] = _OAUTH_DEFAULT_GRANT_TYPE
        return normalized
    if isinstance(value, (list, tuple)):
        normalized = [_normalize_target_replace_value(item, path=(*path, "*")) for item in value]
        if _target_list_is_set_like(path):
            return sorted(
                normalized,
                key=lambda item: json.dumps(
                    item,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    default=str,
                ),
            )
        return normalized
    return value


def _target_replace_shape(target: dict) -> dict:
    """Return the normalized subset whose successful application an update promises."""
    shape: dict = {}
    for key in _TARGET_REPLACE_KEYS:
        value = _normalize_target_replace_value(target.get(key), path=(key,))
        # The three optional top-level fields may be echoed as empty containers
        # when omitted. targetConfiguration is required and its nested empty
        # union members must remain part of the identity.
        if value is not None and not (key != "targetConfiguration" and value in ({}, [])):
            shape[key] = value
    return shape


def _target_replace_mismatches(desired: dict, observed: dict) -> list[str]:
    """Name configuration fields whose read-back is not yet the requested value."""
    wanted = _target_replace_shape(desired)
    actual = _target_replace_shape(observed)
    return [key for key in _TARGET_REPLACE_KEYS if wanted.get(key) != actual.get(key)]


def target_replace_digest(create_params: dict) -> str:
    """Canonical ``sha256:`` digest of the shape a target update would replace.

    Used to decide whether two deployments asking for the same target NAME are asking for the
    same target, so that identical requests can share one and differing requests can be
    refused instead of silently overwriting each other.

    Both sides of every comparison are produced by THIS function from a ``create_params`` dict
    the platform built. It is never computed from a ``get_gateway_target`` response, and that
    is not an accident: the control plane echoes a normalized form of what it was sent, so a
    digest of the echo would drift from a digest of the request for reasons that have nothing
    to do with the configuration changing -- and every redeploy would then be refused as a
    conflict. A comparison that can only ever say "different" is not a check.

    ``sort_keys`` plus compact separators makes the digest independent of the order the
    builders happen to insert keys in, and ``default=str`` keeps a stray non-JSON value (a
    datetime from a spec loader) from turning a governance check into a TypeError.
    """
    shape = _target_replace_shape(create_params)
    payload = json.dumps(shape, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: Set by ``deploy_gateway`` for the duration of one deployment. Every target this deploy
#: creates or updates appends ``{target_id, name, family, digest}`` here, and the result dict
#: carries the list out to ``gateway_step._record_gateway_resources``, which writes one
#: manifest row per target. A ContextVar rather than five new function signatures, matching
#: ``_SECRET_INTENT_JOURNAL`` above: the four target families are deployed by helpers that are
#: several frames deep and are called from more than one place.
_TARGET_RECORD_SINK: ContextVar = ContextVar("gateway_target_record_sink", default=None)


@contextmanager
def collecting_target_records():
    """Collect a record for every gateway target created or updated in the body."""
    records: list[dict] = []
    token = _TARGET_RECORD_SINK.set(records)
    try:
        yield records
    finally:
        _TARGET_RECORD_SINK.reset(token)


def _record_managed_target(target_id: str, target_name: str, create_params: dict, *, arm: str = "created") -> None:
    """Note that this deploy now owns *target_id*, if anyone is collecting.

    Deliberately tolerant of no sink: ``_create_gateway_target_with_retry`` is called from
    teardown-adjacent paths and from tests, and a deploy that cannot record its target must
    still deploy it. The row is what makes the NEXT deploy able to tell a target of ours from
    a target of someone else's, so a missing row degrades to today's behaviour rather than
    refusing anything.
    """
    sink = _TARGET_RECORD_SINK.get()
    if sink is None or not target_id:
        return
    sink.append(
        {
            "target_id": target_id,
            "name": target_name,
            "family": target_family(create_params.get("targetConfiguration")),
            "digest": target_replace_digest(create_params),
            # Provenance: "created" (CreateGatewayTarget succeeded for us), "adopted" (the name
            # already existed and we took it over), "updated" (we replaced an existing target's
            # configuration). Only "created" is unconditionally ours on a reused gateway.
            "arm": arm,
            "source_runtime_arn": "",
            "source_runtime_id": "",
        }
    )


def _annotate_managed_target(target_id: str = "", *, target_name: str = "", source_runtime_arn: str) -> None:
    """Bind the record of a target to the AgentCore runtime it fronts (MCP-runtime targets).

    Matched by id or by name: the id can become known only on a later readiness read, while the
    name is fixed before the create, so the caller annotates by name right after the create
    returns and every branch that learned the id is covered.

    Done after the create returns, on the record itself, rather than through a context variable:
    a ContextVar set in a Lambda's main thread survives into the next warm invocation, and a
    provenance field that can bleed between deployments is worse than none.
    """
    sink = _TARGET_RECORD_SINK.get()
    if not sink or not (target_id or target_name):
        return
    for record in sink:
        if (target_id and record.get("target_id") == target_id) or (target_name and record.get("name") == target_name):
            record["source_runtime_arn"] = source_runtime_arn
            record["source_runtime_id"] = source_runtime_arn.rsplit("/", 1)[-1] if source_runtime_arn else ""


def _create_gateway_target_with_retry(
    agentcore_ctrl,
    gateway_id: str,
    target_name: str,
    create_params: dict,
    max_retries: int = 5,
    *,
    update_existing: bool = False,
) -> dict:
    """Create a gateway target with retry logic. Reuses existing target on conflict.

    With *update_existing*, a target that already holds the name is replaced with
    *create_params* rather than reused as it is. F-66: the custom-tool branch reused
    a redeploy's target unchanged, so it kept invoking the previous deployment's
    function and kept the previous schema; a changed tool never took effect, and the
    tool broke once that deployment was deleted. The update is refused loudly rather
    than falling back to the stale target.
    """
    for attempt in range(max_retries):
        try:
            target = agentcore_ctrl.create_gateway_target(**create_params)
            logger.info("Gateway target created: %s", target.get("targetId"))
            # F-74 (peer 71): a create that never reaches READY used to fall out of this
            # loop and be returned as success, so a FAILED target -- or one still creating
            # when the budget ran out -- was reported as a deployed tool. Terminal FAILED
            # raises with the service's own reasons; an exhausted budget raises too.
            status = ""
            for _ in range(30):
                t = agentcore_ctrl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target["targetId"])
                status = t.get("status") or ""
                if status == "READY":
                    _record_managed_target(target.get("targetId") or "", target_name, create_params, arm="created")
                    return target
                if status in _TARGET_TERMINAL_FAILURE_STATUSES:
                    reasons = "; ".join(str(r) for r in (t.get("statusReasons") or [])) or "no statusReasons given"
                    raise _TargetTerminalFailure(
                        f"Gateway target '{target_name}' ({target['targetId']}) reached {status}: {reasons}. "
                        "Refusing to report a tool plane the gateway cannot serve."
                    )
                time.sleep(2)
            raise _TargetTerminalFailure(
                f"Gateway target '{target_name}' ({target['targetId']}) did not reach READY within the poll "
                f"budget (last status {status or 'unknown'}). Refusing to report it as deployed."
            )
        except _TargetTerminalFailure:
            raise  # never re-tried as a create, never matched by the message checks below
        except Exception as e:
            err_str = str(e)
            # If the target already exists, look it up and reuse it.
            # "already exists" message fallback kept: conflicts can surface as a
            # ValidationException whose message says "already exists".
            if is_error(e, "ConflictException") or "already exists" in err_str:
                if update_existing:
                    return _update_existing_gateway_target(agentcore_ctrl, gateway_id, target_name, create_params)
                # F-74b: every branch below used to end in ``return None`` -- a list failure
                # logged a warning, a name that could not be found said nothing at all -- and
                # four of the five callers on this path DISCARD the return value entirely
                # while the fifth reads it as "skip the readiness wait". So the two states
                # "the target is there and serving this canvas" and "we have no idea what is
                # under this name" both produced a green deployment. They are now distinct:
                # the only way out of this branch is a target we have looked at.
                logger.info("Gateway target '%s' already exists, reusing", target_name)
                try:
                    existing = _list_all_gateway_targets(agentcore_ctrl, gateway_id)
                except Exception as list_err:
                    raise GatewayTargetUnproven(
                        f"Gateway target '{target_name}' already exists on {gateway_id}, and the "
                        f"targets could not be listed to confirm what it is "
                        f"({type(list_err).__name__}). Refusing to report a tool this deploy has "
                        "not verified; retry once the control plane answers."
                    ) from list_err
                found = next((t for t in existing if t.get("name") == target_name), None)
                if found is None:
                    # The create said the name is taken and the list does not show it. Either
                    # the list is lying (a page we did not get, a read that lagged the create)
                    # or something removed it in between. Both mean we cannot say what is
                    # serving this tool.
                    raise GatewayTargetUnproven(
                        f"Gateway target '{target_name}' was refused as already existing on "
                        f"{gateway_id}, but no target with that name is in the listing. Refusing "
                        "to report it as deployed."
                    ) from e
                _reject_incompatible_target_family(agentcore_ctrl, gateway_id, found, target_name, create_params)
                logger.info("Reusing existing target: %s", found.get("targetId"))
                _record_managed_target(found.get("targetId") or "", target_name, create_params, arm="adopted")
                return found
            elif "not ready" in err_str.lower():
                # F-74b: the exhausted budget is raised HERE, not after the loop. The version
                # written first put it after the loop, which is unreachable: on the last
                # attempt this branch's old `and attempt < max_retries - 1` guard was false,
                # so control fell to `else: raise` and re-raised the raw "not ready" error.
                # The message an operator got was therefore about the gateway rather than
                # about the tool that is now missing, and the whole after-loop raise was dead
                # code that read as a guarantee. Its own test caught it.
                if attempt >= max_retries - 1:
                    raise GatewayTargetUnproven(
                        f"Gateway target '{target_name}' could not be created on {gateway_id} within "
                        f"{max_retries} attempts because the gateway never became ready. Nothing is "
                        "serving this tool."
                    ) from e
                time.sleep(10 * (attempt + 1))
            else:
                raise
    # Unreachable by construction: every branch above returns or raises. Kept as a raise and
    # not a `return None`, because a fallthrough that returns is exactly the shape this whole
    # change removed -- if a future edit does make this reachable, it must not be silent.
    raise GatewayTargetUnproven(
        f"Gateway target '{target_name}' on {gateway_id} left the create loop without a result."
    )


def _reject_incompatible_target_family(
    agentcore_ctrl,
    gateway_id: str,
    existing: dict,
    target_name: str,
    create_params: dict,
) -> None:
    """Refuse to adopt a target of a different family than the one this deploy builds (F-74b).

    The family (``lambda`` / ``openApiSchema`` / ``smithyModel`` / ``mcpServer``) is the one
    part of the shape our own code never changes for a given canvas node, so a mismatch is
    positive evidence that the thing holding this name was configured by someone else -- and
    "it is there" has never been evidence that it is ours. Reusing it serves a tool plane that
    is not this canvas; replacing it silently repoints whatever depends on it.

    The family is read from ``get_gateway_target``, because ``list_gateway_targets`` returns
    summaries with no ``targetConfiguration``. A get that FAILS is not treated as a mismatch:
    that would turn a throttle into "someone else owns your target", which is both wrong and
    unactionable. It is ``GatewayTargetUnproven`` instead -- still fatal, but retryable, and it
    says which of the two things went wrong.
    """
    target_id = existing.get("targetId") or existing.get("gatewayTargetId")
    wanted = target_family(create_params.get("targetConfiguration"))
    if not (target_id and wanted):
        return
    try:
        detail = agentcore_ctrl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    except Exception as get_err:
        raise GatewayTargetUnproven(
            f"Gateway target '{target_name}' ({target_id}) already exists and could not be read "
            f"to confirm it is the kind of target this deploy needs ({type(get_err).__name__})."
        ) from get_err
    actual = target_family(detail.get("targetConfiguration"))
    if actual and actual != wanted:
        raise GatewayTargetFamilyConflict(
            f"Gateway target '{target_name}' ({target_id}) on {gateway_id} is a {actual} target, "
            f"but this deployment needs a {wanted} target under that name. It was left exactly "
            "as it is. Rename the tool in this canvas, or remove the existing target if it is "
            "no longer wanted -- this deploy will not repoint a target it did not create."
        )


def _update_existing_gateway_target(agentcore_ctrl, gateway_id: str, target_name: str, create_params: dict) -> dict:
    """Replace a named target and prove the requested configuration is what is READY.

    ``READY`` is not an update acknowledgement: a target is already READY before
    ``UpdateGatewayTarget`` starts, and a control-plane read may continue returning that
    old READY snapshot after the update call. Returning on status alone records the desired
    digest while the gateway can still be serving the previous Lambda/schema/credentials.
    The read-back therefore has to be both terminally READY *and* semantically equal to the
    exact replace shape sent to the service.
    """
    existing = next(
        (t for t in _list_all_gateway_targets(agentcore_ctrl, gateway_id) if t.get("name") == target_name),
        None,
    )
    if not existing or not existing.get("targetId"):
        raise RuntimeError(
            f"Gateway target '{target_name}' already exists but could not be found to update, "
            "so this deploy cannot point it at its own tool."
        )
    # F-74b: the same provenance floor as the reuse path. An update is a FULL REPLACE, so
    # adopting a same-name target of a different family here does not serve the wrong tools --
    # it destroys someone else's configuration and serves ours in its place, which is strictly
    # worse and irreversible.
    _reject_incompatible_target_family(agentcore_ctrl, gateway_id, existing, target_name, create_params)
    target_id = existing["targetId"]
    # UpdateGatewayTarget is a full replace, so every field the create set is sent again,
    # except the two its input shape does not have (clientToken is create-only).
    update = {k: v for k, v in create_params.items() if k not in ("gatewayIdentifier", "clientToken")}
    agentcore_ctrl.update_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id, **update)
    status = ""
    mismatches = list(_TARGET_REPLACE_KEYS)
    polls_left = 30
    resolve_retries_left = _OAUTH_RESOLVE_RETRIES
    while polls_left > 0:
        polls_left -= 1
        detail = agentcore_ctrl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        status = detail.get("status") or ""
        mismatches = _target_replace_mismatches(create_params, detail)
        if status == "READY" and not mismatches:
            logger.info("Gateway target '%s' updated in place: %s", target_name, target_id)
            _record_managed_target(target_id, target_name, create_params, arm="updated")
            return {**existing, **detail, "status": status}
        if status in _TARGET_TERMINAL_FAILURE_STATUSES:
            reasons = "; ".join(str(reason) for reason in (detail.get("statusReasons") or []))
            if resolve_retries_left > 0 and _OAUTH_RESOLVE_FAILURE_RE.search(reasons):
                # The service's resolver has not yet seen a Cognito auth domain the MCP step
                # created moments ago (live 2026-09-28: "Please check the OAuth setup. Failed to
                # resolve hostname: <domain>.auth.us-east-1.amazoncognito.com"). The update is
                # idempotent; give propagation time and re-issue it. Any OTHER terminal reason
                # still fails at once below.
                resolve_retries_left -= 1
                logger.warning(
                    "Gateway target '%s' (%s) update hit an OAuth hostname resolution failure; retrying (%d left)",
                    target_name,
                    target_id,
                    resolve_retries_left,
                )
                time.sleep(_OAUTH_RESOLVE_RETRY_SECONDS)
                agentcore_ctrl.update_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id, **update)
                polls_left += 10
                continue
            suffix = f": {reasons}" if reasons else ""
            raise _TargetTerminalFailure(
                f"Gateway target '{target_name}' ({target_id}) reached {status} after its update"
                f"{suffix}. Refusing to report the requested tool plane as deployed."
            )
        time.sleep(2)
    if status == "READY" and mismatches:
        raise GatewayTargetUnproven(
            f"Gateway target '{target_name}' ({target_id}) returned READY after its update, but "
            f"the service never confirmed the requested {', '.join(mismatches)}. Refusing to "
            "record desired-state provenance for a target that may still be serving its old configuration."
        )
    raise GatewayTargetUnproven(
        f"Gateway target '{target_name}' ({target_id}) did not become READY with the requested "
        f"configuration after its update (last status {status or 'unknown'})."
    )


def _build_gateway_role_policy() -> dict:
    """Build the IAM policy document for gateway roles with scoped permissions."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    # `agent-credential-provider` is not an AWS service, so the
                    # entry that used to sit here authorized nothing -- and it was
                    # the only grant that made this role look like it could reach
                    # the credential providers. It can: the real actions are
                    # bedrock-agentcore:GetResourceOauth2Token and
                    # GetResourceApiKey, covered by the wildcard below. See the
                    # prefix note in services/per_agent_identity.py.
                    "bedrock-agentcore:*",
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
        urllib.request.urlopen(req, timeout=30) as resp  # nosec B310
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


def _build_openapi_schema(
    spec_str: str,
    *,
    connector_id: str,
    region: str,
    deployment_id: str | None = None,
) -> dict:
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

    bucket = _GATEWAY_ARTIFACT_BUCKET.get() or os.environ.get(
        "ARTIFACTS_BUCKET_NAME",
        "",
    )
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
    expected_owner = _GATEWAY_ARTIFACT_OWNER.get() or os.environ.get(
        "AWS_ACCOUNT_ID",
        "",
    )
    put_kwargs = {
        "Bucket": bucket,
        "Key": key,
        "Body": spec_str.encode("utf-8"),
        "ContentType": "application/json",
    }
    if expected_owner:
        put_kwargs["ExpectedBucketOwner"] = expected_owner
    if deployment_id:
        # DELIBERATELY still owner_tags, not governed_tags: this is the one tag site in this
        # module that P0-B governance tags do not reach, and the reason is an AWS limit, not
        # an oversight. An S3 OBJECT accepts at most 10 tags -- a fifth of the 50 every other
        # resource here allows -- and three of those slots are already spent on ManagedBy,
        # AgentCoreStack and DeploymentId. A tag policy with eight keys would therefore take
        # the OpenAPI schema upload from "works" to "fails the whole gateway deploy", and it
        # would fail at the 8th key with no way for the operator to know 7 was the ceiling.
        # governed_tags would validate that set against the 50-tag limit and pass it. Closing
        # this needs a per-sink ceiling in resource_tagging, which is a separate change; until
        # then an S3 object carries ownership only, and nothing reads a governance tag off it.
        put_kwargs["Tagging"] = urllib.parse.urlencode(
            owner_tags(
                region,
                extra={"DeploymentId": str(deployment_id)},
            )
        )
    _aws_client("s3", region_name=region).put_object(
        **put_kwargs,
    )
    s3_block: dict = {"uri": f"s3://{bucket}/{key}"}
    if expected_owner:
        s3_block["bucketOwnerAccountId"] = expected_owner
    logger.info("Staged connector '%s' spec (%d bytes) to s3://%s/%s", connector_id, len(spec_str), bucket, key)
    return {"s3": s3_block}


def purge_credential_provider(
    agentcore_ctrl,
    name: str,
    region: str | None = None,
) -> tuple[bool, str]:
    """Delete *name* from BOTH credential-provider namespaces. Returns (ok, message).

    Needed because the two namespaces are independent and the API is not honest
    about it: ``delete_oauth2_credential_provider`` on an API-key provider returns
    success WITHOUT deleting anything (verified live against bedrock-agentcore-
    control). A teardown that trusts a recorded type therefore reports success and
    strands the provider — exactly what happened to five providers in a live
    account before this existed.

    The discriminator is the matching **getter**: ``get_api_key_credential_provider``
    raises ResourceNotFound for an OAuth provider and vice versa, so probing with it
    tells us which namespace the name actually lives in. Deleting only what the
    probe found also keeps this idempotent, which teardown retries depend on.
    """
    try:
        deleted = delete_owned_credential_provider(agentcore_ctrl, name, region)
    except ResourceDeletionRefused as exc:
        return False, f"Credential provider {name} kept (protected: {exc})"
    except Exception as exc:  # noqa: BLE001
        return False, f"Credential provider {name} delete error: {exc}"
    if not deleted:
        return True, f"Credential provider {name} already gone"
    labels = {
        # The manifest row type -> the provider's own enum label. Both halves are constants
        # from the AgentCore API, not a credential; detect-secrets flags the pair only
        # because the key contains "api_key".
        "api_key_credential_provider": "API_KEY",  # pragma: allowlist secret
        "oauth2_credential_provider": "OAUTH",
    }
    return (
        True,
        "; ".join(f"{labels.get(kind, kind)} credential provider {name} deleted" for kind in deleted),
    )


def _delete_connector_credential_provider(
    agentcore_ctrl,
    entry: str,
    region: str | None = None,
) -> tuple[bool, str]:
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

    typed = {
        "API_KEY": (
            "api_key_credential_provider",
            "delete_api_key_credential_provider",
        ),
        "OAUTH": (
            "oauth2_credential_provider",
            "delete_oauth2_credential_provider",
        ),
    }
    if ptype in typed:
        resource_type, delete_method = typed[ptype]
        try:
            assert_agentcore_resource_owned(
                agentcore_ctrl,
                resource_type,
                name,
                region,
            )
            getattr(agentcore_ctrl, delete_method)(name=name)
            return True, f"{ptype} credential provider {name} deleted"
        except Exception as exc:  # noqa: BLE001
            if resource_is_missing(exc):
                return True, f"Credential provider {name} already gone"
            if isinstance(exc, ResourceDeletionRefused):
                return (
                    False,
                    f"{ptype} credential provider {name} kept (protected: {exc})",
                )
            return False, f"{ptype} provider {name} delete error: {exc}"

    # Untyped (legacy record): the namespace is unknown, so probe both.
    return purge_credential_provider(agentcore_ctrl, name, region)


def _deploy_connector_targets(
    agentcore_ctrl,
    gateway_id: str,
    region: str,
    connectors: list[dict],
    owner_sub: str = "",
    deployment_id: str = "",
    secrets_prebound: bool = False,
    resource_tags: dict | None = None,
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
            _delete_connector_credential_provider(agentcore_ctrl, entry, region)
        if created_secrets:
            sm = _create_secrets_client(region)
            for _sidx, sarn in enumerate(created_secrets):
                try:
                    if deployment_id:
                        delete_deployment_bound_secret(
                            region=region,
                            deployment_id=deployment_id,
                            secret_ref=sarn,
                            secrets_client=sm,
                        )
                    else:
                        # Compatibility for direct calls to this private helper.
                        # Production deploy_gateway requires/threads deployment_id.
                        sm.delete_secret(SecretId=sarn, ForceDeleteWithoutRecovery=True)
                except Exception:  # noqa: BLE001 — best-effort rollback
                    # Log NOTHING from the secret-bearing scope (no ARN/value):
                    # CodeQL py/clear-text-logging taints created_secrets; a
                    # positional index is enough to correlate with the create log.
                    logger.warning("Rollback: could not delete connector secret #%d", _sidx)
        for uri in created_spec_s3_uris:
            try:
                _delete_spec_s3_object(uri, region, deployment_id)
            except Exception:  # noqa: BLE001 — best-effort rollback
                logger.warning("Rollback: could not delete a staged connector specification")

    try:
        _deploy_connector_targets_inner(
            agentcore_ctrl,
            gateway_id,
            region,
            connectors,
            owner_sub,
            deployment_id,
            secrets_prebound,
            created_providers,
            created_secrets,
            created_spec_s3_uris,
            resource_tags=resource_tags,
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


def _delete_spec_s3_object(
    uri: str,
    region: str,
    deployment_id: str,
) -> None:
    """Delete every version of a staged spec, each only after re-reading its own tags."""
    if not uri.startswith("s3://"):
        raise ValueError(f"Malformed staged specification URI: {uri!r}")
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"Malformed staged specification URI: {uri!r}")
    expected_owner = _GATEWAY_ARTIFACT_OWNER.get()
    s3 = _aws_client("s3", region_name=region)
    delete_owned_s3_object(
        s3,
        bucket,
        key,
        region=region,
        deployment_id=deployment_id,
        expected_bucket_owner=expected_owner,
    )


def _deploy_connector_targets_inner(
    agentcore_ctrl,
    gateway_id: str,
    region: str,
    connectors: list[dict],
    owner_sub: str,
    deployment_id: str,
    secrets_prebound: bool,
    created_providers: list,
    created_secrets: list,
    created_spec_s3_uris: list,
    resource_tags: dict | None = None,
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

        # The SFN path has already exact-bound every ref before history
        # serialization. The direct path binds here. Never echo the raw value.
        secret_arn = conn.get("secret_arn") or conn.get("secretArn") or ""
        raw_secret = conn.get("secret_value") or conn.get("secretValue")
        if secrets_prebound:
            if raw_secret:
                raise RuntimeError("A pre-bound connector must not carry a plaintext credential.")
            if secret_arn:
                payload_key = "clientSecret" if auth_method == AUTH_OAUTH2_CC else "apiKey"
                secret_arn, _created = bind_connector_secret_for_deployment(
                    region=region,
                    owner_sub=owner_sub,
                    deployment_id=deployment_id,
                    payload_key=payload_key,
                    secret_ref=secret_arn,
                    resource_tags=resource_tags,
                )
        elif secret_arn or raw_secret:
            payload_key = "clientSecret" if auth_method == AUTH_OAUTH2_CC else "apiKey"
            secret_arn, _created = bind_connector_secret_for_deployment(
                region=region,
                owner_sub=owner_sub,
                deployment_id=deployment_id,
                payload_key=payload_key,
                raw_value=raw_secret,
                secret_ref=secret_arn,
                resource_tags=resource_tags,
            )
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
                scope=gateway_id,
                region=region,
                resource_tags=resource_tags,
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
            provider_arn = _ensure_api_key_credential_provider(
                agentcore_ctrl,
                provider_name,
                secret_arn=secret_arn,
                scope=gateway_id,
                region=region,
                resource_tags=resource_tags,
            )
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
            # Right-stripped for the same reason as _mcp_api_key_cred_config:
            # AgentCore supplies the space between prefix and key, so a
            # user-typed "Bearer " would be sent as "Bearer  <key>" and refused.
            prefix = (prefix or "").rstrip()
            if prefix:
                cred_cfg["credentialProvider"]["apiKeyCredentialProvider"]["credentialPrefix"] = prefix

        # The SCOPED name, matching what the ensure_* helpers actually created —
        # recording the bare name would leave the real provider behind on delete.
        created_providers.append(f"{provider_type}:{_scoped_provider_name(provider_name, gateway_id)}")

        openapi_schema = _build_openapi_schema(
            spec_inline,
            connector_id=connector_id or "generic",
            region=region,
            deployment_id=deployment_id,
        )
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
        # update_existing (F-74b): a redeploy after the connector's spec, base URL or
        # credential provider changed used to REUSE the old target untouched, so the gateway
        # kept serving the previous spec and the change never took effect -- the same defect
        # F-66 fixed for custom tools, on a path that was missed.
        _create_gateway_target_with_retry(agentcore_ctrl, gateway_id, target_name, create_params, update_existing=True)
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
    secrets_prebound: bool = False,
    gateway_consumers=None,
    claim_gateway_name=None,
    resource_tags: dict | None = None,
) -> dict:
    """Deploy a Gateway using pure boto3 APIs.

    Args:
        gateway_config: Gateway configuration dict with ``name``.
        region: AWS region.
        template_id: Optional template identifier.
        gateway_tools: Tool IDs to deploy as Lambda targets.
        identity_config: Optional identity provider config (for external IDPs).
        custom_tools: Optional list of AI-generated custom tool definitions.
        gateway_consumers: ``(gateway_id, pool_id) -> [{owner_sub, client_ids}]`` for the OTHER live
            deployments on a gateway; adopting an existing gateway is refused without it.
            See ``_adoption_authorizer``.
        claim_gateway_name: ``(normalized gateway name) -> None``, raising to refuse. Called
            once, before any Cognito or IAM side effect. See services/gateway_name_claim.

    Returns:
        Dict with ``success``, ``gateway_url``, ``gateway_id``, ``gateway_name``,
        ``client_info``, ``lambda_function_name``. A failure carries
        ``refused_before_side_effects``: True only when it came before the name claim
        was taken, so this call created, claimed and changed nothing.

        ``gateway_targets`` carries one record per target this call created, reused or
        updated, on BOTH the success and the failure return, for the same reason the
        rest of ``_partial`` is returned on failure: a target created before the step
        died is a real resource, and a row is the only thing that makes it recoverable.
    """
    _side_effects_started = False
    # F-74b: every target this deploy touches records itself here, several frames down,
    # and the step handler turns the list into one manifest row per target. Set and reset
    # directly rather than wrapping the body in ``collecting_target_records()``: the body
    # is ~1200 lines, and a reindent of all of it would bury this change in its own diff.
    # The contextmanager remains the API for callers and tests that want a scoped sink.
    # Safe as a ContextVar because nothing in this module runs a target deploy on another
    # thread (a thread starts with an empty Context, and the sink would read as absent).
    _target_records: list[dict] = []
    _target_sink_token = _TARGET_RECORD_SINK.set(_target_records)
    try:
        gateway_tools = gateway_tools or []
        custom_tools = custom_tools or []
        connectors = connectors or []
        external_mcp_servers = external_mcp_servers or []

        # Fail closed on a tool set this deployer cannot faithfully serve, BEFORE any
        # AWS side effect (no client is created yet, so refused_before_side_effects is
        # True). Silently routing a mixed or unknown gateway_tools list is how the live
        # plane diverged from the CFN export: an unknown id just vanished from the
        # advertised schema, and a legacy id beside a canonical id took the legacy
        # branch and dropped every canonical tool. The result carries a SINGULAR
        # lambda_function_name and the manifest records ONE Lambda, so a partial
        # two-family deploy is not recoverable -- refuse it here rather than ship it.
        if gateway_tools:
            _LEGACY_TOOL_IDS_PREFLIGHT = {
                "check_order_status",
                "lookup_customer",
                "search_knowledge_base",
                "get_return_policy",
            }
            _known_tool_ids = set(GATEWAY_TOOL_SCHEMAS) | _LEGACY_TOOL_IDS_PREFLIGHT
            _unknown = [t for t in gateway_tools if t not in _known_tool_ids]
            if _unknown:
                raise RuntimeError(
                    "Refusing to deploy gateway: unknown tool "
                    f"{', '.join(sorted(set(_unknown)))} is not a known gateway tool, so "
                    "it would be silently dropped from the advertised schema instead of "
                    "served. Remove it or add a schema before deploying."
                )
            _legacy_requested = sorted(t for t in gateway_tools if t in _LEGACY_TOOL_IDS_PREFLIGHT)
            _dynamic_requested = sorted(t for t in gateway_tools if t in GATEWAY_TOOL_SCHEMAS and t != "knowledge_base")
            if _legacy_requested and _dynamic_requested:
                raise RuntimeError(
                    "Refusing to deploy gateway: a mixed tool request combines legacy "
                    f"CustomerSupportTools ({', '.join(_legacy_requested)}) with dynamic "
                    f"tools ({', '.join(_dynamic_requested)}). This deployer creates one "
                    "tool Lambda and records one manifest row, so a mixed request would "
                    "drop one family and leak the other. Deploy the two families separately."
                )

        agentcore_ctrl = _create_agentcore_control_client(region)
        cognito_client = _create_cognito_client(region)

        raw_name = gateway_config.get("name", "AgentCoreGateway")
        gateway_name = re.sub(r"[^a-zA-Z0-9-]", "-", raw_name)[:48]
        if not gateway_name or not gateway_name[0].isalnum():
            gateway_name = "gw-" + gateway_name

        # Pre-flight (F-63). A same-name gateway that someone else's live deployment is
        # on is refused HERE, before this deploy mints a client, re-puts the existing
        # gateway role's policy or touches the resource server both would share. The
        # check above the update repeats it against a fresh read, for the race.
        for _existing in _list_all_gateways(agentcore_ctrl):
            if _existing.get("name") != gateway_name:
                continue
            try:
                _existing_detail = agentcore_ctrl.get_gateway(gatewayIdentifier=_existing["gatewayId"])
            except Exception as e:  # noqa: BLE001
                if resource_is_missing(e):
                    break
                raise
            _adoption_consumers(
                gateway_name,
                _existing["gatewayId"],
                _existing_detail.get("authorizerConfiguration") or {},
                owner_sub=owner_sub,
                gateway_consumers=gateway_consumers,
            )
            break

        # The name claim (services/gateway_name_claim). The pre-flight above reads the
        # manifest, and a concurrent deploy that is still creating has no row yet, so
        # both would pass it. The claim is one conditional write, and it comes BEFORE
        # Step 1: a refused deploy has minted no client, created no role and touched no
        # resource server. It comes AFTER the pre-flight, so a name refused there
        # leaves no claim behind on another tenant's gateway.
        # Set before the claim, not after: a claim write whose response was lost may
        # still have landed.
        _side_effects_started = True
        if claim_gateway_name is not None:
            claim_gateway_name(gateway_name)

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
            # owner_sub + deployment_id are passed so the minted client-secret
            # reference is BOUND to this tenant and this deploy, not merely placed in
            # the product's shared secret namespace where a prefix match would claim it.
            cognito_response = _create_cognito_oauth(
                cognito_client,
                gateway_name,
                region,
                owner_sub,
                deployment_id or "",
                resource_tags=resource_tags,
            )
            # A gateway in any account or region reuses the platform's warm
            # HOME-region pool. Conflict adoption and stale-client retirement happen
            # later in this function, so route those operations to that pool's
            # account (platform credentials) and region too. Dedicated pools stay on
            # the original target client, moved only to the pool's own region.
            _auth_pool_id = (cognito_response.get("client_info") or {}).get("user_pool_id", "")
            if _auth_pool_id:
                _auth_pool_region = _pool_region(_auth_pool_id)
                if is_platform_owned_user_pool(_auth_pool_id):
                    cognito_client = _create_platform_cognito_client(_auth_pool_region)
                elif _auth_pool_region != region:
                    cognito_client = _create_cognito_client(_auth_pool_region)

        # Step 1b: Create gateway IAM role
        iam_client = _create_iam_client()
        gw_role_name = regional_iam_role_name(
            f"AgentCoreGateway-{gateway_name}",
            region,
        )
        # Not a duplicate of gw_role_name. That one is the name we INTEND to use and
        # is bound before the first IAM call; this one stays None until IAM has
        # confirmed a role by that name exists, and it is the only thing the
        # deployment manifest is allowed to record.
        #
        # Measured live on 2026-09-21: a deploy refused at Step 1 (the OIDC discovery
        # document advertised a cleartext token_endpoint, so _create_external_oauth_config
        # raised at :3347, ~14 lines before the create_role below ever ran) still wrote
        # `{"type": "iam_role", "name": "AgentCoreGateway-f6probe"}` into
        # created_resources, because _record_gateway_resources derived the row from
        # gateway_name — a string that exists from the top of the function. The delete
        # is idempotent (NoSuchEntity reads as "already absent"), so nothing broke, but
        # the manifest asserted a resource that never existed, which is the same defect
        # class as the shared-pool row documented in the failure-path allow-list below.
        gw_role_confirmed: str | None = None
        gw_role_created = False
        gateway_created = False
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
                # Tagged at creation, so the already-exists branch below can tell this
                # deployment's gateway role from an identically-named foreign one. The
                # live test account holds `AgentCoreGateway-omargw` for a gateway this
                # platform did not create, which is exactly the collision.
                Tags=governed_tag_list(region, resource_tags),
                **create_role_kwargs(),
            )
            gw_role_arn = role_resp["Role"]["Arn"]
            gw_role_created = True
            # The role exists from this line onward, so confirm it HERE rather than
            # after the inline-policy work below. put_role_policy can fail on its own
            # (a permissions boundary or an SCP rejecting the inline policy is the
            # realistic case) and that exception leaves this try-block for the outer
            # handler with a real role already in the account; confirming later would
            # report None for exactly that failure and strand the role with no
            # manifest row to recover it.
            gw_role_confirmed = gw_role_name
            # SECURITY: Scope Lambda invoke to AgentCore-prefixed functions only,
            # and limit bedrock-agentcore actions to gateway-specific operations.
            sts_client = _aws_client("sts")
            sts_client.get_caller_identity()  # validate credentials
            _gw_policy_doc = _build_gateway_role_policy()
            iam_client.put_role_policy(
                RoleName=gw_role_name,
                PolicyName="GatewayLambdaInvoke",
                PolicyDocument=json.dumps(_gw_policy_doc),
            )
            time.sleep(10)
        except iam_client.exceptions.EntityAlreadyExistsException:
            # Prove it is ours FIRST. Two things below make adopting on name alone
            # unsafe: the inline policy is re-put (replacing whatever the real owner
            # granted), and `gw_role_confirmed` puts the role in this deployment's
            # manifest, so teardown would later delete it. The live account holds a
            # foreign `AgentCoreGateway-omargw`, and a gateway named `omargw` on the
            # canvas is all it takes to reach it.
            _existing_gw_role = iam_client.get_role(RoleName=gw_role_name)["Role"]
            assert_this_deployment_may_mutate(
                f"IAM role {gw_role_name}",
                _existing_gw_role.get("Tags"),
                region,
            )
            gw_role_arn = _existing_gw_role["Arn"]
            # F-06: retrofit the permissions boundary once ownership is proven, before the
            # inline policy below is re-put.
            ensure_role_boundary(iam_client, gw_role_name, role=_existing_gw_role)
            # An adopted role is still this gateway's role to record. Not doing so is
            # how AgentCoreGateway-agent-gateway survived several teardowns: the deploy
            # that first created it had no manifest row either, so no run ever named it.
            gw_role_confirmed = gw_role_name
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
                protocolConfiguration=pinned_protocol_configuration(),
                authorizerType="CUSTOM_JWT",
                authorizerConfiguration=cognito_response["authorizer_config"],
                tags=governed_tags(region, resource_tags),
            )
            gateway = {
                "gatewayId": gw_resp["gatewayId"],
                "gatewayUrl": gw_resp.get("gatewayUrl", ""),
                "gatewayArn": gw_resp.get("gatewayArn", ""),
                "roleArn": gw_resp.get("roleArn", ""),
            }
            gateway_created = True
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
                        # F-66e: the read, the consumer list and the repoint all happen under the
                        # gateway's write lock, so a teardown's revoke cannot land between this
                        # read and this update and then be undone by it.
                        with gateway_mutation_lock(agentcore_ctrl, region, gw_id) as gw_lock:
                            gw_detail = gw_lock.read()
                            # `gateway` is DELIBERATELY not assigned yet. The abort handler
                            # at the bottom of this function reads
                            # ``locals().get("gateway")`` and hands its gatewayId to
                            # cleanup_gateway_resources, which DELETES it. That is correct
                            # for a gateway this deploy created, and wrong for one it is
                            # merely re-adopting: a failure between here and the successful
                            # repoint would destroy a gateway that existed before this
                            # deploy started. Assigning it only after the repoint succeeds
                            # is what makes the refusal below true when it says the existing
                            # gateway was left exactly as it was.
                            # The authorizer this gateway is CURRENTLY pinned to. Captured
                            # before the update, because it is the only record of the
                            # previous pool and the previous app client — and after the
                            # update it is gone. Peer finding F-10.
                            _previous_auth = gw_detail.get("authorizerConfiguration") or {}
                            # Prove it is ours BEFORE repointing it. A ConflictException on
                            # an account-global name cannot tell "my own redeploy" from
                            # "someone else already has that name", and the update below
                            # would repoint a stranger's gateway at this deployment's
                            # authorizer -- which stops it validating its real callers'
                            # tokens. Raising here, above the update and above every
                            # assignment to `gateway`, is what makes the refusal honest:
                            # nothing has been touched yet. Peer finding F-10, sub-defect 4.
                            _proof = assert_gateway_is_adoptable(
                                gateway_name,
                                _previous_auth,
                                existing_role_arn=gw_detail.get("roleArn") or gw.get("roleArn") or "",
                                owned_role_arn=gw_role_arn,
                                cognito_client=cognito_client,
                            )
                            logger.info("Adopting existing gateway %s (ownership proof: %s)", gw_id, _proof)
                            # Tenancy and live consumers, still above the update (F-63).
                            _adopted_auth = _adoption_authorizer(
                                gateway_name,
                                gw_id,
                                _previous_auth,
                                cognito_response["authorizer_config"],
                                owner_sub=owner_sub,
                                gateway_consumers=gateway_consumers,
                            )
                            # Repoint the gateway FIRST, and do not swallow a failure.
                            #
                            # This used to delete the old Cognito pool before the update,
                            # then log a warning if the update failed. Both halves were
                            # wrong in the same direction: the gateway was left pinned to a
                            # pool that no longer existed, so it could not validate any
                            # token, and the deploy reported success anyway. A gateway whose
                            # authorizer could not be repointed has a dead tool plane, which
                            # is the one failure mode this module already refuses elsewhere
                            # ("a fallback would be a green deploy with a dead tool plane").
                            try:
                                # A full replace: everything else the gateway holds --
                                # its policy engine, KMS key, interceptors, WAF -- is
                                # re-sent, and only the authorizer changes (F-62).
                                # Applied means the read-back carries THIS authorizer,
                                # not merely READY: a stale READY still holds the old one.
                                gw_ready = gw_lock.update(
                                    preserving_gateway_update(
                                        gw_detail,
                                        gw_id,
                                        overrides={
                                            "authorizerType": "CUSTOM_JWT",
                                            "authorizerConfiguration": _adopted_auth,
                                        },
                                    ),
                                    authorizer_is(_adopted_auth),
                                )
                                logger.info("Updated gateway %s authorizer config", gw_id)
                            except GatewayUpdateUnconfirmed as update_err:
                                raise RuntimeError(
                                    f"Gateway '{gateway_name}' already exists and its authorizer "
                                    f"update was sent but never read back as applied ({update_err}). "
                                    f"It may still land, so the gateway may no longer be as it was; "
                                    f"redeploy once it settles."
                                ) from update_err
                            except Exception as update_err:
                                # The error CODE, not the message: this string becomes a
                                # Step Functions failure Cause the UI renders, and a
                                # botocore message echoes the request -- which here carries
                                # the whole authorizerConfiguration. The code is the
                                # actionable half and is not sensitive. Falls back to the
                                # exception type for a non-ClientError.
                                #
                                # MEASURED, so do not narrow this to "a missing UpdateGateway
                                # grant": UpdateGateway is granted on `*`, and a live probe
                                # still got AccessDeniedException here because `roleArn` above
                                # re-passes the EXISTING gateway's role and `iam:PassRole` is
                                # scoped to `role/AgentCore*`. A gateway adopted on the
                                # authorizer proof whose role sits outside that prefix
                                # therefore cannot be repointed at all -- which fails closed,
                                # correctly, but for a reason the code alone does not name.
                                _code = error_code(update_err) or type(update_err).__name__
                                raise RuntimeError(
                                    f"Gateway '{gateway_name}' already exists but its authorizer "
                                    f"could not be repointed at this deployment's Cognito client "
                                    f"({_code}), so it cannot validate the tokens this deploy "
                                    f"mints. Refusing to report a working gateway. The existing "
                                    f"gateway has been left exactly as it was."
                                ) from update_err
                        # Adopted, repointed and READY. Only now is it ours to hand on
                        # — and only now is it ours for the abort handler to release.
                        gateway = {
                            "gatewayId": gw_id,
                            "gatewayUrl": gw_ready.get("gatewayUrl") or gw_detail.get("gatewayUrl", ""),
                            "gatewayArn": gw_detail.get("gatewayArn", ""),
                            "roleArn": gw_ready.get("roleArn") or gw_detail.get("roleArn", gw.get("roleArn", "")),
                        }
                        # Only NOW is the old authorizer genuinely unused, so retiring
                        # it can no longer strand a live gateway. Two steps, in this
                        # order: the stale app client goes first because it is the
                        # credential, and it is the only step that reaches the SHARED
                        # platform pool (which must survive). Deleting the client is
                        # not redundant for a pool this deploy owns either —
                        # _cleanup_old_cognito_pool swallows its own failures, so a
                        # pool delete that does not happen would otherwise leave the
                        # old client live with nothing naming it.
                        # Against the ADOPTED authorizer: a client a live deployment
                        # still holds was kept in it, so it is not stale (F-63).
                        _retire_stale_gateway_clients(
                            _previous_auth,
                            _adopted_auth,
                            cognito_client,
                        )
                        _cleanup_old_cognito_pool({"authorizerConfiguration": _previous_auth}, cognito_client)
                        logger.info("Reusing gateway %s, url=%s", gw_id, gateway["gatewayUrl"])
                        break
                if gateway is None:
                    raise RuntimeError(f"Gateway '{gateway_name}' exists but not found via list") from create_err
            else:
                raise

        # "" means THIS DEPLOY CREATED NO TOOL LAMBDA. It must stay falsy.
        #
        # This used to be the literal name "AgentCoreLambdaTestFunction", which no
        # branch below ever creates — it was a placeholder. But every consumer reads
        # this field as a function to DELETE: cleanup_gateway_resources sends it to
        # delete_function outright (is_shared_tool_function is False for it), and both
        # manifest writers (gateway_step._record_gateway_resources,
        # deployment._record_resource_best_effort) turn it into a `lambda` row that
        # every later teardown re-deletes. So a gateway with only config-driven
        # targets — the multi-target feature, where no tool Lambda is built —
        # recorded and then deleted a hard-coded shared name it never created.
        #
        # Measured live on 2026-09-21: a failed gateway deploy of `gwfixaafac57e`
        # issued DeleteFunction("AgentCoreLambdaTestFunction") twice, once under the
        # gateway step's role and once under the status-update step's, both returning
        # ResourceNotFoundException only because no such function happened to exist in
        # the account. That is F-7/F-8 (a shared name used as a deletion capability by
        # a deploy that did not create it), not a cosmetic default.
        lambda_function_name = ""
        # Credential resources can be created before the SaaS connector loops
        # (the platform-hosted MCP runtime target below is one example), so the
        # teardown inventory must exist from the first credential operation.
        connector_credential_providers: list[str] = []
        connector_secret_arns: list[str] = []
        connector_spec_s3_uris: list[str] = []

        # Step 3: Create gateway targets based on template.
        #
        # The LEGACY customer-support tools (check_order_status / lookup_customer /
        # search_knowledge_base / get_return_policy) are a distinct contract served
        # by the standalone CustomerSupportTools Lambda. ONLY the
        # customer-support-assistant template uses them. The canonical order tools
        # (get_order / get_customer / list_orders / process_refund) are NOT legacy:
        # they are executed by the DynamicTools Lambda's dispatcher, so the
        # blueprint template and any canvas that wires the canonical tools route to
        # DynamicTools below -- NOT here. Routing them here (the old
        # _has_customer_tools test) shipped the legacy "Unknown tool" handler for
        # get_order and diverged from both the CFN export and the live harness
        # contract. Keeping the assistant as the ONLY reader of the shared
        # CustomerSupportTools function also honours bb F-7/F-8 (one shared
        # stack-region function, never alternate code through it).
        _LEGACY_CUSTOMER_TEMPLATES = {"customer-support-assistant"}
        _LEGACY_TOOL_IDS = {
            "check_order_status",
            "lookup_customer",
            "search_knowledge_base",
            "get_return_policy",
        }
        _has_legacy_tools = bool(set(gateway_tools or []) & _LEGACY_TOOL_IDS)

        if template_id in _LEGACY_CUSTOMER_TEMPLATES or _has_legacy_tools:
            custom_lambda_arn = create_customer_support_lambda(
                region, gateway.get("roleArn", ""), resource_tags=resource_tags
            )
            # The ACTUAL name (stack-scoped, F-7d), read back off the ARN the helper
            # returned, so the manifest row names the function this deploy touched.
            lambda_function_name = custom_lambda_arn.rsplit(":", 1)[-1]
            govern_tool_function_log_group(lambda_function_name, region)
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
                update_existing=True,  # F-74: repoint an existing target to this deploy's function
            )

        elif template_id in ("strands-gateway-agent", "customer-support-blueprint") or gateway_tools:
            # Deploy the DynamicTools Lambda with tool schemas. It executes the web
            # tools AND the canonical order tools, so the blueprint (canonical) and
            # strands (web + canonical) templates both land here.
            dynamic_lambda_arn = create_dynamic_gateway_lambda(
                region, gateway.get("roleArn", ""), resource_tags=resource_tags
            )
            lambda_function_name = dynamic_lambda_arn.rsplit(":", 1)[-1]
            govern_tool_function_log_group(lambda_function_name, region)
            # knowledge_base ("knowledge_base_query") is NOT answered by the
            # DynamicTools dispatcher -- it has its own dedicated KB target created
            # from knowledge_base_result below -- so it never belongs in these
            # inline schemas even if a canvas lists it as a gateway tool. Advertising
            # it here would make the gateway offer a tool that returns "Unknown tool".
            if gateway_tools:
                # A canvas with explicit tools: advertise exactly those it wires,
                # minus the KB tool (routed separately).
                schemas = [
                    GATEWAY_TOOL_SCHEMAS[tid]
                    for tid in gateway_tools
                    if tid in GATEWAY_TOOL_SCHEMAS and tid != "knowledge_base"
                ]
            elif template_id == "customer-support-blueprint":
                # Blueprint with no explicit tool ids: the 4 canonical order tools.
                schemas = [
                    GATEWAY_TOOL_SCHEMAS[tid] for tid in ("get_order", "get_customer", "list_orders", "process_refund")
                ]
            else:
                # strands-gateway-agent with no explicit tools: the 8 executable
                # web + canonical tools (everything the dispatcher can answer),
                # never the 9th knowledge_base entry.
                schemas = [
                    GATEWAY_TOOL_SCHEMAS[tid]
                    for tid in (
                        "duckduckgo_search",
                        "wikipedia_search",
                        "weather_api",
                        "web_page_fetcher",
                        "get_order",
                        "get_customer",
                        "list_orders",
                        "process_refund",
                    )
                ]
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
                # update_existing (F-74): a redeploy onto a gateway that already has this
                # target repoints it to THIS deploy's function ARN and schema and waits for
                # READY, instead of leaving it on the previous function (which, after F-7d,
                # is a different, unscoped name for any gateway that predates the token).
                _create_gateway_target_with_retry(
                    agentcore_ctrl, gateway["gatewayId"], "DynamicTools", create_params, update_existing=True
                )
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
            mcp_full_scope = mcp_oauth["scope"]
            mcp_secret_ref = (
                mcp_oauth.get("client_secret_ref")
                or mcp_oauth.get("clientSecretRef")
                or mcp_oauth.get("client_secret_arn")
                or mcp_oauth.get("clientSecretArn")
            )
            mcp_raw_secret = mcp_oauth.get("client_secret") or mcp_oauth.get("clientSecret")
            if secrets_prebound:
                if mcp_raw_secret:
                    raise RuntimeError("A pre-bound MCP runtime target must not carry a plaintext client secret.")
                if not mcp_secret_ref:
                    raise RuntimeError("MCP runtime OAuth credentials require a deployment-bound client_secret_ref.")
            else:
                mcp_secret_ref, _created = bind_connector_secret_for_deployment(
                    region=region,
                    owner_sub=owner_sub,
                    deployment_id=deployment_id or "",
                    payload_key="clientSecret",
                    raw_value=mcp_raw_secret,
                    secret_ref=mcp_secret_ref,
                    resource_tags=resource_tags,
                )

            if mcp_secret_ref not in connector_secret_arns:
                connector_secret_arns.append(mcp_secret_ref)

            # The provider resolves the client secret from Secrets Manager.
            # Plaintext never enters the AgentCore control-plane request.
            mcp_provider_base_name = f"mcp-cred-{gateway_name}"
            mcp_cred_provider_arn = _ensure_oauth2_credential_provider(
                agentcore_ctrl,
                mcp_provider_base_name,
                vendor="CustomOauth2",
                client_id=mcp_client_id,
                client_secret_arn=mcp_secret_ref,
                discovery_url=mcp_discovery_url,
                scope=gateway["gatewayId"],
                region=region,
                resource_tags=resource_tags,
            )
            connector_credential_providers.append(
                f"OAUTH:{_scoped_provider_name(mcp_provider_base_name, gateway['gatewayId'])}"
            )

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
                update_existing=True,  # F-74: a redeploy repoints the target to this deploy's runtime endpoint
            )
            _annotate_managed_target(target_name="MCPServerRuntime", source_runtime_arn=mcp_server_runtime_arn)
            logger.info("MCP Server Runtime target created, waiting for target to become READY...")

            # MCP targets often fail initially due to IAM propagation delay.
            # Poll status and retry with update if FAILED.
            gw_id = gateway["gatewayId"]
            target_ready = False
            for attempt in range(8):
                time.sleep(15)
                targets_list = _list_all_gateway_targets(agentcore_ctrl, gw_id)
                mcp_target = next(
                    (t for t in targets_list if t.get("name") == "MCPServerRuntime"),
                    None,
                )
                if not mcp_target:
                    logger.info(
                        "MCP target not found yet (attempt %d; %d target(s) listed)",
                        attempt + 1,
                        len(targets_list),
                    )
                    continue
                tid = mcp_target.get("targetId", "")
                _annotate_managed_target(tid, source_runtime_arn=mcp_server_runtime_arn)
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
                final_targets = _list_all_gateway_targets(agentcore_ctrl, gw_id)
                mcp_final = next(
                    (t for t in final_targets if t.get("name") == "MCPServerRuntime"),
                    None,
                )
                if mcp_final and mcp_final.get("status") == "READY":
                    target_ready = True
                    logger.info("MCP target reached READY on final check")
                else:
                    final_status = mcp_final.get("status", "NOT_FOUND") if mcp_final else "NOT_FOUND"
                    raise RuntimeError(
                        "MCP Server Runtime target did not reach READY after retries "
                        f"(final status: {final_status}). Refusing to report a gateway "
                        "whose declared MCP tool plane is unavailable."
                    )

            # F-74: this used to set lambda_function_name = "MCPServerRuntime". No Lambda of
            # that name is ever created -- it is the TARGET's name -- so the assignment wrote a
            # phantom created-Lambda manifest row, and when a built-in tool Lambda had also
            # been created above it OVERWROTE that real name, so teardown never released the
            # real function's invoke grant. The MCP target is a target, not a Lambda; the
            # manifest records it as such elsewhere.
            logger.info("MCP Server Runtime target created for gateway %s", gw_id)

        # Step 4: Deploy custom AI-generated tools as individual Gateway Targets
        custom_tool_lambdas = []
        custom_tool_roles = []
        # Already existed (a redeploy onto this gateway): manifest rows with
        # created_by_deployment False, and never in the abort inventory above.
        custom_tool_lambdas_adopted = []
        custom_tool_roles_adopted = []
        # F-7d (peers 5a/3a): the exact owner+gateway scope each custom tool's function AND
        # role are bound to, keyed by name, so every teardown path can REQUIRE it before a
        # permission, policy or delete mutation. Carried in the success result, the abort
        # inventory and (through the manifest writers) every persisted row.
        custom_tool_bindings: dict[str, str] = {}
        # F-74c: role name -> the function it is the execution role for. Recorded here because
        # only the producer knows it. The binding above is deliberately per owner+gateway and
        # so is IDENTICAL for every tool on one gateway, which means it cannot be used to pair
        # a role with its function: with two custom tools, picking "the other name with this
        # binding" returns the FIRST tool's function for BOTH roles. That value is the
        # shared_lambda_lock key on the role's teardown arm, so the role delete was taking
        # another tool's lambda lock and not serializing against its own.
        custom_tool_pairs: dict[str, str] = {}
        for custom_tool in custom_tools:
            tool_name = custom_tool.get("toolName", custom_tool.get("tool_name", ""))
            lambda_code = custom_tool.get("lambdaCode", custom_tool.get("lambda_code", ""))
            description = custom_tool.get("description", "")
            input_schema = custom_tool.get("inputSchema", custom_tool.get("input_schema", {}))

            if not tool_name or not lambda_code:
                continue

            # Defence in depth, NOT a boundary. This comment used to claim it
            # "prevents arbitrary code execution"; it does not, and saying so
            # made the missing isolation below look like a deliberate choice.
            # A name-based AST check is walkable (see the worked bypasses in
            # tool_tester) and ARCC cnt_MSVB0Kk8WMwmmW says a language
            # restriction "may not be substituted" for isolation. The check is
            # still worth running -- it stops the careless case cheaply, and a
            # tool that trips it is far more likely to be a bad generation than
            # an attack -- but the boundary is the network and IAM scoping.
            #
            # This path matters more than the tool_tester one: there the code
            # lives for one 10s invoke in a function deleted afterwards, whereas
            # here it is deployed permanently and wired to a live gateway target.
            from app.services.tool_tester import _validate_code_safety

            is_safe, safety_error = _validate_code_safety(lambda_code)
            if not is_safe:
                logger.warning("Skipping unsafe custom tool '%s': %s", tool_name, safety_error)
                continue

            # Deployment-scoped names. These were once derived from the tool NAME
            # alone, so two tenants who both called a tool "lookup" got one function:
            # the second deploy took _create_or_update_lambda's already-exists
            # branch and update_function_code REPLACED the first tenant's code,
            # while the resource policy kept the first tenant's gateway
            # authorized to invoke it. Scoping only by owner fixed the cross-tenant
            # case but still made two simultaneously-live deployments by the same
            # owner share and delete one function. That delete is now gated: teardown
            # keeps a row another live deployment's manifest also names. So the scope
            # is the owner's GATEWAY, the same scope as the target (see
            # _custom_tool_resource_names, F-66), and a function or role that already
            # exists is recorded as adopted, never as this deploy's to abort-delete.
            if not owner_sub:
                logger.warning(
                    "No owner_sub for custom tool '%s'; scoping its Lambda name to the stack instead of the tenant.",
                    tool_name,
                )
            # 31 chars keeps both names inside AWS's 64-character limits once the
            # 8-character gateway scope is appended.
            fn_name, role_name_ct, safe_name, scope_binding = _custom_tool_resource_names(
                tool_name,
                owner_sub,
                gateway["gatewayId"],
                region,
            )
            # The exact owner+gateway scope, bound as a tag on BOTH the role and the function
            # and verified before any reuse (F-7d, peer 5a): a name match alone is a collision.
            scope_tags = {"ToolScope": scope_binding}
            custom_tool_bindings[fn_name] = scope_binding
            custom_tool_bindings[role_name_ct] = scope_binding
            custom_tool_pairs[role_name_ct] = fn_name
            try:
                iam_client_ct = _create_iam_client()
                ct_role_outcome: dict[str, bool] = {}
                role_arn_ct = _ensure_lambda_role(
                    iam_client_ct,
                    role_name_ct,
                    f"Role for custom tool {tool_name}",
                    ct_role_outcome,
                    region=region,
                    extra_tags=scope_tags,
                    resource_tags=resource_tags,
                )
                # Record as soon as IAM confirms it. CreateFunction can fail after
                # this line; delaying the append until after Lambda creation left
                # the role absent from both abort inventory and the durable manifest.
                if ct_role_outcome.get("created", True):
                    custom_tool_roles.append(role_name_ct)
                else:
                    custom_tool_roles_adopted.append(role_name_ct)
                zip_bytes_ct = _create_lambda_zip(lambda_code)
                ct_fn_outcome: dict[str, bool] = {}
                custom_lambda_arn = _create_or_update_lambda(
                    _create_lambda_client(region),
                    fn_name,
                    role_arn_ct,
                    zip_bytes_ct,
                    f"AI-generated tool: {tool_name}",
                    gateway.get("roleArn", ""),
                    region=region,
                    outcome=ct_fn_outcome,
                    extra_tags=scope_tags,
                    resource_tags=resource_tags,
                )
                if ct_fn_outcome.get("created", True):
                    custom_tool_lambdas.append(fn_name)
                else:
                    custom_tool_lambdas_adopted.append(fn_name)
                govern_tool_function_log_group(fn_name, region)

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
                _create_gateway_target_with_retry(
                    agentcore_ctrl, gateway["gatewayId"], target_name, ct_params, update_existing=True
                )
                logger.info("Custom tool '%s' deployed as gateway target", tool_name)
            except Exception as ct_err:
                # A declared tool silently disappearing produces a green deployment
                # with a different tool plane from the canvas. Raise into the outer
                # abort handler, which now has every confirmed role/function above.
                logger.error(
                    "Failed to deploy custom tool '%s' (%s)",
                    tool_name,
                    type(ct_err).__name__,
                )
                raise

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
                    resource_tags=resource_tags,
                )
                # The ACTUAL (stack-scoped, F-7d) name off the returned ARN, never re-derived.
                kb_lambda_name = kb_lambda_arn.rsplit(":", 1)[-1]
                govern_tool_function_log_group(kb_lambda_name, region)
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
                    agentcore_ctrl,
                    gateway["gatewayId"],
                    kb_target_name,
                    kb_target_params,
                    update_existing=True,  # F-74: a redeploy repoints the target to this deploy's function
                )
                logger.info("Knowledge Base tool deployed as gateway target: %s", kb_target_name)
            except Exception as kb_err:
                # F-74 (peer 75): this used to log and continue, so a canvas that DECLARED a
                # Knowledge Base tool deployed green with the tool silently absent -- the
                # same failure class F-24 closed for empty targets. Raise into the outer
                # abort handler, which now holds every confirmed role/function above.
                logger.error("Failed to deploy KB tool (%s); aborting the gateway deploy", type(kb_err).__name__)
                raise

        # Step 4c: Deploy SaaS connectors as OpenAPI gateway targets (with their
        # API-key / OAuth2 credential providers). Capture provider + secret refs so
        # teardown can delete them.
        if connectors:
            conn_result = _deploy_connector_targets(
                agentcore_ctrl,
                gateway["gatewayId"],
                region,
                connectors,
                owner_sub=owner_sub,
                deployment_id=deployment_id or "",
                secrets_prebound=secrets_prebound,
                resource_tags=resource_tags,
            )
            connector_credential_providers.extend(conn_result["credential_provider_names"])
            connector_secret_arns.extend(arn for arn in conn_result["secret_arns"] if arn not in connector_secret_arns)
            connector_spec_s3_uris.extend(conn_result.get("spec_s3_uris", []))

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
                deployment_id=deployment_id or "",
                secrets_prebound=secrets_prebound,
                resource_tags=resource_tags,
            )
            mcp_credential_providers = mcp_result["credential_provider_names"]
            mcp_secret_arns = mcp_result["secret_arns"]
            # Fold into the connector teardown refs so DELETE cleans them up.
            connector_credential_providers.extend(mcp_credential_providers)
            connector_secret_arns.extend(arn for arn in mcp_secret_arns if arn not in connector_secret_arns)

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
                deployment_id=deployment_id,
                resource_tags=resource_tags,
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
                for _t in _list_all_gateway_targets(
                    agentcore_ctrl,
                    gateway["gatewayId"],
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
                _existing_targets = _list_all_gateway_targets(
                    agentcore_ctrl,
                    gateway["gatewayId"],
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
                    for _t in _list_all_gateway_targets(
                        agentcore_ctrl,
                        gateway["gatewayId"],
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
                # Raise into the common failure handler instead of returning a
                # connector-only fragment. The common handler carries the gateway,
                # role, Cognito client, providers, secrets, and staged specs to both
                # abort cleanup and the durable manifest. The old early return lost
                # the gateway/role/client handles, so an abort-cleanup failure made
                # those resources permanently undiscoverable.
                raise RuntimeError(
                    f"Gateway {gateway['gatewayId']} serves 0 tools over MCP after "
                    f"deploying connector OpenAPI target(s)." + detail
                )
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
        # Whether the MCP tools/list probe actually CONFIRMED the tool plane, as
        # opposed to being unable to reach it. Travels in the result so a caller
        # never has to infer verification from success alone.
        tool_plane_verified = True
        if expected_tool_count > 0:
            # `probe` records HOW the probe ended, because the count alone cannot
            # tell an empty tool plane apart from a probe that never got a usable
            # answer: _count_served_tools returns -1 for a non-200 or a transport
            # error and _wait_for_gateway_to_serve_tools clamps that to 0. Blaming
            # a service-side provisioning flake for what may be a 403 sent the
            # whole retry loop after the wrong cure (measured live, run df698a37).
            probe: dict = {}
            adopted_plane_converged = False
            served = _wait_for_gateway_to_serve_tools(
                gateway_url, client_info, expected_tool_count, timeout=90, probe=probe
            )
            if served < expected_tool_count:
                # How the tool plane looked, in words the reader can act on.
                _diag = (
                    f"served {served}/{expected_tool_count} tools"
                    if probe.get("got_valid_response")
                    else (
                        f"never answered tools/list ({probe.get('last_status') or 'no response'}) — "
                        "this is an AUTH/REACHABILITY failure, not an empty tool plane"
                    )
                )
                if not probe.get("got_valid_response"):
                    # DO NOT tear the gateway down. The retry's premise is that a
                    # FRESH gateway provisions a working tool plane, and that
                    # premise is not just unsupported here, it is actively
                    # counterproductive: recreating the gateway also recreates the
                    # Cognito user pool AND its hosted domain (_create_cognito_oauth),
                    # and a brand-new Cognito domain is the reason the probe could
                    # not get a token in the first place.
                    #
                    # Measured (us-east-1, throwaway pool): create_user_pool_domain
                    # returns immediately and describe_user_pool_domain reports
                    # Status=ACTIVE within 4 SECONDS, but the DNS name
                    # <domain>.auth.<region>.amazoncognito.com still did not resolve
                    # at t+128s -- the domain provisions a CloudFront distribution and
                    # ACTIVE is not a readiness oracle for it. The tools/list probe
                    # starts ~20s after the domain is created and runs for 90s, so on
                    # a fresh gateway the token endpoint is unreachable for the WHOLE
                    # probe window, every poll raises, _count_served_tools returns -1
                    # and this function clamps it to 0.
                    #
                    # Live confirmation of the doom loop (deploy 3ef480e2, run
                    # df698a37): each of the three attempts created a NEW pool
                    # (v8OiJanup -> QU487tO1L -> yvymML5Pm) with a NEW domain, so each
                    # retry restarted the provisioning clock and was strictly LESS
                    # likely to succeed than the one before. CloudTrail also shows
                    # CreateGateway returning ConflictException on attempts 2 and 3.
                    #
                    # The gateway itself does not depend on the domain -- its
                    # customJWTAuthorizer uses the cognito-idp discoveryUrl -- and the
                    # deployed agent mints its token minutes-to-hours later, when the
                    # domain is warm. So an unreachable token endpoint at deploy time
                    # says nothing about the gateway's health, and destroying it over
                    # that reading is how a healthy gateway came to be deleted and
                    # replaced twice for nothing (368s of billed Lambda).
                    #
                    # Fall through to success with the tool plane marked UNVERIFIED.
                    # The control plane is the evidence that survives a cold domain:
                    # the sync check above already confirmed every configured tool is
                    # synced onto a READY target. Refusing to ship on this signal
                    # would fail a correct deploy; claiming the probe passed would be
                    # a lie. So the result says which one happened.
                    tool_plane_verified = False
                    logger.warning(
                        "Gateway tool plane UNVERIFIED (%d/%d over MCP): %s. NOT recreating the "
                        "gateway -- a fresh gateway would also get a fresh Cognito domain, which "
                        "is the likeliest cause. Proceeding on the control-plane sync signal; see "
                        "the tools/list probe warning above for the failing exception type.",
                        served,
                        expected_tool_count,
                        _diag,
                    )
                elif gateway_retry < 2:
                    # A same-named gateway can be ownership-proven and adopted, but
                    # it still pre-dated THIS deployment. Recreating the tool plane
                    # by deleting every target and then the gateway is valid only
                    # for a gateway this invocation created. Otherwise a readiness
                    # failure becomes destruction of a live pre-existing gateway.
                    if gateway_created is not True:
                        # Redeploy audit 2026-09-28 row 4: on a REDEPLOY the gateway is
                        # adopted, so the recreate remedy below is unavailable and the
                        # first short plane meant an immediate failure, while a created
                        # gateway got two more 90 s probes via recreation. A just-updated
                        # target can still be converging (UPDATING -> READY, provider
                        # repoint, fresh Cognito domain), so give the adopted gateway the
                        # same probe budget in place before refusing.
                        served = _reprobe_adopted_gateway_tool_plane(
                            gateway,
                            gateway_url,
                            client_info,
                            expected_tool_count,
                            probe,
                            _diag,
                        )
                        if served < expected_tool_count:
                            raise RuntimeError(
                                f"Gateway {gateway['gatewayId']} {_diag} after "
                                f"{_ADOPTED_REPROBE_ATTEMPTS + 1} probe rounds, but it "
                                "pre-dated this deployment. Refusing to delete an "
                                "adopted gateway to retry with a fresh one."
                            )
                        adopted_plane_converged = True
                    if not adopted_plane_converged:
                        logger.warning(
                            "Gateway %s %s. Tearing it down and recreating (attempt %d/3).",
                            gateway["gatewayId"],
                            _diag,
                            gateway_retry + 2,
                        )
                        # The retry's whole premise is that a FRESH gateway provisions a
                        # working tool plane. If the delete does not actually happen,
                        # deploy_gateway re-adopts this same gateway by name and re-syncs
                        # the same targets, so every further attempt is bit-identical --
                        # and the failure still gets reported as a service flake after
                        # "3 gateway recreations" that never occurred.
                        #
                        # Measured live (deploy 3ef480e2, run df698a37): DeleteGateway
                        # 403'd because the step role lacked the action, this except
                        # swallowed it as "(non-fatal)", and the deploy burned 368s over
                        # three identical attempts on the same gateway id. So a failed
                        # delete now STOPS the loop and reports the delete error, which
                        # is the real, actionable cause.
                        _del_err: Exception | None = None
                        _gid = gateway["gatewayId"]
                        try:
                            # F-66e: under the gateway's write lock, through a proof of
                            # absence. Without the proof, a delete still in progress is
                            # re-adopted by name on the retry below.
                            with gateway_mutation_lock(agentcore_ctrl, region, _gid) as _gw_lock:
                                for _t in _list_all_gateway_targets(agentcore_ctrl, _gid):
                                    _tid = _t.get("targetId") or _t.get("gatewayTargetId")
                                    if _tid:
                                        agentcore_ctrl.delete_gateway_target(gatewayIdentifier=_gid, targetId=_tid)
                                time.sleep(5)
                                _gw_lock.delete(
                                    lambda: wait_until_absent(
                                        resource_label=f"gateway {_gid}",
                                        read=lambda: agentcore_ctrl.get_gateway(gatewayIdentifier=_gid),
                                        max_attempts=20,
                                        delay_seconds=1.5,
                                    ),
                                    terminal=(DeletionFailedAfterAccept,),
                                )
                        except Exception as del_err:  # noqa: BLE001
                            _del_err = del_err
                            # F-30: a botocore message echoes request parameters (ARCC cnt_rHmO501l15qr2W).
                            logger.warning("Gateway delete before retry FAILED: %s", redact_secrets(str(del_err))[:200])
                        if _del_err is not None:
                            raise RuntimeError(
                                f"Gateway {gateway['gatewayId']} {_diag}, and it could not be deleted to "
                                f"retry with a fresh one, so retrying would re-use the same gateway and "
                                f"fail identically. Fix the delete failure first: {redact_secrets(str(_del_err))[:300]}"
                            )
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
                            # These three were omitted, so a retry silently rebuilt the
                            # gateway WITHOUT the deploy's SaaS connectors or external
                            # MCP servers and with no owner scoping -- a different,
                            # feature-stripped gateway than the one the user asked for,
                            # and if it happened to come up healthy it would have
                            # shipped that way.
                            connectors=connectors,
                            external_mcp_servers=external_mcp_servers,
                            owner_sub=owner_sub,
                            gateway_consumers=gateway_consumers,
                            secrets_prebound=secrets_prebound,
                            # The same deployment re-takes its own lease: idempotent.
                            claim_gateway_name=claim_gateway_name,
                            resource_tags=resource_tags,
                        )
                else:
                    # Exhausted retries — fail closed rather than ship a broken
                    # gateway. This is an `else` of the retry branch, NOT a
                    # fall-through: the unverified-probe branch above deliberately
                    # continues to success, and letting it reach this raise would
                    # turn the false failure back on.
                    #
                    # RAISE, do not return: the `except` handler below is what builds
                    # the teardown inventory (gateway id, Cognito pool, tool Lambdas,
                    # roles, connector providers/secrets) and runs the abort cleanup.
                    # Returning here skipped all of it and handed gateway_step a dict
                    # with no gateway_id, so _record_gateway_resources wrote NO
                    # manifest rows and every resource this deploy created was orphaned
                    # with nothing left naming it. Verified live: gateway
                    # agent-gateway-cs0p5dvpgi stayed up while its deployment row had
                    # no created_resources key at all.
                    raise RuntimeError(
                        f"Gateway {gateway['gatewayId']} {_diag} after {gateway_retry + 1} gateway "
                        f"creation attempt(s). Refusing to ship a runtime against it."
                    )
            # Servable plane confirmed — qualified_tools should reflect it.
            logger.warning("Gateway %s confirmed serving %d tools over MCP", gateway["gatewayId"], served)

        result = {
            "success": True,
            "gateway_url": gateway_url,
            "gateway_id": gateway["gatewayId"],
            "gateway_arn": gateway_arn,
            "gateway_name": gateway_name,
            "gateway_created_by_deployment": gateway_created,
            # The role this deploy actually provisioned, not one derived from the
            # gateway name. See the note at its assignment in Step 1b.
            "gateway_role_name": gw_role_confirmed,
            "gateway_role_created_by_deployment": gw_role_created,
            "client_info": client_info,
            "lambda_function_name": lambda_function_name,
            "custom_tool_lambdas": custom_tool_lambdas,
            "custom_tool_roles": custom_tool_roles,
            "custom_tool_lambdas_adopted": custom_tool_lambdas_adopted,
            "custom_tool_roles_adopted": custom_tool_roles_adopted,
            "custom_tool_bindings": custom_tool_bindings,
            "custom_tool_pairs": custom_tool_pairs,
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
            # False when the tools/list probe could not reach the gateway at all
            # (cold Cognito domain being the known cause) and the deploy proceeded
            # on the control-plane sync signal instead. Success does not imply the
            # MCP plane was exercised, and a caller that reports readiness must say
            # which signal it had.
            "tool_plane_verified": tool_plane_verified,
            # F-74b: one record per gateway target this deploy created, reused or updated
            # ({target_id, name, family, digest}). The step handler writes a manifest row
            # from each; those rows are what lets the NEXT deploy tell a target of ours
            # from one it must not touch, and lets teardown remove only the last reference.
            "gateway_targets": list(_target_records),
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
        # The inventory of everything provisioned before the failure. Returned to
        # the caller (below) as well as handed to the abort cleanup, because the
        # abort is best-effort and can leave resources behind — see the two
        # failure modes documented at the `cleanup_gateway_resources` call.
        #
        # client_info is REDUCED to the one field teardown needs. The full dict
        # nests client_secret, and unlike `result` this dict travels into a
        # RuntimeError message and an SFN failure cause, so the secret must not
        # be in it (CodeQL py/clear-text-logging-sensitive-data).
        #
        # Read `cognito_response` FIRST, not just `client_info`. The pool is created
        # near the top of this function but `client_info` is not bound until the very
        # end (after the tool plane is confirmed), so for a failure anywhere in
        # between — which is most of the deploy, including all target creation — a
        # `client_info`-only lookup finds nothing and the pool is invisible to both
        # the abort cleanup below and the manifest. That is why every stranded
        # gateway found in the live account had a stranded Cognito pool beside it.
        #
        # And when the Cognito helper ITSELF raised, there is no `cognito_response`
        # either: the helper rolled back what it could and attached what it could NOT
        # delete to the exception. Without reading it, a rollback whose delete failed
        # left a live app client (secret still mintable) that no row named.
        _leftover = getattr(e, COGNITO_LEFTOVER_ATTR, None) or {}
        _ci = (
            locals().get("client_info")
            or (locals().get("cognito_response") or {}).get("client_info")
            or _leftover.get("client_info")
            or {}
        )
        _partial = {
            "gateway_id": _leaked_gw_id,
            "gateway_name": locals().get("gateway_name"),
            "gateway_created_by_deployment": bool(locals().get("gateway_created")),
            # None for any failure that happened BEFORE Step 1b, which is what stops
            # the manifest inventing a role. It is deliberately NOT gated on
            # gateway_id: the role is created at Step 1b and the gateway only at
            # Step 2, so a gateway-creation failure leaves a real role that must
            # still be recorded and torn down.
            "gateway_role_name": locals().get("gw_role_confirmed"),
            "gateway_role_created_by_deployment": bool(locals().get("gw_role_created")),
            # False unless this deploy's CreateResourceServer succeeded. The resource
            # server is keyed on the gateway name, so a deploy refused adoption of a
            # same-name gateway reused that gateway's (F-63).
            "resource_server_created_by_deployment": bool(
                (locals().get("cognito_response") or {}).get("resource_server_created")
                or _leftover.get("resource_server_created")
            ),
            # ALLOW-LIST, not a two-field summary. Reducing this to
            # {provider, user_pool_id} kept the secret out, but it also withheld the
            # three fields teardown needs, and the omissions were measured live on
            # 2026-09-21 against a failed deploy of `gwfixaafac57e`:
            #
            #  * `shared_pool` absent  -> _record_gateway_resources took its "a pool
            #    THIS deployment created" branch and wrote a deletable
            #    `cognito_user_pool` row for us-east-1_qiYLOs3Ij — the PLATFORM's
            #    shared gateway-auth pool, RETAINed by the CDK stack, holding every
            #    other gateway's app client. Two later guards refused the delete
            #    (is_platform_owned_user_pool, then classify_user_pool), so nothing
            #    was destroyed, but the manifest asserted the opposite of the truth
            #    and only defence-in-depth stood between it and every agent's auth.
            #  * `client_id` and `scope` absent -> neither the abort cleanup below nor
            #    the manifest could name this gateway's app client or resource server,
            #    so a failed deploy left `gwfixaafac57e-client` (secret still mintable)
            #    and `agentcore-gwfixaafac57e` behind in the shared pool forever. F-23
            #    closed this for a SUCCESSFUL deploy; the failure path stayed open.
            #  * `client_secret_ref` absent -> the per-deployment Secrets Manager copy
            #    of the client secret (minted inside the Cognito setup, well before
            #    any of the failure points) got no `secret` row and was orphaned.
            #
            # The secret itself still cannot be here: this dict travels into a
            # RuntimeError message and an SFN failure cause (CodeQL
            # py/clear-text-logging-sensitive-data). `client_secret_ref` is an ARN —
            # a NAME for the credential, not the credential — which is exactly the
            # "share only the reference" shape ARCC cnt_77BHvX7WzuG1X8 asks for, and
            # is already carried on the success path's surfaces for that reason.
            #
            # An allow-list rather than a copy-and-pop so that a future field added to
            # client_info is NOT published here by default; it has to be named.
            "client_info": {
                "provider": _ci.get("provider", "cognito"),
                **{
                    k: _ci[k]
                    for k in (
                        "user_pool_id",
                        # The shared pool lives in the platform's home region, which a
                        # regional deployment does not share.
                        "user_pool_region",
                        "client_id",
                        "scope",
                        "client_secret_ref",
                        # Without this the failure path loses the secret ROW, which is
                        # the measured defect listed third above — the manifest writer
                        # now takes the row from the minted key, not from
                        # client_secret_ref, so the allow-list has to carry it.
                        "minted_client_secret_ref",
                        "shared_pool",
                    )
                    if _ci.get(k)
                },
            }
            # A secret handle alone is inventory too: a rollback that deleted the
            # dedicated pool can still have failed to delete the secret.
            if _ci.get("user_pool_id") or _ci.get("minted_client_secret_ref")
            else None,
            # No "or <hard-coded name>" default: see the placeholder note at the
            # `lambda_function_name = ""` assignment above. Falsy means this deploy
            # created no tool Lambda, and both the abort cleanup and the manifest
            # writers must then delete nothing.
            "lambda_function_name": locals().get("lambda_function_name") or "",
            # Manifest rows only: cleanup_gateway_resources never reads it, and the
            # deployment delete finds the KB function by its deterministic name. Without
            # it a KB function created before a later failure in this deploy was in no row.
            "kb_lambda_name": locals().get("kb_lambda_name") or "",
            "custom_tool_lambdas": locals().get("custom_tool_lambdas") or [],
            "custom_tool_roles": locals().get("custom_tool_roles") or [],
            # Not abort inventory (cleanup_gateway_resources never reads these): another
            # live deployment on this gateway may be invoking them. Manifest rows only.
            "custom_tool_lambdas_adopted": locals().get("custom_tool_lambdas_adopted") or [],
            "custom_tool_roles_adopted": locals().get("custom_tool_roles_adopted") or [],
            "custom_tool_bindings": locals().get("custom_tool_bindings") or {},
            "custom_tool_pairs": locals().get("custom_tool_pairs") or {},
            "connector_credential_providers": locals().get("connector_credential_providers") or [],
            "connector_secret_arns": locals().get("connector_secret_arns") or [],
            "connector_spec_s3_uris": locals().get("connector_spec_s3_uris") or [],
        }
        # A gateway id is not the first resource created. Cognito credentials and the
        # gateway role exist before CreateGateway, so gating the fast-path cleanup on
        # `_leaked_gw_id` strands exactly those resources when CreateGateway fails.
        # Use only confirmed inventory fields here: gateway_name is deliberately NOT
        # one of them because a requested name is not proof that a role exists.
        _has_abort_inventory = any(
            (
                _partial.get("gateway_id"),
                _partial.get("gateway_role_name"),
                _partial.get("client_info"),
                _partial.get("lambda_function_name"),
                _partial.get("custom_tool_lambdas"),
                _partial.get("custom_tool_roles"),
                _partial.get("connector_credential_providers"),
                _partial.get("connector_secret_arns"),
                _partial.get("connector_spec_s3_uris"),
            )
        )
        if _has_abort_inventory:
            try:
                # cleanup_gateway_resources NEVER raises: it collects per-resource
                # failures into its returned list. Discarding that list (which this
                # did) meant a failed gateway delete was invisible AND the caller
                # logged unconditional success — verified live, where a deploy that
                # failed at CreateApiKeyCredentialProvider left an orphan gateway
                # and Cognito pool behind with no log line and no manifest row.
                # Inspect what it reports and say so out loud.
                _msgs = (
                    cleanup_gateway_resources(
                        "gateway-deploy-abort",
                        region,
                        _partial,
                        deployment_id=deployment_id or "",
                    )
                    or []
                )
                _bad = [m for m in _msgs if "error" in str(m).lower()]
                if _bad:
                    # Count + resource kinds, not the message bodies — see
                    # classify_cleanup_failures for why those cannot be logged.
                    # This still answers the question that matters here ("did the
                    # abort leave something behind, and of what kind"); the
                    # manifest rows returned below are what actually recovers it.
                    logger.warning(
                        "Abort-cleanup left resources behind (%d failure(s)): %s",
                        len(_bad),
                        ", ".join(classify_cleanup_failures(_bad)),
                    )
                else:
                    logger.info("Released partial gateway resources after failed deploy")
            except Exception as _ce:  # noqa: BLE001 — abort cleanup is best-effort
                logger.warning("Abort-cleanup after failed gateway deploy failed: %s", str(_ce)[:160])
        # Return the inventory so the step handler can write manifest rows for it.
        # The abort above is the fast path; the manifest is the durable one — with
        # rows present, the normal teardown finishes the job on a later delete,
        # which is the only thing that turns a silent permanent orphan into a
        # recoverable one.
        # The notes are appended because str(e) drops them, and they are the only
        # place a partial Cognito rollback is reported. They hold error type names
        # only (see _raise_after_compensation), never a request parameter.
        _notes = [str(n) for n in getattr(e, "__notes__", None) or []]
        _error = "; ".join([str(e), *_notes])
        return {
            "success": False,
            "error": _error,
            **_partial,
            "refused_before_side_effects": not _side_effects_started,
            # F-74b: after ``**_partial`` deliberately, so this is not something the abort
            # cleanup above can consume -- targets are deleted with their gateway, and a
            # record of one is for the manifest, not for a second delete attempt. It is on
            # the failure return for the same reason the rest of the inventory is: a target
            # that exists and is in no row is a permanent orphan.
            "gateway_targets": list(_target_records),
        }

    finally:
        # Both returns above copy the list first, so resetting here cannot empty what the
        # caller was handed.
        _TARGET_RECORD_SINK.reset(_target_sink_token)


# ---------------------------------------------------------------------------
# Gateway cleanup
# ---------------------------------------------------------------------------

# Distinctive fixed substring -> resource kind, for reporting WHICH kinds of
# cleanup failed without echoing the message bodies. Ordered most-specific first,
# because "Custom tool Lambda delete error" also contains "Lambda delete error"
# and "Gateway IAM role cleanup error" also contains "IAM role cleanup error";
# first match wins.
#
# Why classify at all instead of logging the messages: cleanup_gateway_resources
# builds them from `client_info` — which also carries `client_secret` — and from
# raw exception text, and a botocore ParamValidationError echoes the failing
# call's parameters. Neither belongs in a log line. CodeQL flags the same thing
# as py/clear-text-logging-sensitive-data.
_CLEANUP_FAILURE_KINDS: tuple[tuple[str, str], ...] = (
    ("Connector spec object delete error", "connector-spec-object"),
    ("Custom tool Lambda delete error", "custom-tool-lambda"),
    # Before the function's own arm: the role's message is not a superstring of it
    # today ("Lambda role delete" vs "Lambda delete"), but the ordering convention is
    # most-specific-first and relying on that near-miss would be a trap for whoever
    # rewords either line next. The role is a distinct kind, not a detail of the
    # function's failure — the function can delete cleanly and leave its role behind,
    # and an operator told only "shared-tool-lambda" would have no reason to look.
    ("Shared tool Lambda role delete error", "shared-tool-lambda-role"),
    ("Shared tool Lambda delete error", "shared-tool-lambda"),
    ("Gateway IAM role cleanup error", "gateway-iam-role"),
    ("IAM role cleanup error", "iam-role"),
    ("Target cleanup error", "gateway-target"),
    ("Gateway delete error", "gateway"),
    # Shared-pool teardown deletes only this gateway's own app client and resource
    # server inside the platform-owned pool, so its failures are NOT "cognito-pool"
    # — that kind would tell an operator the whole pool failed to delete, when the
    # pool is RETAINed by design and was never going to be deleted. Both must be
    # listed even though the exhaustiveness check only extracts the one written on
    # a single source line: a hard-wrapped f-string is invisible to that regex, so
    # it cannot be relied on to notice the second one.
    ("Shared-pool app client delete error", "shared-pool-app-client"),
    ("Shared-pool resource server delete error", "shared-pool-resource-server"),
    ("Cognito cleanup error", "cognito-pool"),
    ("Lambda delete error", "tool-lambda"),
    ("Connector secret delete error", "connector-secret"),
    ("API_KEY provider", "credential-provider"),
    ("OAUTH provider", "credential-provider"),
)


def classify_cleanup_failures(messages) -> list[str]:
    """Resource kinds named by *messages*, as constants rather than excerpts.

    Returning entries from ``_CLEANUP_FAILURE_KINDS`` — never a slice of the
    input — is the whole point: a substring of a string carrying a secret still
    carries it, so mapping to a fixed vocabulary is what actually keeps the
    message bodies out of the log.

    An unrecognized failure yields ``"unclassified"`` rather than being dropped,
    so a message this vocabulary has not caught up with still gets counted and
    still gets seen. ``test_gateway_cleanup_failure_labels.py`` asserts the
    vocabulary stays exhaustive against the source, so that fallback should never
    fire in practice.
    """
    kinds: set[str] = set()
    for message in messages:
        text = str(message)
        for needle, kind in _CLEANUP_FAILURE_KINDS:
            if needle in text:
                kinds.add(kind)
                break
        else:
            kinds.add("unclassified")
    return sorted(kinds)


# Tool Lambdas created once per STACK and reused by every gateway deploy of that stack.
# They must never be unconditionally deleted on a per-gateway teardown (Defect C) — doing
# so bricks every other live gateway still wired to them. They are released by reference
# count instead (see _release_shared_tool_lambda).
#
# F-7d: the names are scoped to the owning stack and region through
# naming.scoped_function_name (``AgentCore-<token>-DynamicTools``), not account-global
# literals. Two installs in one account, or two regions of one install, therefore never
# share a function name, and a name collision is by construction either this stack's own
# function or somebody else's — which is what lets the create path REFUSE a collision it
# cannot prove ownership of instead of adopting it.
_SHARED_TOOL_KINDS = ("DynamicTools", "CustomerSupportTools")

# The unscoped names pre-F-7d builds created, and their home-region execution roles.
# Recognized ONLY so a manifest row written by such a build is still released through the
# ownership-gated refcount path on teardown. Nothing creates, adopts or updates them.
_LEGACY_SHARED_TOOL_LAMBDAS = {
    "AgentCoreDynamicTools": ("DynamicTools", "AgentCoreDynamicToolsLambdaRole"),
    "AgentCoreCustomerSupportTools": ("CustomerSupportTools", "AgentCoreCustomerSupportLambdaRole"),
}


def shared_tool_function_name(kind: str, region: str | None = None) -> str:
    """This stack's name for the shared *kind* tool Lambda in *region*."""
    if kind not in _SHARED_TOOL_KINDS:
        raise ValueError(f"unknown shared tool Lambda kind {kind!r}")
    return scoped_function_name(kind, stack_id(region))


def shared_tool_role_name(kind: str, region: str | None = None) -> str:
    """The execution role of :func:`shared_tool_function_name`. Stack-scoped like the
    function, so it can never be the stale unowned singleton another install left behind.

    It cannot be a manifest ``iam_role`` row: the row would order an unconditional delete on
    the FIRST gateway teardown, which pulls the execution role out from under every other
    live gateway of this stack still invoking the function. It dies with the function
    instead, on the refcount-zero branch below.
    """
    if kind not in _SHARED_TOOL_KINDS:
        raise ValueError(f"unknown shared tool Lambda kind {kind!r}")
    return scoped_role_name(kind, stack_id(region))


_SCOPED_SHARED_TOOL_NAME = re.compile(
    r"^AgentCore-(?P<token>[0-9a-f]{10})-(?P<kind>DynamicTools|CustomerSupportTools)$"
)


def is_shared_tool_function(function_name: str, region: str | None = None) -> bool:
    """True for a scoped shared tool Lambda (any stack's) and for the legacy unscoped names.

    Structural, not recomputed from this process's stack identity: a manifest row is read
    by teardown paths whose region argument may be absent or be the HOME region of a
    cross-region deploy, and misclassifying a shared function as per-deployment would send
    it to the hard-delete branch while other gateways still invoke it. The shape
    ``AgentCore-<10 hex>-DynamicTools`` is unambiguous on its own. *region* is accepted for
    call-site symmetry with the other classifiers and is not needed.
    """
    if not function_name:
        return False
    if function_name in _LEGACY_SHARED_TOOL_LAMBDAS:
        return True
    return _SCOPED_SHARED_TOOL_NAME.match(function_name) is not None


def _shared_tool_lambda_role_name(
    function_name: str,
    region: str | None = None,
) -> str:
    """Return the exact account-global role name for a shared Lambda, or "" if not shared.

    For a scoped function the role is derived from the TOKEN IN THE NAME, so the pair stays
    consistent even when the caller's region argument is not the function's region.
    """
    legacy = _LEGACY_SHARED_TOOL_LAMBDAS.get(function_name)
    if legacy:
        return regional_iam_role_name(legacy[1], region)
    match = _SCOPED_SHARED_TOOL_NAME.match(function_name or "")
    if match:
        return f"AgentCore-{match.group('token')}-{match.group('kind')}Role"
    return ""


def _release_shared_tool_lambda_role(function_name: str, region: str | None = None) -> str:
    """Delete the execution role of a shared tool Lambda that was just deleted.

    Gated on the ownership tag, and that gate is not paperwork. These role names are
    account-global singletons; the live test account contains an
    ``AgentCoreDynamicToolsLambdaRole`` created months earlier by something unrelated to
    this platform. Deleting a role by name alone would break whatever else assumed it.
    An untagged role is therefore left alone and said out loud — ``is_owned_by_this_stack``
    returns False for untagged deliberately.

    The ``kept`` line distinguishes UNTAGGED from OWNED-BY-SOMEONE-ELSE, and that
    distinction is the whole reason this function has three branches instead of two.
    ``Tags=owner_tag_list()`` is applied in ``_ensure_lambda_role``'s ``create_role`` call
    only; the ``EntityAlreadyExistsException`` branch reuses the role and does NOT tag it.
    So in any account that ever ran the pre-tag code, the singleton already exists
    untagged, ``create_role`` never runs again, and this release can never fire —
    measured live: ``AgentCoreDynamicToolsLambdaRole`` (2026-07-19) and
    ``AgentCoreCustomerSupportLambdaRole`` (2026-09-20) both have ``Tags: null``. A single
    "kept (not owned by this stack)" line could not tell that apart from the gate doing
    its job on a genuinely foreign role, which is exactly the misreading to prevent.

    Tagging on the reuse branch is the obvious fix and it is the wrong one: it would claim
    the foreign ``AgentCoreDynamicToolsLambdaRole`` that a live, unrelated Lambda is using
    right now, which is the case this gate exists for. Adopting an untagged singleton is an
    ownership claim an operator has to make knowingly (``aws iam tag-role``, or delete the
    role once nothing uses it and let the next deploy recreate it tagged), not something
    this code can infer from a name.

    Best-effort: returns a log line, never raises. The function is already gone at this
    point, so a surviving role is a tidy-up issue, not a broken teardown.
    """
    role_name = _shared_tool_lambda_role_name(function_name, region)
    if not role_name:
        return ""
    try:
        iam_client = _create_iam_client()
        # get_role returns Tags, so no ListRoleTags grant is needed.
        role = iam_client.get_role(RoleName=role_name)["Role"]
    except Exception as e:  # noqa: BLE001
        if is_error(e, "NoSuchEntity", "NoSuchEntityException"):
            return f"Shared tool Lambda role {role_name} already absent"
        return f"Shared tool Lambda role {role_name} kept (unreadable: {type(e).__name__})"

    if not is_owned_by_this_stack(role.get("Tags"), region):
        owner = {t.get("Key"): t.get("Value") for t in (role.get("Tags") or []) if isinstance(t, dict)}.get(
            OWNER_TAG_KEY
        )
        if not owner:
            # No owner tag at all. This release can never fire for this role, so say that
            # rather than something that reads like the gate working, and give the remedy.
            return (
                f"Shared tool Lambda role {role_name} kept (NO OWNER TAG — ownership unprovable, so it "
                "is never auto-deleted; it predates ownership tagging or belongs to something else. "
                "To bring it in scope, delete it by hand once no function uses it and the next deploy "
                "recreates it tagged)"
            )
        # An owner tag naming a different stack: the gate working exactly as intended.
        return f"Shared tool Lambda role {role_name} kept (owned by another stack)"

    try:
        # Re-read ownership immediately before mutation and paginate both policy
        # families through the shared hardened helper.
        delete_owned_iam_role(iam_client, role_name, region)
        return f"Shared tool Lambda role {role_name} deleted"
    except Exception as e:  # noqa: BLE001
        if is_error(e, "NoSuchEntity", "NoSuchEntityException"):
            return f"Shared tool Lambda role {role_name} already absent"
        return f"Shared tool Lambda role delete error: {e}"


def _authorize_tool_function_deletion(
    lambda_client, function_name: str, region: str | None = None, *, required_tags: dict[str, str] | None = None
) -> str:
    """Decide whether teardown may DELETE *function_name* (F-7c). ``""`` means go ahead.

    A non-empty return is the reason to keep it, already formatted as a log line suffix.

    This is the mirror of :func:`_authorize_tool_function_replacement`, and it deliberately
    answers the untagged case the OPPOSITE way. That asymmetry is the whole point:

    * On the WRITE path an untagged function is now **refused** too (F-7d; it used to be
      adopted and backfilled, until a foreign function+role pair was shown to pass).
    * On the DELETE path an untagged function is **kept**, because a delete is not
      recoverable and the remedy for a wrong one does not exist. Leaking a 128 MB function
      an operator can remove by hand is strictly better than removing someone else's.
      Both paths now agree that an unprovable name is nobody's to touch; they differ only
      in what "touch" costs when the guess is wrong.

    Why this is needed at all. Teardown gated ``delete_function`` on the reference count of
    ``AllowAgentCoreInvoke-*`` statements in the function's own resource policy, and on
    nothing else. Those statements are *ours*: a foreign function has none, so after removing
    the one we added the count is zero and the function is deleted. The refcount can only
    establish that nothing *of ours* still needs the function, which is not the same as the
    function being ours to delete. Our own manifest row plus our own refcount were being
    treated as a deletion capability over a function we had merely found by name — F-8's
    shape applied to F-7's residual. Combined with the adoption residual above, the reachable
    chain was: a foreign untagged ``AgentCoreDynamicTools`` is adopted, its code replaced,
    our invoke grant added, and then deleted when our last gateway goes away.

    Be precise about what was and was not observed, because the first version of this
    docstring was not. **No foreign function was ever deleted in this account.** A fully
    paginated CloudTrail sweep of the whole 90-day window (392 ``CreateFunction`` +
    ``DeleteFunction`` events) shows every delete of either shared name preceded by a create
    from this platform; the function ``AgentCoreDynamicTools`` was created 2026-09-20 12:39
    by our own ``acfe2e-p0920-step-gateway``. The genuinely foreign untagged artifact from
    2026-07-19 is the *role* ``AgentCoreDynamicToolsLambdaRole``, which the sibling role gate
    already kept. So this is a code property provable by reading, plus a demonstrated hazard:
    that foreign role is direct evidence another tool in this account uses this exact name
    family, and nothing stood between it and the chain above. A ``--max-results`` CloudTrail
    query returns newest-first and would have hidden exactly the older events that settled
    this — the sweep has to paginate.

    What *was* measured live, 2026-09-21, is this gate. Three functions differing only in
    their tags, plus one named ``AgentCoreDynamicTools`` with no tags, driven through the
    deployed delete path: one tag read per function from the deployment role, exactly one
    delete (the one tagged ours), and the untagged shared singleton kept with its reason. The
    AccessDenied branch was proved under a role deliberately minted without ``lambda:ListTags``.

    The sibling ``_release_shared_tool_lambda_role`` was already tag-gated and says so out
    loud when it declines; only the function was not. ``_authorize_tool_function_replacement``'s
    own docstring already asserted that without the backfill "teardown will not delete it",
    so the intent was there and the code did not implement it. This makes that claim true.

    An AccessDenied on the tag read keeps the function, for the same reason the read is a
    separate ``ListTags`` call in the first place: the alternative is that a missing grant
    makes every function look unowned, and here "unowned" would mean *deletable*.
    """
    try:
        fn = lambda_client.get_function(FunctionName=function_name)
        function_arn = fn["Configuration"]["FunctionArn"]
        tags = lambda_client.list_tags(Resource=function_arn).get("Tags") or {}
    except Exception as exc:  # noqa: BLE001
        if is_error(exc, "ResourceNotFoundException", "ResourceNotFound"):
            return ""  # already gone; the caller's own delete will say so
        return (
            f"kept (ownership unreadable: {type(exc).__name__} — refusing to delete a function "
            f"this deployment cannot prove it owns. Grant lambda:ListTags and lambda:GetFunction "
            f"on function:AgentCore* to the deploy role)"
        )

    # Runtime teardown must not consume CloudFormation's Project/Environment
    # tags as deletion authority.  Those tags prove that this installation may
    # UPDATE a CDK-owned singleton, but CloudFormation still owns its lifecycle.
    # Only the exact dynamic-resource owner tag authorizes delete_function -- plus, for a
    # per-deployment or per-scope function, the exact binding (DeploymentId / ToolScope):
    # a same-stack name collision is not a function of ours to delete either (peer 3c).
    if is_owned_by_this_stack(tags, region):
        bound = tag_map(tags)
        mismatch = {k: bound.get(k) for k, v in (required_tags or {}).items() if bound.get(k) != v}
        if not mismatch:
            return ""
        return (
            "kept (this stack's function, but bound to "
            + ", ".join(f"{k}={v or '<untagged>'}" for k, v in sorted(mismatch.items()))
            + " -- not this deployment's; a name collision is never deletion authority)"
        )

    mapped = tag_map(tags)
    owner = mapped.get(OWNER_TAG_KEY)
    if owner:
        try:
            expected = stack_id(region)
        except Exception:  # noqa: BLE001 — absence of identity must stay fail-closed
            expected = "<unconfigured>"
        return (
            f"kept (belongs to {owner}, not to {expected} — Lambda function names are "
            f"account-global, so this deployment will not delete it)"
        )
    if mapped.get(CDK_PROJECT_TAG_KEY):
        return (
            "kept (CloudFormation-owned: Project/Environment tags permit this "
            "installation to update the function but do not transfer deletion "
            "authority to runtime teardown)"
        )
    return (
        "kept (NO OWNER TAG — ownership unprovable, so it is never auto-deleted; it predates "
        "ownership tagging or belongs to something else. Delete it by hand once nothing uses it)"
    )


def _release_shared_tool_lambda(
    lambda_client, function_name: str, gateway_role_name: str | None, region: str | None = None
) -> str:
    """Reference-counted teardown for a SHARED tool Lambda, under its write lock (F-7d).

    The lock spans authorize -> RemovePermission -> prune -> refcount -> delete -> role
    release, so a deploy cannot add its grant between this teardown's refcount and its
    delete, and this teardown cannot delete between a deploy's authorization and its
    AddPermission. A lock that cannot be taken keeps the function and says so.
    """
    from app.services.gateway_mutation_lock import GatewayMutationBusy

    try:
        lock_region = region or os.environ.get("APP_AWS_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
        with shared_lambda_lock(lock_region, function_name):
            return _release_shared_tool_lambda_locked(lambda_client, function_name, gateway_role_name, region)
    except GatewayMutationBusy as busy:
        return f"Shared tool Lambda {function_name} kept ({busy})"


def _release_shared_tool_lambda_locked(
    lambda_client, function_name: str, gateway_role_name: str | None, region: str | None = None
) -> str:
    """Reference-counted teardown for a SHARED singleton tool Lambda (Defect C).

    Remove only THIS gateway's ``AllowAgentCoreInvoke-<role>`` statement, then
    delete the function ONLY when no such invoke statements remain (i.e. no other
    live gateway still depends on it). Returns a human-readable log line.

    A shared Lambda deleted while another gateway still uses it makes that
    gateway serve 0 tools (its MCP ``tools/list`` hits a missing function) — the
    "tear down A, B goes dead" failure the matrix run reproduced live.

    Ownership FIRST (F-7d, peer 2c): ``is_shared_tool_function`` matches any stack's scoped
    name by shape, so a manifest row naming another stack's function must not be allowed
    to remove a statement from, or prune, that function's resource policy. The ownership
    read that used to gate only the final delete now gates every mutation below; an
    unowned, untagged or unreadable function is kept untouched, with the reason.
    """
    refusal = _authorize_tool_function_deletion(lambda_client, function_name, region)
    if refusal:
        return f"Shared tool Lambda {function_name} {refusal}"

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

    # F-7c: a refcount of zero says "no gateway of OURS still needs this function". It
    # does not say the function is ours to delete, and until now nothing else did either.
    # The statements counted above are the ones this platform adds, so a foreign function
    # has none of them and scores zero on its first teardown.
    refusal = _authorize_tool_function_deletion(lambda_client, function_name, region)
    if refusal:
        return f"Shared tool Lambda {function_name} {refusal}"

    try:
        lambda_client.delete_function(FunctionName=function_name)
    except Exception as e:  # noqa: BLE001
        if is_error(e, "ResourceNotFoundException"):
            return f"Shared tool Lambda {function_name} already absent"
        return f"Shared tool Lambda delete error: {e}"
    # The function is gone, so nothing needs its execution role any more. Released
    # HERE and only here: while any other gateway's invoke grant survived we returned
    # above, so this is the one point at which the role is provably unreferenced.
    line = f"Shared tool Lambda {function_name} deleted (last gateway released it)"
    role_line = _release_shared_tool_lambda_role(function_name, region)
    return f"{line}; {role_line}" if role_line else line


_SCOPED_CUSTOM_TOOL_NAME = re.compile(r"^AgentCore-[0-9a-f]{10}-CustomTool(?:Role)?-")
_SCOPED_KB_TOOL_NAME = re.compile(r"^AgentCore-[0-9a-f]{10}-KBTool(?:Role)?-")


def tool_binding_requirement(
    name: str, recorded_binding: str | None, deployment_id: str | None
) -> dict[str, str] | None:
    """The tags a per-deployment or per-scope tool function/role must carry before teardown may
    touch it (F-7d, peers 82/5a/3a). Stack ownership alone is checked separately, always.

    * a scoped KB tool (``AgentCore-<token>-KBTool[Role]-...``) -> ``{"DeploymentId": <this deployment>}``;
    * a scoped custom tool with a recorded binding -> ``{"ToolScope": <binding>}``, exactly;
    * a scoped custom tool with NO recorded binding -> refused: it was created with one, and a
      row that lost it proves nothing; a name collision is never deletion authority;
    * a legacy unscoped name (pre-token builds never carried these tags and cannot collide
      with a scoped name) -> ``None``: the stack-ownership gate alone, as it always was.
    """
    if _SCOPED_KB_TOOL_NAME.match(name or ""):
        if not deployment_id:
            raise ForeignResourceError(
                f"{name} is a per-deployment KB tool resource but no deployment id is known to bind it to."
            )
        return {"DeploymentId": deployment_id}
    if recorded_binding:
        return {"ToolScope": recorded_binding}
    if _SCOPED_CUSTOM_TOOL_NAME.match(name or ""):
        raise ForeignResourceError(
            f"{name} is a scoped custom-tool resource but this teardown holds no ToolScope binding for it; "
            "refusing to delete on the name alone."
        )
    return None


# Kept as the name the first callers used.
custom_tool_binding_requirement = tool_binding_requirement


def paired_custom_tool_function(role_name: str, pairs: dict[str, str] | None, bindings: dict[str, str] | None) -> str:
    """The function *role_name* is the execution role for, or ``""`` if it cannot be known.

    The value is the ``shared_lambda_lock`` key the role's teardown arm takes, so naming
    ANOTHER tool's function here is worse than naming none: the delete then runs fenced
    against a lambda nobody is touching while its own is unfenced.

    ``pairs`` is authoritative — the producer records it (``custom_tool_pairs``). Rows written
    before F-74c have no pairs, and the binding cannot substitute for one, because it is scoped
    to owner+gateway and is therefore the SAME value for every tool on that gateway. So a
    binding is used only when it identifies exactly one candidate; with two custom tools it
    identifies two and the honest answer is "unknown".
    """
    if pairs and pairs.get(role_name):
        return pairs[role_name]
    bindings = bindings or {}
    want = bindings.get(role_name)
    if not want:
        return ""
    candidates = [n for n, b in bindings.items() if b == want and n != role_name and n not in (pairs or {})]
    return candidates[0] if len(candidates) == 1 else ""


def custom_tool_manifest_row(
    res_type: str,
    name: str,
    bindings: dict[str, str] | None,
    pairs: dict[str, str] | None = None,
) -> dict[str, str]:
    """One teardown-manifest row for a custom-tool lambda or role, built the same way on every
    path that writes one.

    Used by both manifest writers (``step_handlers/gateway_step`` and ``services/deployment``)
    for CREATED and ADOPTED resources alike. F-74c: the adopted rows used to be written as a
    bare type+name, while the created rows carried ``tool_scope``. Because a redeploy ADOPTS,
    the last deployment standing on a gateway — the one whose teardown must actually reclaim
    the function and role — was exactly the one holding a row with no binding, and
    :func:`tool_binding_requirement` then correctly refused to delete on the name alone. Every
    redeploy therefore leaked a Lambda and an IAM role, proven live on ``acfe2e-p0920``.
    """
    row: dict[str, str] = {"type": res_type, "name": name}
    binding = (bindings or {}).get(name)
    if binding:
        row["tool_scope"] = binding
        if res_type == "iam_role":
            row["paired_function"] = paired_custom_tool_function(name, pairs, bindings)
    return row


def assert_role_binding(iam_client, role_name: str, required_tags: dict[str, str] | None) -> str:
    """``""`` when *role_name* carries every required binding tag (or none is required, or the
    role is already gone); otherwise the reason it is kept. Read fresh, right before the delete."""
    if not required_tags:
        return ""
    try:
        role_tags = tag_map(iam_client.get_role(RoleName=role_name)["Role"].get("Tags"))
    except Exception as exc:  # noqa: BLE001
        if is_error(exc, "NoSuchEntity", "NoSuchEntityException"):
            return ""
        return f"kept (binding unreadable: {type(exc).__name__})"
    mismatch = {k: role_tags.get(k) for k, v in required_tags.items() if role_tags.get(k) != v}
    if mismatch:
        return (
            "kept (bound to "
            + ", ".join(f"{k}={v or '<untagged>'}" for k, v in sorted(mismatch.items()))
            + " -- not this deployment's scope; a name collision is never deletion authority)"
        )
    return ""


def cleanup_gateway_resources(
    runtime_id: str,
    region: str,
    gateway_config: dict | None = None,
    *,
    deployment_id: str = "",
) -> list[str]:
    """Clean up all gateway resources: targets, Lambda, gateway, and Cognito."""
    cleanup_log: list[str] = []

    if gateway_config is None:
        return ["No gateway config provided"]

    gateway_id = gateway_config.get("gateway_id")
    client_info = gateway_config.get("client_info")
    gateway_created_by_deployment = gateway_config.get("gateway_created_by_deployment")
    gateway_role_created_by_deployment = gateway_config.get("gateway_role_created_by_deployment")
    # `or ""`, and no hard-coded fallback: a config with no tool Lambda (the
    # multi-target path builds none) must delete NOTHING here. The old default named
    # a function this module never creates and handed it straight to delete_function
    # below — see the placeholder note in deploy_gateway.
    lambda_name = gateway_config.get("lambda_function_name") or ""

    def _cleanup_gateway_role() -> None:
        """Delete only the confirmed gateway role this deployment may own."""
        gw_role_name = gateway_config.get("gateway_role_name")
        if not gw_role_name:
            return
        if gateway_role_created_by_deployment is False:
            cleanup_log.append("Gateway IAM role left in place: it pre-dated this deployment (protected)")
            return
        try:
            iam_client = _create_iam_client()
            delete_owned_iam_role(iam_client, gw_role_name, region)
            cleanup_log.append(f"Gateway IAM role {gw_role_name} deleted")
        except ResourceDeletionRefused:
            cleanup_log.append("Gateway IAM role cleanup error: exact stack ownership could not be proven (protected)")
        except Exception as e:  # noqa: BLE001
            if not is_error(e, "NoSuchEntity", "NoSuchEntityException"):
                cleanup_log.append(f"Gateway IAM role cleanup error: {e}")

    # An adopted gateway's authorizer, targets, Lambdas, and connector credentials
    # are one attached graph. Deleting only those dependencies would leave the
    # retained gateway live but unusable. Preserve the entire graph. A role newly
    # created before CreateGateway discovered the name conflict is independent and
    # unused (the adopted gateway keeps its existing role), so it is the only
    # resource this fast abort may still reclaim.
    if gateway_id and gateway_created_by_deployment is False:
        cleanup_log.append(f"Gateway {gateway_id} left in place: it pre-dated this deployment (protected)")
        cleanup_log.append(
            "Gateway dependency graph left in place because the retained gateway still references it (protected)"
        )
        _cleanup_gateway_role()
        return cleanup_log

    agentcore_ctrl = None
    gateway_owned = False
    if gateway_id:
        try:
            agentcore_ctrl = _create_agentcore_control_client(region)
            assert_agentcore_resource_owned(
                agentcore_ctrl,
                "gateway",
                gateway_id,
                region,
            )
            gateway_owned = True
        except Exception as exc:  # noqa: BLE001
            if resource_is_missing(exc):
                cleanup_log.append(f"Gateway {gateway_id} already absent")
            else:
                # The authorizer, targets, providers, client, and tool Lambdas
                # form one attached graph.  If the live gateway is foreign or
                # unreadable, deleting any of those dependencies could break it.
                cleanup_log.append(
                    f"Gateway {gateway_id} left in place: exact live stack ownership could not be proven (protected)"
                )
                cleanup_log.append(
                    "Gateway dependency graph left in place because its live ownership is unknown (protected)"
                )
                _cleanup_gateway_role()
                return cleanup_log

    if gateway_id and gateway_owned:
        try:
            # F-66e: under the gateway's write lock through the proof of absence, so no
            # update computed before the delete lands after it. Ownership is proved
            # again under the lock: the read above is a decision, this one is the
            # check at time of use. A gateway gone by then is absent, as it is above.
            with gateway_mutation_lock(agentcore_ctrl, region, gateway_id) as gw_lock:
                assert_agentcore_resource_owned(agentcore_ctrl, "gateway", gateway_id, region)
                # Delete every target page. Parent ownership is the authority over this
                # attached child graph; a first-page-only cleanup strands later targets.
                try:
                    targets = list_all(
                        agentcore_ctrl,
                        "list_gateway_targets",
                        item_keys=("items", "targets", "gatewayTargetSummaries"),
                        request={"gatewayIdentifier": gateway_id, "maxResults": 100},
                    )
                    for target in targets:
                        tid = target.get("targetId") or target.get("gatewayTargetId")
                        if tid:
                            agentcore_ctrl.delete_gateway_target(
                                gatewayIdentifier=gateway_id,
                                targetId=tid,
                            )
                            cleanup_log.append(f"Target {tid} deleted")
                except Exception as e:  # noqa: BLE001
                    cleanup_log.append(f"Target cleanup error: {e}")

                # Delete gateway (wait briefly for target deletion to propagate)
                time.sleep(3)
                gw_lock.delete(
                    lambda: wait_until_absent(
                        resource_label=f"gateway {gateway_id}",
                        read=lambda: agentcore_ctrl.get_gateway(gatewayIdentifier=gateway_id),
                        max_attempts=8,
                        delay_seconds=1.5,
                    ),
                    terminal=(DeletionFailedAfterAccept,),
                )
            cleanup_log.append(f"Gateway {gateway_id} confirmed deleted")
        except ResourceDeletionRefused as e:
            cleanup_log.append(f"Gateway {gateway_id} left in place: {e} (protected)")
            cleanup_log.append(
                "Gateway dependency graph left in place because gateway deletion was not confirmed (protected)"
            )
            return cleanup_log
        except Exception as e:  # noqa: BLE001
            if resource_is_missing(e):
                # Gone at the re-check or refused as not found: a definitive answer
                # (the lock is released), and the graph below is ours, as above.
                cleanup_log.append(f"Gateway {gateway_id} already absent")
            else:
                cleanup_log.append(f"Gateway delete error: {e}")
                cleanup_log.append("Gateway dependency graph left in place because gateway deletion failed (protected)")
                return cleanup_log
    elif not gateway_id:
        # LiteLLM and some partial failures legitimately have no AgentCore
        # gateway, but may still own a deployment-bound secret/provider.
        cleanup_log.append("No gateway_id in config")

    # Delete Cognito resources (only if provider is Cognito)
    if client_info:
        idp_provider = client_info.get("provider", "cognito")
        if idp_provider == "cognito" or not idp_provider:
            try:
                user_pool_id = client_info.get("user_pool_id")
                pool_region = client_info.get("user_pool_region") or (
                    _pool_region(user_pool_id) if user_pool_id else region
                )
                cognito_client = (
                    _create_platform_cognito_client(pool_region)
                    if user_pool_id and is_platform_owned_user_pool(user_pool_id)
                    else _create_cognito_client(pool_region)
                )
                client_id_val = client_info.get("client_id")
                # THREE-WAY, not a boolean. The previous `if shared / elif user_pool_id`
                # made "we could not identify this pool" mean "delete the pool", because
                # the second branch is the destructive one. Any pool we cannot prove we
                # created now reaches neither branch. Classify through the SAME client
                # the deletes go through, so a cross-account teardown reads tags in the
                # target account rather than in the home account.
                pool_class = classify_user_pool(user_pool_id, cognito_client=cognito_client)
                if user_pool_id and pool_class == POOL_SHARED_EXACT:
                    # Shared platform pool: delete ONLY this gateway's own app
                    # client and resource server. The pool and its hosted domain are
                    # platform-owned (RETAIN in the CDK stack) and hold every other
                    # deployed gateway's credentials; deleting the domain alone would
                    # break every agent's token mint for the >381s it takes to
                    # reprovision. See gateway_auth_pool.py.
                    if client_id_val:
                        try:
                            cognito_client.delete_user_pool_client(UserPoolId=user_pool_id, ClientId=client_id_val)
                            cleanup_log.append("Shared-pool gateway app client deleted")
                        except Exception as e:  # noqa: BLE001
                            cleanup_log.append(f"Shared-pool app client delete error: {type(e).__name__}")
                    # The resource server identifier is the scope's prefix
                    # ("agentcore-<gateway>/invoke"). Derive it from client_info
                    # rather than re-deriving it from a gateway name that is not in
                    # scope here — the scope is what the client was actually granted,
                    # so it cannot disagree with what was created.
                    _rs_id = (client_info.get("scope") or "").split("/", 1)[0]
                    # Keyed on the gateway NAME, so a deploy refused adoption of a
                    # same-name gateway shares it with that gateway's owner. The same
                    # co-residency check both teardown paths make, after our client
                    # is gone (F-63).
                    if _rs_id and gateway_config.get("resource_server_created_by_deployment") is False:
                        cleanup_log.append(
                            "Shared-pool gateway resource server left in place: it pre-dated this deployment (protected)"
                        )
                    elif _rs_id and not resource_server_is_unused(user_pool_id, _rs_id, cognito_client):
                        cleanup_log.append(
                            "Shared-pool gateway resource server left in place: still in use (protected)"
                        )
                    elif _rs_id:
                        try:
                            cognito_client.delete_resource_server(UserPoolId=user_pool_id, Identifier=_rs_id)
                            cleanup_log.append("Shared-pool gateway resource server deleted")
                        except Exception as e:  # noqa: BLE001
                            cleanup_log.append(f"Shared-pool resource server delete error: {type(e).__name__}")
                elif user_pool_id and pool_class == POOL_OWNED_BY_STACK:
                    # This deployment created this pool (proven by the AgentCoreStack
                    # owner tag), so it is ours to delete. Domain first — required
                    # before pool deletion.
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
                elif user_pool_id:
                    # FOREIGN_OR_UNKNOWN. Not the shared pool and not provably ours:
                    # an untagged pool from an older build, a pool belonging to another
                    # product, or a pool whose tags we could not read (AccessDenied,
                    # throttle, already deleted). Zero mutating calls. Reported so the
                    # leak is visible rather than silent — an operator can delete a
                    # stranded pool by hand, but nobody can undo deleting someone
                    # else's.
                    cleanup_log.append("Cognito pool left in place: ownership could not be proven (protected)")
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
        if not lambda_name:
            # No tool Lambda was created by this deploy. Deleting by a defaulted name
            # would destroy a function belonging to someone else (F-7/F-8).
            cleanup_log.append("No tool Lambda recorded for this gateway — nothing to delete")
        elif is_shared_tool_function(lambda_name, region):
            gw_role_name = gateway_config.get("gateway_role_name")
            if not gw_role_name and gateway_config.get("gateway_name"):
                # Compatibility with configs persisted before deploy_gateway returned
                # the exact role name. Those builds used this unsuffixed spelling in
                # every region. Current configs always win above, so a regional role
                # is never re-derived with the obsolete convention.
                gw_role_name = f"AgentCoreGateway-{gateway_config['gateway_name']}"
            cleanup_log.append(_release_shared_tool_lambda(lambda_client, lambda_name, gw_role_name, region))
        else:
            # Not shared, but still only OURS to delete if the tags say so. The name comes
            # from a manifest row this deployment wrote, and a manifest row is a record of
            # intent, not a capability over whatever currently holds that name (F-8).
            # Under the function's write lock (F-7d): the ownership read and the delete are
            # one fenced sequence, so a concurrent deploy cannot re-create or re-grant in between.
            with shared_lambda_lock(region, lambda_name):
                refusal = _authorize_tool_function_deletion(lambda_client, lambda_name, region)
                if refusal:
                    logger.warning("Not deleting Lambda %s: %s", lambda_name, refusal)
                    cleanup_log.append(f"Lambda {lambda_name} {refusal}")
                else:
                    lambda_client.delete_function(FunctionName=lambda_name)
                    cleanup_log.append(f"Lambda {lambda_name} deleted")
    except Exception as e:
        if not is_error(e, "ResourceNotFoundException"):
            cleanup_log.append(f"Lambda delete error: {e}")

    # Delete custom tool Lambdas
    custom_tool_lambdas = gateway_config.get("custom_tool_lambdas", [])
    custom_tool_bindings = gateway_config.get("custom_tool_bindings") or {}
    for fn_name in custom_tool_lambdas:
        try:
            if not lambda_client:
                lambda_client = _create_lambda_client(region)
            with shared_lambda_lock(region, fn_name):  # F-7d: ownership read + delete, fenced
                required = tool_binding_requirement(fn_name, custom_tool_bindings.get(fn_name), deployment_id)
                refusal = _authorize_tool_function_deletion(lambda_client, fn_name, region, required_tags=required)
                if refusal:
                    logger.warning("Not deleting custom tool Lambda %s: %s", fn_name, refusal)
                    cleanup_log.append(f"Custom tool Lambda {fn_name} {refusal}")
                    continue
                lambda_client.delete_function(FunctionName=fn_name)
                cleanup_log.append(f"Custom tool Lambda {fn_name} deleted")
        except Exception as e:
            if not is_error(e, "ResourceNotFoundException"):
                cleanup_log.append(f"Custom tool Lambda delete error: {e}")

    # Delete custom tool IAM roles. A saved role name is inventory, not authority:
    # IAM names are account-global and can be reoccupied after the manifest is
    # written, so re-read the live tags immediately before any mutation.
    custom_tool_roles = gateway_config.get("custom_tool_roles", [])
    for role_name in custom_tool_roles:
        try:
            iam_client = _create_iam_client()
            # Same exact-binding rule as the function (F-7d): the role is the function's
            # paired resource, so it is checked under the function's lock when the pair is
            # known, and under its own name otherwise.
            required = tool_binding_requirement(role_name, custom_tool_bindings.get(role_name), deployment_id)
            paired_fn = (
                paired_custom_tool_function(role_name, gateway_config.get("custom_tool_pairs"), custom_tool_bindings)
                or role_name
            )
            with shared_lambda_lock(region, paired_fn):
                binding_refusal = assert_role_binding(iam_client, role_name, required)
                if binding_refusal:
                    cleanup_log.append(f"IAM role {role_name} {binding_refusal}")
                    continue
                delete_owned_iam_role(iam_client, role_name, region)
            cleanup_log.append(f"IAM role {role_name} deleted")
        except ForeignResourceError as e:
            cleanup_log.append(f"IAM role cleanup error for {role_name}: {e}")
        except ResourceDeletionRefused:
            cleanup_log.append(
                f"IAM role cleanup error for {role_name}: exact stack ownership could not be proven (protected)"
            )
        except Exception as e:
            if not is_error(e, "NoSuchEntity", "NoSuchEntityException"):
                cleanup_log.append(f"IAM role cleanup error for {role_name}: {e}")

    # Delete the gateway's own execution role.
    # P-PLAT-TEARDOWN: failed deploys leave orphaned IAM roles because the gateway
    # step creates the role early (before targets) but only records resources at
    # the END on success. On failure the role never gets into the manifest. Delete
    # it explicitly here so abort-cleanup catches it. The producer sets
    # gateway_role_name only after IAM confirms the role; deriving it from the
    # requested gateway name would let a pre-IAM failure delete a colliding foreign
    # role. Live tags are re-checked because even confirmed inventory can go stale.
    _cleanup_gateway_role()

    # Delete SaaS connector credential providers (API-key OR OAuth2 — try both,
    # since the stored name doesn't record the type). Non-fatal per item.
    connector_providers = gateway_config.get("connector_credential_providers", [])
    if connector_providers:
        if agentcore_ctrl is None:
            agentcore_ctrl = _create_agentcore_control_client(region)
        for provider_entry in connector_providers:
            _ok, _msg = _delete_connector_credential_provider(
                agentcore_ctrl,
                provider_entry,
                region,
            )
            cleanup_log.append(_msg)

    # Delete connector secrets only after re-proving exact current-deployment
    # ownership from live tags. A saved ARN is an inventory record, not delete
    # authority over whichever secret currently occupies that identifier.
    connector_secret_arns = list(gateway_config.get("connector_secret_arns", []) or [])
    # Cognito's generated app-client secret is a deployment-bound secret too, but it
    # lives under client_info rather than the connector inventory. Missing it here
    # leaves the raw client credential orphaned on both abort and legacy inline
    # teardown. Only the explicit minted key is accepted: client_secret_ref alone is
    # a customer's external-IDP secret on that path and must never be deleted.
    minted_client_secret_ref = client_info.get("minted_client_secret_ref") if isinstance(client_info, dict) else None
    if minted_client_secret_ref and minted_client_secret_ref not in connector_secret_arns:
        connector_secret_arns.append(minted_client_secret_ref)
    if connector_secret_arns:
        sm_client = _create_secrets_client(region)
        for secret_arn in connector_secret_arns:
            try:
                deleted = delete_deployment_bound_secret(
                    region=region,
                    deployment_id=deployment_id,
                    secret_ref=secret_arn,
                    secrets_client=sm_client,
                )
                cleanup_log.append(f"Connector secret {secret_arn} {'deleted' if deleted else 'already absent'}")
            except ConnectorSecretDeletionRefused:
                cleanup_log.append("Connector secret delete refused: exact deployment ownership not proven")
            except Exception as e:  # noqa: BLE001
                if not is_error(e, "ResourceNotFoundException"):
                    cleanup_log.append(f"Connector secret delete error: {e}")

    # Delete staged OpenAPI spec objects (large connector specs routed to S3).
    for uri in gateway_config.get("connector_spec_s3_uris", []):
        try:
            _delete_spec_s3_object(uri, region, deployment_id)
            cleanup_log.append(f"Connector spec object {uri} deleted")
        except ResourceDeletionRefused as exc:
            cleanup_log.append(f"Connector spec object {uri} kept (protected: {exc})")
        except Exception as exc:  # noqa: BLE001
            if is_error(exc, "NoSuchKey", "NotFound", "NotFoundException"):
                cleanup_log.append(f"Connector spec object {uri} already absent")
            else:
                cleanup_log.append(f"Connector spec object delete error: {type(exc).__name__}")

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

    The prefix is right-stripped, and that is load-bearing rather than cosmetic:
    **AgentCore joins ``credentialPrefix`` to the key with its own single space.**
    So a descriptor saying ``"Bearer "`` yields the header value ``Bearer  <key>``
    (two spaces), which is not a valid credential — servers reject it. Verified
    against real AWS on a live LiteLLM proxy: three otherwise-identical
    ``mcpServer`` targets on one gateway, ``prefix="Bearer"`` reached READY while
    ``prefix="Bearer "`` went FAILED with *"returned HTTP 400 to the initialize
    handshake"*. Stripping here fixes every caller at once — the curated catalog,
    a custom canvas endpoint, and an OpenAPI target's user-typed prefix — and no
    caller can legitimately want a trailing space, since AgentCore supplies the
    separator itself. A prefix that is only whitespace collapses to empty and is
    therefore dropped, which is the same "send the raw key" meaning as ``""``.
    """
    api_key_cfg: dict = {
        "providerArn": provider_arn,
        "credentialParameterName": descriptor.get("parameter_name") or "Authorization",
        "credentialLocation": descriptor.get("location") or "HEADER",
    }
    prefix = (descriptor.get("prefix") or "").rstrip()
    if prefix:
        api_key_cfg["credentialPrefix"] = prefix
    return {
        "credentialProviderType": "API_KEY",
        "credentialProvider": {"apiKeyCredentialProvider": api_key_cfg},
    }


def _custom_api_key_descriptor(sel: dict) -> dict:
    """The API-key descriptor for a CUSTOM (non-catalog) MCP endpoint.

    Curated catalog entries ship an explicit descriptor, so only this path has to
    pick a default — and the default matters, because a wrong one produces a
    target that authenticates against nothing. It is ``Authorization: Bearer
    <key>``: a bare ``Authorization: <key>`` carries no auth scheme, is not valid
    per RFC 7235, and is refused by real servers. Verified against a live LiteLLM
    proxy: bare ``Authorization`` answers 500 on ``/mcp/`` where the Bearer form
    returns a valid MCP handshake.

    A caller needing another shape sends ``parameter_name`` (LiteLLM also accepts
    its own ``x-litellm-api-key``; some servers want ``x-api-key``). An explicit
    ``prefix: ""`` means send the raw key with no scheme — distinct from omitting
    ``prefix``, which takes the ``Bearer`` default.

    Note the default carries **no trailing space**: AgentCore inserts the
    separator between prefix and key itself, so a trailing space would produce
    ``Bearer  <key>``. ``_mcp_api_key_cred_config`` right-strips as a backstop.
    """
    raw = sel.get("api_key_descriptor") or sel.get("apiKeyDescriptor") or {}
    prefix = raw.get("prefix")
    return {
        "location": raw.get("location") or "HEADER",
        "parameter_name": raw.get("parameter_name") or raw.get("parameterName") or "Authorization",
        "prefix": "Bearer" if prefix is None else str(prefix),
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
    region: str | None = None,
    resource_tags: dict | None = None,
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
        provider_arn = _ensure_api_key_credential_provider(
            agentcore_ctrl,
            f"mcp-{target_name}",
            secret_arn=secret_arn,
            scope=gateway_id,
            region=region,
            resource_tags=resource_tags,
        )
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
    region: str | None = None,
    resource_tags: dict | None = None,
) -> dict:
    """Create a Gateway `mcpServer` target for an external MCP catalog entry.

    ``endpoint`` overrides the catalog endpoint (needed when the catalog URL has
    ``{placeholders}`` like a Databricks workspace or a Shopify store domain).
    Returns the created/reused/updated target dict, and raises otherwise. F-74b: it
    used to be able to return None for "creation was non-fatally skipped", and the
    caller's ``if target_id:`` then skipped the readiness handshake -- so the one
    outcome nobody had verified was the one that bypassed verification. There is no
    non-fatal skip any more. Target name is derived from the catalog id (kept short
    so the resulting ``<target>___<tool>`` qualified names stay under 64 chars).
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
        region=region,
        resource_tags=resource_tags,
    )
    logger.info(
        "Creating external MCP target '%s' (tier=%s, auth=%s)",
        name,
        catalog_entry.get("tier"),
        catalog_entry.get("auth_type"),
    )
    # update_existing (F-74b): an external MCP server whose endpoint or credential provider
    # changed between deploys used to leave the old target in place, so the gateway kept
    # calling the previous endpoint with the previous credentials.
    return _create_gateway_target_with_retry(agentcore_ctrl, gateway_id, name, params, update_existing=True)


def _wait_for_mcp_target_ready(
    agentcore_ctrl, gateway_id: str, target_id: str, target_name: str, timeout: int = 120
) -> None:
    """Block until an external ``mcpServer`` target leaves CREATING; raise if FAILED.

    ``create_gateway_target`` returns while the target is still CREATING —
    AgentCore then performs its own ``initialize`` handshake against the remote
    endpoint and settles on READY or FAILED. Without this gate a bad endpoint,
    key, or credential prefix produced a **green deploy with a toolless agent**:
    the same silent-empty-tool-plane failure ``_wait_for_gateway_to_serve_tools``
    exists to prevent on the gateway itself, which this path had no equivalent of.
    Observed for real — a target whose prefix made the outbound header malformed
    sat at FAILED with *"returned HTTP 400 to the initialize handshake"* while the
    deployment recorded ``succeeded``.

    AgentCore's ``statusReasons`` are specific and name the remote status code, so
    they are surfaced verbatim: that message is the whole diagnostic value. A
    FAILED target is deleted before raising so a corrected redeploy is not blocked
    by the leftover name. Polling errors are tolerated (propagation races); only a
    definitive FAILED, or a timeout, stops the deploy.
    """
    deadline = time.time() + timeout
    last_status = "UNKNOWN"
    while time.time() < deadline:
        try:
            detail = agentcore_ctrl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        except Exception as e:  # noqa: BLE001 — a read race must not fail the deploy
            logger.warning("Could not read MCP target '%s' status (will retry): %s", target_name, e)
            time.sleep(5)
            continue
        last_status = detail.get("status") or "UNKNOWN"
        if last_status == "FAILED":
            reasons = "; ".join(detail.get("statusReasons") or []) or "no reason reported"
            try:
                agentcore_ctrl.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
            except Exception:  # noqa: BLE001 — best-effort so a retry isn't name-blocked
                logger.warning("Could not delete FAILED MCP target '%s'", target_name)
            raise RuntimeError(f"External MCP target '{target_name}' failed to connect: {reasons}")
        if last_status not in ("CREATING", "UPDATING", "SYNCHRONIZING"):
            logger.info("External MCP target '%s' is %s", target_name, last_status)
            return
        time.sleep(5)
    raise RuntimeError(
        f"External MCP target '{target_name}' did not become ready within {timeout}s (last status {last_status})."
    )


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
    deployment_id: str = "",
    secrets_prebound: bool = False,
    resource_tags: dict | None = None,
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
    secrets_client = _create_secrets_client(region)

    def _rollback_partial() -> None:
        for pname in created_providers:
            _delete_connector_credential_provider(agentcore_ctrl, pname, region)
        if created_secrets:
            for _sidx, sarn in enumerate(created_secrets):
                try:
                    delete_deployment_bound_secret(
                        region=region,
                        deployment_id=deployment_id,
                        secret_ref=sarn,
                        secrets_client=secrets_client,
                    )
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
                    "api_key_descriptor": _custom_api_key_descriptor(sel),
                }
            else:
                if not server_id:
                    raise RuntimeError("External MCP selection needs a 'server_id' or a custom 'endpoint'.")
                entry = get_mcp_server(server_id)
                if entry is None:
                    raise RuntimeError(f"Unknown MCP server id: {server_id}")
                # Warn, don't block: a region-restricted endpoint (aws-mcp is
                # us-east-1-only) still creates a valid target, it just won't
                # resolve at invoke time. Saying so here beats an opaque
                # connect error later.
                allowed_regions = entry.get("region_restricted")
                if allowed_regions and region not in allowed_regions:
                    logger.warning(
                        "MCP server %r is hosted only in %s — this deployment is in %s, "
                        "so its endpoint %s will not be reachable at invoke time.",
                        server_id,
                        ", ".join(allowed_regions),
                        region,
                        entry.get("endpoint"),
                    )

            endpoint = _fill_endpoint_placeholders(
                entry.get("endpoint") or "", sel.get("endpoint_vars") or sel.get("endpointVars") or {}
            )
            auth_type = entry.get("auth_type", "none")

            secret_arn = sel.get("secret_arn") or sel.get("secretArn")
            oauth_provider_arn = None
            oauth_scopes = None

            # Tier 2 — consume only an exact-current-deployment secret. The API
            # path has already staged one before StartExecution; the direct path
            # binds here. Calling the same binder in both cases re-verifies the
            # live tags and payload shape instead of trusting an ARN-shaped value.
            if auth_type == "api_key":
                raw_key = sel.get("secret_value") or sel.get("secretValue")
                if secrets_prebound and raw_key:
                    raise RuntimeError("A pre-bound external MCP target must not carry a plaintext API key.")
                if not raw_key and not secret_arn:
                    raise RuntimeError(
                        f"MCP '{server_id}' needs an API key. Supply the key so the "
                        "platform can store a deployment-bound credential."
                    )
                secret_arn, _created = bind_connector_secret_for_deployment(
                    region=region,
                    owner_sub=owner_sub,
                    deployment_id=deployment_id,
                    payload_key="apiKey",
                    raw_value=None if secrets_prebound else raw_key,
                    secret_ref=secret_arn,
                    secrets_client=secrets_client,
                    resource_tags=resource_tags,
                )
                if secret_arn not in created_secrets:
                    created_secrets.append(secret_arn)

            # Tier 3 — create the OAuth2 client-credentials provider from user creds.
            if auth_type == "oauth2_client_credentials":
                oauth = sel.get("oauth") or {}
                client_id = oauth.get("client_id") or oauth.get("clientId")
                client_secret = oauth.get("client_secret") or oauth.get("clientSecret")
                client_secret_ref = (
                    oauth.get("client_secret_arn")
                    or oauth.get("clientSecretArn")
                    or oauth.get("client_secret_ref")
                    or oauth.get("clientSecretRef")
                )
                # A CustomOauth2 client-credentials provider resolves its token
                # endpoint from the IdP's OIDC discovery document.
                discovery_url = (
                    oauth.get("discovery_url")
                    or oauth.get("discoveryUrl")
                    or oauth.get("token_url")
                    or oauth.get("tokenUrl")
                )
                if not (client_id and discovery_url):
                    raise RuntimeError(f"MCP '{server_id}' needs oauth {{client_id, client_secret, discovery_url}}.")
                if secrets_prebound and client_secret:
                    raise RuntimeError(
                        "A pre-bound external MCP OAuth target must not carry a plaintext client secret."
                    )
                cs_arn, _created = bind_connector_secret_for_deployment(
                    region=region,
                    owner_sub=owner_sub,
                    deployment_id=deployment_id,
                    payload_key="clientSecret",
                    raw_value=None if secrets_prebound else client_secret,
                    secret_ref=client_secret_ref,
                    secrets_client=secrets_client,
                    resource_tags=resource_tags,
                )
                if cs_arn not in created_secrets:
                    created_secrets.append(cs_arn)
                oauth_provider_arn = _ensure_oauth2_credential_provider(
                    agentcore_ctrl,
                    f"mcp-{server_id}",
                    vendor="CustomOauth2",
                    client_id=client_id,
                    client_secret_arn=cs_arn,
                    discovery_url=discovery_url,
                    scope=gateway_id,
                    region=region,
                    resource_tags=resource_tags,
                )
                # TYPE-prefixed, matching the connector path (see the OAUTH:/API_KEY:
                # convention where connector providers are recorded). gateway_step.py
                # turns each entry into a manifest row and defaults an *untyped* name
                # to oauth2_credential_provider, so an unprefixed API-key provider is
                # deleted from the wrong vault namespace and survives teardown while
                # the delete still reports success.
                created_providers.append(f"OAUTH:{_scoped_provider_name(f'mcp-{server_id}', gateway_id)}")
                oauth_scopes = oauth.get("scopes") or (entry.get("oauth_descriptor") or {}).get("scopes")

            # Tier-2's API-key provider is created inside deploy_external_mcp_target;
            # track its name so teardown can remove it. The derivation MIRRORS that
            # function's own (target_name = mcp-<id> truncated to 48, provider =
            # mcp-<target_name>, then gateway-scoped) rather than re-spelling it —
            # a name that does not match byte-for-byte silently orphans a
            # credential provider on delete. The API_KEY: prefix is what routes the
            # manifest row to delete_api_key_credential_provider; without it the row
            # is recorded as oauth2 and the provider is never actually deleted.
            if auth_type == "api_key":
                _tname = _sanitize_provider_name(f"mcp-{server_id}")[:48]
                created_providers.append(f"API_KEY:{_scoped_provider_name(f'mcp-{_tname}', gateway_id)}")

            created = deploy_external_mcp_target(
                agentcore_ctrl,
                gateway_id=gateway_id,
                catalog_entry=entry,
                endpoint=endpoint,
                secret_arn=secret_arn,
                oauth_provider_arn=oauth_provider_arn,
                oauth_scopes=oauth_scopes,
                region=region,
                resource_tags=resource_tags,
            )

            # Prove the target actually connected. The create call returns while
            # AgentCore is still handshaking with the remote endpoint, so without
            # this a wrong endpoint/key/prefix deploys "successfully" and the
            # agent simply has no tools. See _wait_for_mcp_target_ready.
            target_id = (created or {}).get("targetId")
            # F-74b: this used to be ``if target_id:``, which made a target we could not
            # identify SKIP the very check that exists to prove the target connected. The
            # deploy path's one unverified outcome was routed straight past its own oracle.
            # ``_create_gateway_target_with_retry`` no longer has a None exit, so an id that
            # is still missing here means a response shape we do not understand.
            if not target_id:
                raise GatewayTargetUnproven(
                    f"External MCP server '{server_id}' was deployed to gateway {gateway_id} but "
                    "no targetId came back, so this deploy cannot confirm the target reached "
                    "READY. Refusing to report tools that may not be served."
                )
            _wait_for_mcp_target_ready(
                agentcore_ctrl,
                gateway_id,
                target_id,
                (created or {}).get("name") or str(server_id),
            )
    except Exception:
        logger.error("External MCP deploy failed mid-loop; rolling back partial resources")
        _rollback_partial()
        raise

    return {"credential_provider_names": created_providers, "secret_arns": created_secrets}


#: The tag a function's OWNER must set to let this platform write an invoke grant
#: into that function's resource policy. The deploy roles hold
#: lambda:AddPermission on ``function:*`` ONLY under this tag
#: (infra/stacks/platform/step_lambdas.py — keep the two in step), so consent is
#: enforced in IAM and not merely in this module.
GATEWAY_TARGET_OPT_IN_TAG = "AgentCoreGatewayTarget"
GATEWAY_TARGET_OPT_IN_VALUE = "allow"


def _grant_gateway_invoke_on_lambda(region: str, function_arn: str, gateway_role_arn: str) -> None:
    """Grant the gateway execution role ``lambda:InvokeFunction`` on a
    user-supplied Lambda ARN (idempotent, per-role StatementId).

    Mirrors the grant `_create_or_update_lambda` applies to our managed tool
    Lambdas, but targets a function we did NOT create (a raw ARN from a gateway
    ``lambda`` target). Best-effort on propagation races; a conflicting statement
    (already granted) is treated as success.

    AccessDenied is FATAL and is re-raised as an actionable ``ValueError``. The
    deploy roles are scoped to functions the platform created (``function:AgentCore*``)
    plus functions whose owner opted in with the tag above, and that boundary is
    deliberate: blanket ``lambda:AddPermission`` on ``function:*`` would let any
    tenant's canvas make this platform rewrite the resource policy of ANY function in
    the account — naming someone else's function is not authority over it (F-7).
    Least privilege here follows ARCC cnt_BBrFTwAEgWxA30 (scope to the exact resources
    needed) and cnt_dIF0SRA5SUuWSk (enumerate actions, avoid wildcards).

    Failing closed rather than warning is the F-24 lesson: a target the gateway cannot
    invoke must not deploy as "successful" with the tool silently broken at call time.
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
        except Exception as e:
            # A raw AccessDeniedException here reads as a platform bug ("not
            # authorized to perform: lambda:AddPermission on resource: ...") and
            # tells the user nothing they can act on. Measured live 2026-09-21: a
            # gateway target naming a customer function outside the AgentCore* prefix
            # failed the whole deploy with exactly that message and no remedy.
            #
            # is_error, not a substring test on str(e): an error code quoted inside
            # some other exception's message must not steer this branch.
            if not is_error(e, "AccessDeniedException"):
                raise
            raise ValueError(
                f"Not permitted to grant the gateway invoke access on {function_arn}. "
                f"A Lambda gateway target that this platform did not create must be "
                f"opted in by its owner: tag the function "
                f"{GATEWAY_TARGET_OPT_IN_TAG}={GATEWAY_TARGET_OPT_IN_VALUE} "
                f"(aws lambda tag-resource --resource {function_arn} --tags "
                f"{GATEWAY_TARGET_OPT_IN_TAG}={GATEWAY_TARGET_OPT_IN_VALUE}) and redeploy. "
                f"Without that grant the gateway would be created but every call to this "
                f"tool would fail with AccessDeniedException at invoke time, so the deploy "
                f"is stopped here instead."
            ) from e


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


def _openapi_target_cred_config(
    agentcore_ctrl,
    target: dict,
    base_name: str,
    gateway_id: str | None = None,
    region: str | None = None,
    resource_tags: dict | None = None,
) -> dict | None:
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
            agentcore_ctrl,
            f"openapi-{base_name}",
            secret_arn=secret_arn,
            scope=gateway_id,
            region=region,
            resource_tags=resource_tags,
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
    deployment_id: str = "",
    resource_tags: dict | None = None,
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

    Unknown families and ``mcp_server`` entries are skipped with a warning (never
    fatal) — the first is forward compatibility, the second is handled elsewhere.
    A target of a KNOWN family that is missing its required payload is **fatal**,
    and that asymmetry is deliberate. Skipping one produced a *green deployment
    with the user's tool silently absent*: observed live as
    ``Gateway lambda target #0 has no function_arn; skipping`` on a deploy that
    reported success. The canvas hands out ``{type: 'lambda', functionArn: ''}``
    as the default for a new target, so leaving the field blank was enough, and
    frontend validation only checked the ARN's *format* when one was present
    (its openapi sibling, four lines below in ``validation.ts``, correctly
    required the field — the same check for lambda was missing). Both layers are
    fixed; failing here is the backstop for an API caller that bypasses the UI.
    """
    created_target_names: list[str] = []

    def _incomplete(family: str, missing: str) -> ValueError:
        return ValueError(
            f"Gateway target #{idx} of type '{family}' is missing {missing}. "
            "A gateway cannot be deployed with a declared target that has no payload — "
            "the deployment would report success while the tool was silently absent."
        )

    for idx, raw in enumerate(targets or []):
        target = raw or {}
        ttype = target.get("type") or target.get("target_type") or ""
        base_name = re.sub(r"[^a-zA-Z0-9-]", "-", f"{name_prefix}-{ttype or 'x'}-{idx}")[:48]

        if ttype == "lambda":
            function_arn = target.get("function_arn") or target.get("functionArn")
            if not function_arn:
                raise _incomplete("lambda", "function_arn (the Lambda ARN to expose as a tool)")
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
            # update_existing (F-74b): ``base_name`` is derived from the name prefix, the family
            # and the target's INDEX, so a redeploy that edits target #0's ARN or tool schema
            # asks for the same name with different contents. Without the update this reused the
            # old target, and the canvas change never reached the gateway.
            _create_gateway_target_with_retry(
                agentcore_ctrl, gateway_id, base_name, create_params, update_existing=True
            )
            created_target_names.append(base_name)
            logger.info("Deployed lambda gateway target %s -> %s", base_name, _safe_log_token(function_arn))

        elif ttype == "openapi":
            spec_inline = target.get("spec_content") or target.get("specContent")
            spec_url = target.get("spec_url") or target.get("specUrl")
            if not spec_inline:
                if not spec_url:
                    raise _incomplete("openapi", "spec_url or spec_content (the OpenAPI specification)")
                spec_inline = _fetch_openapi_spec(spec_url)
            openapi_schema = _build_openapi_schema(
                spec_inline,
                connector_id=base_name,
                region=region,
                deployment_id=deployment_id,
            )
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
            cred_cfg = _openapi_target_cred_config(
                agentcore_ctrl,
                target,
                base_name,
                gateway_id,
                region,
                resource_tags=resource_tags,
            )
            if cred_cfg is not None:
                create_params["credentialProviderConfigurations"] = [cred_cfg]
            # update_existing (F-74b): an edited spec, a changed spec_url, or a credential
            # provider that moved from public to api_key all land on the same ``base_name``.
            _create_gateway_target_with_retry(
                agentcore_ctrl, gateway_id, base_name, create_params, update_existing=True
            )
            created_target_names.append(base_name)
            logger.info("Deployed openapi gateway target %s", base_name)

        elif ttype == "smithy":
            # AgentCore's smithyModel is an inline/staged API schema. The bare
            # model_name ('dynamodb') carries no schema, so require inline content
            # (model_content / spec_content).
            model_content = (
                target.get("model_content")
                or target.get("modelContent")
                or target.get("spec_content")
                or target.get("specContent")
            )
            if not model_content:
                raise _incomplete(
                    "smithy",
                    "model_content (a bare model_name such as "
                    f"{target.get('model_name') or target.get('modelName')!r} carries no schema)",
                )
            create_params = {
                "gatewayIdentifier": gateway_id,
                "name": base_name,
                "targetConfiguration": {"mcp": {"smithyModel": {"inlinePayload": model_content}}},
                # Smithy models front AWS SDK services (e.g. DynamoDB): they ARE
                # AWS-native, so GATEWAY_IAM_ROLE (SigV4 signed by the gateway
                # execution role) is the correct provider here — unlike openapi.
                "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
            }
            # update_existing (F-74b): the whole payload is the inline model, so an edited
            # model under an unchanged name is exactly the case that used to be discarded.
            _create_gateway_target_with_retry(
                agentcore_ctrl, gateway_id, base_name, create_params, update_existing=True
            )
            created_target_names.append(base_name)
            logger.info("Deployed smithy gateway target %s", base_name)

        elif ttype == "mcp_server":
            # mcp_server entries are wired via external_mcp_servers (secret hygiene
            # + SSRF validation live there); ignore here to avoid double-deploy.
            continue

        else:
            logger.warning("Unknown gateway target family '%s' (#%d); skipping", ttype, idx)

    return {"target_names": created_target_names}
