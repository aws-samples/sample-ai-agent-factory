"""A shared-pool gateway's app client must be revoked by the path teardown takes.

Found live: a torn-down deployment left its Cognito app client and resource server
behind in the platform's shared gateway-auth pool, with the client's secret still
mintable. The code said otherwise — ``gateway_step`` deliberately did not record the
pool (correct: it is platform-owned and holds every other gateway's client) and its
comment said the app client "is cleaned up by cleanup_gateway_resources instead".

That function does delete it. Its CALLER gates it on ``not manifest_used``
(``deployment_handler``, "Step 1"), so on any deployment that wrote a manifest — every
modern one — it never ran. Two correct halves, one dead path between them, and the
comment asserting the coverage is what made it invisible.

So these tests are written against the manifest dispatcher, which is the path teardown
actually takes, and the recorder, which is where the row has to come from. A test of
``cleanup_gateway_resources`` alone would have passed throughout.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "src")

_POOL = "us-east-1_ShAr3dPool"
_CLIENT = "3uu33jeagpcn7085mqn0u2e8nb"


def _gateway_result(shared: bool) -> dict:
    return {
        "success": True,
        "gateway_id": "agent-gateway-1kjpgafkwg",
        "gateway_name": "agent-gateway",
        "client_info": {
            "provider": "cognito",
            "user_pool_id": _POOL,
            "client_id": _CLIENT,
            "shared_pool": shared,
            "scope": "agentcore-agent-gateway/invoke",
        },
    }


def _recorded(gateway_result: dict) -> list[dict]:
    """Drive the real recorder in gateway_step and collect its manifest rows."""
    from app.step_handlers import gateway_step

    rows: list[dict] = []
    store = MagicMock()
    store.record_resource.side_effect = lambda _dep_id, row: rows.append(row)
    gateway_step._record_gateway_resources(store, "dep-1234", "us-east-1", gateway_result)
    return rows


def test_a_shared_pool_deploy_records_its_app_client():
    rows = _recorded(_gateway_result(shared=True))
    types = [r["type"] for r in rows]
    assert "cognito_user_pool" not in types, (
        "the shared platform pool must never be recorded as deletable — it holds every "
        f"other gateway's app client and a >381s hosted domain. Rows: {rows!r}"
    )
    client_rows = [r for r in rows if r["type"] == "cognito_app_client"]
    assert client_rows, (
        "no cognito_app_client row, so manifest teardown revokes nothing and the app "
        f"client's secret stays mintable after delete. Rows: {rows!r}"
    )
    assert client_rows[0]["id"] == _CLIENT
    assert client_rows[0]["pool_id"] == _POOL, "the row must carry the pool, or the delete has no container"


def test_an_owned_pool_deploy_records_the_pool_and_not_the_client():
    """No double row: deleting the pool deletes its clients with it."""
    rows = _recorded(_gateway_result(shared=False))
    types = [r["type"] for r in rows]
    assert "cognito_user_pool" in types, f"a pool this deployment created must be deletable: {rows!r}"
    assert "cognito_app_client" not in types, (
        "an owned pool's client needs no row — the pool delete cascades, and a second "
        f"row only produces a ResourceNotFound counted as a teardown failure: {rows!r}"
    )


@pytest.fixture
def _cognito(monkeypatch):
    import app.deployment_handler as dh

    cog = MagicMock()
    monkeypatch.setattr("app.services.step_clients.client", lambda *a, **k: cog)
    return dh, cog


def test_the_manifest_dispatcher_deletes_the_app_client(_cognito, monkeypatch):
    dh, cog = _cognito
    monkeypatch.setattr(
        "app.services.gateway_deployer.classify_user_pool",
        lambda *_a, **_k: "SHARED_EXACT",
    )
    msg = dh._delete_managed_resource({"type": "cognito_app_client", "id": _CLIENT, "pool_id": _POOL}, "us-east-1")
    cog.delete_user_pool_client.assert_called_once_with(UserPoolId=_POOL, ClientId=_CLIENT)
    assert "deleted" in msg


def test_an_unprovable_pool_is_never_touched(_cognito, monkeypatch):
    """A manifest row is persisted data, so the container is re-verified live.

    The same rule the pool row already follows: "not the shared pool" is not "ours".
    A pool that classifies as neither platform-owned shape reaches no delete at all,
    because a row naming a foreign pool would otherwise revoke a stranger's client.
    """
    dh, cog = _cognito
    monkeypatch.setattr(
        "app.services.gateway_deployer.classify_user_pool",
        lambda *_a, **_k: "FOREIGN_OR_UNKNOWN",
    )
    msg = dh._delete_managed_resource(
        {"type": "cognito_app_client", "id": _CLIENT, "pool_id": "us-east-1_someoneElse"}, "us-east-1"
    )
    cog.delete_user_pool_client.assert_not_called()
    assert "skipped" in msg and "protected" in msg


def test_a_row_with_no_pool_is_skipped_not_guessed(_cognito):
    dh, cog = _cognito
    msg = dh._delete_managed_resource({"type": "cognito_app_client", "id": _CLIENT}, "us-east-1")
    cog.delete_user_pool_client.assert_not_called()
    assert "skipped" in msg


def test_an_already_deleted_client_is_not_a_teardown_failure(_cognito, monkeypatch):
    dh, cog = _cognito
    monkeypatch.setattr(
        "app.services.gateway_deployer.classify_user_pool",
        lambda *_a, **_k: "SHARED_EXACT",
    )
    cog.delete_user_pool_client.side_effect = Exception(
        "An error occurred (ResourceNotFoundException) when calling the "
        "DeleteUserPoolClient operation: Client does not exist."
    )
    msg = dh._delete_managed_resource({"type": "cognito_app_client", "id": _CLIENT, "pool_id": _POOL}, "us-east-1")
    assert "already gone" in msg


def test_the_app_client_is_deleted_before_the_pool_and_after_the_gateway():
    """Ordering, read out of the dispatcher's own priority map.

    Before the pool: an owned pool's delete would otherwise race its clients. After the
    gateway: its ``customJWTAuthorizer`` pins this client id, so revoking the client
    first leaves a live gateway nothing can authenticate to mid-teardown.
    """
    import inspect

    import app.deployment_handler as dh

    src = inspect.getsource(dh._run_delete_cleanup)
    assert '"cognito_app_client": 8' in src, "the app client has no explicit delete priority"
    assert '"cognito_user_pool": 9' in src
    assert '"gateway": 2' in src


# ---------------------------------------------------------------------------
# The FAILURE path is a second, independent dispatcher.
#
# ``status_update_step._cleanup_resource`` is a hand-mirrored copy of
# _delete_managed_resource that runs inline when a deploy fails, and it had no
# ``cognito_app_client`` arm either — so fixing only the delete path would still
# leak the client on every FAILED shared-pool deploy. Two dispatchers means two
# sets of tests; a shared one would not have caught the asymmetry.
# ---------------------------------------------------------------------------


@pytest.fixture
def _sus(monkeypatch):
    from app.step_handlers import status_update_step as sus

    cog = MagicMock()
    monkeypatch.setattr("app.services.step_clients.client", lambda *a, **k: cog)
    # A shared-pool child goes through platform credentials (F-42); which account is
    # chosen is pinned by test_shared_pool_account_session_routing, not here.
    monkeypatch.setattr("app.services.gateway_deployer._create_platform_cognito_client", lambda region: cog)
    return sus, cog


def test_the_failure_path_deletes_the_app_client(_sus, monkeypatch):
    sus, cog = _sus
    monkeypatch.setattr(
        "app.services.gateway_deployer.classify_user_pool",
        lambda *_a, **_k: "SHARED_EXACT",
    )
    sus._cleanup_resource({"type": "cognito_app_client", "id": _CLIENT, "pool_id": _POOL}, "us-east-1", {})
    cog.delete_user_pool_client.assert_called_once_with(UserPoolId=_POOL, ClientId=_CLIENT)


def test_the_failure_path_accepts_shared_where_the_pool_arm_refuses_it(_sus, monkeypatch):
    """The two arms must disagree about SHARED_EXACT, and that is the point.

    The pool arm refuses the shared pool outright — deleting it would revoke every
    other gateway. The client arm must ACCEPT it, because the shared pool is exactly
    where this deployment's own client lives. A single shared predicate would have to
    be wrong for one of them.
    """
    sus, cog = _sus
    monkeypatch.setattr("app.services.gateway_deployer.is_platform_owned_user_pool", lambda pid: pid == _POOL)
    monkeypatch.setattr(
        "app.services.gateway_deployer.classify_user_pool",
        lambda *_a, **_k: "SHARED_EXACT",
    )
    with pytest.raises(sus._ResourceRetained, match="shared platform gateway-auth pool"):
        sus._cleanup_resource({"type": "cognito_user_pool", "id": _POOL}, "us-east-1", {})
    cog.delete_user_pool.assert_not_called()

    sus._cleanup_resource({"type": "cognito_app_client", "id": _CLIENT, "pool_id": _POOL}, "us-east-1", {})
    cog.delete_user_pool_client.assert_called_once_with(UserPoolId=_POOL, ClientId=_CLIENT)


def test_the_failure_path_never_touches_an_unprovable_pool(_sus, monkeypatch):
    sus, cog = _sus
    monkeypatch.setattr(
        "app.services.gateway_deployer.classify_user_pool",
        lambda *_a, **_k: "FOREIGN_OR_UNKNOWN",
    )
    with pytest.raises(sus._ResourceRetained, match="ownership could not be proven"):
        sus._cleanup_resource(
            {"type": "cognito_app_client", "id": _CLIENT, "pool_id": "us-east-1_someoneElse"},
            "us-east-1",
            {},
        )
    cog.delete_user_pool_client.assert_not_called()


def test_the_failure_path_skips_a_row_with_no_pool(_sus):
    sus, cog = _sus
    with pytest.raises(sus._ResourceRetained, match="no pool_id"):
        sus._cleanup_resource({"type": "cognito_app_client", "id": _CLIENT}, "us-east-1", {})
    cog.delete_user_pool_client.assert_not_called()


def test_the_failure_path_orders_the_client_before_the_pool():
    import inspect

    from app.step_handlers import status_update_step as sus

    src = inspect.getsource(sus._auto_cleanup_on_failure)
    assert '"cognito_app_client": 8' in src, "the failure path has no explicit app-client priority"
    assert '"cognito_user_pool": 9' in src


def test_the_step_role_can_actually_delete_a_user_pool_client():
    """The code arm is inert without the IAM action — measured, not assumed.

    The live ``StepStatusUpdateRole`` had DeleteUserPool, DeleteUserPoolDomain and
    DescribeUserPool but NOT DeleteUserPoolClient, so the arm above would have failed
    with AccessDenied and the leak would have survived the fix. The grant is asserted
    against the CDK source that mints the role, in the same test file as the arm, so
    the two cannot drift apart.
    """
    import pathlib

    cdk = pathlib.Path(__file__).resolve().parents[2] / "infra" / "stacks" / "platform" / "step_lambdas.py"
    src = cdk.read_text()
    marker = 'if step_name == "status_update":'
    assert marker in src, f"the status_update role branch moved; re-anchor this test ({cdk})"
    branch = src.split(marker)[-1]
    assert '"cognito-idp:DeleteUserPoolClient"' in branch, (
        "the status_update step role cannot delete an app client, so the failure-path "
        "cleanup arm is dead on arrival (AccessDenied)"
    )
    assert '"cognito-idp:DescribeUserPool"' in branch, "classify_user_pool reads tags via DescribeUserPool"


# ---------------------------------------------------------------------------
# The resource server: the same leak one level up, and the reason it is harder.
#
# `agentcore-<gateway_name>` is keyed on the gateway NAME, and create_resource_server
# treats AlreadyExists as success — so two deployments that picked the same name SHARE
# one resource server, and deleting it on the first teardown revokes the co-resident
# gateway's scope. The delete is therefore gated on a co-residency check that reads
# client NAMES only, never a secret.
# ---------------------------------------------------------------------------

_RS = "agentcore-agent-gateway"


def test_a_shared_pool_deploy_records_its_resource_server():
    rows = _recorded(_gateway_result(shared=True))
    rs = [r for r in rows if r["type"] == "cognito_resource_server"]
    assert rs, f"no cognito_resource_server row, so the scope accumulates forever: {rows!r}"
    assert rs[0]["id"] == _RS, "the id must come from the granted scope, not a re-derived name"
    assert rs[0]["pool_id"] == _POOL


def test_an_owned_pool_deploy_records_no_resource_server():
    """The pool delete cascades; a row would only produce a counted ResourceNotFound."""
    rows = _recorded(_gateway_result(shared=False))
    assert "cognito_resource_server" not in [r["type"] for r in rows]


def _cog_with_clients(*names):
    cog = MagicMock()
    cog.list_user_pool_clients.return_value = {
        "UserPoolClients": [{"ClientId": f"id-{n}", "ClientName": n} for n in names]
    }
    return cog


def test_the_resource_server_is_deleted_once_no_client_holds_its_scope(monkeypatch):
    import app.deployment_handler as dh

    cog = _cog_with_clients("some-other-gateway-client")
    monkeypatch.setattr("app.services.step_clients.client", lambda *a, **k: cog)
    monkeypatch.setattr("app.services.gateway_deployer.classify_user_pool", lambda *_a, **_k: "SHARED_EXACT")
    msg = dh._delete_managed_resource({"type": "cognito_resource_server", "id": _RS, "pool_id": _POOL}, "us-east-1")
    cog.delete_resource_server.assert_called_once_with(UserPoolId=_POOL, Identifier=_RS)
    assert "deleted" in msg


def test_a_co_resident_gateway_keeps_its_scope(monkeypatch):
    """The case a name-derived delete gets wrong, and the reason for the whole check.

    Another deployment picked the same gateway name, so its client is still in the pool
    holding ``agentcore-agent-gateway/invoke``. Deleting the resource server here would
    revoke a LIVE gateway's scope.
    """
    import app.deployment_handler as dh

    cog = _cog_with_clients("agent-gateway-client")
    monkeypatch.setattr("app.services.step_clients.client", lambda *a, **k: cog)
    monkeypatch.setattr("app.services.gateway_deployer.classify_user_pool", lambda *_a, **_k: "SHARED_EXACT")
    msg = dh._delete_managed_resource({"type": "cognito_resource_server", "id": _RS, "pool_id": _POOL}, "us-east-1")
    cog.delete_resource_server.assert_not_called()
    assert "still has a client" in msg and "protected" in msg


def test_an_unreadable_client_list_keeps_the_resource_server(monkeypatch):
    """Fails CLOSED. "No clients found" is the answer that authorizes the delete, so an
    error must not be allowed to look like it."""
    import app.deployment_handler as dh

    cog = MagicMock()
    cog.list_user_pool_clients.side_effect = Exception("AccessDeniedException")
    monkeypatch.setattr("app.services.step_clients.client", lambda *a, **k: cog)
    monkeypatch.setattr("app.services.gateway_deployer.classify_user_pool", lambda *_a, **_k: "SHARED_EXACT")
    msg = dh._delete_managed_resource({"type": "cognito_resource_server", "id": _RS, "pool_id": _POOL}, "us-east-1")
    cog.delete_resource_server.assert_not_called()
    assert "protected" in msg


def test_the_co_residency_check_paginates():
    """A co-resident client on page 2 is invisible to an unpaginated read, and
    "none found" is precisely the answer that authorizes the delete — so a truncated
    list is a WRONG DELETE, not a missed one."""
    from app.services.gateway_deployer import resource_server_is_unused

    cog = MagicMock()
    cog.list_user_pool_clients.side_effect = [
        {"UserPoolClients": [{"ClientId": "a", "ClientName": "unrelated-client"}], "NextToken": "t1"},
        {"UserPoolClients": [{"ClientId": "b", "ClientName": "agent-gateway-client"}]},
    ]
    assert resource_server_is_unused(_POOL, _RS, cog) is False
    assert cog.list_user_pool_clients.call_count == 2
    assert cog.list_user_pool_clients.call_args_list[1].kwargs["NextToken"] == "t1"


def test_a_repeated_client_page_token_keeps_the_resource_server():
    """Malformed pagination is absence of proof, never proof that no client uses it."""
    from app.services.gateway_deployer import resource_server_is_unused

    cog = MagicMock()
    cog.list_user_pool_clients.side_effect = [
        {"UserPoolClients": [{"ClientId": "a", "ClientName": "unrelated-client"}], "NextToken": "t1"},
        {"UserPoolClients": [], "NextToken": "t1"},
    ]

    assert resource_server_is_unused(_POOL, _RS, cog) is False
    assert cog.list_user_pool_clients.call_count == 2


def test_the_co_residency_check_never_reads_a_client_secret():
    """DescribeUserPoolClient authorizes on the POOL, so the only grant that answers
    this question directly also reads every gateway's client secret. The check must
    therefore use ListUserPoolClients and nothing else."""
    from app.services.gateway_deployer import resource_server_is_unused

    cog = MagicMock()
    cog.list_user_pool_clients.return_value = {"UserPoolClients": []}
    assert resource_server_is_unused(_POOL, _RS, cog) is True
    cog.describe_user_pool_client.assert_not_called()


def test_an_unexpected_resource_server_id_is_not_guessed_at():
    """Without the `agentcore-` prefix there is no client name to compare, so there is
    no evidence of non-use — refuse rather than delete."""
    from app.services.gateway_deployer import resource_server_is_unused

    cog = MagicMock()
    cog.list_user_pool_clients.return_value = {"UserPoolClients": []}
    assert resource_server_is_unused(_POOL, "something-else", cog) is False
    cog.list_user_pool_clients.assert_not_called()


def test_the_failure_path_applies_the_same_co_residency_guard(monkeypatch):
    from app.step_handlers import status_update_step as sus

    cog = _cog_with_clients("agent-gateway-client")
    monkeypatch.setattr("app.services.step_clients.client", lambda *a, **k: cog)
    monkeypatch.setattr("app.services.gateway_deployer.classify_user_pool", lambda *_a, **_k: "SHARED_EXACT")
    with pytest.raises(sus._ResourceRetained, match="still has an app client"):
        sus._cleanup_resource({"type": "cognito_resource_server", "id": _RS, "pool_id": _POOL}, "us-east-1", {})
    cog.delete_resource_server.assert_not_called()

    cog2 = _cog_with_clients()
    monkeypatch.setattr("app.services.step_clients.client", lambda *a, **k: cog2)
    sus._cleanup_resource({"type": "cognito_resource_server", "id": _RS, "pool_id": _POOL}, "us-east-1", {})
    cog2.delete_resource_server.assert_called_once_with(UserPoolId=_POOL, Identifier=_RS)


def test_the_resource_server_is_deleted_after_its_client_on_both_paths():
    """Ordering is load-bearing, not cosmetic: the guard is "no client holds this
    scope", which is false until our own client is gone."""
    import inspect

    import app.deployment_handler as dh
    from app.step_handlers import status_update_step as sus

    for src in (inspect.getsource(dh._run_delete_cleanup), inspect.getsource(sus._auto_cleanup_on_failure)):
        assert '"cognito_app_client": 8' in src
        assert '"cognito_resource_server": 9' in src


def test_both_teardown_roles_can_delete_a_resource_server_and_list_clients():
    """Measured live before this was written: DeploymentLambdaRole had NEITHER action,
    so the pre-existing resource-server cleanup in cleanup_gateway_resources was
    silently inert on the delete path."""
    import pathlib

    infra = pathlib.Path(__file__).resolve().parents[2] / "infra" / "stacks" / "platform"
    step = (infra / "step_lambdas.py").read_text()
    marker = 'if step_name == "status_update":'
    assert marker in step, "the status_update role branch moved; re-anchor this test"
    for name, src in (
        ("status_update step role", step.split(marker)[-1]),
        ("deployment role", (infra / "lambdas.py").read_text()),
    ):
        assert '"cognito-idp:DeleteResourceServer"' in src, f"{name} cannot delete a resource server"
        assert '"cognito-idp:ListUserPoolClients"' in src, f"{name} cannot run the co-residency check"
