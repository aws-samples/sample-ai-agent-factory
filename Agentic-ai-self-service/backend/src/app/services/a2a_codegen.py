"""A2A (Agent-to-Agent) protocol code generator — Gap 3A.

Emits a SELF-CONTAINED Python agent for the AgentCore Runtime that:

  1. Builds a Strands ``Agent`` with a ``call_a2a_peer`` ``@tool``.
  2. Serves an A2A *agent card* as JSON at ``/.well-known/agent-card.json``
     via a Starlette route registered on the ``BedrockAgentCoreApp`` (which
     subclasses ``starlette.applications.Starlette``, so ``app.add_route`` is
     available directly).
  3. Exposes a ``call_a2a_peer(peer_url, message)`` tool that discovers a peer's
     agent card, extracts its invoke ``url`` and POSTs the message via ``httpx``.

WHY SELF-CONTAINED (deviation from the literal task text)
---------------------------------------------------------
The deployment dependency bundle (``strands-mcp.zip``) ships the Strands/Bedrock
A2A *glue* layers (``strands.multiagent.a2a``, ``strands_tools.a2a_client``,
``bedrock_agentcore.runtime.a2a``) but NOT the top-level ``a2a`` (a2a-sdk)
package. Every one of those glue modules hard-imports ``from a2a... import ...``
and ``bedrock_agentcore.runtime.__getattr__`` raises ``ImportError`` when a2a-sdk
is absent. So ``serve_a2a`` / ``A2AServer`` / ``A2AClientToolProvider`` would
ImportError at runtime. ``httpx``, ``starlette`` and ``uvicorn`` ARE bundled.

Therefore the generated agent serves over the HTTP ``serverProtocol`` (the
default ``BedrockAgentCoreApp`` entrypoint) and implements an A2A *interop*
layer (agent-card discovery + JSON message POST) entirely with stdlib + httpx —
NO ``from a2a`` import anywhere in the output.

SSRF GUARD (Critic Finding 2)
-----------------------------
``call_a2a_peer`` enforces, before any network call:
  * scheme must be http/https;
  * exact-host membership in an env-injected ``A2A_PEER_ALLOWLIST``
    (fail-closed: empty/absent allowlist => ALL peers refused);
  * a DNS-resolve + private/link-local/IMDS CIDR denylist (an inlined copy of
    gateway_deployer's ``_DISALLOWED_NETWORKS``).

Bug 125 ordering discipline: every helper (denylist, allowlist parse, SSRF
check, the agent-card route handler) is DEFINED BEFORE the ``@tool`` and before
``@app.entrypoint``, and uses aliased local imports + env-driven region so the
module is import-safe on any template. Config values are escaped through the
same sanitizers code_generator.py uses to prevent f-string injection.
"""

from typing import Optional

from app.services.code_generator import (
    _escape_triple_quotes,
    _sanitize_string_literal,
)


def _emit_str_list_literal(values) -> str:
    """Emit a Python list literal of double-quoted, injection-safe strings.

    Each element is run through ``_sanitize_string_literal`` so quotes,
    backslashes and newlines cannot break out of the surrounding f-string.
    """
    if not values:
        return "[]"
    items = []
    for v in values:
        if v is None:
            continue
        items.append('"' + _sanitize_string_literal(str(v)) + '"')
    return "[" + ", ".join(items) + "]"


