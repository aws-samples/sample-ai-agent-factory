"""LiteLLM as the authoritative registry catalog — an ADDITIONAL provider.

Workstream B of the post-demo customer feedback. The customer runs LiteLLM and
wants it to BE their catalog rather than maintain a second one here. This module
makes that possible; ``dynamodb`` stays the default and is untouched.

What LiteLLM can and cannot back
--------------------------------
LiteLLM exposes ``GET /v1/mcp/server`` and ``GET /mcp-rest/tools/list``, and no
create/update/delete for MCP *server* records at all — registration is Admin-UI
(needs ``STORE_MODEL_IN_DB=True``) or ``config.yaml``. Its catalog object is "an
MCP server with tools": no canvas snapshot, no per-entry owner, no
pending/approved/rejected review state. So:

===========================  ==============================================
``/api/registry`` surface    LiteLLM provider
===========================  ==============================================
list / search / get          LiteLLM IS the catalog (projected + sidecar)
pre-deploy approval gate     LiteLLM IS the source of truth (see the gate)
publish / update / delete    Sidecar rows in the platform's own table
clone-to-canvas              Sidecar only — LiteLLM has no canvas snapshot
approve / reject             Sidecar only — 501 on a projected row
===========================  ==============================================

Say this to the customer plainly rather than hiding it behind a fallback:
*LiteLLM becomes the authoritative catalog and the governance source of truth;
the platform keeps a thin sidecar for canvas snapshots and review metadata,
because LiteLLM has no write API for server records.* A silent degradation here
would look like it worked and then quietly diverge.

Two decisions worth naming
--------------------------
**Projected rows are ``status="approved"``, set EXPLICITLY.** Under this provider
the approval signal is "present in LiteLLM's server list" — which is exactly what
the deploy gate checks, so the badge the UI shows and the verdict the gate reaches
cannot disagree. It is set explicitly because ``RegistryEntry.status`` DEFAULTS to
``"approved"``: relying on that default would mean any future field-ordering or
projection change silently keeps handing out approval, and there would be nothing
in the code saying it was intended. Disabled servers are not projected at all —
LiteLLM's list is its catalog, so absence from it is absence from the catalog.

**A sidecar publish that collides with a projected slug is REFUSED.** Otherwise a
developer could publish "github" and shadow the real LiteLLM ``github`` server in
every listing — impersonation of a governed catalog entry. Refusing keeps the slug
namespace unambiguous and means listings never need a precedence rule.

SECURITY: the virtual key lives only in Secrets Manager under the dedicated
``agentcore-registry/`` namespace (NOT ``agentcore-provider/``, which is scoped to
model-provider keys, and not ``agentcore-connector/``, which teardown sweeps per
deployment — a registry credential outlives every deployment). The base URL goes
through the existing ``_validate_outbound_url`` SSRF guard. No key is ever logged
or returned by any API.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error

from app.services.aws_agent_registry import RegistryQueryFailed
from app.services.litellm_gateway_deployer import (
    _SERVERS_PATH,
    _get_json,
    _items,
)
from app.services.registry_providers.base import (
    SOURCE_LITELLM,
    SOURCE_PLATFORM,
    RegistryCapabilities,
    UnsupportedRegistryOperation,
)
from app.services.registry_store import RegistryEntry, get_registry_store, slugify

logger = logging.getLogger(__name__)

# Nobody owns a projected row. A sentinel with a colon can never equal a Cognito
# sub (subs are UUIDs), so `entry.owner_sub == caller_sub` in the router's
# visibility check can never accidentally grant ownership of a LiteLLM entry.
LITELLM_OWNER_SENTINEL = "litellm:catalog"

# Secrets Manager namespace for the registry credential. Deliberately its own
# prefix — see the module docstring.
SECRET_NAMESPACE = "agentcore-registry/"

_CONFIG_SK = "SETTING#litellm_registry"

_CAPABILITIES = RegistryCapabilities(
    provider="litellm",
    authoritative_catalog="LiteLLM MCP catalog",
    supports_publish=True,
    supports_update=True,
    supports_delete=True,
    supports_review=True,
    supports_clone=True,
    read_only_sources=(SOURCE_LITELLM,),
    notes=(
        "LiteLLM is the authoritative catalog and the approval source of truth. "
        "MCP servers projected from LiteLLM are read-only here — register, enable "
        "or disable them in LiteLLM (Admin UI or config.yaml), which has no write "
        "API for server records. Agents published from a canvas are stored in the "
        "platform sidecar and keep the normal review workflow, because LiteLLM has "
        "no canvas-snapshot or review-state counterpart."
    ),
)


class RegistryEntryConflict(ValueError):
    """A write would collide with an entry the active backend already owns."""


# ---------------------------------------------------------------------------
# Config (Settings row in the TagPolicy table — third use of that pattern)
# ---------------------------------------------------------------------------


def _region() -> str:
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))


def _settings_table():
    import boto3

    name = os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy")
    return boto3.resource("dynamodb", region_name=_region()).Table(name)


def get_litellm_registry_config() -> dict | None:
    """The configured LiteLLM registry, or None when unconfigured.

    Shape: ``{"base_url": str, "api_key_ref": str, "verified": bool}``. Env
    overrides win so a deployment can pin this statically; otherwise the Settings
    row is read. NEVER returns the key itself.
    """
    env_url = os.environ.get("LITELLM_REGISTRY_BASE_URL", "").strip()
    if env_url:
        return {
            "base_url": env_url.rstrip("/"),
            "api_key_ref": os.environ.get("LITELLM_REGISTRY_API_KEY_REF", "").strip(),
            "verified": True,
        }
    try:
        item = _settings_table().get_item(Key={"org_id": "default", "sk": _CONFIG_SK}).get("Item")
    except Exception as e:  # noqa: BLE001
        logger.info("litellm registry config lookup failed: %s", e)
        return None
    if not item or not item.get("value"):
        return None
    return {
        "base_url": str(item["value"]).rstrip("/"),
        "api_key_ref": str(item.get("api_key_ref") or ""),
        # A base URL that could not be probed at save time is still usable (a
        # self-hosted LiteLLM may be private and unreachable from the control
        # plane while perfectly reachable from a VPC-mode Runtime). The flag is
        # surfaced so the UI can say so instead of implying it was validated.
        "verified": str(item.get("verified", "false")).lower() == "true",
    }


def set_litellm_registry_config(base_url: str, api_key_ref: str, *, verified: bool) -> None:
    """Persist the LiteLLM registry config. *api_key_ref* is an ARN, never a key."""
    _settings_table().put_item(
        Item={
            "org_id": "default",
            "sk": _CONFIG_SK,
            "value": base_url.rstrip("/"),
            "api_key_ref": api_key_ref,
            "verified": "true" if verified else "false",
        }
    )


def clear_litellm_registry_config() -> None:
    _settings_table().delete_item(Key={"org_id": "default", "sk": _CONFIG_SK})


def put_registry_secret(api_key: str, region: str | None = None) -> str:
    """Mint the virtual key into Secrets Manager and return only its ARN.

    Mirrors ``gateway_deployer._put_connector_secret`` but under this module's own
    namespace. Logs a CONSTANT — never the generated name or the payload.
    """
    import uuid as _uuid

    import boto3

    sm = boto3.client("secretsmanager", region_name=region or _region())
    resp = sm.create_secret(
        Name=f"{SECRET_NAMESPACE}litellm/{_uuid.uuid4().hex[:12]}",
        SecretString=json.dumps({"apiKey": api_key}),
        Description="AgentCore LiteLLM registry credential (auto-managed)",
    )
    logger.info("Created registry credential resource")
    return resp["ARN"]


def _read_api_key(api_key_ref: str) -> str:
    """Resolve the virtual key from its ARN. Never logged, never returned by an API."""
    if not api_key_ref:
        return ""
    import boto3

    sm = boto3.client("secretsmanager", region_name=_region())
    payload = json.loads(sm.get_secret_value(SecretId=api_key_ref)["SecretString"])
    return str(payload.get("apiKey") or "")


def validate_secret_ref(api_key_ref: str) -> str:
    """Lock a tenant-supplied secret ARN to this module's namespace.

    Same discipline as ``deployment_handler``'s ``provider_api_key_ref`` check:
    without it a tenant could point the registry at an arbitrary foreign secret
    and have the control plane read it back for them.
    """
    if api_key_ref and f":secret:{SECRET_NAMESPACE}" not in api_key_ref:
        raise ValueError(
            f"api_key_ref must be a Secrets Manager ARN in the {SECRET_NAMESPACE} namespace",
        )
    return api_key_ref


# ---------------------------------------------------------------------------
# Reading LiteLLM's catalog
# ---------------------------------------------------------------------------


def _server_is_enabled(server: dict) -> bool:
    """Whether LiteLLM considers this server usable.

    Presence in the list is the primary signal — LiteLLM lists what it is
    configured with. Some releases carry an explicit flag; honour it when present
    so an operator who disabled a server in the Admin UI sees it leave the
    catalog, and default to enabled when the field is absent rather than hiding
    every server on a release that does not report it.
    """
    for key in ("enabled", "is_enabled", "active"):
        if key in server:
            return bool(server[key])
    for key in ("disabled", "is_disabled"):
        if key in server:
            return not bool(server[key])
    status = str(server.get("status") or "").strip().lower()
    if status in ("disabled", "inactive", "deleted"):
        return False
    return True


def _server_name(server: dict) -> str:
    info = server.get("mcp_info") or {}
    return str(
        server.get("alias")
        or server.get("server_name")
        or info.get("server_name")
        or server.get("server_id")
        or server.get("id")
        or ""
    ).strip()


def _server_description(server: dict) -> str:
    info = server.get("mcp_info") or {}
    desc = server.get("description") or info.get("description") or ""
    return str(desc)[:2000]


def list_litellm_servers() -> list[dict]:
    """Every MCP server the configured virtual key can see.

    Raises :class:`RegistryQueryFailed` — the SAME class the AWS federation path
    raises — when the catalog cannot be read. That identity is not incidental: the
    deploy gate in ``deployment_handler`` catches ``RegistryQueryFailed`` imported
    from ``aws_agent_registry`` and turns it into a 503, and its outer
    ``except Exception`` would otherwise swallow a different exception class and
    let the deploy through. A governance gate that opens on error is not a gate,
    and a second identically-named class in another module is exactly how that
    happens.
    """
    cfg = get_litellm_registry_config()
    if cfg is None:
        raise RegistryQueryFailed("LiteLLM registry is selected but not configured (no base URL)")
    try:
        api_key = _read_api_key(cfg["api_key_ref"])
    except Exception as e:  # noqa: BLE001
        raise RegistryQueryFailed(f"could not read the LiteLLM registry credential: {type(e).__name__}") from e
    try:
        payload = _get_json(cfg["base_url"] + _SERVERS_PATH, api_key)
    except urllib.error.HTTPError as e:
        detail = "rejected the virtual key" if e.code in (401, 403) else f"returned HTTP {e.code}"
        raise RegistryQueryFailed(f"LiteLLM registry {detail} at {_SERVERS_PATH}") from e
    except Exception as e:  # noqa: BLE001
        raise RegistryQueryFailed(f"LiteLLM registry is unreachable: {type(e).__name__}") from e
    return [s for s in _items(payload) if isinstance(s, dict)]


def probe_litellm_registry(base_url: str, api_key: str) -> dict:
    """Check a candidate LiteLLM registry before it is persisted.

    Returns ``{"reachable": bool, "servers": int, "detail": str}``.

    Raises :class:`ValueError` — a 400 at the API boundary — only for a REAL
    misconfiguration the admin must fix: a rejected key (401/403) or a base URL
    with no MCP route (404). The SSRF guard has already rejected a private or
    non-https URL before this is called.

    A network-level failure is NOT an error here, and that asymmetry is the whole
    point. A self-hosted LiteLLM is often private, and the control-plane Lambda has
    no VPC egress (``gateway_deployer`` has no network config at all — only the
    Runtime does, via ``runtime_deployer._build_network_configuration``). So an
    unreachable-from-here proxy may be perfectly reachable from the VPC-mode
    Runtime that will actually use it. Failing closed on unreachability would make
    private LiteLLM unusable; instead the config saves as ``verified=false`` and the
    UI says so, rather than implying it was validated.
    """
    try:
        payload = _get_json(base_url.rstrip("/") + _SERVERS_PATH, api_key)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ValueError(
                f"LiteLLM rejected the virtual key (HTTP {e.code}). Check the key and that "
                "it is permitted to use MCP servers."
            ) from e
        if e.code == 404:
            raise ValueError(
                f"LiteLLM has no {_SERVERS_PATH} route (HTTP 404). Check the base URL points "
                "at the LiteLLM proxy root and that MCP is enabled on it."
            ) from e
        raise ValueError(f"LiteLLM returned HTTP {e.code} at {_SERVERS_PATH}.") from e
    except Exception as e:  # noqa: BLE001
        return {
            "reachable": False,
            "servers": 0,
            "detail": (
                f"Could not reach the proxy from the control plane ({type(e).__name__}). Saved "
                "as unverified — this is expected for a private LiteLLM, which a VPC-mode "
                "runtime can still reach."
            ),
        }
    servers = [s for s in _items(payload) if isinstance(s, dict) and _server_is_enabled(s)]
    return {"reachable": True, "servers": len(servers), "detail": f"{len(servers)} enabled MCP server(s)."}


def _project(server: dict) -> RegistryEntry | None:
    """Turn one LiteLLM MCP server into a catalog entry, or None if unusable."""
    name = _server_name(server)
    if not name:
        return None
    tools = server.get("tools") or (server.get("mcp_info") or {}).get("tools") or []
    tool_names = [str(t.get("name") or t) for t in tools if t] if isinstance(tools, list) else []
    description = _server_description(server) or (
        f"MCP server on LiteLLM ({len(tool_names)} tool(s))" if tool_names else "MCP server on LiteLLM"
    )
    return RegistryEntry(
        agent_slug=slugify(name),
        owner_sub=LITELLM_OWNER_SENTINEL,
        display_name=name,
        description=description,
        tags=["litellm", "mcp", *tool_names[:18]],
        visibility="org",
        canvas_snapshot={},
        # See the module docstring: set explicitly, never inherited from the
        # model default, and equal to what the deploy gate will decide.
        status="approved",
        source=SOURCE_LITELLM,
        created_at=str(server.get("created_at") or ""),
        updated_at=str(server.get("updated_at") or server.get("created_at") or ""),
    )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class LiteLLMRegistryProvider:
    """``RegistryProvider`` backed by LiteLLM, with a platform sidecar for writes."""

    def capabilities(self) -> RegistryCapabilities:
        return _CAPABILITIES

    # -- projection helpers ----------------------------------------------

    def _projected(self) -> dict[str, RegistryEntry]:
        by_slug: dict[str, RegistryEntry] = {}
        for server in list_litellm_servers():
            if not _server_is_enabled(server):
                continue
            entry = _project(server)
            if entry is not None:
                by_slug[entry.agent_slug] = entry
        return by_slug

    def _merge(self, sidecar: list[RegistryEntry], projected: dict[str, RegistryEntry]) -> list[RegistryEntry]:
        """Sidecar rows plus projections, sidecar winning on slug.

        ``put`` refuses colliding slugs, so a collision here can only come from a
        row published BEFORE this provider was switched on. Preferring the sidecar
        row keeps that pre-existing entry reachable and mutable instead of making
        it disappear behind a read-only projection its owner cannot touch.
        """
        taken = {e.agent_slug for e in sidecar}
        return [*sidecar, *(e for slug, e in projected.items() if slug not in taken)]

    # -- reads -----------------------------------------------------------

    def get(self, org_id: str, agent_slug: str) -> RegistryEntry | None:
        existing = get_registry_store().get(org_id, agent_slug)
        if existing is not None:
            return existing
        return self._projected().get(agent_slug)

    def list_for_org(self, org_id: str) -> list[RegistryEntry]:
        return self._merge(get_registry_store().list_for_org(org_id), self._projected())

    def list_pending(self, org_id: str) -> list[RegistryEntry]:
        # Projections are never pending — LiteLLM has no review state, and a row
        # that cannot be approved must not sit in an admin's review queue forever.
        return get_registry_store().list_pending(org_id)

    def list_for_owner(self, owner_sub: str) -> list[RegistryEntry]:
        # Nobody owns a projected row, so "mine" is sidecar-only by definition.
        return get_registry_store().list_for_owner(owner_sub)

    def list_public(self) -> list[RegistryEntry]:
        # LiteLLM has no visibility model; projections are org-visible, not public.
        return get_registry_store().list_public()

    # -- writes ----------------------------------------------------------

    def put(self, entry: RegistryEntry) -> RegistryEntry:
        if entry.agent_slug in self._projected():
            raise RegistryEntryConflict(
                f"'{entry.agent_slug}' already names an MCP server in the LiteLLM catalog. "
                "Publishing over it would shadow a governed entry — choose a different "
                "display name."
            )
        entry.source = SOURCE_PLATFORM
        return get_registry_store().put(entry)

    def update(self, org_id: str, agent_slug: str, updates: dict) -> RegistryEntry | None:
        self._assert_sidecar(org_id, agent_slug, "updated")
        return get_registry_store().update(org_id, agent_slug, updates)

    def delete(self, org_id: str, agent_slug: str) -> bool:
        self._assert_sidecar(org_id, agent_slug, "deleted")
        return get_registry_store().delete(org_id, agent_slug)

    def increment_usage(self, org_id: str, agent_slug: str) -> None:
        # Telemetry only, and projections have no row to bump. Silently skipping is
        # right here: a missing usage count must never fail a clone.
        if get_registry_store().get(org_id, agent_slug) is not None:
            get_registry_store().increment_usage(org_id, agent_slug)

    def _assert_sidecar(self, org_id: str, agent_slug: str, verb: str) -> None:
        """Defence in depth behind the router's provenance check.

        The router already refuses a mutation on a read-only source, so reaching
        here means either a new call site or a projection with no sidecar row.
        """
        if get_registry_store().get(org_id, agent_slug) is not None:
            return
        if agent_slug in self._projected():
            raise UnsupportedRegistryOperation(
                f"'{agent_slug}' is an MCP server projected from the LiteLLM catalog and "
                f"cannot be {verb} here — LiteLLM exposes no write API for server records. "
                "Change it in LiteLLM (Admin UI or config.yaml)."
            )


# ---------------------------------------------------------------------------
# Deploy-time governance gate
# ---------------------------------------------------------------------------


def _identifier_matches(ident: str, server: dict, blob: str) -> bool:
    if not ident:
        return False
    if ident == _server_name(server) or slugify(ident) == slugify(_server_name(server)):
        return True
    # URL identifiers: an endpoint is approved when an enabled server points at it.
    # `aws_agent_registry.unapproved_integrations` substring-matches EVERY identifier
    # against its record blob; here the blob is a whole LiteLLM server object,
    # tool names and descriptions included, so the same rule would let a short or
    # generic name ("mcp", "search") match some unrelated field and be waved
    # through. Restricting the blob match to URL-shaped identifiers costs nothing —
    # names are already matched exactly and by slug above — and keeps a bare name
    # from borrowing approval from a server it has nothing to do with.
    return "://" in ident and ident in blob


def litellm_unapproved_integrations(identifiers: list[str]) -> list[str]:
    """Of *identifiers*, those NOT backed by an enabled LiteLLM MCP server.

    The LiteLLM analogue of ``aws_agent_registry.unapproved_integrations``, with
    the same fail-closed contract: an identifier with no matching enabled server is
    UNAPPROVED, and a catalog that could not be read raises
    :class:`RegistryQueryFailed` rather than returning a verdict.

    Reads ONLY the server listing. It deliberately never calls
    ``POST /mcp-rest/tools/call`` — a governance check must not be able to invoke
    a tool as a side effect of deciding whether that tool is allowed.
    """
    if not identifiers:
        return []
    servers = [s for s in list_litellm_servers() if _server_is_enabled(s)]
    blobs = [(s, json.dumps(s, default=str)) for s in servers]
    return [ident for ident in identifiers if ident and not any(_identifier_matches(ident, s, b) for s, b in blobs)]
