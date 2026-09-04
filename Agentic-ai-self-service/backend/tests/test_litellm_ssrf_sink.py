"""The LiteLLM SSRF guard is enforced at the request sink, not by convention.

Every LiteLLM probe funnels through `litellm_gateway_deployer._get_json`, and that
is where the URL is validated — https-only plus a DNS-resolved private/IMDS
denylist. The API boundary validates too, and that is not redundant with this:

* A base URL is also read back out of **persisted settings** (`list_litellm_servers`
  reads `cfg["base_url"]`), so the value reaching the sink did not necessarily
  arrive through the API on this request. A settings row written straight to
  DynamoDB never saw the boundary check, and before the sink was guarded that row
  would have been fetched.
* A check enforced only at call sites is one new call site away from being absent.

Two exception classes matter here and they are easy to conflate: a bad *scheme*
raises `_DiscoveryUrlInvalid` while a disallowed *address* raises
`_DiscoveryUrlBlocked`. Both subclass ValueError. Code that handles only the
latter silently lets `http://` through to whatever its fallback path is — which is
exactly what the last test pins.

DNS is patched rather than resolved, following `test_gateway_deployer_ssrf.py`:
the guard resolves every A/AAAA record, so a test that relied on a real lookup
would be a network dependency and would change behaviour the day a fixture domain
started or stopped resolving.
"""

from __future__ import annotations

import socket
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, "src")

from app.services import litellm_gateway_deployer as G  # noqa: E402
from app.services.gateway_deployer import (  # noqa: E402
    _DiscoveryUrlBlocked,
    _DiscoveryUrlInvalid,
)
from app.services.registry_providers import litellm as L  # noqa: E402

_KEY = "sk-not-a-real-credential"
_PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def _resolves_to(ip: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


# (url, dns answer, expected exception). Loopback, the IMDS address, the Lambda
# credential endpoint, and RFC1918 — reached both as literal IPs and, for the last
# one, as a public-looking hostname that resolves inward (DNS rebinding's static
# cousin, which a scheme/regex check alone would not catch).
_BLOCKED = [
    ("http://litellm.example.com/v1/mcp/server", _PUBLIC, _DiscoveryUrlInvalid),
    ("https://127.0.0.1:4000/v1/mcp/server", _resolves_to("127.0.0.1"), _DiscoveryUrlBlocked),
    ("https://169.254.169.254/latest/meta-data/", _resolves_to("169.254.169.254"), _DiscoveryUrlBlocked),
    ("https://169.254.170.2/v2/credentials", _resolves_to("169.254.170.2"), _DiscoveryUrlBlocked),
    ("https://10.0.0.5/v1/mcp/server", _resolves_to("10.0.0.5"), _DiscoveryUrlBlocked),
    ("https://litellm.example.com/v1/mcp/server", _resolves_to("192.168.1.10"), _DiscoveryUrlBlocked),
]


@pytest.fixture(autouse=True)
def _never_actually_send(monkeypatch):
    """Fail loudly if a blocked URL ever reaches urlopen.

    Asserting only that _get_json raised would still pass if the request had
    already gone out and the exception came from something after it.
    """

    def _boom(*a, **k):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("a request was sent for a URL the guard should have blocked")

    monkeypatch.setattr(G.urllib.request, "urlopen", _boom)


@pytest.mark.parametrize(("url", "dns", "expected"), _BLOCKED)
def test_the_sink_refuses_to_fetch_a_blocked_url(url, dns, expected):
    with patch("socket.getaddrinfo", return_value=dns), pytest.raises(expected):
        G._get_json(url, _KEY)


@pytest.mark.parametrize(("url", "dns", "expected"), _BLOCKED)
def test_the_rejection_names_litellm(url, dns, expected):
    """The shared guard's default label reads "OIDC discovery URL", which would
    send an admin off to their IdP configuration to fix a LiteLLM base URL."""
    with patch("socket.getaddrinfo", return_value=dns), pytest.raises(expected) as ei:
        G._get_json(url, _KEY)
    assert "LiteLLM" in str(ei.value)


def test_a_poisoned_settings_row_is_blocked_at_read_time(monkeypatch):
    """The case caller-side validation cannot cover: the stored config IS the
    input, so nothing validated it on this request."""
    monkeypatch.setattr(
        L,
        "get_litellm_registry_config",
        lambda: {"base_url": "https://169.254.169.254", "api_key_ref": "stub", "verified": True},
    )
    monkeypatch.setattr(L, "_read_api_key", lambda _ref: _KEY)

    # Fails CLOSED: the governance gate turns RegistryQueryFailed into a 503 and
    # blocks the deploy, rather than reading an unreadable catalog as approval.
    with patch("socket.getaddrinfo", return_value=_resolves_to("169.254.169.254")):
        with pytest.raises(L.RegistryQueryFailed):
            L.list_litellm_servers()


@pytest.mark.parametrize(
    ("base_url", "dns"),
    [
        ("https://169.254.169.254", _resolves_to("169.254.169.254")),
        ("https://10.0.0.5", _resolves_to("10.0.0.5")),
        # The scheme case. This is the one that regresses first, because it raises
        # a *different* class from the address cases above.
        ("http://litellm.example.com", _PUBLIC),
    ],
)
def test_a_blocked_url_is_not_saved_as_merely_unverified(monkeypatch, base_url, dns):
    """`probe_litellm_registry` treats an unreachable proxy as verified=false on
    purpose — a self-hosted LiteLLM is often private and the control-plane Lambda
    has no VPC egress. A refused URL must NOT ride that lenient path, or pointing
    the registry at the metadata endpoint persists with a reassuring "could not
    reach the proxy" message instead of being rejected.
    """
    monkeypatch.setattr(L, "_read_api_key", lambda _ref: _KEY)
    with patch("socket.getaddrinfo", return_value=dns), pytest.raises(ValueError):
        L.probe_litellm_registry(base_url, _KEY)


def test_a_public_https_url_is_allowed_through_to_the_request(monkeypatch):
    """The guard has to still permit the normal case. A suite that only proved
    things are blocked would pass just as happily if everything were blocked."""
    sent = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"[]"

    def _capture(req, timeout=None):
        sent["url"] = req.full_url
        sent["headers"] = req.headers
        return _Resp()

    monkeypatch.setattr(G.urllib.request, "urlopen", _capture)
    with patch("socket.getaddrinfo", return_value=_PUBLIC):
        assert G._get_json("https://litellm.example.com/v1/mcp/server", _KEY) == []
    assert sent["url"] == "https://litellm.example.com/v1/mcp/server"
