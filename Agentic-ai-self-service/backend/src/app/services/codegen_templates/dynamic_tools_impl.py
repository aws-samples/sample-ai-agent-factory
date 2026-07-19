"""Canonical built-in web tool implementations (DEPLOYED-CODE TEMPLATE, not app code).

Single source of truth for the DuckDuckGo search, Wikipedia search, Open-Meteo
weather, and SSRF-guarded webpage-fetch tools plus the shared ``_http_get``
retry helper, ``ToolUnavailable`` semantics, and the ``WMO_CODES`` table.

This file is read as TEXT and embedded into:
  - the Gateway dynamic-tools Lambda (gateway_deployer)
  - generated agent code (code_generator: web-search + mcp-server templates)
  - downloadable CFN bundles (cfn_template_generator)

Edit here once; every deploy surface picks it up.
"""

import ipaddress
import json
import time
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0"
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Foggy",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Thunderstorm with heavy hail",
}


class ToolUnavailable(Exception):
    # Raised when an external dependency (web/api) can't be reached after retries.
    # The dispatcher turns this into a STRUCTURED {"error":"tool_unavailable",...}
    # body so the agent (and tests) can distinguish "the tool failed" from
    # "the tool ran and found nothing".
    pass


def _http_get(url, timeout=10, retries=2):
    # Refuse non-http(s) schemes outright — urlopen would happily follow
    # file:// or ftp:// (Bandit B310); the SSRF net-range guard in
    # _do_fetch_webpage covers hosts, this covers schemes for every caller.
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ToolUnavailable(f"unsupported URL scheme: {url.split(':', 1)[0]}")
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  # scheme validated above
                return resp.read()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1 * (attempt + 1))
    raise ToolUnavailable(str(last_err))


def _do_duckduckgo_search(query):
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode({"q": query, "format": "json", "no_html": "1"})
    data = json.loads(_http_get(url, timeout=12).decode())
    results = []
    if data.get("Abstract"):
        results.append(
            {"title": data.get("Heading", query), "snippet": data["Abstract"], "url": data.get("AbstractURL", "")}
        )
    for topic in data.get("RelatedTopics", [])[:5]:
        if isinstance(topic, dict) and topic.get("Text"):
            results.append(
                {
                    "title": topic.get("Text", "")[:80],
                    "snippet": topic.get("Text", ""),
                    "url": topic.get("FirstURL", ""),
                }
            )
    return json.dumps(results) if results else json.dumps({"message": f"No results found for: {query}"})


def _do_wikipedia_search(query):
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(query)
    try:
        data = json.loads(_http_get(url, timeout=10).decode())
        return json.dumps(
            {
                "title": data.get("title", query),
                "summary": data.get("extract", ""),
                "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
            }
        )
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
    wx_url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
        }
    )
    wx = json.loads(_http_get(wx_url, timeout=8).decode())
    cur = wx.get("current", {})
    code = cur.get("weather_code", -1)
    desc = WMO_CODES.get(code, f"Code {code}")
    return json.dumps(
        {
            "location": f"{place}, {country}",
            "description": desc,
            "temperature_F": cur.get("temperature_2m"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "wind_mph": cur.get("wind_speed_10m"),
        }
    )


_FETCH_BLOCKED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "::/128",
        "::ffff:0:0/96",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
]


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
        infos = _socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80), _socket.AF_UNSPEC, _socket.SOCK_STREAM
        )
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
