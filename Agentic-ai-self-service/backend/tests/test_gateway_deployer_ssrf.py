"""SSRF guard regression tests for gateway_deployer._validate_discovery_url.

Critic Finding 2: the prior literal-IP check was bypassable via DNS rebinding
(hostname like ``evil.attacker.com`` resolving to 169.254.169.254). The
replacement validator must:
  * Resolve the hostname and reject any RFC1918 / loopback / link-local /
    multicast / CGNAT / reserved IP.
  * Reject literal-IP URLs that resolve into those ranges.
  * Reject non-https schemes.
  * Honor the operator-configured ``OIDC_DISCOVERY_HOST_ALLOWLIST`` env var.
  * Pass for normal public hostnames (mocked).
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from app.services.gateway_deployer import (
    _DiscoveryUrlBlocked,
    _DiscoveryUrlInvalid,
    _validate_discovery_url,
)


def _addrinfo_for(ip: str, family: int = socket.AF_INET):
    """Build a getaddrinfo-shaped tuple for a single IP."""
    if family == socket.AF_INET:
        sockaddr = (ip, 443)
    else:
        sockaddr = (ip, 443, 0, 0)
    return [(family, socket.SOCK_STREAM, 0, "", sockaddr)]


# ---------------------------------------------------------------------------
# Negative path: hostnames that resolve to disallowed IPs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocked_ip",
    [
        "169.254.169.254",   # AWS IMDS
        "169.254.170.2",     # Lambda credentials endpoint
        "127.0.0.1",         # loopback
        "10.0.0.1",          # RFC1918
        "172.16.5.5",        # RFC1918
        "192.168.1.1",       # RFC1918
        "100.64.0.1",        # CGNAT
        "224.0.0.1",         # multicast
        "0.0.0.0",           # this network
    ],
)
def test_dns_rebind_to_blocked_ipv4_is_rejected(blocked_ip: str) -> None:
    """A hostname resolving to any disallowed IPv4 must raise."""
    with patch(
        "socket.getaddrinfo",
        return_value=_addrinfo_for(blocked_ip),
    ):
        with pytest.raises(_DiscoveryUrlBlocked) as exc:
            _validate_discovery_url("https://evil.attacker.com/.well-known/openid-configuration")
        assert "disallowed IP" in str(exc.value) or "could not be resolved" in str(exc.value)


@pytest.mark.parametrize(
    "blocked_ipv6",
    [
        "::1",         # loopback
        "fe80::1",     # link-local
        "fc00::1",     # ULA
    ],
)
def test_dns_rebind_to_blocked_ipv6_is_rejected(blocked_ipv6: str) -> None:
    with patch(
        "socket.getaddrinfo",
        return_value=_addrinfo_for(blocked_ipv6, family=socket.AF_INET6),
    ):
        with pytest.raises(_DiscoveryUrlBlocked):
            _validate_discovery_url("https://evil.attacker.com/.well-known/openid-configuration")


def test_literal_private_ip_url_is_rejected() -> None:
    """URL whose host is a literal RFC1918 IP must be rejected (getaddrinfo passes the IP through)."""
    with patch(
        "socket.getaddrinfo",
        return_value=_addrinfo_for("10.0.0.1"),
    ):
        with pytest.raises(_DiscoveryUrlBlocked):
            _validate_discovery_url("https://10.0.0.1/.well-known/openid-configuration")


def test_literal_imds_ip_url_is_rejected() -> None:
    with patch(
        "socket.getaddrinfo",
        return_value=_addrinfo_for("169.254.169.254"),
    ):
        with pytest.raises(_DiscoveryUrlBlocked):
            _validate_discovery_url("https://169.254.169.254/latest/meta-data/")


def test_multi_record_dns_with_one_blocked_ip_is_rejected() -> None:
    """If ANY resolved A record points at a private IP, reject — DNS rebinding can pick whichever."""
    multi = (
        _addrinfo_for("8.8.8.8")
        + _addrinfo_for("169.254.169.254")
    )
    with patch("socket.getaddrinfo", return_value=multi):
        with pytest.raises(_DiscoveryUrlBlocked):
            _validate_discovery_url("https://mixed.attacker.example/.well-known/openid-configuration")


# ---------------------------------------------------------------------------
# Negative path: structural URL problems
# ---------------------------------------------------------------------------


def test_http_scheme_is_rejected() -> None:
    with pytest.raises(_DiscoveryUrlInvalid) as exc:
        _validate_discovery_url("http://login.example.com/.well-known/openid-configuration")
    assert "https" in str(exc.value)


def test_file_scheme_is_rejected() -> None:
    with pytest.raises(_DiscoveryUrlInvalid):
        _validate_discovery_url("file:///etc/passwd")


def test_empty_url_is_rejected() -> None:
    with pytest.raises(_DiscoveryUrlInvalid):
        _validate_discovery_url("")


def test_url_with_no_host_is_rejected() -> None:
    with pytest.raises(_DiscoveryUrlInvalid):
        _validate_discovery_url("https:///path-only")


def test_dns_failure_is_rejected_loudly() -> None:
    """A DNS resolution failure must raise — never silently fall through."""
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("nodename nor servname provided")):
        with pytest.raises(_DiscoveryUrlBlocked) as exc:
            _validate_discovery_url("https://nonexistent.invalid/.well-known/openid-configuration")
        assert "could not be resolved" in str(exc.value)


# ---------------------------------------------------------------------------
# Positive path: legitimate public hosts pass
# ---------------------------------------------------------------------------


def test_public_cognito_endpoint_passes() -> None:
    with patch(
        "socket.getaddrinfo",
        return_value=_addrinfo_for("52.84.123.45"),  # arbitrary public IP
    ):
        out = _validate_discovery_url(
            "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc/.well-known/openid-configuration"
        )
        assert out.startswith("https://cognito-idp.us-east-1.amazonaws.com/")


def test_public_okta_endpoint_passes() -> None:
    with patch(
        "socket.getaddrinfo",
        return_value=_addrinfo_for("99.84.10.20"),
    ):
        out = _validate_discovery_url(
            "https://acme.okta.com/.well-known/openid-configuration"
        )
        assert out == "https://acme.okta.com/.well-known/openid-configuration"


# ---------------------------------------------------------------------------
# Allowlist (OIDC_DISCOVERY_HOST_ALLOWLIST) behavior
# ---------------------------------------------------------------------------


def test_allowlist_matching_host_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "OIDC_DISCOVERY_HOST_ALLOWLIST",
        "*.okta.com,*.auth0.com,*.amazoncognito.com",
    )
    with patch(
        "socket.getaddrinfo",
        return_value=_addrinfo_for("52.84.10.10"),
    ):
        out = _validate_discovery_url(
            "https://acme.okta.com/.well-known/openid-configuration"
        )
        assert out == "https://acme.okta.com/.well-known/openid-configuration"


def test_allowlist_non_matching_host_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "OIDC_DISCOVERY_HOST_ALLOWLIST",
        "*.okta.com,*.auth0.com",
    )
    # Even with a perfectly public IP, the host must match the allowlist.
    with patch(
        "socket.getaddrinfo",
        return_value=_addrinfo_for("99.99.99.99"),
    ):
        with pytest.raises(_DiscoveryUrlBlocked) as exc:
            _validate_discovery_url(
                "https://login.evil-idp.example/.well-known/openid-configuration"
            )
        assert "allowlist" in str(exc.value).lower() or "OIDC_DISCOVERY_HOST_ALLOWLIST" in str(exc.value)


def test_allowlist_unset_does_not_filter_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OIDC_DISCOVERY_HOST_ALLOWLIST", raising=False)
    with patch(
        "socket.getaddrinfo",
        return_value=_addrinfo_for("99.99.99.99"),
    ):
        # Should not raise on host alone (only IP-denylist applies).
        out = _validate_discovery_url(
            "https://login.evil-idp.example/.well-known/openid-configuration"
        )
        assert out


# ---------------------------------------------------------------------------
# Sanity: validator does not call socket.getaddrinfo for invalid scheme
# ---------------------------------------------------------------------------


def test_invalid_scheme_short_circuits_before_dns() -> None:
    with patch("socket.getaddrinfo") as mock_gai:
        with pytest.raises(_DiscoveryUrlInvalid):
            _validate_discovery_url("gopher://evil.example/")
        mock_gai.assert_not_called()


# ---------------------------------------------------------------------------
# Confirm exception classes are distinct ValueError subclasses (callers can keep
# their broad `except ValueError` while we discriminate)
# ---------------------------------------------------------------------------


def test_exception_classes_are_value_errors() -> None:
    assert issubclass(_DiscoveryUrlInvalid, ValueError)
    assert issubclass(_DiscoveryUrlBlocked, ValueError)
    assert _DiscoveryUrlInvalid is not _DiscoveryUrlBlocked


# ---------------------------------------------------------------------------
# Integration: _create_external_oauth_config refuses to build authorizer for an
# IMDS-rebound discovery_url (regression test for Critic Finding 2)
# ---------------------------------------------------------------------------


def test_create_external_oauth_config_rejects_imds_rebound_url() -> None:
    from app.services.gateway_deployer import _create_external_oauth_config

    identity_config = {
        "provider": "custom",
        "client_id": "abc",
        "client_secret": "shh",
        "discovery_url": "https://evil.attacker.example/.well-known/openid-configuration",
        "scopes": ["read"],
    }
    with patch(
        "socket.getaddrinfo",
        return_value=_addrinfo_for("169.254.169.254"),
    ):
        with pytest.raises(_DiscoveryUrlBlocked):
            _create_external_oauth_config(identity_config, region="us-east-1")


def test_create_external_oauth_config_rejects_http_scheme() -> None:
    from app.services.gateway_deployer import _create_external_oauth_config

    identity_config = {
        "provider": "custom",
        "client_id": "abc",
        "client_secret": "shh",
        "discovery_url": "http://login.example/.well-known/openid-configuration",
    }
    with pytest.raises(_DiscoveryUrlInvalid):
        _create_external_oauth_config(identity_config, region="us-east-1")


def test_create_external_oauth_config_no_url_skips_validation() -> None:
    """When operator omits discovery_url entirely, we don't try to fetch anything."""
    from app.services.gateway_deployer import _create_external_oauth_config

    identity_config = {
        "provider": "custom",
        "client_id": "abc",
        "client_secret": "shh",
        # discovery_url intentionally omitted
    }
    out = _create_external_oauth_config(identity_config, region="us-east-1")
    assert out["client_info"]["token_endpoint"] == ""
    assert out["authorizer_config"]["customJWTAuthorizer"]["discoveryUrl"] == ""
