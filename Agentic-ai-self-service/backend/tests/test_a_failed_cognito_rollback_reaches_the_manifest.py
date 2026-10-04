"""A Cognito rollback that could not finish hands what it left to the manifest.

The helpers roll back their own creations, but a rollback delete can fail too. Before
this, a shared-pool secret-store failure followed by a failed DeleteUserPoolClient
returned ``client_info=None``: the helper raised before deploy_gateway had a
``cognito_response``, the only record of the live app client (its secret still
mintable) was an exception note, and ``str(e)`` dropped even that. No manifest row
could be derived, so no teardown would ever delete it.

Each test drives the real deploy_gateway abort, derives the rows with the real
manifest writer, then heals the fake and runs the real teardown dispatcher over those
rows, asserting the deletes that actually happened.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import app.deployment_handler as dh
import pytest
from app.services import gateway_deployer as gd
from app.services.resource_ownership import owner_tags
from app.step_handlers.gateway_step import _gateway_manifest_resources

from tests.test_cognito_helper_rolls_back_its_own_creations import (
    CLIENT,
    DEDICATED,
    DOMAIN,
    GW,
    POOL,
    RS,
    _Cognito,
)

SECRET = "notarealsecret-0000"  # pragma: allowlist secret
_PRIORITY = {"cognito_app_client": 0, "cognito_resource_server": 1, "cognito_user_pool": 2}


class _Pool(_Cognito):
    """Adds a failing pool delete and the owner tags classify_user_pool proves."""

    def __init__(self, *, fail_pool_delete: bool = False, **kw):
        super().__init__(**kw)
        self.fail_pool_delete = fail_pool_delete
        self.pools: dict[str, dict] = {}

    def create_user_pool(self, **kw):
        out = super().create_user_pool(**kw)
        self.pools[DEDICATED] = dict(kw.get("UserPoolTags") or {})
        return out

    def describe_user_pool(self, **kw):
        out = super().describe_user_pool(**kw)
        out["UserPool"]["UserPoolTags"] = self.pools.get(kw["UserPoolId"], {})
        return out

    def delete_user_pool(self, **kw):
        if self.fail_pool_delete:
            raise RuntimeError("injected delete_user_pool failure")
        super().delete_user_pool(**kw)
        self.pools.pop(kw["UserPoolId"], None)


class _Session:
    def __init__(self, cog):
        self._cog = cog

    def client(self, service, **_kw):
        assert service == "cognito-idp"
        return self._cog


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(gd.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(dh.time, "sleep", lambda *_a: None)


@pytest.fixture(autouse=True)
def _secret_store_fails(monkeypatch):
    def _put(*_a, **_k):
        raise RuntimeError("injected secret store failure")

    monkeypatch.setattr(gd, "_put_connector_secret", _put)


def _deploy(monkeypatch, cog, *, shared: bool) -> dict:
    if shared:
        monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_ID", POOL)
        monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_DOMAIN", DOMAIN)
    else:
        monkeypatch.delenv("GATEWAY_SHARED_USER_POOL_ID", raising=False)
        monkeypatch.delenv("GATEWAY_SHARED_USER_POOL_DOMAIN", raising=False)
    ctrl = MagicMock()
    ctrl.list_gateways.return_value = {"items": []}
    iam = MagicMock()
    iam.exceptions.EntityAlreadyExistsException = type("E", (Exception,), {})
    monkeypatch.setattr(gd, "_create_platform_cognito_client", lambda region: cog)
    monkeypatch.setattr(gd, "_create_cognito_client", lambda region: cog)
    monkeypatch.setattr(gd, "_create_agentcore_control_client", lambda region: ctrl)
    monkeypatch.setattr(gd, "_create_iam_client", lambda *a, **k: iam)
    out = gd.deploy_gateway({"name": GW}, "us-east-1", owner_sub="sub-a", deployment_id="dep-1")
    assert out["success"] is False
    assert ctrl.create_gateway.call_count == 0
    return out


def _teardown(rows: list[dict], cog) -> list[str]:
    return [
        dh._delete_managed_resource(row, "us-east-1", deployment_id="dep-1", target_session=_Session(cog))
        for row in sorted(rows, key=lambda r: _PRIORITY[r["type"]])
    ]


def _no_secret_anywhere(out: dict, rows: list[dict], cog) -> None:
    assert cog.no_secret is False, "the fake must actually mint a secret for this to mean anything"
    for surface in (json.dumps(out, default=str), json.dumps(rows, default=str), out["error"]):
        assert SECRET not in surface


def test_a_client_the_rollback_could_not_delete_is_recorded_and_torn_down(monkeypatch):
    cog = _Pool(fail="delete_user_pool_client")
    out = _deploy(monkeypatch, cog, shared=True)

    # The inventory reached the result, and so did the rollback report.
    assert out["client_info"]["client_id"] == CLIENT
    assert out["client_info"]["scope"] == f"{RS}/invoke"
    assert out["client_info"]["shared_pool"] is True
    assert out["client_info"]["user_pool_region"] == "us-east-1"
    assert out["resource_server_created_by_deployment"] is True
    assert "injected secret store failure" in out["error"]
    assert "app client: RuntimeError" in out["error"]
    assert CLIENT in cog.clients and (POOL, RS) in cog.resource_servers, "precondition: both leaked"

    rows = _gateway_manifest_resources("us-east-1", out)
    assert sorted((r["type"], r["id"], r["pool_id"]) for r in rows) == [
        ("cognito_app_client", CLIENT, POOL),
        ("cognito_resource_server", RS, POOL),
    ]
    _no_secret_anywhere(out, rows, cog)

    cog.fail = ""  # the fault clears; the later teardown runs off the rows alone
    _teardown(rows, cog)
    assert cog.clients == {}
    assert cog.resource_servers == set()
    assert cog.deletes()[-2:] == [
        ("delete_user_pool_client", POOL, CLIENT),
        ("delete_resource_server", POOL, RS),
    ]


def test_a_resource_server_a_concurrent_deploy_joined_is_recorded_and_kept_until_free(monkeypatch):
    cog = _Pool(foreign_client=True)
    out = _deploy(monkeypatch, cog, shared=True)

    assert "client_id" not in out["client_info"], "our client WAS deleted; naming it would be a phantom row"
    assert out["client_info"]["scope"] == f"{RS}/invoke"
    rows = _gateway_manifest_resources("us-east-1", out)
    assert [(r["type"], r["id"]) for r in rows] == [("cognito_resource_server", RS)]
    _no_secret_anywhere(out, rows, cog)

    # While the other deploy's client exists, teardown proves co-residency and keeps it.
    assert "protected" in _teardown(rows, cog)[0]
    assert (POOL, RS) in cog.resource_servers
    # Once that deploy is gone, the same row deletes it.
    cog.clients.pop("someone-else")
    _teardown(rows, cog)
    assert cog.resource_servers == set()


def test_a_dedicated_pool_the_rollback_could_not_delete_is_recorded_and_torn_down(monkeypatch):
    cog = _Pool(fail_pool_delete=True)
    out = _deploy(monkeypatch, cog, shared=False)

    assert out["client_info"]["user_pool_id"] == DEDICATED
    assert not out["client_info"].get("shared_pool")
    assert "user pool: RuntimeError" in out["error"]
    assert DEDICATED in cog.pools, "precondition: the pool leaked"

    rows = _gateway_manifest_resources("us-east-1", out)
    assert [(r["type"], r["id"]) for r in rows] == [("cognito_user_pool", DEDICATED)]
    _no_secret_anywhere(out, rows, cog)

    cog.fail_pool_delete = False
    _teardown(rows, cog)
    assert DEDICATED not in cog.pools
    assert ("delete_user_pool", DEDICATED) in cog.deletes()


def test_a_complete_rollback_records_nothing(monkeypatch):
    # The happy path of the rollback: without it the tests above are compatible with a
    # deploy_gateway that reports every Cognito handle whether or not it survived.
    cog = _Pool()
    out = _deploy(monkeypatch, cog, shared=True)
    assert out["client_info"] is None
    assert out["resource_server_created_by_deployment"] is False
    assert _gateway_manifest_resources("us-east-1", out) == []
    assert cog.clients == {} and cog.resource_servers == set()


def test_the_pool_owner_tag_is_what_the_teardown_proves(monkeypatch):
    # Pins the precondition the dedicated test depends on: the fake pool carries the
    # stack's real owner tags, so the teardown's ownership proof is the real one.
    cog = _Pool(fail_pool_delete=True)
    _deploy(monkeypatch, cog, shared=False)
    assert cog.pools[DEDICATED].items() >= owner_tags("us-east-1").items()