def _generate_a2a_agent(
    system_prompt: str,
    model_id: str,
    region: str,
    peer_config: Optional[dict] = None,
) -> str:
    """Return Python source for a self-contained A2A interop agent.

    Args:
        system_prompt: Already triple-quote-escaped system prompt (caller in
            generate_agent_code escapes it via ``_escape_triple_quotes``).
        model_id: Sanitized cross-region model id.
        region: AWS region string.
        peer_config: Optional dict with ``capabilities`` (list[str]),
            ``advertised_description`` (str) and ``peer_allowlist`` (list[str]).
            These are *defaults* baked into the source; at runtime the
            corresponding ``A2A_*`` env vars (injected by runtime_configure_step)
            take precedence so the canvas config drives the live agent card.

    The generated module is import-safe against strands / bedrock_agentcore /
    starlette / httpx stubs and contains NO ``from a2a`` (a2a-sdk) import.
    """
    peer_config = peer_config or {}
    capabilities = peer_config.get("capabilities") or []
    advertised_description = peer_config.get("advertised_description") or peer_config.get(
        "advertisedDescription"
    ) or "An AgentCore agent exposing the A2A interop protocol."
    peer_allowlist = peer_config.get("peer_allowlist") or peer_config.get("peerAllowlist") or []

    # Injection-safe literals baked in as fallback defaults.
    caps_literal = _emit_str_list_literal([str(c)[:64] for c in capabilities][:32])
    allow_literal = _emit_str_list_literal([str(u)[:512] for u in peer_allowlist][:64])
    # advertised_description goes inside a """...""" block — escape triple quotes.
    desc_escaped = _escape_triple_quotes(str(advertised_description)[:512])

    return f'''"""AgentCore Runtime - A2A (Agent-to-Agent) Interop Agent

Self-contained A2A interop layer: serves an agent card at
/.well-known/agent-card.json and exposes a call_a2a_peer tool that discovers a
peer's card and POSTs messages to it. Uses BedrockAgentCoreApp (HTTP
serverProtocol) + httpx + stdlib only — NO a2a-sdk import (it is not bundled).
"""
import os
import json
import ipaddress
import socket
import urllib.parse

import httpx
from starlette.responses import JSONResponse

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """{system_prompt}"""
MODEL_ID = os.environ.get("MODEL_ID", "{model_id}")
REGION = os.environ.get("AWS_REGION", "{region}")

# Agent-card metadata. Env vars (injected by runtime_configure_step) override
# the baked-in canvas defaults so the live card reflects the deployed config.
A2A_ADVERTISED_DESCRIPTION = os.environ.get(
    "A2A_ADVERTISED_DESCRIPTION",
    """{desc_escaped}""",
)
A2A_AGENT_NAME = os.environ.get("A2A_AGENT_NAME", "agentcore-a2a-agent")
A2A_AGENT_VERSION = os.environ.get("A2A_AGENT_VERSION", "1.0.0")
# Default capabilities baked from canvas config; A2A_CAPABILITIES env overrides.
_DEFAULT_CAPABILITIES = {caps_literal}
# Default peer allowlist baked from canvas config; A2A_PEER_ALLOWLIST overrides.
# SECURITY: empty allowlist => call_a2a_peer refuses ALL peers (fail-closed).
_DEFAULT_PEER_ALLOWLIST = {allow_literal}

# HTTP timeouts (seconds). Kept strict to bound the DNS-rebinding race window.
_A2A_HTTP_TIMEOUT = 12.0


# ── SSRF denylist (inlined from gateway_deployer._DISALLOWED_NETWORKS) ──
# Built once at module import. Covers loopback, link-local (IMDS at
# 169.254.169.254 + Lambda creds at 169.254.170.2), RFC1918, CGNAT, multicast,
# "this network", and IPv4/IPv6 reserved space.
_A2A_DISALLOWED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "::/128",
        "::ffff:0:0/96",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "2001:db8::/32",
    )
)


def _a2a_capabilities():
    """Return capabilities from env (comma-separated) or baked defaults."""
    raw = os.environ.get("A2A_CAPABILITIES", "").strip()
    if raw:
        return [c.strip() for c in raw.split(",") if c.strip()][:32]
    return list(_DEFAULT_CAPABILITIES)


def _a2a_peer_allowlist():
    """Return the peer allowlist from env (comma-separated) or baked defaults.

    SECURITY: an empty list means EVERY peer is refused (fail-closed default).
    """
    raw = os.environ.get("A2A_PEER_ALLOWLIST", "").strip()
    if raw:
        return [h.strip().lower() for h in raw.split(",") if h.strip()][:64]
    return [h.strip().lower() for h in _DEFAULT_PEER_ALLOWLIST if str(h).strip()]


def _a2a_self_url():
    """Best-effort self invoke URL advertised in the agent card."""
    return (
        os.environ.get("AGENTCORE_RUNTIME_URL")
        or os.environ.get("A2A_SELF_URL")
        or "/invocations"
    )


def _build_agent_card():
    """Build the A2A agent card as a plain dict (no a2a-sdk import)."""
    return {{
        "name": A2A_AGENT_NAME,
        "description": A2A_ADVERTISED_DESCRIPTION,
        "url": _a2a_self_url(),
        "version": A2A_AGENT_VERSION,
        "protocolVersion": "0.2.0",
        "capabilities": _a2a_capabilities(),
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {{
                "id": cap,
                "name": cap,
                "description": cap,
                "tags": ["a2a"],
            }}
            for cap in _a2a_capabilities()
        ],
    }}


async def _agent_card_route(request):
    """Serve GET /.well-known/agent-card.json."""
    return JSONResponse(_build_agent_card())


# Register the agent-card route on the underlying Starlette app. The route is
# added BEFORE any request is served. BedrockAgentCoreApp subclasses Starlette,
# so add_route is available directly.
try:
    app.add_route(
        "/.well-known/agent-card.json",
        _agent_card_route,
        methods=["GET"],
    )
except Exception as _route_err:  # pragma: no cover - defensive
    import logging as _a2a_logging
    _a2a_logging.getLogger("agentcore.a2a").warning(
        "Could not register A2A agent-card route: %s", _route_err
    )


def _a2a_check_peer_host(host):
    """SSRF guard: validate ``host`` against the allowlist + DNS denylist.

    Returns an error string if the host is refused, or None if it is allowed.
    Mirrors gateway_deployer._validate_discovery_url's denylist logic.
    """
    if not host:
        return "peer_url has no host component"
    host = host.lower()
    allowlist = _a2a_peer_allowlist()
    # Fail-closed: with no allowlist configured, refuse every peer.
    if not allowlist:
        return (
            "no A2A_PEER_ALLOWLIST configured — all peers are refused "
            "(fail-closed). Add the peer host to the A2A node allowlist."
        )
    if host not in allowlist:
        return "peer host '%s' is not on the A2A_PEER_ALLOWLIST" % host

    # Resolve every A/AAAA record under a strict timeout, then check the
    # denylist so an allowlisted hostname cannot point at a private IP.
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5)
    try:
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except (socket.gaierror, socket.timeout, OSError) as e:
            return "peer host '%s' could not be resolved: %s" % (host, e)
    finally:
        socket.setdefaulttimeout(prev_timeout)
    if not infos:
        return "peer host '%s' returned no DNS records" % host
    for info in infos:
        ip_str = info[4][0]
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as e:
            return "peer host '%s' resolved to unparseable IP '%s': %s" % (host, ip_str, e)
        for net in _A2A_DISALLOWED_NETWORKS:
            if ip_obj.version != net.version:
                continue
            if ip_obj in net:
                return "peer host '%s' resolves to a disallowed IP (%s in %s)" % (
                    host,
                    ip_str,
                    net,
                )
    return None


@tool
def call_a2a_peer(peer_url: str, message: str) -> str:
    """Call another A2A agent (peer). Discovers the peer's agent card at
    ``<peer_url>/.well-known/agent-card.json``, reads its invoke ``url`` and
    POSTs the message to it, returning the peer's JSON response.

    Use this when the user asks you to delegate to, ask, or collaborate with
    another agent. ``peer_url`` is the base URL of the peer A2A agent.

    SECURITY: the peer host must be on the configured allowlist and must not
    resolve to a private/link-local/metadata IP, or the call is refused.
    """
    if not peer_url or not isinstance(peer_url, str):
        return json.dumps({{"status": "ERROR", "error": "peer_url is required"}})
    parsed = urllib.parse.urlparse(peer_url)
    if parsed.scheme != "https":
        return json.dumps(
            {{"status": "ERROR", "error": "peer_url must use https scheme"}}
        )
    host = parsed.hostname or ""
    block_reason = _a2a_check_peer_host(host)
    if block_reason is not None:
        return json.dumps({{"status": "BLOCKED", "error": block_reason}})

    base = peer_url.rstrip("/")
    card_url = base + "/.well-known/agent-card.json"
    try:
        with httpx.Client(timeout=_A2A_HTTP_TIMEOUT, follow_redirects=False) as client:
            card_resp = client.get(card_url)
            card_resp.raise_for_status()
            card = card_resp.json()
    except Exception as e:  # noqa: BLE001
        return json.dumps(
            {{"status": "ERROR", "error": "could not fetch peer agent card: %s" % e}}
        )

    invoke_url = (card or {{}}).get("url") or base + "/invocations"
    # Re-validate the invoke endpoint (the card may point elsewhere). It MUST be
    # https and pass the host denylist — a card pointing at http or an internal
    # host is fail-closed BLOCKED, never silently followed.
    invoke_parsed = urllib.parse.urlparse(invoke_url)
    if invoke_parsed.scheme != "https":
        return json.dumps(
            {{"status": "BLOCKED", "error": "peer invoke url must use https scheme"}}
        )
    invoke_block = _a2a_check_peer_host(invoke_parsed.hostname or "")
    if invoke_block is not None:
        return json.dumps({{"status": "BLOCKED", "error": invoke_block}})
    elif not invoke_parsed.scheme:
        # Relative URL from the card — resolve against the validated base.
        invoke_url = base + "/" + invoke_url.lstrip("/")
    else:
        return json.dumps(
            {{"status": "ERROR", "error": "peer card invoke url uses an unsupported scheme"}}
        )

    payload = {{"prompt": message, "message": message}}
    try:
        with httpx.Client(timeout=_A2A_HTTP_TIMEOUT, follow_redirects=False) as client:
            resp = client.post(invoke_url, json=payload)
            resp.raise_for_status()
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = {{"text": resp.text}}
    except Exception as e:  # noqa: BLE001
        return json.dumps(
            {{"status": "ERROR", "error": "peer invocation failed: %s" % e}}
        )
    return json.dumps({{"status": "OK", "peer_url": peer_url, "response": body}})


_model = None
_agent = None


def _get_agent():
    global _model, _agent
    if _agent is None:
        if _model is None:
            _model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
        _agent = Agent(model=_model, system_prompt=SYSTEM_PROMPT, tools=[call_a2a_peer])
    return _agent


@app.entrypoint
def invoke(payload):
    """Process a user prompt; the agent may call A2A peers via call_a2a_peer."""
    message = payload.get("prompt", "Hello")
    result = _get_agent()(message)
    return {{"response": str(result)}}


if __name__ == "__main__":
    app.run()
'''
