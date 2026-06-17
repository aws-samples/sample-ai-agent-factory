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
from typing import Optional

import boto3

logger = logging.getLogger(__name__)


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
        "0.0.0.0/8",         # "this network"
        "10.0.0.0/8",        # RFC1918
        "100.64.0.0/10",     # CGNAT
        "127.0.0.0/8",       # loopback
        "169.254.0.0/16",    # link-local (IMDS, Lambda creds)
        "172.16.0.0/12",     # RFC1918
        "192.0.0.0/24",      # IETF
        "192.0.2.0/24",      # TEST-NET-1
        "192.168.0.0/16",    # RFC1918
        "198.18.0.0/15",     # benchmark
        "198.51.100.0/24",   # TEST-NET-2
        "203.0.113.0/24",    # TEST-NET-3
        "224.0.0.0/4",       # multicast
        "240.0.0.0/4",       # reserved (incl. 255.255.255.255)
        # IPv6
        "::1/128",           # loopback
        "::/128",            # unspecified
        "::ffff:0:0/96",     # IPv4-mapped (so an IPv4 RFC1918 mapped into v6 is also blocked
                             # via the v4 check, but we keep this for belt-and-braces)
        "fc00::/7",          # ULA (private)
        "fe80::/10",         # link-local
        "ff00::/8",          # multicast
        "2001:db8::/32",     # documentation
    )
)


