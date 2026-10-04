"""The OAuth client secret must only ever be POSTed to a validated endpoint.

F-6 (authority injection / confused deputy). The *discovery* URL has been guarded
against DNS rebinding for a while; the *token* endpoint — one function away, and the
only one of the two that carries a credential — was not guarded at all. It is not
operator-typed, which is why it was missed: it arrives in the ``token_endpoint`` field
of the OIDC discovery **document**, so whoever serves that document chooses where the
secret goes, and the same field is read back out of the DynamoDB deployment record on
every redeploy and readiness probe.

Measured before the fix, with a real socket rather than a mock: ``get_cognito_token``
delivered ``client_secret=sk-fake-not-a-real-key-0000`` over plaintext HTTP to
``http://127.0.0.1:<port>/latest/meta-data/iam/security-credentials/`` and returned the
attacker's access token as if nothing had happened.

Three sinks, three guards, and they are deliberately not the same strength:

* ``get_cognito_token`` — opens the socket itself, so the DNS-resolving check runs.
* ``_create_external_oauth_config`` — the hop where the endpoint enters, so a bad
  document fails the deploy with an error naming the document.
* ``runtime_configure_step`` / ``deployment.py`` — bake the endpoint into the agent's
  environment, where the *agent* sends the secret from inside the runtime. Shape-only
  there (https + literal-IP denylist, no DNS): a deploy-time DNS answer says nothing
  about what the runtime will resolve at invoke time, and resolving would make a
  resolver hiccup fail every gateway deploy on the default Cognito path.

The listener-based tests are the ones that matter. A mocked ``PoolManager`` proves the
call was not made to the mock; a real socket proves no bytes left the process.

ARCC ``cnt_77BHvX7WzuG1X8`` (the secret goes only to the party it authenticates to),
``cnt_n8LpZcqYi2t3I2`` (never hold a secret in an env var).
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
from unittest.mock import MagicMock, patch

import pytest
from app.services import gateway_deployer as gd
from app.services.gateway_deployer import (
    _DiscoveryUrlBlocked,
    _DiscoveryUrlInvalid,
    get_cognito_token,
    validate_token_endpoint,
    validate_token_endpoint_shape,
)

FAKE_SECRET = "sk-fake-not-a-real-key-0000"  # pragma: allowlist secret


def _addrinfo_for(ip: str, family: int = socket.AF_INET):
    sockaddr = (ip, 443) if family == socket.AF_INET else (ip, 443, 0, 0)
    return [(family, socket.SOCK_STREAM, 0, "", sockaddr)]


class _Sink:
    """A real HTTP listener on loopback that records what it received.

    Loopback is the point: it is in the denylist, so a guard that works means this
    object never sees a byte. ``received`` is the assertion surface — an empty dict is
    proof of a *closed* sink, which a mocked transport cannot give you.
    """

    def __init__(self) -> None:
        self.received: dict = {}
        sink = self

        class _H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                sink.received["body"] = self.rfile.read(n).decode()
                sink.received["path"] = self.path
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"access_token": "attacker-issued"}).encode())

            def log_message(self, *a):  # silence the default stderr access log
                pass

        self._srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture
def sink():
    s = _Sink()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# The sink is closed — proven with a real socket
# ---------------------------------------------------------------------------


def test_no_bytes_reach_a_loopback_token_endpoint(sink):
    """The exact pre-fix reproduction, inverted into a regression test."""
    client_info = {
        "client_id": "probe-client",
        "client_secret": FAKE_SECRET,
        "token_endpoint": f"http://127.0.0.1:{sink.port}/latest/meta-data/iam/security-credentials/",
        "scope": "agentcore/invoke",
    }
    with pytest.raises(_DiscoveryUrlInvalid) as exc:
        get_cognito_token(client_info)

    assert "https" in str(exc.value)
    assert sink.received == {}, f"the secret still left the process: {sink.received}"


def test_an_https_loopback_endpoint_is_also_refused(sink):
    """Not just a scheme check — an https URL at a denylisted literal is refused too,
    so nobody can conclude "add TLS and it is fine"."""
    client_info = {
        "client_id": "probe-client",
        "client_secret": FAKE_SECRET,
        "token_endpoint": f"https://127.0.0.1:{sink.port}/oauth2/token",
        "scope": "s",
    }
    with pytest.raises(_DiscoveryUrlBlocked):
        get_cognito_token(client_info)
    assert sink.received == {}


@pytest.mark.parametrize(
    "blocked_ip",
    [
        "169.254.169.254",  # IMDS
        "169.254.170.2",  # Lambda credentials endpoint
        "10.1.2.3",  # RFC1918 — a VPC-internal listener
    ],
)
def test_a_rebound_hostname_is_refused_before_the_socket_opens(blocked_ip):
    """A public-looking name that resolves inside the denylist. The endpoint here is
    a NAME, so only DNS resolution can catch it — which is why this sink keeps the
    resolving guard while the bake-in paths do not."""
    client_info = {
        "client_id": "c",
        "client_secret": FAKE_SECRET,
        "token_endpoint": "https://token.attacker.example/oauth2/token",
        "scope": "s",
    }
    http_mock = MagicMock()
    with (
        patch("socket.getaddrinfo", return_value=_addrinfo_for(blocked_ip)),
        patch("urllib3.PoolManager", return_value=http_mock),
    ):
        with pytest.raises(_DiscoveryUrlBlocked):
            get_cognito_token(client_info)
    http_mock.request.assert_not_called()


def test_the_secret_is_not_even_resolved_when_the_destination_is_refused():
    """Ordering, not decoration: validate the destination BEFORE dereferencing the
    Secrets Manager reference. Resolving first would pull the plaintext into the frame
    (and spend a GetSecretValue) for a request that is about to be refused."""
    client_info = {
        "client_id": "c",
        # A Secrets Manager NAME, not a secret — which is the whole point of the
        # reference indirection. pragma: allowlist secret
        "client_secret_ref": "agentcore-gateway/should-never-be-read",  # pragma: allowlist secret
        "token_endpoint": "http://169.254.169.254/latest/meta-data/",
        "scope": "s",
    }
    with patch.object(gd, "resolve_client_secret") as resolver:
        with pytest.raises(_DiscoveryUrlInvalid):
            get_cognito_token(client_info)
    resolver.assert_not_called()


# ---------------------------------------------------------------------------
# The happy path still works — a refusal-only suite would hide a dead one
# ---------------------------------------------------------------------------


def test_a_real_cognito_token_endpoint_still_mints_a_token():
    client_info = {
        "client_id": "c",
        "client_secret": FAKE_SECRET,
        "token_endpoint": "https://ac-demo.auth.us-east-1.amazoncognito.com/oauth2/token",
        "scope": "agentcore-demo/invoke",
    }
    resp = MagicMock(status=200, data=json.dumps({"access_token": "real-token"}).encode())
    http_mock = MagicMock()
    http_mock.request.return_value = resp
    with (
        patch("socket.getaddrinfo", return_value=_addrinfo_for("52.94.236.248")),
        patch("urllib3.PoolManager", return_value=http_mock),
    ):
        assert get_cognito_token(client_info) == "real-token"
    # And it went where it was told to go, with the secret in the body.
    args, kwargs = http_mock.request.call_args
    assert args[1] == client_info["token_endpoint"]
    assert FAKE_SECRET in kwargs["body"]


def test_pinning_an_identity_provider_does_not_break_every_cognito_gateway(monkeypatch):
    """The regression the dedicated env var exists to prevent.

    An operator sets ``OIDC_DISCOVERY_HOST_ALLOWLIST=*.okta.com`` to pin their IDP.
    That says nothing about token endpoints, and every Cognito gateway in the account
    mints against ``*.amazoncognito.com`` from an endpoint this platform derived
    itself. Honouring that pin here would have broken the default path while claiming
    to secure it — the same mistake ``_validate_outbound_url`` already had to undo
    once for connector and LiteLLM URLs.
    """
    monkeypatch.setenv("OIDC_DISCOVERY_HOST_ALLOWLIST", "*.okta.com")
    monkeypatch.setenv("OUTBOUND_HOST_ALLOWLIST", "*.okta.com")
    monkeypatch.delenv("OAUTH_TOKEN_ENDPOINT_HOST_ALLOWLIST", raising=False)
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("52.94.236.248")):
        assert validate_token_endpoint("https://ac-demo.auth.us-east-1.amazoncognito.com/oauth2/token")


def test_an_operator_can_pin_token_endpoints_with_their_own_variable(monkeypatch):
    monkeypatch.setenv("OAUTH_TOKEN_ENDPOINT_HOST_ALLOWLIST", "*.okta.com")
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("52.94.236.248")):
        assert validate_token_endpoint("https://acme.okta.com/oauth2/v1/token")
        with pytest.raises(_DiscoveryUrlBlocked) as exc:
            validate_token_endpoint("https://ac-demo.auth.us-east-1.amazoncognito.com/oauth2/token")
    # The rejection names the variable that actually blocked it, not one the operator
    # never set.
    assert "OAUTH_TOKEN_ENDPOINT_HOST_ALLOWLIST" in str(exc.value)


def test_an_allowlisted_host_still_cannot_resolve_into_the_denylist(monkeypatch):
    """An allowlist is not an override. Same property the discovery guard has."""
    monkeypatch.setenv("OAUTH_TOKEN_ENDPOINT_HOST_ALLOWLIST", "*.okta.com")
    with patch("socket.getaddrinfo", return_value=_addrinfo_for("169.254.169.254")):
        with pytest.raises(_DiscoveryUrlBlocked):
            validate_token_endpoint("https://acme.okta.com/oauth2/v1/token")


# ---------------------------------------------------------------------------
# The entry point: a discovery document that advertises somewhere else
# ---------------------------------------------------------------------------


def _discovery_response(doc: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(doc).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: False
    return resp


def test_a_discovery_document_cannot_redirect_the_secret_to_imds():
    """The confused deputy in one test: the document is fetched from a host that
    passes validation, and the endpoint it advertises is IMDS."""
    identity_config = {
        "provider": "custom",
        "client_id": "abc",
        "clientSecretRef": "agentcore-gateway/ref",  # pragma: allowlist secret
        "discovery_url": "https://login.vendor.example/.well-known/openid-configuration",
    }
    doc = _discovery_response({"token_endpoint": "http://169.254.169.254/latest/meta-data/"})
    with (
        patch("socket.getaddrinfo", return_value=_addrinfo_for("52.94.236.248")),
        patch("urllib.request.urlopen", return_value=doc),
    ):
        with pytest.raises(_DiscoveryUrlInvalid) as exc:
            gd._create_external_oauth_config(identity_config, region="us-east-1")

    msg = str(exc.value)
    # The error must name the DOCUMENT as the source. Inside the fetch try-block the
    # surrounding `except Exception` would have rewrapped this as "Failed to fetch OIDC
    # discovery document", sending the operator to look at their network when the fetch
    # worked perfectly and the content was refused.
    assert "discovery document" in msg
    assert "Failed to fetch" not in msg


def test_a_well_behaved_discovery_document_is_accepted_unchanged():
    identity_config = {
        "provider": "okta",
        "client_id": "abc",
        "clientSecretRef": "agentcore-gateway/ref",  # pragma: allowlist secret
        "discovery_url": "https://acme.okta.com/.well-known/openid-configuration",
        "scopes": ["gw/invoke"],
    }
    endpoint = "https://acme.okta.com/oauth2/v1/token"
    doc = _discovery_response({"token_endpoint": endpoint})
    with (
        patch("socket.getaddrinfo", return_value=_addrinfo_for("52.94.236.248")),
        patch("urllib.request.urlopen", return_value=doc),
    ):
        out = gd._create_external_oauth_config(identity_config, region="us-east-1")
    assert out["client_info"]["token_endpoint"] == endpoint


# ---------------------------------------------------------------------------
# The bake-in path: what the AGENT is handed
# ---------------------------------------------------------------------------


def test_the_shape_check_asks_no_dns_question():
    """Pins the deliberate weakness. If someone "strengthens" this into a resolving
    check, every gateway deploy on the default Cognito path gains a DNS dependency it
    does not need — and still learns nothing about what the runtime resolves later."""
    with patch("socket.getaddrinfo", side_effect=AssertionError("no DNS here")) as dns:
        assert validate_token_endpoint_shape("https://ac-demo.auth.us-east-1.amazoncognito.com/oauth2/token")
    dns.assert_not_called()


@pytest.mark.parametrize(
    ("url", "exc"),
    [
        ("http://ac.auth.us-east-1.amazoncognito.com/oauth2/token", _DiscoveryUrlInvalid),
        ("https://169.254.169.254/oauth2/token", _DiscoveryUrlBlocked),
        ("https://127.0.0.1/oauth2/token", _DiscoveryUrlBlocked),
        ("https://[::1]/oauth2/token", _DiscoveryUrlBlocked),
        ("https://10.0.0.5/oauth2/token", _DiscoveryUrlBlocked),
        ("", _DiscoveryUrlInvalid),
        ("https:///oauth2/token", _DiscoveryUrlInvalid),
    ],
)
def test_the_shape_check_refuses_what_is_decidable_without_dns(url, exc):
    with pytest.raises(exc):
        validate_token_endpoint_shape(url)


@pytest.mark.parametrize(
    "provider",
    [
        # Two separate writes, two separate branches, and the env var names differ
        # (COGNITO_TOKEN_ENDPOINT vs OAUTH_TOKEN_ENDPOINT). A test covering only one
        # leaves the other guard unexercised — mutation-testing this fix found exactly
        # that: deleting the Cognito-branch guard left the whole suite green.
        "cognito",
        None,  # absent provider falls into the same Cognito branch
        "okta",
    ],
)
def test_no_runtime_is_created_when_the_agent_would_be_handed_a_cleartext_endpoint(provider):
    """Fail-closed at the step that writes the env var: the runtime must not exist.

    This is the sink the platform's own guards can never cover — the generated agent
    POSTs its secret from inside the runtime, so the last moment anyone can refuse is
    before the runtime is created.
    """
    from app.step_handlers import runtime_configure_step as rcs

    client_info = {
        "client_id": "abc",
        "client_secret_ref": "agentcore-gateway/ref",  # pragma: allowlist secret
        "token_endpoint": "http://attacker.example/collect",
        "scope": "gw/invoke",
    }
    if provider is not None:
        client_info["provider"] = provider

    event = {
        "deployment_id": "d-ssrf",
        "config": {
            "name": "SSRF Probe",
            "model": {"modelId": "us.anthropic.claude-sonnet-5"},
            "systemPrompt": "hi",
        },
        "s3_bucket": "b",
        "s3_key": "k",
        "role_arn": "arn:aws:iam::123456789012:role/AgentCoreRuntime-ssrf-probe",
        "gateway_result": {
            "gateway_url": "https://gw.example/mcp",
            "client_info": client_info,
        },
    }

    with (
        patch.object(rcs, "_get_deployment_store", return_value=MagicMock()),
        patch.object(rcs, "_get_env", side_effect=lambda n, d="": d),
        patch.object(rcs, "create_agent_runtime") as create,
    ):
        with pytest.raises(_DiscoveryUrlInvalid):
            rcs.handler(event, None)

    create.assert_not_called()


def test_the_legacy_direct_deploy_path_guards_the_same_endpoint():
    """``WorkflowExecutor.deploy`` builds its own env block and has its own copy of this
    write, so it needs its own guard.

    STRUCTURAL, not behavioural, and deliberately so: ``deploy`` is a full orchestration
    coroutine with no test harness in this repo, and standing up enough of AWS to reach
    the env block would test the mocks rather than the code. The same trade-off is
    already taken and documented in
    ``test_the_gateway_client_secret_travels_by_reference.py``. Its limit is real — this
    catches the guard being *removed*, which is how the two paths actually diverge,
    and would not catch the guard being *weakened*.
    """
    import inspect

    from app.services.deployment import WorkflowExecutor

    src = inspect.getsource(WorkflowExecutor.deploy)
    marker = 'env_vars["COGNITO_TOKEN_ENDPOINT"] = _te'
    assert marker in src, "the direct deploy path no longer emits COGNITO_TOKEN_ENDPOINT by this name"
    before = src[: src.index(marker)]
    assert "validate_token_endpoint_shape(_te" in before[-600:], (
        "the direct deploy path hands COGNITO_TOKEN_ENDPOINT to the agent without "
        "validating it; the generated agent POSTs its client secret there (see the "
        "urllib.request.Request(COGNITO_TOKEN_ENDPOINT, ...) it emits) and performs no "
        "check of its own, so this is the last place a cleartext destination can be refused"
    )
    # Empty stays empty: this path has always emitted the key unconditionally, and ""
    # means "no token endpoint configured" rather than a destination to validate.
    assert "if _te:" in before, "the empty-stays-empty guard is gone; an unconfigured deploy will now fail"


def test_the_generated_agent_cannot_defend_itself():
    """Why the bake-in guard is load-bearing rather than belt-and-braces.

    The generated agent's own token fetch takes the endpoint straight from its
    environment and opens the connection. It has no denylist, no allowlist and no
    resolution check, and it is holding the client secret when it does this. If the
    platform hands over a bad value there is nothing downstream that will refuse it.
    """
    import inspect

    from app.services.deployment import generate_unified_agent_code

    emitted = inspect.getsource(generate_unified_agent_code)
    assert "urllib.request.Request(COGNITO_TOKEN_ENDPOINT" in emitted
    # If this ever stops being true, the guard above can be reconsidered — not before.
    assert "validate_token_endpoint" not in emitted


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
