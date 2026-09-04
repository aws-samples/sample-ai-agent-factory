"""LiteLLM MCP Gateway — an ADDITIONAL gateway provider alongside AgentCore.

Workstream A of the post-demo customer feedback. A customer who already runs a
LiteLLM proxy as their MCP gateway can point a canvas Gateway node at it instead
of having the platform create an AgentCore Gateway. AgentCore remains the
default and is completely untouched: this module is only reached when
``gateway_config["gateway_provider"] == "litellm"``.

``deploy_litellm_gateway`` returns the SAME dict contract ``deploy_gateway``
returns, which is what lets the Step Functions state machine stay unchanged —
the step's inputs and outputs are identical, so ``has_gateway`` /
``HasGateway?`` / ``HasGatewayForAuth?`` all keep working. (Contrast with the
``harness`` deployment mode, which needed its own branch because it replaces the
whole runtime.)

Almost no AWS resources are created. The work is:

1. Validate the customer-supplied base URL through the EXISTING SSRF guard
   (``gateway_deployer._validate_outbound_url``) — https-only, DNS-resolved
   private/IMDS denylist. Reused, deliberately not reimplemented.
2. Mint the LiteLLM virtual key into Secrets Manager via the existing
   ``_put_connector_secret`` (``agentcore-connector/`` namespace, so no new IAM
   grant is needed) and hand back only the ARN.
3. Probe readiness over LiteLLM's real MCP REST surface —
   ``GET /v1/mcp/server`` then ``GET /mcp-rest/tools/list`` — and fail loud on
   zero tools, mirroring the AgentCore path's Bug 138 empty-tool-plane gate. A
   gateway that serves no tools is a silent wiring failure, not a success.
4. Resolve the MCP endpoint the generated agent connects to.

Auth model difference that drives everything downstream: AgentCore Gateway uses
an OAuth2 client-credentials exchange, LiteLLM uses a STATIC virtual key. We
signal that by returning ``client_info["provider"] = "litellm"``, which
``step_handlers/runtime_configure_step.py`` already branches on — it gets a third
arm emitting ``GATEWAY_AUTH_MODE=static_bearer``, and the generated agent skips
the token exchange entirely.

SECURITY: the raw virtual key is transient. It is minted into Secrets Manager as
early as possible and dropped from the config before anything is persisted or
re-emitted; only the ARN travels. No secret is ever logged, including in the
readiness-probe failure paths.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from app.services.gateway_deployer import (
    _put_connector_secret,
    _validate_outbound_url,
)

logger = logging.getLogger(__name__)

# LiteLLM's MCP surface. Confirmed read-only: LiteLLM exposes no POST/PATCH/DELETE
# for MCP *server* records (registration is Admin-UI or config.yaml), so this
# provider consumes an already-configured proxy and never tries to register one.
_SERVERS_PATH = "/v1/mcp/server"
_TOOLS_LIST_PATH = "/mcp-rest/tools/list"

# LiteLLM authenticates with a virtual key in its own header. It also accepts
# Authorization: Bearer, but x-litellm-api-key is the documented MCP-REST header
# and does not collide with a proxy in front of it.
_KEY_HEADER = "x-litellm-api-key"

# The header VALUE must carry a "Bearer " prefix, which is easy to get wrong
# because the REST probe paths tolerate a bare key. Verified against a live
# proxy: on /v1/mcp/server and /mcp-rest/tools/list both forms return 200, but
# on the MCP protocol endpoint /mcp/ the bare key falls through to a virtual-key
# DB lookup and fails, while "Bearer <key>" returns a valid initialize result.
# LiteLLM strips the prefix server-side (proxy/auth/user_api_key_auth.py
# _get_bearer_token), so prefixing is a no-op for the paths that already worked
# and is what its own mcp_debug.py documents.
_KEY_PREFIX = "Bearer "

# Header carrying a comma-separated list of MCP server aliases to scope a request
# to, when the canvas pins specific servers instead of using the aggregate.
_SERVERS_HEADER = "x-mcp-servers"

_PROBE_TIMEOUT = 15


class LiteLLMGatewayError(RuntimeError):
    """A LiteLLM gateway could not be validated. Message is always secret-free."""


# ---------------------------------------------------------------------------
# Platform default (Settings row) — mirrors services/deploy_target.py in shape
# ---------------------------------------------------------------------------

_PROVIDER_SK = "SETTING#gateway_provider"

_VALID_PROVIDERS = ("agentcore", "litellm")


def _region_default() -> str:
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))


def _settings_table():
    import boto3

    name = os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy")
    return boto3.resource("dynamodb", region_name=_region_default()).Table(name)


def default_gateway_provider() -> str:
    """The platform-wide default provider. Always ``agentcore`` unless an admin
    has explicitly set the Settings row — a missing row, an unreadable table and
    an unrecognized value all resolve to ``agentcore`` so the existing behavior
    is what you get when anything is uncertain.

    Note the pre-existing keying inconsistency this deliberately preserves:
    settings rows use ``org_id="default"`` while registry entries use
    ``"default-org"``.
    """
    env = os.environ.get("DEFAULT_GATEWAY_PROVIDER", "").strip().lower()
    if env in _VALID_PROVIDERS:
        return env
    try:
        item = _settings_table().get_item(Key={"org_id": "default", "sk": _PROVIDER_SK}).get("Item")
        value = str((item or {}).get("value", "")).strip().lower()
        return value if value in _VALID_PROVIDERS else "agentcore"
    except Exception as e:  # noqa: BLE001
        logger.info("gateway_provider setting lookup failed (default agentcore): %s", e)
        return "agentcore"


def set_default_gateway_provider(provider: str) -> None:
    """Admin setter for the platform-wide default gateway provider."""
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"gateway provider must be one of {list(_VALID_PROVIDERS)}")
    _settings_table().put_item(Item={"org_id": "default", "sk": _PROVIDER_SK, "value": provider})


def resolve_gateway_provider(gateway_config: dict | None) -> str:
    """Which provider deploys this gateway node.

    Per-agent choice on the canvas wins; the platform Settings default applies
    only when the node says nothing. An unrecognized value falls back to
    ``agentcore`` rather than failing the deploy — the model layer already
    rejects bad values at the API boundary, so reaching here with garbage means
    a legacy stored canvas, and the old behavior is the safe answer.
    """
    raw = (gateway_config or {}).get("gateway_provider") or (gateway_config or {}).get("gatewayProvider")
    provider = str(raw or "").strip().lower()
    if provider in _VALID_PROVIDERS:
        return provider
    if provider:
        logger.warning("Unrecognized gateway_provider %r — falling back to agentcore", provider)
        return "agentcore"

    fallback = default_gateway_provider()
    if fallback == "litellm" and _looks_like_an_agentcore_node(gateway_config):
        # The platform default must never silently reinterpret a canvas that was
        # built for AgentCore. A node carrying targetType/targets and no LiteLLM
        # base URL cannot be deployed as LiteLLM at all — it would fail
        # validation for a missing base URL and lose its configured targets — so
        # honoring the default here would turn an admin setting into a breaking
        # change for every pre-existing agent. That is the one thing this whole
        # feature promised not to do: it is additive, not a substitution.
        logger.info(
            "Platform default is litellm but this gateway node is AgentCore-shaped "
            "(targets configured, no litellmBaseUrl) — deploying it as agentcore."
        )
        return "agentcore"
    return fallback


def _looks_like_an_agentcore_node(gateway_config: dict | None) -> bool:
    """True when *gateway_config* is unmistakably an AgentCore Gateway node: it
    wires at least one target and names no LiteLLM base URL."""
    cfg = gateway_config or {}
    if cfg.get("litellm_base_url") or cfg.get("litellmBaseUrl"):
        return False
    return bool(cfg.get("targets") or cfg.get("target_type") or cfg.get("targetType"))


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def bearer_key_value(api_key: str) -> str:
    """The ``x-litellm-api-key`` header value for *api_key*.

    Idempotent: a key an operator already pasted with the prefix is not doubled.
    """
    key = (api_key or "").strip()
    if not key or key.startswith(_KEY_PREFIX):
        return key
    return _KEY_PREFIX + key


def _headers(api_key: str, servers: list[str] | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if api_key:
        headers[_KEY_HEADER] = bearer_key_value(api_key)
    if servers:
        headers[_SERVERS_HEADER] = ",".join(servers)
    return headers


def _get_json(url: str, api_key: str, servers: list[str] | None = None) -> object:
    """GET *url* and parse JSON, validating the URL here at the sink.

    Callers at the API boundary validate too. This does not rely on that, for two
    reasons that are not stylistic:

    * A base URL is also read back out of **persisted settings**, so the value
      arriving here did not necessarily come through the API on this request — a
      settings row written straight to DynamoDB never saw the boundary check.
    * A guard enforced only at call sites is one new call site away from being
      absent, and this is the single point every LiteLLM probe funnels through.

    It narrows but does NOT close the window between resolving a hostname and
    connecting to it: urlopen resolves DNS again, so a record that changes in
    between remains a rebinding risk. Closing that needs connect-time IP pinning,
    which urllib does not expose.
    """
    validated = _validate_outbound_url(url, label="LiteLLM URL rejected —")
    # CodeQL reports py/full-ssrf on the line below, and it is right that the URL is
    # customer-controlled: the entire feature is "point the platform at YOUR LiteLLM
    # proxy", so a path from the request body to here exists by design and cannot be
    # removed without removing the feature. What the query cannot see is that the
    # line above is a barrier — a validator that raises is not something it models.
    # So the mitigation is stated here: https-only, every resolved A/AAAA record
    # checked against the private / link-local / IMDS denylist, plus an optional host
    # allowlist. Unconditional, at the sink, covered by test_litellm_ssrf_sink.py.
    # The residual risk is DNS rebinding, documented in the docstring above; no
    # validation at this layer closes it.
    #
    # The alert is dismissed in code scanning ("won't fix") rather than suppressed
    # here: this repo uses CodeQL default setup, which ignores `# codeql[...]`
    # comments. One was tried first and had no effect — so if a future reader adds
    # one expecting it to work, that is why it doesn't.
    req = urllib.request.Request(validated, headers=_headers(api_key, servers), method="GET")
    # nosemgrep: dynamic-urllib-use-detected -- validated on the line above
    # (https-only + DNS-resolved private/IMDS denylist).
    with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8") or "null")


def _items(payload: object) -> list:
    """LiteLLM has shipped both a bare list and a wrapped object on these routes.
    Accept either rather than tying the probe to one release's envelope.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "tools", "servers", "result", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _tool_names(payload: object) -> list[str]:
    names = []
    for tool in _items(payload):
        if isinstance(tool, dict):
            name = tool.get("name") or tool.get("tool_name")
            if name:
                names.append(str(name))
        elif isinstance(tool, str):
            names.append(tool)
    return names