def _load_oidc_host_allowlist() -> Optional[tuple[str, ...]]:
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
        raise _DiscoveryUrlInvalid(
            f"OIDC discovery URL must use https scheme (got '{parsed.scheme}')"
        )
    host = parsed.hostname
    if not host:
        raise _DiscoveryUrlInvalid("OIDC discovery URL has no host component")

    allowlist = _load_oidc_host_allowlist()
    if allowlist is not None and not _host_matches_allowlist(host, allowlist):
        raise _DiscoveryUrlBlocked(
            f"OIDC discovery host '{host}' is not on OIDC_DISCOVERY_HOST_ALLOWLIST"
        )

    # Resolve every A/AAAA record under a strict timeout so an attacker cannot stall
    # us on DNS to keep a half-validated socket alive.
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5)
    try:
        try:
            infos = socket.getaddrinfo(
                host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        except (socket.gaierror, socket.timeout, OSError) as e:
            raise _DiscoveryUrlBlocked(
                f"OIDC discovery URL host '{host}' could not be resolved: {e}"
            ) from e
    finally:
        socket.setdefaulttimeout(prev_timeout)

    if not infos:
        raise _DiscoveryUrlBlocked(
            f"OIDC discovery URL host '{host}' returned no DNS records"
        )

    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        # IPv6 sockaddr can carry a scope id like "fe80::1%eth0" — strip it.
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as e:
            raise _DiscoveryUrlBlocked(
                f"OIDC discovery URL resolved to unparseable IP '{ip_str}': {e}"
            ) from e
        for net in _DISALLOWED_NETWORKS:
            # ip_address(v4) in ip_network(v6) raises TypeError, so guard on family.
            if ip_obj.version != net.version:
                continue
            if ip_obj in net:
                raise _DiscoveryUrlBlocked(
                    "OIDC discovery URL resolves to disallowed IP "
                    f"({ip_str} in {net})"
                )

    return url


# ---------------------------------------------------------------------------
# Response key helpers
# ---------------------------------------------------------------------------


def _resolve_gateway_tool_actions(
    agentcore_ctrl, gateway_id: str, timeout: int = 180
) -> tuple[list, int]:
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
            resp = agentcore_ctrl.list_gateway_targets(
                gatewayIdentifier=gateway_id, maxResults=50
            )
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
            d = agentcore_ctrl.get_gateway_target(
                gatewayIdentifier=gateway_id, targetId=tid
            )
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
                detail = agentcore_ctrl.get_gateway_target(
                    gatewayIdentifier=gateway_id, targetId=tid
                )
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
                    tstatus in ("READY", "ACTIVE")
                    and synced_at is not None
                    and synced_at != pre_sync.get(tid)
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
            logger.warning(
                "Gateway %s tool plane synced: %d/%d tools", gateway_id, len(actions), expected
            )
            return actions, expected
        _t.sleep(5)

    logger.warning(
        "Gateway %s tool plane not fully synced within %ds; %d/%d tools synced",
        gateway_id, timeout, len(actions), expected,
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


# ---------------------------------------------------------------------------
# Lambda code constants (embedded Lambda source for gateway targets)
# ---------------------------------------------------------------------------

CUSTOMER_SUPPORT_LAMBDA_CODE = """
import json

def lambda_handler(event, context):
    tool_name = context.client_context.custom.get('bedrockAgentCoreToolName', 'unknown')

    if 'check_order_status' in tool_name:
        order_id = event.get('order_id', '').upper()
        orders = {
            'ORD-12345': {'order_id': 'ORD-12345', 'status': 'Shipped', 'tracking_number': '1Z999AA10123456784', 'estimated_delivery': '2025-02-10', 'items': [{'name': 'Laptop Pro 15', 'qty': 1, 'price': '$1,299.00'}, {'name': 'USB-C Charger', 'qty': 1, 'price': '$49.99'}], 'total': '$1,348.99'},
            'ORD-67890': {'order_id': 'ORD-67890', 'status': 'Processing', 'tracking_number': None, 'estimated_delivery': '2025-02-15', 'items': [{'name': 'Wireless Mouse', 'qty': 1, 'price': '$29.99'}, {'name': 'Mechanical Keyboard', 'qty': 1, 'price': '$89.99'}], 'total': '$119.98'},
            'ORD-11111': {'order_id': 'ORD-11111', 'status': 'Delivered', 'tracking_number': '1Z999AA10123456785', 'delivered_date': '2025-01-28', 'items': [{'name': '27 inch 4K Monitor', 'qty': 1, 'price': '$449.00'}], 'total': '$449.00'},
            'ORD-22222': {'order_id': 'ORD-22222', 'status': 'Cancelled', 'reason': 'Customer requested cancellation', 'refund_status': 'Refund processed', 'items': [{'name': 'Noise-Cancelling Headphones', 'qty': 1, 'price': '$199.00'}], 'total': '$199.00'},
        }
        order = orders.get(order_id)
        if not order:
            return {'statusCode': 200, 'body': json.dumps({'error': f'Order {order_id} not found. Valid demo order IDs: ORD-12345, ORD-67890, ORD-11111, ORD-22222'})}
        return {'statusCode': 200, 'body': json.dumps(order)}

    elif 'lookup_customer' in tool_name:
        email = event.get('email', '').lower()
        customers = {
            'john@example.com': {'name': 'John Smith', 'customer_id': 'CUST-001', 'email': 'john@example.com', 'membership_tier': 'Gold', 'member_since': '2022-03-15', 'orders': ['ORD-12345', 'ORD-11111'], 'total_spent': '$1,797.99'},
            'jane@example.com': {'name': 'Jane Doe', 'customer_id': 'CUST-002', 'email': 'jane@example.com', 'membership_tier': 'Silver', 'member_since': '2023-06-20', 'orders': ['ORD-67890'], 'total_spent': '$119.98'},
            'bob@example.com': {'name': 'Bob Wilson', 'customer_id': 'CUST-003', 'email': 'bob@example.com', 'membership_tier': 'Platinum', 'member_since': '2021-01-10', 'orders': ['ORD-22222'], 'total_spent': '$4,599.00'},
        }
        customer = customers.get(email)
        if not customer:
            return {'statusCode': 200, 'body': json.dumps({'error': f'No customer found with email {email}. Try: john@example.com, jane@example.com, bob@example.com'})}
        return {'statusCode': 200, 'body': json.dumps(customer)}

    elif 'search_knowledge_base' in tool_name:
        query = event.get('query', '').lower()
        articles = [
            {'id': 'KB-001', 'title': 'How to Reset Your Account Password', 'summary': 'Go to Settings > Security > Reset Password.'},
            {'id': 'KB-002', 'title': 'Return and Refund Policy', 'summary': 'Items can be returned within 30 days of delivery.'},
            {'id': 'KB-003', 'title': 'Shipping and Delivery Information', 'summary': 'Standard shipping: 5-7 business days.'},
            {'id': 'KB-004', 'title': 'Warranty Coverage Details', 'summary': 'All electronics include 1-year manufacturer warranty.'},
            {'id': 'KB-005', 'title': 'Troubleshooting Blue Screen Errors', 'summary': 'Common causes: outdated drivers, hardware failure.'},
            {'id': 'KB-006', 'title': 'How to Track Your Order', 'summary': 'Log in to your account > My Orders.'},
            {'id': 'KB-007', 'title': 'Membership Tiers and Benefits', 'summary': 'Silver: free standard shipping. Gold: free express.'},
        ]
        matches = [a for a in articles if query in a['title'].lower() or query in a['summary'].lower()]
        if not matches:
            matches = articles[:3]
        return {'statusCode': 200, 'body': json.dumps({'results': matches, 'total_found': len(matches)})}

    elif 'get_return_policy' in tool_name:
        category = event.get('product_category', 'general').lower()
        policies = {
            'electronics': {'category': 'Electronics', 'return_window': '30 days', 'condition': 'Must be in original packaging'},
            'accessories': {'category': 'Accessories', 'return_window': '60 days', 'condition': 'Must be unused'},
            'software': {'category': 'Software', 'return_window': '14 days', 'condition': 'Physical media only'},
            'general': {'category': 'General', 'return_window': '30 days', 'condition': 'Item must be in resalable condition'},
        }
        policy = policies.get(category, policies['general'])
        return {'statusCode': 200, 'body': json.dumps(policy)}

    return {'statusCode': 200, 'body': json.dumps({'message': f'Unknown tool: {tool_name}'})}
"""

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

DYNAMIC_TOOLS_LAMBDA_CODE = """
import ipaddress
import json
import time
import urllib.request
import urllib.parse

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0"
WMO_CODES = {0:"Clear sky",1:"Mainly clear",2:"Partly cloudy",3:"Overcast",45:"Foggy",48:"Rime fog",51:"Light drizzle",53:"Moderate drizzle",55:"Dense drizzle",61:"Slight rain",63:"Moderate rain",65:"Heavy rain",71:"Slight snow",73:"Moderate snow",75:"Heavy snow",80:"Slight rain showers",81:"Moderate rain showers",82:"Violent rain showers",95:"Thunderstorm",96:"Thunderstorm with hail",99:"Thunderstorm with heavy hail"}

def _http_get(url, timeout=10, retries=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1 * (attempt + 1))
    raise last_err

def _do_duckduckgo_search(query):
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode({"q": query, "format": "json", "no_html": "1"})
    data = json.loads(_http_get(url, timeout=12).decode())
    results = []
    if data.get("Abstract"):
        results.append({"title": data.get("Heading", query), "snippet": data["Abstract"], "url": data.get("AbstractURL", "")})
    for topic in data.get("RelatedTopics", [])[:5]:
        if isinstance(topic, dict) and topic.get("Text"):
            results.append({"title": topic.get("Text", "")[:80], "snippet": topic.get("Text", ""), "url": topic.get("FirstURL", "")})
    return json.dumps(results) if results else json.dumps({"message": f"No results found for: {query}"})

def _do_wikipedia_search(query):
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(query)
    try:
        data = json.loads(_http_get(url, timeout=10).decode())
        return json.dumps({"title": data.get("title", query), "summary": data.get("extract", ""), "url": data.get("content_urls", {}).get("desktop", {}).get("page", "")})
    except Exception:
        return json.dumps({"error": f"No Wikipedia article found for: {query}"})

def _do_weather(location):
    geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({"name": location, "count": 1})
    geo = json.loads(_http_get(geo_url, timeout=8).decode())
    results = geo.get("results", [])
    if not results:
        return json.dumps({"error": f"Location not found: {location}"})
    lat, lon = results[0]["latitude"], results[0]["longitude"]
    place = results[0].get("name", location)
    country = results[0].get("country", "")
    wx_url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
    })
    wx = json.loads(_http_get(wx_url, timeout=8).decode())
    cur = wx.get("current", {})
    code = cur.get("weather_code", -1)
    desc = WMO_CODES.get(code, f"Code {code}")
    return json.dumps({"location": f"{place}, {country}", "description": desc, "temperature_F": cur.get("temperature_2m"), "humidity_pct": cur.get("relative_humidity_2m"), "wind_mph": cur.get("wind_speed_10m")})

_FETCH_BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8","10.0.0.0/8","100.64.0.0/10","127.0.0.0/8","169.254.0.0/16",
    "172.16.0.0/12","192.0.0.0/24","192.168.0.0/16","198.18.0.0/15","224.0.0.0/4",
    "240.0.0.0/4","::1/128","::/128","::ffff:0:0/96","fc00::/7","fe80::/10","ff00::/8",
)]

def _do_fetch_webpage(url):
    # SECURITY: Validate scheme + DNS-resolve host and block private/link-local/IMDS
    # ranges. Substring/literal-host denylists are bypassable via DNS rebinding.
    import socket as _socket
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return json.dumps({"error": "Only http/https URLs are allowed"})
    host = (parsed.hostname or "").lower()
    if not host:
        return json.dumps({"error": "URL has no host component"})
    try:
        infos = _socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), _socket.AF_UNSPEC, _socket.SOCK_STREAM)
    except Exception as e:
        return json.dumps({"error": f"DNS resolution failed: {e}"})
    for info in infos:
        ip_str = info[4][0].split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return json.dumps({"error": f"Unparseable resolved IP: {ip_str}"})
        for net in _FETCH_BLOCKED_NETS:
            if ip_obj.version == net.version and ip_obj in net:
                return json.dumps({"error": "Requests to internal/private endpoints are blocked"})
    text = _http_get(url, timeout=12).decode(errors="replace")
    return json.dumps({"url": url, "content": text[:8000]})

CUSTOMERS = {
    "CUST-001": {"customer_id": "CUST-001", "name": "John Doe", "email": "john@example.com", "member_since": "2023-06-01"},
    "CUST-002": {"customer_id": "CUST-002", "name": "Jane Smith", "email": "jane@example.com", "member_since": "2024-01-15"},
}

ORDERS = {
    "ORD-12345": {"order_id": "ORD-12345", "customer_id": "CUST-001", "status": "delivered", "items": [{"name": "Wireless Headphones", "quantity": 1, "price": 79.99}], "total": 79.99, "order_date": "2025-01-15", "delivery_date": "2025-01-20"},
    "ORD-12300": {"order_id": "ORD-12300", "customer_id": "CUST-001", "status": "delivered", "items": [{"name": "Running Shoes", "quantity": 1, "price": 249.00}], "total": 249.00, "order_date": "2025-01-02", "delivery_date": "2025-01-08"},
    "ORD-12400": {"order_id": "ORD-12400", "customer_id": "CUST-001", "status": "delivered", "items": [{"name": "USB-C Charging Cable", "quantity": 2, "price": 12.99}], "total": 25.98, "order_date": "2025-01-20", "delivery_date": "2025-01-23"},
    "ORD-99000": {"order_id": "ORD-99000", "customer_id": "CUST-002", "status": "delivered", "items": [{"name": "Premium Laptop", "quantity": 1, "price": 1299.00}], "total": 1299.00, "order_date": "2025-01-10", "delivery_date": "2025-01-15"},
    "ORD-99010": {"order_id": "ORD-99010", "customer_id": "CUST-002", "status": "delivered", "items": [{"name": "Yoga Mat", "quantity": 1, "price": 45.00}], "total": 45.00, "order_date": "2025-01-18", "delivery_date": "2025-01-21"},
}

REFUNDS = {}

def _do_get_order(event):
    order_id = event.get("order_id", "")
    order = ORDERS.get(order_id)
    if not order:
        return json.dumps({"error": f"Order {order_id} not found. Valid IDs: {', '.join(ORDERS.keys())}"})
    return json.dumps(order)

def _do_get_customer(event):
    customer_id = event.get("customer_id", "")
    customer = CUSTOMERS.get(customer_id)
    if not customer:
        return json.dumps({"error": f"Customer {customer_id} not found. Valid IDs: {', '.join(CUSTOMERS.keys())}"})
    customer_orders = [o for o in ORDERS.values() if o["customer_id"] == customer_id]
    return json.dumps({**customer, "total_orders": len(customer_orders), "total_spent": round(sum(o["total"] for o in customer_orders), 2)})

def _do_list_orders(event):
    customer_id = event.get("customer_id", "")
    limit = event.get("limit", 10)
    if customer_id not in CUSTOMERS:
        return json.dumps({"error": f"Customer {customer_id} not found"})
    orders = [{"order_id": o["order_id"], "total": o["total"], "status": o["status"], "order_date": o["order_date"]} for o in ORDERS.values() if o["customer_id"] == customer_id]
    orders.sort(key=lambda x: x["order_date"], reverse=True)
    return json.dumps({"customer_id": customer_id, "orders": orders[:limit]})

def _do_process_refund(event):
    import uuid as _uuid
    order_id = event.get("order_id", "")
    amount = event.get("amount")
    reason = event.get("reason", "")
    order = ORDERS.get(order_id)
    if not order:
        return json.dumps({"error": f"Order {order_id} not found"})
    if amount is None or amount <= 0:
        return json.dumps({"error": "Refund amount must be positive"})
    if amount > order["total"]:
        return json.dumps({"error": f"Refund amount ${amount} exceeds order total ${order['total']}"})
    refund_id = f"REF-{_uuid.uuid4().hex[:5].upper()}"
    return json.dumps({"success": True, "refund_id": refund_id, "order_id": order_id, "amount": amount, "reason": reason, "status": "processed", "message": f"Refund of ${amount:.2f} processed. Customer will receive funds in 3-5 business days."})

def lambda_handler(event, context):
    tool_name = context.client_context.custom.get("bedrockAgentCoreToolName", "unknown")
    try:
        if "duckduckgo_search" in tool_name:
            return {"statusCode": 200, "body": _do_duckduckgo_search(event.get("query", ""))}
        elif "wikipedia_search" in tool_name:
            return {"statusCode": 200, "body": _do_wikipedia_search(event.get("query", ""))}
        elif "weather_api" in tool_name or "get_weather" in tool_name:
            return {"statusCode": 200, "body": _do_weather(event.get("location", ""))}
        elif "web_page_fetcher" in tool_name or "fetch_webpage" in tool_name:
            return {"statusCode": 200, "body": _do_fetch_webpage(event.get("url", ""))}
        elif "get_order" in tool_name and "list_orders" not in tool_name:
            return {"statusCode": 200, "body": _do_get_order(event)}
        elif "get_customer" in tool_name:
            return {"statusCode": 200, "body": _do_get_customer(event)}
        elif "list_orders" in tool_name:
            return {"statusCode": 200, "body": _do_list_orders(event)}
        elif "process_refund" in tool_name:
            return {"statusCode": 200, "body": _do_process_refund(event)}
        else:
            return {"statusCode": 200, "body": json.dumps({"error": f"Unknown tool: {tool_name}"})}
    except Exception as e:
        return {"statusCode": 200, "body": json.dumps({"error": str(e)})}
"""

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

KNOWLEDGE_BASE_LAMBDA_TEMPLATE = '''
import json
import os
import boto3

bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))

def lambda_handler(event, context):
    query = event.get("query", "")
    kb_id = os.environ["KNOWLEDGE_BASE_ID"]
    model_arn = os.environ["FOUNDATION_MODEL_ARN"]

    try:
        resp = bedrock_runtime.retrieve_and_generate(
            input={"text": query},
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": kb_id,
                    "modelArn": model_arn,
                }
            }
        )
        answer = resp.get("output", {}).get("text", "No answer found.")
        citations = []
        for c in resp.get("citations", [])[:5]:
            refs = c.get("retrievedReferences", [])
            for ref in refs[:2]:
                loc = ref.get("location", {})
                citations.append({
                    "text": ref.get("content", {}).get("text", "")[:200],
                    "source": loc.get("s3Location", {}).get("uri", "") or loc.get("webLocation", {}).get("url", ""),
                })
        return {"statusCode": 200, "body": json.dumps({"answer": answer, "citations": citations})}
    except Exception as e:
        return {"statusCode": 200, "body": json.dumps({"error": str(e)})}
'''


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
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": [
                    "bedrock:Retrieve",
                    "bedrock:RetrieveAndGenerate",
                    "bedrock:InvokeModel",
                ],
                "Resource": "*",
            }],
        }),
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
            try:
                lambda_client.add_permission(
                    FunctionName=fn_name,
                    StatementId="AllowAgentCoreInvoke",
                    Action="lambda:InvokeFunction",
                    Principal=gateway_role_arn,
                )
            except lambda_client.exceptions.ResourceConflictException:
                pass
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


