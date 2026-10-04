"""A deployment may only record for deletion the secret it created itself.

Found live on 2026-09-21 while re-verifying the gateway-role manifest fix. A probe
deploy against a fake *external* identity provider wrote this into the deployment's
resource inventory::

    {"type": "secret", "id": "agentcore-gateway/f6pb2-does-not-exist"}

That reference came from ``identity_config["clientSecretRef"]`` — the CUSTOMER's OAuth
client secret, living in the CUSTOMER's Secrets Manager. The platform created nothing.
``_create_external_oauth_config`` copies the reference into ``client_info`` under the
same key the Cognito paths use for the per-deployment copy they mint themselves, so
``_record_gateway_resources`` could not tell the two apart and recorded both.

What that row means is the severity. The teardown arm for a ``secret`` row is::

    delete_secret(SecretId=rid, ForceDeleteWithoutRecovery=True)

No ownership check, and no 7-day recovery window. An external identity provider's client
secret is normally shared by every agent authenticating against that provider, so
deleting ONE agent would have irrecoverably destroyed the credential for all of them,
with re-issuing it in the IDP the only way back. That is pinned below rather than
described, because it is what makes the row dangerous rather than untidy.

The fix is the same shape as the gateway-role fix next door
(``test_the_manifest_records_only_the_role_that_exists.py``): the producer states what
it created. The two Cognito paths publish the ref a second time as
``minted_client_secret_ref``; the external path publishes only ``client_secret_ref``;
the manifest reads the minted key. Ownership is asserted by whoever minted it, never
inferred from the name — a prefix like ``agentcore-gateway/`` proves the namespace and
nothing else, which is the recurring lesson in this file's neighbours (F-6, F-7).

ARCC ``cnt_77BHvX7WzuG1X8`` (share the reference, not the secret) — and note the mirror
of it here: holding a reference does not confer authority to destroy the referent.

Mutation-tested 7/7, including both halves of the asymmetry (mark the external ref as
minted; drop the mark from either Cognito producer), reading ``client_secret_ref`` again,
recording the row unconditionally, dropping the key from the failure-path allow-list, and
softening the delete.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "src")

from app.services import gateway_deployer as gd  # noqa: E402
from app.step_handlers import gateway_step as gs  # noqa: E402

MINTED = "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-gateway/dep-1-abc"
CUSTOMER = "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/okta/agent-client-AbCdEf"


class _Store:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record_resource(self, _deployment_id: str, resource: dict) -> None:
        self.rows.append(resource)


def _secret_rows(result: dict) -> list[str]:
    store = _Store()
    gs._record_gateway_resources(store, "d-1", "us-east-1", result)
    return [r["id"] for r in store.rows if r["type"] == "secret"]


# ---------------------------------------------------------------------------
# What the row costs, so the tests below are read with the right stakes
# ---------------------------------------------------------------------------


def test_a_secret_row_cannot_delete_without_exact_deployment_proof():
    """A manifest row is inventory, not delete authority.

    The deletion is still irrecoverable once authorized, so the dispatcher must
    refuse a customer reference before issuing any Secrets Manager mutation.
    """
    from app.deployment_handler import _delete_managed_resource
    from app.services import step_clients

    # NOT patch("boto3.client"): the dispatcher rebinds the name `boto3` to a local
    # cross-account shim, so every client it builds goes through step_clients.client.
    # Patching the real boto3 left this test making a live SecretsManager call, which
    # failed here only because no credentials were configured.
    sm = MagicMock()
    with (
        patch.object(step_clients, "client", return_value=sm),
        pytest.raises(gd.ConnectorSecretDeletionRefused),
    ):
        _delete_managed_resource(
            {"type": "secret", "id": CUSTOMER},
            "us-east-1",
            deployment_id="dep-1",
        )

    sm.describe_secret.assert_not_called()
    sm.delete_secret.assert_not_called()


# ---------------------------------------------------------------------------
# The producers
# ---------------------------------------------------------------------------


def test_an_external_provider_ref_is_not_marked_as_ours():
    """The live case. The reference is carried (the runtime has to resolve it) but it is
    NOT marked as something this deploy minted."""
    doc = MagicMock()
    doc.read.return_value = b'{"token_endpoint": "https://acme.okta.com/oauth2/v1/token"}'
    doc.__enter__ = lambda s: s
    doc.__exit__ = lambda *a: False
    identity_config = {
        "provider": "okta",
        "client_id": "abc",
        "clientSecretRef": CUSTOMER,  # pragma: allowlist secret
        "discovery_url": "https://acme.okta.com/.well-known/openid-configuration",
    }
    with (
        patch("socket.getaddrinfo", return_value=[(2, 1, 0, "", ("52.94.236.248", 443))]),
        patch("urllib.request.urlopen", return_value=doc),
    ):
        ci = gd._create_external_oauth_config(identity_config, region="us-east-1")["client_info"]

    assert ci["client_secret_ref"] == CUSTOMER
    assert "minted_client_secret_ref" not in ci


def test_a_dedicated_pool_marks_its_own_copy():
    """The other half of a refusal-only suite: the platform's per-deployment copy must
    still be recorded, or every Cognito gateway teardown orphans a secret holding a
    live app-client credential."""
    cog = MagicMock()
    cog.create_user_pool.return_value = {"UserPool": {"Id": "us-east-1_AAAA"}}
    cog.create_user_pool_client.return_value = {
        "UserPoolClient": {"ClientId": "cid", "ClientSecret": "unused-in-this-test"}  # pragma: allowlist secret
    }
    with (
        patch.dict("os.environ", {"GATEWAY_SHARED_USER_POOL_ID": "", "GATEWAY_SHARED_USER_POOL_DOMAIN": ""}),
        patch.object(gd, "_mint_client_secret_ref", return_value=MINTED),
        patch.object(gd.time, "sleep"),
    ):
        ci = gd._create_cognito_oauth(cog, "my-gw", "us-east-1", "sub-1", "dep-1")["client_info"]

    assert ci["minted_client_secret_ref"] == MINTED == ci["client_secret_ref"]


def test_the_shared_pool_path_marks_its_own_copy_too():
    """Two producers, two chances to forget. The shared-pool path is the one that runs
    on every current stack."""
    cog = MagicMock()
    cog.create_user_pool_client.return_value = {
        "UserPoolClient": {"ClientId": "cid", "ClientSecret": "unused-in-this-test"}  # pragma: allowlist secret
    }
    with patch.object(gd, "_mint_client_secret_ref", return_value=MINTED):
        ci = gd._create_cognito_oauth_in_shared_pool(
            cog, "my-gw", "us-east-1", "us-east-1_SHARED", "acme-gw-domain", "sub-1", "dep-1"
        )["client_info"]

    assert ci["minted_client_secret_ref"] == MINTED
    assert ci["shared_pool"] is True


# ---------------------------------------------------------------------------
# What the manifest does with each
# ---------------------------------------------------------------------------


def test_a_customer_secret_gets_no_row():
    rows = _secret_rows(
        {
            "success": False,
            "gateway_name": "gw",
            "client_info": {"provider": "okta", "client_secret_ref": CUSTOMER},  # pragma: allowlist secret
        }
    )
    assert rows == [], f"recorded a force-delete for a secret the platform never created: {rows}"


def test_a_minted_secret_still_gets_its_row():
    rows = _secret_rows(
        {
            "success": True,
            "gateway_name": "gw",
            "client_info": {
                "user_pool_id": "us-east-1_AAAA",
                "client_secret_ref": MINTED,  # pragma: allowlist secret
                "minted_client_secret_ref": MINTED,  # pragma: allowlist secret
                "shared_pool": True,
                "client_id": "cid",
                "scope": "agentcore-gw/invoke",
            },
        }
    )
    assert rows == [MINTED]


def test_a_failed_deploy_still_records_the_minted_secret():
    """The failure path reduces client_info to an allow-list, and the minted key has to
    be on it. It was measured missing once already for ``client_secret_ref`` itself: a
    Cognito deploy that failed mid-way orphaned the per-deployment copy of the app
    client's secret, which is a live credential nothing else names.
    """
    iam, ctrl = MagicMock(), MagicMock()
    ctrl.list_gateways.return_value = {"items": []}  # no same-name gateway: the create path
    iam.exceptions.EntityAlreadyExistsException = type("E", (Exception,), {})
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123456789012:role/AgentCoreGateway-gw"}}
    ctrl.create_gateway.side_effect = RuntimeError("AccessDeniedException")
    with (
        patch.object(gd, "_create_agentcore_control_client", return_value=ctrl),
        patch.object(gd, "_create_cognito_client", return_value=MagicMock()),
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd.boto3, "client", return_value=MagicMock()),
        patch.object(gd.time, "sleep"),
        patch.object(
            gd,
            "_create_cognito_oauth",
            return_value={
                "authorizer_config": {},
                "client_info": {
                    "user_pool_id": "us-east-1_AAAA",
                    "client_id": "cid",
                    "scope": "agentcore-gw/invoke",
                    "client_secret_ref": MINTED,  # pragma: allowlist secret
                    "minted_client_secret_ref": MINTED,  # pragma: allowlist secret
                    "shared_pool": True,
                },
            },
        ),
    ):
        out = gd.deploy_gateway(gateway_config={"name": "gw"}, region="us-east-1")

    assert out["success"] is False
    assert out["client_info"]["minted_client_secret_ref"] == MINTED
    assert _secret_rows(out) == [MINTED]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
