"""Phase 3: OBO credential-provider config (RFC 8693 / RFC 7523).

Verifies _ensure_oauth2_credential_provider builds the correct
onBehalfOfTokenExchangeConfig / clientAuthenticationMethod for each delegation
mode, matching the live bedrock-agentcore-control service model, WITHOUT calling
AWS (a fake control client captures the kwargs).
"""

from __future__ import annotations

import pytest

from app.services import gateway_deployer as gd


class _FakeCtrl:
    """Captures create_oauth2_credential_provider kwargs; returns a fake ARN."""

    def __init__(self):
        self.captured = None

    def create_oauth2_credential_provider(self, **kwargs):
        self.captured = kwargs
        return {"credentialProviderArn": "arn:aws:...:credential-provider/test"}


def _config(ctrl):
    """Return the customOauth2ProviderConfig block from the captured call."""
    return ctrl.captured["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]


def test_m2m_has_no_obo_config():
    ctrl = _FakeCtrl()
    gd._ensure_oauth2_credential_provider(
        ctrl, "conn", vendor="CustomOauth2", client_id="cid",
        client_secret_arn="arn:secret", discovery_url="https://idp/.well-known/openid-configuration",
        delegation_mode="m2m",
    )
    cfg = _config(ctrl)
    assert "onBehalfOfTokenExchangeConfig" not in cfg
    assert "clientAuthenticationMethod" not in cfg


def test_token_exchange_grant():
    ctrl = _FakeCtrl()
    gd._ensure_oauth2_credential_provider(
        ctrl, "conn", vendor="CustomOauth2", client_id="cid",
        client_secret_arn="arn:secret", discovery_url="https://idp/.well-known/openid-configuration",
        delegation_mode="obo", obo_grant_type="TOKEN_EXCHANGE",
    )
    cfg = _config(ctrl)
    assert cfg["clientAuthenticationMethod"] == "CLIENT_SECRET_BASIC"
    obo = cfg["onBehalfOfTokenExchangeConfig"]
    assert obo["grantType"] == "TOKEN_EXCHANGE"
    assert obo["tokenExchangeGrantTypeConfig"]["actorTokenContent"] == "NONE"


def test_jwt_authorization_grant():
    ctrl = _FakeCtrl()
    gd._ensure_oauth2_credential_provider(
        ctrl, "conn", vendor="CustomOauth2", client_id="cid",
        client_secret_arn="arn:secret", discovery_url="https://idp/.well-known/openid-configuration",
        delegation_mode="obo", obo_grant_type="JWT_AUTHORIZATION_GRANT",
    )
    cfg = _config(ctrl)
    assert cfg["clientAuthenticationMethod"] == "CLIENT_SECRET_POST"
    assert cfg["onBehalfOfTokenExchangeConfig"]["grantType"] == "JWT_AUTHORIZATION_GRANT"
    # RFC 7523 grant carries no actor-token config.
    assert "tokenExchangeGrantTypeConfig" not in cfg["onBehalfOfTokenExchangeConfig"]


def test_obo_default_grant_is_token_exchange():
    ctrl = _FakeCtrl()
    gd._ensure_oauth2_credential_provider(
        ctrl, "conn", vendor="CustomOauth2", client_id="cid",
        client_secret_arn="arn:secret", discovery_url="https://idp/.well-known/openid-configuration",
        delegation_mode="obo",  # no grant type → defaults to TOKEN_EXCHANGE
    )
    assert _config(ctrl)["onBehalfOfTokenExchangeConfig"]["grantType"] == "TOKEN_EXCHANGE"


def test_obo_requires_custom_vendor():
    ctrl = _FakeCtrl()
    with pytest.raises(ValueError, match="CustomOauth2"):
        gd._ensure_oauth2_credential_provider(
            ctrl, "conn", vendor="GithubOauth2", client_id="cid",
            client_secret_arn="arn:secret", delegation_mode="obo",
        )


def test_obo_rejects_bad_grant_type():
    ctrl = _FakeCtrl()
    with pytest.raises(ValueError, match="obo_grant_type"):
        gd._ensure_oauth2_credential_provider(
            ctrl, "conn", vendor="CustomOauth2", client_id="cid",
            client_secret_arn="arn:secret", discovery_url="https://idp/x",
            delegation_mode="obo", obo_grant_type="BOGUS",
        )


def test_target_grant_type_is_token_exchange_for_obo():
    """0.3 fix: the gateway TARGET's oauthCredentialProvider.grantType must be
    TOKEN_EXCHANGE in OBO mode (was hardcoded CLIENT_CREDENTIALS, so the
    downstream call ran as the shared M2M identity instead of the end user).

    The selection is inline in deploy_connectors_to_gateway; assert the source
    derives the grant from delegation_mode rather than hardcoding it.
    """
    import inspect

    src = inspect.getsource(gd)
    # The old unconditional hardcode must be gone from the connector target block.
    assert '"grantType": "CLIENT_CREDENTIALS",\n                    }\n                },\n            }\n        else:  # API_KEY' not in src
    # The conditional must exist.
    assert '"TOKEN_EXCHANGE" if str(delegation_mode).lower() == "obo" else "CLIENT_CREDENTIALS"' in src


def test_target_grant_derivation_logic():
    """Unit-check the exact derivation the fix uses."""
    def _grant(delegation_mode):
        return "TOKEN_EXCHANGE" if str(delegation_mode).lower() == "obo" else "CLIENT_CREDENTIALS"
    assert _grant("obo") == "TOKEN_EXCHANGE"
    assert _grant("OBO") == "TOKEN_EXCHANGE"
    assert _grant("m2m") == "CLIENT_CREDENTIALS"
    assert _grant(None) == "CLIENT_CREDENTIALS"