def _server_aliases(payload: object) -> list[str]:
    aliases = []
    for server in _items(payload):
        if not isinstance(server, dict):
            continue
        info = server.get("mcp_info") or {}
        alias = (
            server.get("alias")
            or server.get("server_name")
            or info.get("server_name")
            or server.get("server_id")
            or server.get("id")
        )
        if alias:
            aliases.append(str(alias))
    return aliases


# ---------------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------------


def resolve_mcp_url(base_url: str, servers: list[str] | None = None) -> str:
    """The streamable-HTTP MCP endpoint the generated agent connects to.

    LiteLLM serves an aggregate endpoint at ``/mcp/`` covering every server the
    key can see, and a per-server endpoint at ``/{alias}/mcp``. We use the
    per-server form ONLY when exactly one alias is pinned — with several pinned
    servers the aggregate plus the ``x-mcp-servers`` header is the documented way
    to scope, and picking one arbitrarily would silently drop the rest.
    """
    base = base_url.rstrip("/")
    if servers and len(servers) == 1:
        return f"{base}/{urllib.parse.quote(servers[0].strip('/'), safe='')}/mcp"
    return f"{base}/mcp/"


# ---------------------------------------------------------------------------
# Readiness probe
# ---------------------------------------------------------------------------


def probe_litellm_gateway(base_url: str, api_key: str, servers: list[str] | None = None) -> dict:
    """Confirm the proxy is reachable, authenticated, and actually serving tools.

    Returns ``{"servers": [...], "tools": [...]}``. Raises
    :class:`LiteLLMGatewayError` on any failure, including a successful HTTP call
    that yields ZERO tools — same fail-loud stance as the AgentCore path's
    empty-tool-plane gate, because an agent that comes up with ``tools=[]`` looks
    healthy and silently has no capabilities.

    Distinguishes failure classes deliberately: a 401/403 is a bad key and a 404
    is a wrong base URL — both are real misconfigurations the user must fix. A
    network-level error means unreachable, which for a SELF-HOSTED LiteLLM may
    simply mean it is private and not routable from this Lambda (the control
    plane has no VPC egress); the caller decides how to treat that.
    """
    base = base_url.rstrip("/")

    def _fail(stage: str, exc: Exception) -> LiteLLMGatewayError:
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code in (401, 403):
                return LiteLLMGatewayError(
                    f"LiteLLM gateway rejected the virtual key at {stage} (HTTP {exc.code}). "
                    "Check the key and that it is permitted to use MCP servers."
                )
            if exc.code == 404:
                return LiteLLMGatewayError(
                    f"LiteLLM gateway has no {stage} route (HTTP 404). Check the base URL "
                    "points at the LiteLLM proxy root and that MCP is enabled on it."
                )
            return LiteLLMGatewayError(f"LiteLLM gateway returned HTTP {exc.code} at {stage}.")
        # Never interpolate the exception's own message blindly for URL errors —
        # it can echo back the URL, which is fine, but keep it short and typed.
        return LiteLLMGatewayError(f"LiteLLM gateway is unreachable at {stage}: {type(exc).__name__}.")

    try:
        servers_payload = _get_json(base + _SERVERS_PATH, api_key)
    except Exception as e:  # noqa: BLE001
        raise _fail(_SERVERS_PATH, e) from e

    available = _server_aliases(servers_payload)

    # A pinned alias that the proxy does not serve is a wiring typo that would
    # otherwise surface as an agent with no tools. Catch it here, by name.
    if servers:
        unknown = [s for s in servers if s not in available]
        if available and unknown:
            raise LiteLLMGatewayError(
                f"LiteLLM gateway does not serve MCP server(s) {unknown}. Available: {available}."
            )

    try:
        tools_payload = _get_json(base + _TOOLS_LIST_PATH, api_key, servers)
    except Exception as e:  # noqa: BLE001
        raise _fail(_TOOLS_LIST_PATH, e) from e

    tools = _tool_names(tools_payload)
    if not tools:
        raise LiteLLMGatewayError(
            "LiteLLM gateway served 0 tools. Deploying an agent against an empty tool "
            "plane is a silent wiring failure, so this is refused. Register at least one "
            "MCP server on the proxy (LiteLLM Admin UI or config.yaml) and retry."
        )

    logger.info("LiteLLM gateway probe OK: %d server(s), %d tool(s)", len(available), len(tools))
    return {"servers": available, "tools": tools}


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------