def _create_or_update_lambda(
    lambda_client,
    function_name: str,
    role_arn: str,
    zip_bytes: bytes,
    description: str,
    gateway_role_arn: Optional[str] = None,
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
        lambda_client.update_function_code(FunctionName=function_name, ZipFile=zip_bytes)
        resp = lambda_client.get_function(FunctionName=function_name)
        lambda_arn = resp["Configuration"]["FunctionArn"]

    # ALWAYS grant the invoking gateway role (idempotent, per-role StatementId) so
    # a shared Lambda reused by a NEW gateway still authorizes that gateway.
    if gateway_role_arn:
        # StatementId must be unique per principal + match ^[A-Za-z0-9-_]+$.
        role_name = gateway_role_arn.rsplit("/", 1)[-1]
        stmt_id = re.sub(r"[^A-Za-z0-9_-]", "-", f"AllowAgentCoreInvoke-{role_name}")[:100]
        try:
            lambda_client.add_permission(
                FunctionName=function_name,
                StatementId=stmt_id,
                Action="lambda:InvokeFunction",
                Principal=gateway_role_arn,
            )
            logger.info("Granted %s invoke on %s", role_name, function_name)
        except lambda_client.exceptions.ResourceConflictException:
            pass  # this gateway role is already permitted — fine

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
            with urllib.request.urlopen(req, timeout=10) as resp:  # nosemgrep: dynamic-urllib-use-detected -- URL validated by _validate_discovery_url (scheme=https + IP denylist + optional allowlist)
                discovery_doc = json.loads(resp.read().decode())
                token_endpoint = discovery_doc.get("token_endpoint", "")
        except Exception as e:
            # Re-raise: a transient discovery-doc fetch failure must fail the
            # deploy, not silently produce a gateway with empty token_endpoint.
            raise RuntimeError(
                f"Failed to fetch OIDC discovery document from {validated_url}: {e}"
            ) from e

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
            "POST", gateway_url, body=body,
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


def _wait_for_gateway_to_serve_tools(
    gateway_url: str, client_info: dict, expected: int, timeout: int = 90
) -> int:
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
) -> Optional[dict]:
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
            # If the target already exists, look it up and reuse it
            if "ConflictException" in err_str or "already exists" in err_str:
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


