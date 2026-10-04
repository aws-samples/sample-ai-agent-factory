"""The deployment manifest must not name an IAM role that was never created.

Found by a live measurement, not by reading. On 2026-09-21 an F-6 probe deploy was
refused at Step 1 of ``deploy_gateway`` — the OIDC discovery document advertised a
cleartext ``token_endpoint``, so ``_create_external_oauth_config`` raised roughly
fourteen lines before ``create_role`` could run. ``aws iam list-roles`` filtered on the
probe name returned empty, and ``list-gateways`` likewise. Yet the deployment record
carried::

    {"M": {"region": {"S": "us-east-1"},
           "type": {"S": "iam_role"},
           "name": {"S": "AgentCoreGateway-f6probe"}}}

because ``_record_gateway_resources`` derived the row from ``gateway_name``, a string
bound at the top of the function and therefore present for *every* failure, including
ones that happen before any AWS call.

Nothing broke: the teardown's ``delete_role`` reads ``NoSuchEntity`` as "already
absent". The defect is that the manifest asserted a resource that never existed, which
is the same class as the shared-pool row documented in the failure-path allow-list in
``gateway_deployer`` — and there the defence-in-depth that saved it was two unrelated
guards, not the manifest being right.

The asymmetry is the whole design of the fix, and both directions are pinned below:

* fail BEFORE Step 1b -> no ``iam_role`` row (nothing exists to record)
* fail AFTER Step 1b, before Step 2 -> the row MUST be written, because the role is
  real and this failed deploy's manifest is the only thing that will ever name it.

So the row cannot be gated on ``gateway_id`` either; it is gated on a field
``deploy_gateway`` sets only once IAM has confirmed the role.

Mutation-tested 7/8. The survivor is equivalent, not a gap: swapping
``gw_role_confirmed`` for ``gw_role_name`` on the SUCCESS result cannot be observed,
because reaching that line means both role arms ran and the two names are provably the
same string. It stays written as the confirmed field so there is one source of truth
rather than two expressions that only happen to agree.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "src")

from app.services import gateway_deployer as gd  # noqa: E402
from app.step_handlers import gateway_step as gs  # noqa: E402


class _Store:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record_resource(self, deployment_id: str, resource: dict) -> None:
        self.rows.append(resource)


def _kinds(store: _Store) -> list[str]:
    return [r["type"] for r in store.rows]


# ---------------------------------------------------------------------------
# What deploy_gateway reports
# ---------------------------------------------------------------------------


def _mock_clients(iam: MagicMock, ctrl: MagicMock):
    """Patch away every client deploy_gateway builds before Step 2."""
    return (
        patch.object(gd, "_create_agentcore_control_client", return_value=ctrl),
        patch.object(gd, "_create_cognito_client", return_value=MagicMock()),
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd.boto3, "client", return_value=MagicMock()),
        patch.object(gd.time, "sleep"),
    )


_IDENTITY = {"provider": "custom", "client_id": "abc", "discovery_url": "https://idp.example/.well-known/x"}


def test_a_failure_before_the_role_step_reports_no_role():
    """The exact live case, inverted. Nothing in IAM was touched, so there is nothing
    to put in the manifest — and the proof is that ``create_role`` was never called."""
    iam, ctrl = MagicMock(), MagicMock()
    ctrl.list_gateways.return_value = {"items": []}  # no same-name gateway: the create path
    ps = _mock_clients(iam, ctrl)
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with patch.object(
            gd,
            "_create_external_oauth_config",
            side_effect=gd._DiscoveryUrlInvalid("token_endpoint ... must use https scheme (got 'http')"),
        ):
            out = gd.deploy_gateway(gateway_config={"name": "f6probe"}, region="us-east-1", identity_config=_IDENTITY)

    assert out["success"] is False
    iam.create_role.assert_not_called()
    # gateway_name is still reported (teardown's other arms key off it); the ROLE is not.
    assert out["gateway_name"] == "f6probe"
    assert out.get("gateway_role_name") is None


def test_a_failure_after_the_role_step_still_reports_the_role():
    """The direction that must NOT regress. The role is created at Step 1b and the
    gateway only at Step 2, so a gateway-creation failure leaves a real role behind.
    Dropping this row — the obvious way to "fix" the case above, by gating on
    gateway_id — would strand an IAM role on every failed CreateGateway.
    """
    iam, ctrl = MagicMock(), MagicMock()
    ctrl.list_gateways.return_value = {"items": []}  # no same-name gateway: the create path
    iam.exceptions.EntityAlreadyExistsException = type("E", (Exception,), {})
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123456789012:role/AgentCoreGateway-f6probe"}}
    ctrl.create_gateway.side_effect = RuntimeError("AccessDeniedException: not authorized to CreateGateway")
    ps = _mock_clients(iam, ctrl)
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with patch.object(
            gd,
            "_create_external_oauth_config",
            return_value={
                "authorizer_config": {"customJWTAuthorizer": {}},
                "client_info": {"provider": "custom", "user_pool_id": "", "client_id": "abc"},
            },
        ):
            out = gd.deploy_gateway(gateway_config={"name": "f6probe"}, region="us-east-1", identity_config=_IDENTITY)

    assert out["success"] is False
    iam.create_role.assert_called_once()
    assert out["gateway_role_name"] == "AgentCoreGateway-f6probe"
    # And no gateway id, which is exactly why gating the row on one would be wrong.
    assert not out.get("gateway_id")


def test_a_failure_before_gateway_creation_immediately_cleans_confirmed_inventory():
    """The fast abort path cannot wait for a gateway id.

    Cognito setup and the gateway IAM role both happen before CreateGateway. A
    CreateGateway failure therefore has no gateway id but already has resources
    holding credentials/permissions that should be released immediately; the
    manifest remains the durable fallback if that cleanup reports a failure.
    """
    iam, ctrl = MagicMock(), MagicMock()
    ctrl.list_gateways.return_value = {"items": []}  # no same-name gateway: the create path
    iam.exceptions.EntityAlreadyExistsException = type("E", (Exception,), {})
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123456789012:role/AgentCoreGateway-f6probe"}}
    ctrl.create_gateway.side_effect = RuntimeError("AccessDeniedException: not authorized to CreateGateway")
    ps = _mock_clients(iam, ctrl)
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with (
            patch.object(
                gd,
                "_create_external_oauth_config",
                return_value={
                    "authorizer_config": {"customJWTAuthorizer": {}},
                    "client_info": {"provider": "custom", "user_pool_id": "", "client_id": "abc"},
                },
            ),
            patch.object(gd, "cleanup_gateway_resources", return_value=[]) as cleanup,
        ):
            out = gd.deploy_gateway(
                gateway_config={"name": "f6probe"},
                region="us-east-1",
                identity_config=_IDENTITY,
            )

    assert out["success"] is False
    cleanup.assert_called_once()
    abort_config = cleanup.call_args.args[2]
    assert abort_config.get("gateway_id") is None
    assert abort_config["gateway_role_name"] == "AgentCoreGateway-f6probe"


def test_a_role_that_could_not_be_created_is_not_reported():
    """The narrower window the first test cannot see. It fails at Step 1, before the
    role NAME is even built, so a "confirm at declaration" bug is invisible to it —
    mutation testing showed that mutant surviving. Here ``CreateRole`` itself is
    refused (a deploy role without ``iam:CreateRole`` is the realistic cause): the name
    has been computed, nothing exists in IAM, and the manifest must stay silent.
    """
    iam, ctrl = MagicMock(), MagicMock()
    ctrl.list_gateways.return_value = {"items": []}  # no same-name gateway: the create path
    iam.exceptions.EntityAlreadyExistsException = type("E", (Exception,), {})
    iam.create_role.side_effect = RuntimeError("AccessDenied: not authorized to perform iam:CreateRole")
    ps = _mock_clients(iam, ctrl)
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with patch.object(
            gd,
            "_create_external_oauth_config",
            return_value={"authorizer_config": {}, "client_info": {"provider": "custom"}},
        ):
            out = gd.deploy_gateway(gateway_config={"name": "f6probe"}, region="us-east-1", identity_config=_IDENTITY)

    assert out["success"] is False
    ctrl.create_gateway.assert_not_called()
    assert out.get("gateway_role_name") is None


def test_a_role_created_but_left_unpolicied_is_still_reported():
    """The mirror hazard, and the reason the confirmation sits immediately after
    ``create_role`` rather than after the inline-policy work: a permissions boundary or
    SCP can reject ``put_role_policy`` while the role itself already exists. Confirming
    after that call would report None for the one failure that definitely leaves a role
    behind.
    """
    iam, ctrl = MagicMock(), MagicMock()
    ctrl.list_gateways.return_value = {"items": []}  # no same-name gateway: the create path
    iam.exceptions.EntityAlreadyExistsException = type("E", (Exception,), {})
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123456789012:role/AgentCoreGateway-f6probe"}}
    iam.put_role_policy.side_effect = RuntimeError("AccessDenied: permissions boundary denies PutRolePolicy")
    ps = _mock_clients(iam, ctrl)
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with patch.object(
            gd,
            "_create_external_oauth_config",
            return_value={"authorizer_config": {}, "client_info": {"provider": "custom"}},
        ):
            out = gd.deploy_gateway(gateway_config={"name": "f6probe"}, region="us-east-1", identity_config=_IDENTITY)

    assert out["success"] is False
    iam.create_role.assert_called_once()
    assert out["gateway_role_name"] == "AgentCoreGateway-f6probe", (
        "a role was created and then stranded: the policy call failed and the manifest "
        "does not name the role, so no teardown will ever delete it"
    )


def _adopt_role_deploy(role_tags):
    """Drive deploy_gateway to the point where create_role raises EntityAlreadyExists.

    ``role_tags`` is what ``get_role`` reports, which is the whole question after F-7:
    IAM role names are account-global, so an already-exists role is either ours or a
    stranger's and only the tags can tell them apart.
    """
    iam, ctrl = MagicMock(), MagicMock()
    ctrl.list_gateways.return_value = {"items": []}  # no same-name gateway: the create path

    class _Exists(Exception):
        pass

    iam.exceptions.EntityAlreadyExistsException = _Exists
    iam.create_role.side_effect = _Exists()
    role: dict = {"Arn": "arn:aws:iam::123456789012:role/AgentCoreGateway-f6probe"}
    if role_tags is not None:
        role["Tags"] = role_tags
    iam.get_role.return_value = {"Role": role}
    ctrl.create_gateway.side_effect = RuntimeError("AccessDeniedException")
    ps = _mock_clients(iam, ctrl)
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with patch.object(
            gd,
            "_create_external_oauth_config",
            return_value={"authorizer_config": {}, "client_info": {"provider": "custom"}},
        ):
            return iam, gd.deploy_gateway(
                gateway_config={"name": "f6probe"}, region="us-east-1", identity_config=_IDENTITY
            )


def test_an_adopted_role_is_reported_too(monkeypatch):
    """``EntityAlreadyExists`` on a role we can PROVE is ours means the role exists and
    this gateway is using it. Not recording it is how ``AgentCoreGateway-agent-gateway``
    survived several teardowns: the deploy that first created it had no manifest row
    either, so no run ever named it."""
    monkeypatch.setenv("PROJECT_NAME", "acfe2e")
    monkeypatch.setenv("ENVIRONMENT", "p0920")
    # Pinned, because the gateway role's create_role stamps ``owner_tag_list()`` with
    # NO region argument and the reuse check reads it back the same way, so both
    # resolve the region from the environment. Leaving it unset makes the expected
    # owner id depend on the machine's AWS default region (here us-west-2), and the
    # test would fail for a reason that has nothing to do with what it is checking.
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    from app.services.resource_ownership import owner_tag_list

    _iam, out = _adopt_role_deploy(owner_tag_list("us-east-1"))
    assert out["gateway_role_name"] == "AgentCoreGateway-f6probe"


def test_a_role_we_cannot_prove_is_ours_is_refused_instead_of_adopted(monkeypatch):
    """The F-7 counterpart, and the reason the test above now has to supply tags.

    ``Tags: null`` is the shape of all four foreign ``AgentCore*`` roles measured live.
    Adopting one wrote our owner tag onto it, overwrote its inline policy, and recorded
    it for deletion -- so the correct outcome is a refusal, and there is then nothing to
    put in the manifest because nothing was adopted.
    """
    monkeypatch.setenv("PROJECT_NAME", "acfe2e")
    monkeypatch.setenv("ENVIRONMENT", "p0920")
    # Pinned, because the gateway role's create_role stamps ``owner_tag_list()`` with
    # NO region argument and the reuse check reads it back the same way, so both
    # resolve the region from the environment. Leaving it unset makes the expected
    # owner id depend on the machine's AWS default region (here us-west-2), and the
    # test would fail for a reason that has nothing to do with what it is checking.
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    iam, out = _adopt_role_deploy(None)

    assert out["success"] is False
    assert "will not modify it" in out["error"]
    assert out.get("gateway_role_name") in (None, ""), (
        "a refused role must not reach the manifest: a row would make teardown delete "
        f"a role this deployment never touched. Got: {out.get('gateway_role_name')}"
    )
    assert iam.tag_role.call_count == 0
    assert iam.put_role_policy.call_count == 0


# ---------------------------------------------------------------------------
# What the manifest writer does with it
# ---------------------------------------------------------------------------


def test_no_role_row_without_a_confirmed_role():
    store = _Store()
    gs._record_gateway_resources(store, "d-1", "us-east-1", {"success": False, "gateway_name": "f6probe"})
    assert "iam_role" not in _kinds(store), f"invented a role row from the gateway name: {store.rows}"


def test_the_row_names_the_confirmed_role_verbatim():
    """Not re-derived from the gateway name here either: the writer copies the field,
    so the name in the manifest is the name IAM acknowledged."""
    store = _Store()
    gs._record_gateway_resources(
        store,
        "d-1",
        "us-east-1",
        {"success": False, "gateway_name": "f6probe", "gateway_role_name": "AgentCoreGateway-f6probe"},
    )
    assert {
        "type": "iam_role",
        "name": "AgentCoreGateway-f6probe",
        "region": "us-east-1",
        "created_by_deployment": False,
        gs.GATEWAY_GRAPH_FIELD: True,
    } in store.rows


def test_a_litellm_result_records_the_proxy_and_no_role():
    """LiteLLM never reaches Step 1b, so the field is absent and the role row falls
    away on its own. The branch used to have to suppress it explicitly; this pins that
    removing that suppression did not bring the row back."""
    store = _Store()
    gs._record_gateway_resources(
        store,
        "d-1",
        "eu-central-1",
        {
            "success": True,
            "gateway_provider": "litellm",
            "gateway_name": "custproxy",
            "litellm_base_url": "https://proxy.customer.example",
        },
    )
    assert _kinds(store) == ["litellm_gateway"]
    assert store.rows[0]["id"] == "https://proxy.customer.example"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