def deploy_litellm_gateway(
    *,
    gateway_config: dict,
    region: str,
    owner_sub: str = "",
    deployment_id: str | None = None,
    **_ignored,
) -> dict:
    """Wire a canvas Gateway node to a customer-run LiteLLM MCP Gateway.

    Returns the same shape ``gateway_deployer.deploy_gateway`` returns, so every
    downstream step handler and the direct deploy path consume it unchanged.
    ``**_ignored`` absorbs the AgentCore-only kwargs (``gateway_tools``,
    ``custom_tools``, ``connectors``, …) that the call sites pass positionally by
    name; they have no meaning for an external gateway and are deliberately not
    silently half-applied.
    """
    name = gateway_config.get("name") or "litellm-gateway"
    raw_base = gateway_config.get("litellm_base_url") or gateway_config.get("litellmBaseUrl") or ""
    servers = gateway_config.get("litellm_servers") or gateway_config.get("litellmServers") or []
    if isinstance(servers, str):
        servers = [s.strip() for s in servers.split(",") if s.strip()]
    servers = [str(s) for s in servers if str(s).strip()]

    if not str(raw_base).strip():
        return {"success": False, "error": "LiteLLM gateway requires a base URL (litellm_base_url)."}

    # SSRF: the existing guard, not a new one. https-only, and rejects the 21
    # disallowed CIDRs including the 169.254.169.254 instance-metadata endpoint.
    # The label is passed so the rejection names the LiteLLM base URL instead of the
    # guard's original subject — a live deploy against http://10.0.0.5 reported
    # "OIDC discovery URL must use https scheme", which points at the wrong config.
    # No prefix on the message: the label already names the LiteLLM base URL, and
    # gateway_step re-prefixes with "Gateway deployment failed: " on the way out.
    try:
        base_url = _validate_outbound_url(str(raw_base).strip(), label="LiteLLM base URL rejected —")
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

    # The raw key may arrive inline (first deploy) or already be an ARN (redeploy).
    raw_key = gateway_config.get("litellm_api_key") or gateway_config.get("litellmApiKey") or ""
    secret_arn = gateway_config.get("litellm_api_key_ref") or gateway_config.get("litellmApiKeyRef") or ""

    api_key = str(raw_key)
    if not api_key and secret_arn:
        try:
            api_key = _read_secret_key(region, secret_arn)
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"Could not read the stored LiteLLM key: {type(e).__name__}"}

    if not api_key:
        return {
            "success": False,
            "error": "LiteLLM gateway requires a virtual key (litellm_api_key or litellm_api_key_ref).",
        }

    try:
        probe = probe_litellm_gateway(base_url, api_key, servers)
    except LiteLLMGatewayError as e:
        return {"success": False, "error": str(e)}

    # Mint the key only AFTER the probe proves it works, so a typo does not leave
    # an orphan secret behind on every failed attempt.
    if not secret_arn:
        try:
            secret_arn = _put_connector_secret(region, owner_sub, {"apiKey": api_key})
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"Could not store the LiteLLM key: {type(e).__name__}"}

    gateway_url = resolve_mcp_url(base_url, servers)
    tools = probe["tools"]

    return {
        "success": True,
        "gateway_url": gateway_url,
        # No AWS gateway exists, so there is no gatewayId/ARN. These stay None
        # rather than being faked: _record_gateway_resources and the teardown
        # dispatcher both key off truthiness, so a None means "nothing to delete"
        # instead of an unresolvable identifier.
        "gateway_id": None,
        "gateway_arn": None,
        "gateway_name": name,
        "gateway_provider": "litellm",
        "litellm_base_url": base_url,
        "litellm_servers": servers,
        # provider="litellm" is the signal runtime_configure_step branches on to
        # emit a static bearer instead of Cognito client-credentials env vars.
        "client_info": {
            "provider": "litellm",
            "api_key_ref": secret_arn,
        },
        "lambda_function_name": None,
        "custom_tool_lambdas": [],
        "custom_tool_roles": [],
        "kb_lambda_name": None,
        "connector_credential_providers": [],
        # The one AWS resource created — teardown already handles type "secret".
        "connector_secret_arns": [secret_arn],
        "connector_spec_s3_uris": [],
        # Cedar policy generation consumes these. LiteLLM tool names are not
        # AgentCore-qualified (no "target___tool" prefix), so pass them through
        # as-is and let the policy step scope on the real names.
        "qualified_tools": tools,
        "expected_tool_count": len(tools),
    }


def _read_secret_key(region: str, secret_arn: str) -> str:
    """Read back a previously minted virtual key. Never logged."""
    import boto3

    sm = boto3.client("secretsmanager", region_name=region)
    payload = json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])
    return str(payload.get("apiKey") or "")