def deploy_gateway(
    gateway_config: dict,
    region: str,
    template_id: Optional[str] = None,
    gateway_tools: Optional[list] = None,
    identity_config: Optional[dict] = None,
    custom_tools: Optional[list[dict]] = None,
    mcp_server_runtime_arn: Optional[str] = None,
    mcp_oauth: Optional[dict] = None,
    knowledge_base_result: Optional[dict] = None,
    deployment_id: Optional[str] = None,
    gateway_retry: int = 0,
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
            if "ConflictException" in err_str or "already exists" in err_str:
                logger.info("Gateway '%s' already exists, looking up", gateway_name)
                existing = agentcore_ctrl.list_gateways()
                for gw in _get_gateways_from_response(existing):
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
                    raise RuntimeError(f"Gateway '{gateway_name}' exists but not found via list")
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
            logger.info("Created OAuth2 credential provider: %s", mcp_cred_provider_arn)  # nosemgrep: python-logger-credential-disclosure -- logs resource ARN, not secret

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
                    region, gateway.get("roleArn", ""), kb_id, kb_model_arn, dep_id,
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
                _create_gateway_target_with_retry(agentcore_ctrl, gateway["gatewayId"], kb_target_name, kb_target_params)
                logger.info("Knowledge Base tool deployed as gateway target: %s", kb_target_name)
            except Exception as kb_err:
                logger.error("Failed to deploy KB tool: %s", kb_err)

        # Step 5: Synchronize NON-LAMBDA targets (OpenAPI / external MCP) for
        # semantic discovery. Bug 134: synchronize_gateway_targets REQUIRES a
        # targetIdList (the old call omitted it → silent failure) AND rejects
        # LAMBDA targets ("Target type LAMBDA is not supported for
        # synchronization") — Lambda targets serve their inline tools directly.
        # So only sync targets that actually need crawling.
        if gateway_config.get("semanticSearchEnabled"):
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
                    logger.warning("Synchronizing %d non-lambda target(s) on gateway %s", len(_sync_ids), gateway["gatewayId"])
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
            except Exception:  # noqa: BLE001
                pass
        qualified_tools, expected_tool_count = _resolve_gateway_tool_actions(
            agentcore_ctrl, gateway["gatewayId"]
        )

        gateway_url = gateway.get("gatewayUrl", "")
        client_info = cognito_response["client_info"]

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
            served = _wait_for_gateway_to_serve_tools(
                gateway_url, client_info, expected_tool_count, timeout=90
            )
            if served < expected_tool_count:
                if gateway_retry < 2:
                    logger.warning(
                        "Gateway %s serves %d/%d tools over MCP — likely the "
                        "AgentCore empty-tool-plane flake. Tearing it down and "
                        "recreating (attempt %d/3).",
                        gateway["gatewayId"], served, expected_tool_count, gateway_retry + 2,
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
                        gateway_config, region, template_id=template_id,
                        gateway_tools=gateway_tools, identity_config=identity_config,
                        custom_tools=custom_tools, mcp_server_runtime_arn=mcp_server_runtime_arn,
                        mcp_oauth=mcp_oauth, knowledge_base_result=knowledge_base_result,
                        deployment_id=deployment_id, gateway_retry=gateway_retry + 1,
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
            # Fully-qualified tool action names for Cedar policy generation, plus
            # how many tools the gateway CONFIGURED so the policy step can
            # fail-closed on a partial (synced < configured) tool plane.
            "qualified_tools": qualified_tools,
            "expected_tool_count": expected_tool_count,
        }
        # Log the gateway id/arn (stable identifiers) rather than the full
        # gateway_url. The URL is a public MCP endpoint (not a secret), but
        # logging a url-typed value trips py/clear-text-logging-sensitive-data's
        # heuristic; the id+arn are sufficient to correlate and carry no such flag.
        logger.info(
            "Gateway deployed: id=%s, arn=%s, tools=%d",
            result["gateway_id"],
            gateway_arn,
            len(qualified_tools),
        )
        return result

    except Exception as e:
        logger.error("Gateway deployment failed: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Gateway cleanup
# ---------------------------------------------------------------------------


def cleanup_gateway_resources(runtime_id: str, region: str, gateway_config: Optional[dict] = None) -> list[str]:
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
                    except Exception:
                        pass
                    if client_id_val:
                        try:
                            cognito_client.delete_user_pool_client(UserPoolId=user_pool_id, ClientId=client_id_val)
                        except Exception:
                            pass
                    cognito_client.delete_user_pool(UserPoolId=user_pool_id)
                    cleanup_log.append(f"Cognito pool {user_pool_id} deleted")
            except Exception as e:
                cleanup_log.append(f"Cognito cleanup error: {e}")
        else:
            cleanup_log.append(f"External IDP ({idp_provider}) — no Cognito cleanup needed")

    # Delete Lambda function
    try:
        lambda_client = _create_lambda_client(region)
        lambda_client.delete_function(FunctionName=lambda_name)
        cleanup_log.append(f"Lambda {lambda_name} deleted")
    except Exception as e:
        if "ResourceNotFound" not in str(e):
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
            if "ResourceNotFound" not in str(e):
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
            if "NoSuchEntity" not in str(e):
                cleanup_log.append(f"IAM role cleanup error for {role_name}: {e}")

    return cleanup_log
