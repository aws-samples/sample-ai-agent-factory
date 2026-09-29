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
same sanitizers code_generator.py uses, so a value cannot terminate the literal it
is embedded in. Not "to prevent f-string injection" -- that was the old framing and
it is wrong: these templates are f-strings evaluated here, and an interpolated value
is never rescanned for placeholders. Believing otherwise is what led the shared
sanitizer to double curly braces and silently corrupt every prompt containing JSON.
"""

from app.services.code_generator import (
    _as_triple_quoted_body,
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
    peer_config: dict | None = None,
) -> str:
    """Return Python source for a self-contained A2A interop agent.

    Args:
        system_prompt: Already triple-quote-escaped system prompt (caller in
            generate_agent_code escapes it via ``_as_triple_quoted_body``).
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
    advertised_description = (
        peer_config.get("advertised_description")
        or peer_config.get("advertisedDescription")
        or "An AgentCore agent exposing the A2A interop protocol."
    )
    peer_allowlist = peer_config.get("peer_allowlist") or peer_config.get("peerAllowlist") or []

    # Injection-safe literals baked in as fallback defaults.
    caps_literal = _emit_str_list_literal([str(c)[:64] for c in capabilities][:32])
    allow_literal = _emit_str_list_literal([str(u)[:512] for u in peer_allowlist][:64])
    # advertised_description goes inside a """...""" block, so every quote has to be
    # escaped, not just a run of three: a description ending in one closed the literal
    # early and the emitted module would not import. See _as_triple_quoted_body.
    desc_escaped = _as_triple_quoted_body(str(advertised_description)[:512])

    return f'''"""AgentCore Runtime - A2A (Agent-to-Agent) Interop Agent

Self-contained A2A interop layer: serves an agent card at
/.well-known/agent-card.json and exposes a call_a2a_peer tool that discovers a
peer's card and POSTs messages to it. Uses BedrockAgentCoreApp (HTTP
serverProtocol) + httpx + stdlib only — NO a2a-sdk import (it is not bundled).
"""
import os
import json
import ipaddress
import re
import socket
import urllib.parse
import uuid

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


def _a2a_refused_redirect(resp):
    """The refusal reason if ``resp`` is a redirect, else None.

    Redirects are never followed, and the reason is this exact scenario: a peer host that passes the allowlist and the IP denylist answers
    302 with a Location at the metadata service, so every check that mattered ran
    against a URL the client was about to abandon. ``follow_redirects=False`` means the
    second request is never made -- that part was already right, and measured on both
    the card hop and the invoke hop.

    What was wrong is what the refusal was called. httpx surfaces the 3xx as a generic
    HTTPStatusError, so it came back as "peer invocation failed", indistinguishable from
    a flaky peer, and an operator reading that log line sees bad weather rather than an
    SSRF attempt against their agent. Classify it as BLOCKED and quote the Location that
    was refused. Fail-closed is not enough on its own if the refusal is invisible.
    """
    if 300 <= resp.status_code < 400:
        return "peer answered %d redirecting to %r, which is not followed" % (
            resp.status_code,
            resp.headers.get("location", "<none>"),
        )
    return None


# An AgentCore runtime peer is not reachable the way every other A2A peer is, and this is
# measured against a live deployed peer, not inferred:
#
#   GET  <data-plane>/runtimes/<arn>/.well-known/agent-card.json  -> 404 Not Found
#   POST <data-plane>/runtimes/<arn>/invocations       (unsigned)  -> 403 Forbidden,
#                                                    {{"message": "Missing Authentication
#                                                      Token"}}
#
# Two INDEPENDENT walls, which is the part that determines the design. Signing the POST
# and changing nothing else would still die at the card fetch, because discovery has no
# unauthenticated route at all -- the well-known path is simply not served by the data
# plane. So for a peer named by its runtime ARN this tool does not use httpx and does not
# attempt discovery: the ARN already names the invoke target, and InvokeAgentRuntime signs
# with the container's own task role.
#
# That the 403 is an auth decision rather than a catch-all is also measured, and needed a
# control: "Missing Authentication Token" is equally what AWS says for an unrouted path,
# so the body alone could not distinguish them. The identical JSON-RPC envelope delivered
# to the same runtime through a SIGNED invoke returns 200. The route exists.
#
# Not supported, deliberately: resolving a peer's card first. Our own exports serve it as
# the JSON-RPC method agent/getAuthenticatedExtendedCard, and GetAgentCard is refused for
# an HTTP-declared runtime (which every export of this generator is -- see the README's
# A2A section for why). A discovery hop that can only fail is not worth making when the
# ARN is already sufficient to send a message.
# Deliberately written with no backslash escapes at all: a hyphen last in a character
# class is already literal, and [0-9] says the same thing as the digit shorthand. This
# whole module is emitted through the generator's own f-string template, so a backslash
# escape Python does not recognise is a SyntaxWarning in the EMITTER -- reported at the
# line where the template literal starts, not here, which makes it a puzzle to locate.
# That applies to comments too: a comment inside the template is still part of the
# string literal, so writing the shorthand even to warn about it re-triggers it. Which
# is how this comment came to be phrased the long way round.
_A2A_PEER_ARN_RE = re.compile(
    "^arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:[0-9]{{12}}:runtime/[A-Za-z0-9_-]+"
    "(?:/runtime-endpoint/[A-Za-z0-9_-]+)?$"
)


def _a2a_agentcore_peer_arn(peer_url):
    """The peer's runtime ARN if ``peer_url`` names an AgentCore runtime, else None.

    Accepts the bare ARN, and also the data-plane URL form, because that is the form an
    AgentCore runtime's own agent card advertises: its ``url`` comes back as an absolute
    https URL on ``bedrock-agentcore.<region>.amazonaws.com`` whose path carries the
    url-encoded ARN. Recognising only the bare ARN would mean a card obtained by any other
    means still sent an unsigned POST straight into the 403.
    """
    candidate = (peer_url or "").strip()
    if candidate.startswith("arn:"):
        return candidate if _A2A_PEER_ARN_RE.fullmatch(candidate) else None
    parsed = urllib.parse.urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if not host.startswith("bedrock-agentcore.") or not host.endswith(".amazonaws.com"):
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2 or segments[0] != "runtimes":
        return None
    # ``fullmatch``, not ``match``, and this is the copy where the difference bites. A
    # trailing dollar in Python also matches just before a newline at the end of the string,
    # so ``match`` accepts an ARN with a trailing newline -- and this branch url-decodes a
    # path segment, so a peer url ending the segment in the percent-encoded form of a
    # newline reaches it. The bare-ARN branch above is stripped and so was never exposed;
    # this one was.
    #
    # The same recognition runs at export time to build the IAM grant, and there
    # CloudFormation trims trailing whitespace from a parameter value before validating it,
    # so the newline was normalized away and nothing looked wrong. Nothing trims here: the
    # string with the newline still in it is what would be handed to
    # ``invoke_agent_runtime`` as the runtime ARN and compared against the allowlist. A
    # trailing dollar is not the full match it reads as.
    arn = urllib.parse.unquote(segments[1])
    return arn if _A2A_PEER_ARN_RE.fullmatch(arn) else None


def _a2a_check_peer_arn(arn):
    """Allowlist check for an AgentCore peer. Error string if refused, else None.

    The host denylist cannot apply here -- there is no customer-controlled host to
    resolve, the endpoint is an AWS service endpoint derived from the ARN's own region, so
    the SSRF class this tool guards against does not exist on this path. What replaces it
    is a stricter check than the host path gets: the ARN must appear on the allowlist
    VERBATIM. No wildcards, no prefix matching, no per-account rule -- a peer is either
    named or refused.

    Fail-closed identically to the host path: with no allowlist, every peer is refused.
    """
    allowlist = _a2a_peer_allowlist()
    if not allowlist:
        return (
            "no A2A_PEER_ALLOWLIST configured — all peers are refused "
            "(fail-closed). Add the peer runtime ARN to the A2A node allowlist."
        )
    # _a2a_peer_allowlist lowercases, which is right for hostnames and wrong for an ARN:
    # the RuntimeId segment is case-sensitive. Compare case-insensitively so an ARN a
    # customer pasted in its real mixed case still matches its lowercased allowlist entry.
    #
    # An entry is also accepted when it is the SAME runtime written as its data-plane URL,
    # because that is the form the peer's own agent card advertises and so the form a
    # customer is most likely to copy. Resolving the entry through the same function that
    # resolved the request is not a loosening: both sides collapse to one exact ARN, and an
    # entry that names no runtime at all (a bare hostname, say) resolves to None and still
    # matches nothing. Without this the refusal read "ARN ... is not on the allowlist" to a
    # customer looking at an allowlist that plainly contained that runtime.
    wanted = arn.lower()
    for entry in allowlist:
        if entry == wanted:
            return None
        resolved = _a2a_agentcore_peer_arn(entry)
        if resolved is not None and resolved.lower() == wanted:
            return None
    return "peer runtime ARN '%s' is not on the A2A_PEER_ALLOWLIST" % arn


def _a2a_invoke_agentcore_peer(arn, payload):
    """InvokeAgentRuntime on an AgentCore peer. Dict on success, (status, error) on not.

    boto3 is imported here rather than at module scope so an export that never calls an
    AgentCore peer carries no import-time dependency on it.
    """
    try:
        import boto3

        client = boto3.client("bedrock-agentcore", region_name=arn.split(":")[3])
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
            runtimeSessionId=str(uuid.uuid4()),
        )
    except Exception as e:  # noqa: BLE001
        # An AccessDenied here is almost always the A2APeerRuntimeArns stack parameter --
        # the grant is opt-in by design, so an export does not silently acquire the right
        # to invoke other agents -- but this code cannot see the role's policy and so must
        # not assert that as the cause. It names the likely fix and then hands over the
        # service's own words, because those words are the diagnosis: the denial says
        # `on resource: <arn>` when the endpoint ARN is the one missing and
        # `on resource: <arn>/runtime-endpoint/DEFAULT` when the runtime ARN is, and the
        # parameter needs BOTH spellings per peer.
        #
        # 600 characters, and the number has a history worth keeping. It was 200, which cut
        # a real denial off mid-token inside the caller's own role name and threw the `on
        # resource:` clause away entirely -- the only part that says which of the two ARNs to
        # add. Truncating a diagnostic before its diagnosis is worse than not printing it.
        #
        # It was then 500, justified against a hand-transcribed "real" message of 476
        # characters. A live capture measured 501: the transcription had guessed the wrong
        # shape for the one field that varies. The principal is
        # `assumed-role/AgentCoreRuntime-<stack>/BedrockAgentCore-<uuid>`, so the message
        # length is a function of the *recipient's stack name* -- 496 for `acme-agent`, 523
        # for `acme-prod-customer-support-agent-euc1` -- and there is no sample to fit to.
        # 600 leaves about 100 characters over the longest plausible stack name while still
        # bounding an unbounded service string. The clause this exists to preserve sits
        # around index 290 either way.
        text = str(e)
        if "AccessDenied" in text or "not authorized" in text:
            return (
                "ERROR",
                "not authorized to invoke peer runtime %s. Check the A2APeerRuntimeArns "
                "stack parameter: it must name BOTH the peer's runtime ARN and that "
                "runtime's endpoint ARN (<arn>,<arn>/runtime-endpoint/DEFAULT), because "
                "InvokeAgentRuntime is authorized against both. The `on resource:` ARN in "
                "the message below is the one that is missing. (%s)" % (arn, text[:600]),
            )
        return ("ERROR", "peer invocation failed: %s" % text[:300])

    try:
        raw = resp.get("response")
        body = raw.read() if hasattr(raw, "read") else raw
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        if not body:
            return ("ERROR", "peer returned an empty response")
        try:
            return json.loads(body)
        except Exception:  # noqa: BLE001
            return {{"text": body}}
    except Exception as e:  # noqa: BLE001
        return ("ERROR", "could not read peer response: %s" % e)


def _a2a_exchange(peer_url, message, send):
    """Run the A2A message exchange over ``send``, and interpret what comes back.

    ``send(payload)`` is the transport: an unsigned httpx POST for an ordinary A2A peer,
    a SigV4-signed InvokeAgentRuntime for an AgentCore one. It returns a dict on success
    and a (status, error) pair on failure. Keeping the envelope and the interpretation
    here, once, is what stops the two transports drifting -- the legacy retry in
    particular is easy to implement on one path and forget on the other, and a peer's
    JSON-RPC-ness has nothing to do with how the bytes got there.
    """
    # A peer that publishes an A2A agent card is an A2A agent, and A2A over HTTP is
    # JSON-RPC, so that is what goes on the wire. The previous shape -- a bare
    # {{"prompt": ...}} -- is what THIS generator's own older exports understood and
    # what a spec peer rejects, so it stays as a fallback rather than as the default.
    payload = {{
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {{
            "message": {{
                "role": "user",
                "messageId": str(uuid.uuid4()),
                "parts": [{{"kind": "text", "text": message}}],
            }}
        }},
    }}
    body = send(payload)
    if not isinstance(body, dict):
        # (status, error) -- BLOCKED for a refused redirect, ERROR for a transport
        # failure. Keeping them distinct is the whole point of the pair.
        return json.dumps({{"status": body[0], "error": body[1]}})
    if "error" in body or "result" in body or "jsonrpc" in body:
        # A real JSON-RPC response, whichever way it went. An error here is the peer's
        # considered refusal and is returned as such -- retrying it as a plain prompt
        # would turn a clear "method not found" into an answer to a different question.
        if "error" in body:
            return json.dumps({{"status": "ERROR", "peer_url": peer_url, "error": body["error"]}})
        result = body.get("result")
        text = _a2a_text_from_message(result) if isinstance(result, dict) else ""
        return json.dumps(
            {{"status": "OK", "peer_url": peer_url, "response": text or result}}
        )

    # No jsonrpc, no result, no error: the peer is not speaking JSON-RPC at all. That is
    # an export of this generator from before it did, and it has just answered the
    # literal default "Hello" rather than the message -- HTTP 200, nothing in the body
    # to say so. Measured. One bounded retry in the shape that peer does understand.
    legacy = send({{"prompt": message, "message": message}})
    if not isinstance(legacy, dict):
        return json.dumps({{"status": legacy[0], "error": legacy[1]}})
    return json.dumps(
        {{
            "status": "OK",
            "peer_url": peer_url,
            "response": legacy,
            "note": "peer does not speak A2A JSON-RPC; retried as a plain prompt",
        }}
    )


def _a2a_http_failure(resp):
    """The error string for a non-2xx ``resp``, including a bounded body snippet.

    httpx's own raise_for_status message carries the status and the URL and throws the
    body away. That is what a customer calling an AgentCore peer used to get: "Client
    error '403 Forbidden'" and nothing about why. The peer's body is the part that says
    why -- {{"message": "Missing Authentication Token"}} for an unsigned request, which
    is a completely different remedy from a 403 for a denied IAM action -- so a bounded
    prefix of it goes into the returned error.

    Bounded, and RETURNED rather than logged. Response
    payloads are customer content and must not reach service logs; this string is the
    tool's return value to the agent that asked for the call, which is where it belongs.
    """
    snippet = ""
    try:
        snippet = (resp.text or "")[:200]
    except Exception:  # noqa: BLE001
        snippet = ""
    return "peer returned HTTP %d%s" % (
        resp.status_code,
        (": " + snippet) if snippet else "",
    )


def _a2a_post(url, payload):
    """POST ``payload`` to ``url``; return the decoded body, or a (status, error) pair.

    Returns a dict on success and a 2-tuple on failure, so a caller can tell the two
    apart -- and tell a refusal from a transport failure -- without exception handling
    of its own. Nothing here logs the payload or the body:
    those are customer content and do not belong in logs, and the message being relayed
    to a peer is the user's.
    """
    try:
        with httpx.Client(timeout=_A2A_HTTP_TIMEOUT, follow_redirects=False) as client:
            resp = client.post(url, json=payload)
            refused = _a2a_refused_redirect(resp)
            if refused is not None:
                return ("BLOCKED", refused)
            if resp.status_code >= 400:
                return ("ERROR", _a2a_http_failure(resp))
            try:
                return resp.json()
            except Exception:  # noqa: BLE001
                return {{"text": resp.text}}
    except Exception as e:  # noqa: BLE001
        return ("ERROR", "peer invocation failed: %s" % e)


@tool
def call_a2a_peer(peer_url: str, message: str) -> str:
    """Call another A2A agent (peer) and return its JSON response.

    Use this when the user asks you to delegate to, ask, or collaborate with
    another agent. ``peer_url`` accepts either form of peer:

    * The base URL of an A2A agent, e.g. ``https://agent.example.com``. Its agent
      card is discovered at ``<peer_url>/.well-known/agent-card.json`` and the
      message is POSTed to the invoke ``url`` the card advertises.
    * An AgentCore runtime, named by its ARN
      (``arn:aws:bedrock-agentcore:<region>:<account>:runtime/<id>``). The message
      is delivered by a signed InvokeAgentRuntime call, because an AgentCore
      runtime serves neither an unauthenticated card nor an unsigned invoke.

    SECURITY: every peer must be on the configured allowlist, which is fail-closed
    -- with no allowlist, every peer is refused. A URL peer's host must also not
    resolve to a private, link-local or metadata IP, and redirects are never
    followed. An ARN peer must additionally be granted in the stack's
    ``A2APeerRuntimeArns`` parameter, which is what scopes the runtime's IAM
    permission to that specific peer.
    """
    if not peer_url or not isinstance(peer_url, str):
        return json.dumps({{"status": "ERROR", "error": "peer_url is required"}})

    # An AgentCore peer takes a different road entirely: signed, and with no discovery
    # hop, because its well-known card path 404s and its invoke path is SigV4-only. Both
    # measured -- see _a2a_agentcore_peer_arn. Checked FIRST because a bare ARN has no
    # scheme and the https check below would refuse it, which is the same class of
    # ordering mistake that once made this whole tool unreachable.
    peer_arn = _a2a_agentcore_peer_arn(peer_url)
    if peer_arn is not None:
        arn_block = _a2a_check_peer_arn(peer_arn)
        if arn_block is not None:
            return json.dumps({{"status": "BLOCKED", "error": arn_block}})
        return _a2a_exchange(
            peer_url, message, lambda p: _a2a_invoke_agentcore_peer(peer_arn, p)
        )

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
            refused = _a2a_refused_redirect(card_resp)
            if refused is not None:
                return json.dumps({{"status": "BLOCKED", "error": refused}})
            card_resp.raise_for_status()
            card = card_resp.json()
    except Exception as e:  # noqa: BLE001
        return json.dumps(
            {{"status": "ERROR", "error": "could not fetch peer agent card: %s" % e}}
        )

    invoke_url = (card or {{}}).get("url") or base + "/invocations"
    # Resolve a relative url BEFORE validating the scheme, because the two orders are
    # not equivalent and the other one is unreachable. A relative url has scheme "", so
    # a `scheme != "https"` check ahead of the join refuses it. Two ways a real peer
    # produces one, both measured live: a card carrying no `url` key at all, which falls
    # back to the `base + "/invocations"` on the line above, and a card whose `url` is
    # relative, which is spec-legal and ordinary for a peer that is not an AgentCore
    # runtime.
    #
    # Do NOT justify this ordering by our own exports, which is what an earlier version
    # of this comment did. A deployed AgentCore runtime's card comes back ABSOLUTE even
    # when the template declares neither AGENTCORE_RUNTIME_URL nor A2A_SELF_URL
    # (measured: both absent from template.yaml and from the control plane's view of the
    # runtime's environment, card url absolute anyway). _a2a_self_url reads only those
    # two variables and there is no third source in this file, so the service itself
    # injects one into the container, and the "/invocations" fallback below is dead code
    # on a real AgentCore runtime however true it is of this module read in isolation.
    #
    # An earlier version validated first and then had the join in an `elif not scheme` arm the
    # validation had already made unreachable, with a final `else` that returned
    # "unsupported scheme" on the one path that was actually correct. Net effect,
    # measured: no input existed for which this tool reached its POST. Relative first,
    # then https, then the host denylist -- so a card pointing at http or at an
    # internal host is still fail-closed BLOCKED and never silently followed.
    #
    # The join is deliberately string concatenation and NOT urllib.parse.urljoin, which
    # is what it looks like it wants to be. urljoin treats a protocol-relative url as a
    # host: urljoin("https://example.com/", "//evil.example.net/x") is
    # "https://evil.example.net/x", so a peer's card could move the request to a host
    # the allowlist never saw. Concatenating after lstrip("/") turns the same input into
    # a path on the validated base instead. Re-checking the host after the join is the
    # backstop that makes either form safe, and it is the order that matters: check
    # first and join second and the backstop guards the wrong string.
    invoke_parsed = urllib.parse.urlparse(invoke_url)
    if not invoke_parsed.scheme:
        invoke_url = base + "/" + invoke_url.lstrip("/")
        invoke_parsed = urllib.parse.urlparse(invoke_url)
    if invoke_parsed.scheme != "https":
        return json.dumps(
            {{"status": "BLOCKED", "error": "peer invoke url must use https scheme"}}
        )
    invoke_block = _a2a_check_peer_host(invoke_parsed.hostname or "")
    if invoke_block is not None:
        return json.dumps({{"status": "BLOCKED", "error": invoke_block}})

    return _a2a_exchange(peer_url, message, lambda p: _a2a_post(invoke_url, p))


_model = None
_agent = None


def _get_agent():
    global _model, _agent
    if _agent is None:
        if _model is None:
            _model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
        _agent = Agent(model=_model, system_prompt=SYSTEM_PROMPT, tools=[call_a2a_peer])
    return _agent


_A2A_JSONRPC_VERSION = "2.0"


def _a2a_is_jsonrpc(payload):
    """True if the caller sent a JSON-RPC envelope rather than a plain prompt.

    Keyed on the envelope's own markers, not on whether we can serve it: a request
    recognisable as JSON-RPC that we cannot serve has to come back as a JSON-RPC
    error, never be treated as a plain payload. Measured live before this existed --
    a spec-compliant message/send returned HTTP 200 and the agent ran on the literal
    default "Hello", because params.message.parts[].text was never read. A silent
    wrong answer is worse for a peer than a refusal.

    ``method`` alone counts only when there is no ``prompt``, so that an existing
    plain caller that happens to send an unrelated ``method`` field keeps working
    instead of being refused as malformed JSON-RPC. A real A2A peer always sends
    ``jsonrpc``, so nothing in the spec path depends on the looser half.
    """
    if not isinstance(payload, dict):
        return False
    if "jsonrpc" in payload:
        return True
    return isinstance(payload.get("method"), str) and "prompt" not in payload


def _a2a_text_from_message(message):
    """Concatenate the text parts of an A2A Message.

    A part counts as text when it carries a string ``text``, whatever its
    ``kind``/``type`` field says, because both spellings are in circulation and
    refusing one of them would look like a broken agent.
    """
    parts = message.get("parts") if isinstance(message, dict) else None
    texts = [
        part["text"]
        for part in (parts if isinstance(parts, list) else [])
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return "\\n".join(texts).strip()


def _a2a_rpc_error(rpc_id, code, message):
    return {{
        "jsonrpc": _A2A_JSONRPC_VERSION,
        "id": rpc_id,
        "error": {{"code": code, "message": message}},
    }}


def _a2a_rpc_result(rpc_id, result):
    return {{"jsonrpc": _A2A_JSONRPC_VERSION, "id": rpc_id, "result": result}}


def _a2a_handle_jsonrpc(payload):
    """Serve the JSON-RPC methods a peer needs over InvokeAgentRuntime.

    Two of them, and the second exists because of a tension in the declaration that
    cannot be resolved in the declaration. A peer holding only a runtime ARN cannot
    reach the GET /.well-known/agent-card.json route this module registers. Measured,
    with signed requests to the data plane: the path under /runtimes/<arn>/ is a 404
    UnknownOperationException, and the real operation for it, GetAgentCard -- which
    does exist in the service model, though the CLI exposes no subcommand for it --
    returns 400 "GetAgentCard API is only supported for A2A agents". This runtime is
    declared HTTP, so it is refused. Declaring A2A would unlock GetAgentCard and make
    every InvokeAgentRuntime call return 424 instead, because the container serves an
    HTTP entrypoint and not an A2A server (see cfn_template_generator._runtime_protocol).
    One declaration cannot buy both, so the card is served through the entrypoint.

    Nothing here logs the payload or the result.
    Request bodies and response payloads are customer content and do not belong in
    service logs; the method name is echoed back to the caller but truncated, since
    it is caller-controlled.
    """
    rpc_id = payload.get("id")
    method = payload.get("method")
    if method == "agent/getAuthenticatedExtendedCard":
        return _a2a_rpc_result(rpc_id, _build_agent_card())
    if method != "message/send":
        return _a2a_rpc_error(rpc_id, -32601, "Method not found: " + str(method)[:100])
    params = payload.get("params")
    text = _a2a_text_from_message(params.get("message") if isinstance(params, dict) else None)
    if not text:
        return _a2a_rpc_error(
            rpc_id, -32602, "Invalid params: params.message.parts[].text is required"
        )
    result = str(_get_agent()(text))
    return _a2a_rpc_result(
        rpc_id,
        {{
            "kind": "message",
            "role": "agent",
            "messageId": str(uuid.uuid4()),
            "parts": [{{"kind": "text", "text": result}}],
        }},
    )


@app.entrypoint
def invoke(payload):
    """Process a user prompt; the agent may call A2A peers via call_a2a_peer.

    Two payload shapes, because there are two kinds of caller. ``{{"prompt": "..."}}``
    is what the platform and a curl user send. A JSON-RPC envelope is what an A2A
    peer sends, and it is answered with a JSON-RPC response object carrying the same
    ``id`` -- see _a2a_handle_jsonrpc.
    """
    if not isinstance(payload, dict):
        payload = {{}}
    if _a2a_is_jsonrpc(payload):
        return _a2a_handle_jsonrpc(payload)
    message = payload.get("prompt", "Hello")
    result = _get_agent()(message)
    return {{"response": str(result)}}


if __name__ == "__main__":
    app.run()
'''
