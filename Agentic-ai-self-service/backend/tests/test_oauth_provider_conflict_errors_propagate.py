"""Redeploy audit 2026-09-28, row 3: the OAuth2 provider conflict branch must not swallow its own errors.

On every redeploy of the MCP-server-gateway template the shared ``mcp-cred-<gateway>`` provider is
REPOINTED at the new client. The repoint's ``RuntimeError`` and the ownership refusal used to be raised
inside a ``try`` whose ``except Exception: logger.debug(...)`` fell through to a bare ``raise`` of the
original ConflictException, so a denied UpdateOauth2CredentialProvider surfaced as "already exists" and
the provider silently kept the previous deployment's client id and secret.
"""

from __future__ import annotations

import sys

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, "src")

from app.services import gateway_deployer as gd  # noqa: E402

PROVIDER_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/mcp-cred-gw"
)


def _conflict(op: str) -> ClientError:
    return ClientError({"Error": {"Code": "ConflictException", "Message": "already exists"}}, op)


class _Vault:
    def __init__(self, *, update_fails: bool = False, lookup_fails: bool = False) -> None:
        self.update_fails = update_fails
        self.lookup_fails = lookup_fails
        self.updates = 0

    def create_oauth2_credential_provider(self, **_kw):
        raise _conflict("CreateOauth2CredentialProvider")

    def get_oauth2_credential_provider(self, *, name):
        if self.lookup_fails:
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "GetOauth2CredentialProvider"
            )
        return {"credentialProviderArn": PROVIDER_ARN, "name": name}

    def update_oauth2_credential_provider(self, **_kw):
        self.updates += 1
        if self.update_fails:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
                "UpdateOauth2CredentialProvider",
            )
        return {}


def _ensure(vault):
    return gd._ensure_oauth2_credential_provider(
        vault,
        "mcp-cred-gw",
        vendor="CustomOauth2",
        client_id="client-2",
        client_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-connector/x",
        discovery_url="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_x/.well-known/openid-configuration",
        region="us-east-1",
    )


@pytest.fixture(autouse=True)
def _owned(monkeypatch):
    monkeypatch.setattr(gd, "assert_agentcore_resource_owned", lambda *a, **k: None)


def test_a_conflict_repoints_the_existing_provider_and_returns_its_arn():
    vault = _Vault()
    assert _ensure(vault) == PROVIDER_ARN
    assert vault.updates == 1


def test_a_denied_repoint_fails_with_its_own_reason_not_as_already_exists():
    vault = _Vault(update_fails=True)
    with pytest.raises(RuntimeError) as error:
        _ensure(vault)
    text = str(error.value)
    assert "could not be repointed" in text
    assert "AccessDeniedException" in text
    assert vault.updates == 1


def test_an_ownership_refusal_propagates_as_itself(monkeypatch):
    class Foreign(RuntimeError):
        pass

    def _refuse(*_a, **_k):
        raise Foreign("provider belongs to another stack")

    monkeypatch.setattr(gd, "assert_agentcore_resource_owned", _refuse)
    vault = _Vault()
    with pytest.raises(Foreign):
        _ensure(vault)
    assert vault.updates == 0, "a foreign provider must never be repointed"


def test_only_a_failed_lookup_falls_back_to_the_original_conflict():
    vault = _Vault(lookup_fails=True)
    with pytest.raises(ClientError) as error:
        _ensure(vault)
    assert error.value.response["Error"]["Code"] == "ConflictException"
    assert vault.updates == 0
