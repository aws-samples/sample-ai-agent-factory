"""F-66c: two tombstones must not keep a shared gateway alive for each other.

Measured live 2026-09-23 (gateway g8gw1790134015-fflsxwz9gp). A redeploy adopts the
gateway, its role and the resource server, so two deployments list them. Deleting the
older one correctly left them for the newer one -- and recorded that as
``delete_retained``. The co-residency scan counts a ``delete_retained`` row as a
reference, so deleting the newer one, the last user of the gateway, left them too
"because another live deployment still references" them: the tombstone. Retrying
either delete repeated that forever, and nothing in the product could reclaim them.

These tests run the real reference scan over a fake table, and the real teardown loop,
in the order a user does it.
"""

from unittest.mock import MagicMock, patch

import pytest
from app.services.deployment_state_store import CO_RESIDENT_REFUSAL, DeploymentStateStore
from botocore.exceptions import ClientError

GW = {"type": "gateway", "id": "gw-shared", "name": "shared", "region": "us-east-1"}
GW_ROLE = {"type": "iam_role", "id": "AgentCoreGateway-shared", "name": "AgentCoreGateway-shared"}


class _Table:
    """The deployments table as far as the co-residency scan reads it."""

    def __init__(self, items: dict[str, dict]):
        self.items = items

    def scan(self, **_kwargs):
        return {"Items": [dict(v) for v in self.items.values()]}


def _record(rid: str, *, gateway_created: bool) -> dict:
    return {
        # The scan skips the row whose deployment_id equals the cleanup key; with an
        # empty id the key falls back to the runtime id, so the ids are runtime ids.
        "deployment_id": rid,
        "user_id": "owner-1",
        "runtime_id": rid,
        "target_region": "us-east-1",
        "resource_manifest_complete": True,
        "created_resources": [
            {"type": "agent_runtime", "id": rid, "region": "us-east-1", "created_by_deployment": True},
            {**GW, "created_by_deployment": gateway_created},
            {**GW_ROLE, "created_by_deployment": gateway_created},
        ],
    }


def _missing(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "gone"}}, "Op")


def _wire(monkeypatch, table: _Table, *, runtime_delete=None):
    """Point the real teardown at the fake table; return the list of deleted rows."""
    from app import deployment_handler
    from app.services import step_clients

    store = DeploymentStateStore.__new__(DeploymentStateStore)
    store._table = table
    monkeypatch.setattr(deployment_handler, "_get_state_store", lambda: store)
    monkeypatch.setattr(
        deployment_handler,
        "_scan_for_runtime",
        # The handler gets a copy with an empty id, which keeps the unrelated
        # KB-tool sweep (keyed on the id's first 8 chars) out of these tests.
        lambda _t, rid: {**table.items[rid], "deployment_id": ""},
    )
    monkeypatch.setattr(step_clients, "session_for_event", lambda _e: MagicMock())
    deleted: list[tuple[str, str]] = []

    def _delete(res, *_a, **_k):
        if res["type"] == "agent_runtime" and runtime_delete is not None:
            return runtime_delete(res)
        deleted.append((res["type"], res["id"]))
        return f"[manifest] {res['type']} {res['id']} deleted"

    monkeypatch.setattr(deployment_handler, "_delete_managed_resource", _delete)
    monkeypatch.setattr(
        deployment_handler,
        "destroy_runtime",
        MagicMock(side_effect=AssertionError("the sealed manifest owns the runtime")),
    )
    return deleted


def _delete_as_the_product_does(rid: str, table: _Table) -> dict:
    """``_run_delete_cleanup`` plus the dispatcher's own status verdict."""
    from app import deployment_handler

    result = deployment_handler._run_delete_cleanup(rid, "owner-1")
    status = "deleted" if result.success else "delete_retained" if result.retained else "delete_failed"
    table.items[rid]["delete_status"] = status
    return {"status": status, "message": result.message}


def test_deleting_both_deployments_in_turn_reclaims_the_shared_gateway(monkeypatch):
    table = _Table({"rt-1": _record("rt-1", gateway_created=True), "rt-2": _record("rt-2", gateway_created=False)})
    deleted = _wire(monkeypatch, table)

    first = _delete_as_the_product_does("rt-1", table)
    # The newer deployment still uses the gateway: it survives, and the older
    # deployment is still honestly deleted, because it no longer uses anything.
    assert ("gateway", "gw-shared") not in deleted
    assert first["status"] == "deleted", first["message"]
    assert "[manifest] handed off gateway gw-shared" in first["message"]

    second = _delete_as_the_product_does("rt-2", table)
    # The last user reclaims it. Before F-66c this was delete_retained, forever.
    assert second["status"] == "deleted", second["message"]
    assert ("gateway", "gw-shared") in deleted
    assert ("iam_role", "AgentCoreGateway-shared") in deleted


def test_the_live_deadlock_is_recovered_by_retrying_the_deletes(monkeypatch):
    """The exact state the old code left on the demo stack: both rows delete_retained."""
    table = _Table({"rt-1": _record("rt-1", gateway_created=True), "rt-2": _record("rt-2", gateway_created=False)})
    for item in table.items.values():
        item["delete_status"] = "delete_retained"
    deleted = _wire(monkeypatch, table, runtime_delete=lambda res: f"[manifest] agent_runtime {res['id']} already gone")

    assert _delete_as_the_product_does("rt-1", table)["status"] == "deleted"
    assert ("gateway", "gw-shared") not in deleted
    assert _delete_as_the_product_does("rt-2", table)["status"] == "deleted"
    assert ("gateway", "gw-shared") in deleted


MCP_SHARED = {"type": "agent_runtime", "id": "mcp-server-shared", "region": "us-east-1"}


def _mcp_target_version(rid: str) -> dict:
    """One version of an mcp-server-gateway-target agent: its own runtime, plus the MCP server
    runtime, gateway and role every version adopts (created_by_deployment False on each)."""
    return {
        "deployment_id": rid,
        "user_id": "owner-1",
        "runtime_id": rid,
        "target_region": "us-east-1",
        "resource_manifest_complete": True,
        "created_resources": [
            {"type": "agent_runtime", "id": rid, "region": "us-east-1", "created_by_deployment": True},
            {**MCP_SHARED, "created_by_deployment": False},
            {**GW, "created_by_deployment": False},
            {**GW_ROLE, "created_by_deployment": False},
        ],
    }


def test_a_handed_off_shared_mcp_runtime_does_not_turn_the_hand_offs_into_retentions(monkeypatch):
    """Measured live 2026-10-01: an older version handed off the shared MCP server runtime (an
    agent_runtime row) and, counting that as its own runtime possibly still running, kept every
    hand-off as a retention. Its delete_retained tombstone then protected the shared rows from
    the last version too."""
    table = _Table({"v-old": _mcp_target_version("v-old"), "v-new": _mcp_target_version("v-new")})
    deleted = _wire(monkeypatch, table)

    first = _delete_as_the_product_does("v-old", table)
    assert first["status"] == "deleted", first["message"]
    assert "[manifest] handed off agent_runtime mcp-server-shared" in first["message"]
    assert "[manifest] handed off gateway gw-shared" in first["message"]
    assert ("agent_runtime", "v-old") in deleted
    assert ("agent_runtime", "mcp-server-shared") not in deleted
    assert ("gateway", "gw-shared") not in deleted

    second = _delete_as_the_product_does("v-new", table)
    assert second["status"] == "deleted", second["message"]
    assert ("agent_runtime", "mcp-server-shared") in deleted
    assert ("gateway", "gw-shared") in deleted
    assert ("iam_role", "AgentCoreGateway-shared") in deleted


def test_the_live_mcp_tombstones_are_recovered_by_retrying_the_deletes(monkeypatch):
    """The state the old rule left in the matrix account: the older version delete_retained."""
    table = _Table({"v-old": _mcp_target_version("v-old"), "v-new": _mcp_target_version("v-new")})
    table.items["v-old"]["delete_status"] = "delete_retained"
    deleted = _wire(monkeypatch, table, runtime_delete=lambda res: f"[manifest] agent_runtime {res['id']} handled")

    assert _delete_as_the_product_does("v-old", table)["status"] == "deleted"
    assert ("gateway", "gw-shared") not in deleted
    assert _delete_as_the_product_does("v-new", table)["status"] == "deleted"
    assert ("gateway", "gw-shared") in deleted


def test_a_hand_off_is_a_retention_while_our_runtime_may_still_be_running(monkeypatch):
    """A runtime that was not deleted may still call the gateway; its row keeps protecting it."""
    from app.services.gateway_deployer import ResourceDeletionRefused

    table = _Table({"rt-1": _record("rt-1", gateway_created=True), "rt-2": _record("rt-2", gateway_created=False)})

    def _refuse(_res):
        raise ResourceDeletionRefused("runtime ownership could not be proven")

    deleted = _wire(monkeypatch, table, runtime_delete=_refuse)
    out = _delete_as_the_product_does("rt-1", table)

    assert out["status"] == "delete_retained"
    assert out["message"].startswith(
        "Resources retained by deletion-authority policy: agent_runtime, gateway, iam_role"
    )
    assert "Left in place for another deployment" not in out["message"]
    assert deleted == []


def test_the_verdict_survives_the_stored_message_cap(monkeypatch):
    """delete_message is stored truncated to 1 KiB; the verdict used to be the part cut off."""
    table = _Table({"rt-1": _record("rt-1", gateway_created=True), "rt-2": _record("rt-2", gateway_created=False)})
    many = [
        {"type": "s3_object", "id": f"s3://b/deployments/{'x' * 80}/{i}/code.zip", "created_by_deployment": True}
        for i in range(20)
    ]
    table.items["rt-1"]["created_resources"] += many
    _wire(monkeypatch, table)

    out = _delete_as_the_product_does("rt-1", table)

    assert len(out["message"]) > 1024
    assert out["message"][:1024].startswith("Left in place for another deployment that still uses them")


def _auto_state(resources: list[dict]):
    state = MagicMock()
    state.model_dump.return_value = {
        "deployment_id": "failed-redeploy",
        "user_id": "owner-1",
        "target_account_id": "111111111111",
        "status": "failed",
        "target_region": "us-east-1",
        "created_resources": resources,
    }
    return state


def _auto_cleanup(monkeypatch, resources, refusal_for):
    from app.step_handlers import status_update_step

    store = MagicMock()
    store.get.return_value = _auto_state(resources)
    monkeypatch.setattr(status_update_step, "manifest_delete_refusal", lambda _s, _d, res, **_k: refusal_for(res))
    monkeypatch.setattr(status_update_step, "_cleanup_resource", MagicMock())
    status_update_step._auto_cleanup_on_failure(store, "failed-redeploy", {})
    return store.update_delete_status.call_args.args


def test_a_failed_redeploy_hands_its_adopted_gateway_back_instead_of_holding_it(monkeypatch):
    status = _auto_cleanup(
        monkeypatch,
        [
            {"type": "agent_runtime", "id": "rt-new", "region": "us-east-1", "created_by_deployment": True},
            {**GW, "created_by_deployment": False},
        ],
        lambda res: CO_RESIDENT_REFUSAL if res["type"] == "gateway" else None,
    )
    assert status[1] == "deleted"
    assert "left for another deployment=1" in status[2]


def test_a_failed_redeploy_whose_runtime_survived_still_protects_the_gateway(monkeypatch):
    status = _auto_cleanup(
        monkeypatch,
        [
            {"type": "agent_runtime", "id": "rt-new", "region": "us-east-1", "created_by_deployment": True},
            {**GW, "created_by_deployment": False},
        ],
        lambda res: CO_RESIDENT_REFUSAL if res["type"] == "gateway" else "ownership could not be proven",
    )
    assert status[1] == "delete_retained"
    assert "protected=2" in status[2]


def test_only_the_co_residency_refusal_is_a_hand_off():
    """Every other refusal is a real retention; the handler matches on this exact value."""
    from app.services.deployment_state_store import manifest_delete_refusal

    store = MagicMock()
    store.has_other_live_resource_reference.return_value = True
    assert manifest_delete_refusal(store, "d", dict(GW)) == CO_RESIDENT_REFUSAL
    store.has_other_live_resource_reference.side_effect = RuntimeError("throttled")
    assert manifest_delete_refusal(store, "d", dict(GW)) != CO_RESIDENT_REFUSAL


# F-66b: a hand-off revokes this deployment's client on the gateway it leaves behind.


def _with_client(record: dict, client_id: str) -> dict:
    record["created_resources"].append(
        {"type": "cognito_app_client", "id": client_id, "pool_id": "us-east-1_pool", "created_by_deployment": True}
    )
    return record


class _GatewayCtrl:
    """A gateway whose reads after an update are scripted, never inferred from the update.

    ``after`` lists what successive GetGateway calls return once an update was sent,
    each ``(status, allowedClients)``, the last one repeating. Without it the gateway
    reads back exactly as before, READY with the old list: a stale read-back, which a
    revoke must not take for success (F-66d).
    """

    def __init__(self, clients, *, update_error=None, get_error=None, after=None):
        self.detail = {
            "gatewayId": "gw-shared",
            "name": "shared",
            "roleArn": "arn:aws:iam::111111111111:role/AgentCoreGateway-shared",
            "protocolType": "MCP",
            "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": {
                "customJWTAuthorizer": {"discoveryUrl": "https://issuer/.well-known", "allowedClients": list(clients)}
            },
            "status": "READY",
        }
        self.update_error = update_error
        self.get_error = get_error
        self.after = list(after or [])
        self.updates: list[dict] = []
        self.events: list[str] = []
        self.reads_after_update = 0

    def get_gateway(self, **_k):
        if self.get_error:
            raise self.get_error
        if not self.updates or not self.after:
            if self.updates:
                self.reads_after_update += 1
            return self.detail
        status, listed = self.after[min(self.reads_after_update, len(self.after) - 1)]
        self.reads_after_update += 1
        jwt = {**self.detail["authorizerConfiguration"]["customJWTAuthorizer"], "allowedClients": list(listed)}
        return {**self.detail, "status": status, "authorizerConfiguration": {"customJWTAuthorizer": jwt}}

    def update_gateway(self, **kwargs):
        self.events.append("update_gateway")
        if self.update_error:
            raise self.update_error
        self.updates.append(kwargs)


def _wire_revoke(monkeypatch, table, ctrl):
    from app import deployment_handler
    from app.services import step_clients

    deleted = _wire(monkeypatch, table)
    session = MagicMock()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111111111111"}
    # The teardown's name hold asks STS for the account a record names none of.
    session.client.side_effect = lambda service, **_k: sts if service == "sts" else ctrl
    monkeypatch.setattr(step_clients, "session_for_event", lambda _e: session)
    monkeypatch.setattr(
        deployment_handler,
        "assert_agentcore_resource_owned",
        lambda c, _t, _i, _r: c.get_gateway(gatewayIdentifier=_i),
    )
    original = deployment_handler._delete_managed_resource

    def _delete(res, *a, **k):
        if res["type"] == "cognito_app_client":
            ctrl.events.append(f"delete_client {res['id']}")
        return original(res, *a, **k)

    monkeypatch.setattr(deployment_handler, "_delete_managed_resource", _delete)
    return deleted


def _two_on_one_gateway() -> _Table:
    return _Table(
        {
            "rt-1": _with_client(_record("rt-1", gateway_created=True), "client-1"),
            "rt-2": _with_client(_record("rt-2", gateway_created=False), "client-2"),
        }
    )


def test_a_hand_off_revokes_our_client_on_the_gateway_before_deleting_it(monkeypatch):
    from app import deployment_handler

    table = _two_on_one_gateway()
    ctrl = _GatewayCtrl(
        ["client-2", "client-1"], after=[("UPDATING", ["client-2", "client-1"]), ("READY", ["client-2"])]
    )
    monkeypatch.setattr(deployment_handler.time, "sleep", lambda _s: None)
    deleted = _wire_revoke(monkeypatch, table, ctrl)

    out = _delete_as_the_product_does("rt-1", table)

    assert out["status"] == "deleted", out["message"]
    assert ctrl.events == ["update_gateway", "delete_client client-1"]
    (update,) = ctrl.updates
    assert update["authorizerConfiguration"]["customJWTAuthorizer"] == {
        "discoveryUrl": "https://issuer/.well-known",
        "allowedClients": ["client-2"],
    }
    # A full replace: everything else the gateway holds is re-sent.
    assert update["roleArn"] == ctrl.detail["roleArn"] and update["protocolType"] == "MCP"
    assert ("cognito_app_client", "client-1") in deleted
    assert "gateway gw-shared no longer allows client client-1" in out["message"]


def test_the_last_deployment_deletes_the_gateway_and_revokes_nothing(monkeypatch):
    table = _two_on_one_gateway()
    table.items["rt-1"]["delete_status"] = "deleted"
    ctrl = _GatewayCtrl(["client-2"])
    deleted = _wire_revoke(monkeypatch, table, ctrl)

    out = _delete_as_the_product_does("rt-2", table)

    assert out["status"] == "deleted", out["message"]
    assert ctrl.updates == [] and "update_gateway" not in ctrl.events
    assert ("gateway", "gw-shared") in deleted


def test_an_only_client_is_revoked_to_the_marker_never_to_an_empty_list(monkeypatch):
    """F-66d. An empty list pins no client, so every client in the pool passes; leaving
    the id listed kept a token minted before the delete accepted (measured live)."""
    from app import deployment_handler
    from app.services.gateway_update import NO_CLIENT_ALLOWED

    table = _two_on_one_gateway()
    ctrl = _GatewayCtrl(["client-1"], after=[("READY", [NO_CLIENT_ALLOWED])])
    monkeypatch.setattr(deployment_handler.time, "sleep", lambda _s: None)
    deleted = _wire_revoke(monkeypatch, table, ctrl)

    out = _delete_as_the_product_does("rt-1", table)

    assert out["status"] == "deleted", out["message"]
    assert ctrl.events == ["update_gateway", "delete_client client-1"]
    (update,) = ctrl.updates
    assert update["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] == [NO_CLIENT_ALLOWED]
    assert ("cognito_app_client", "client-1") in deleted
    assert "gateway gw-shared no longer allows client client-1" in out["message"]


def test_the_marker_is_an_id_cognito_can_never_issue():
    import re

    import botocore.session
    from app.services.gateway_update import NO_CLIENT_ALLOWED

    model = botocore.session.get_session().get_service_model("cognito-idp")
    pattern = model.shape_for("ClientIdType").metadata["pattern"]
    assert pattern == r"[\w+]+"
    assert NO_CLIENT_ALLOWED and not re.fullmatch(pattern, NO_CLIENT_ALLOWED)


def test_an_only_client_still_listed_after_the_update_fails_the_teardown(monkeypatch):
    from app import deployment_handler

    table = _two_on_one_gateway()
    ctrl = _GatewayCtrl(["client-1"])
    monkeypatch.setattr(deployment_handler.time, "sleep", lambda _s: None)
    _wire_revoke(monkeypatch, table, ctrl)

    out = _delete_as_the_product_does("rt-1", table)

    assert out["status"] == "delete_failed", out["message"]
    assert "still allows client client-1" in out["message"]


def test_a_stale_ready_read_back_is_not_a_revoke(monkeypatch):
    """READY with the old list is the gateway before the update landed, not after."""
    from app import deployment_handler

    table = _two_on_one_gateway()
    ctrl = _GatewayCtrl(["client-2", "client-1"], after=[("READY", ["client-2", "client-1"])])
    monkeypatch.setattr(deployment_handler.time, "sleep", lambda _s: None)
    _wire_revoke(monkeypatch, table, ctrl)

    out = _delete_as_the_product_does("rt-1", table)

    assert out["status"] == "delete_failed", out["message"]
    assert "gateway gw-shared still allows client client-1: GatewayUpdateUnconfirmed" in out["message"]
    assert "no longer allows" not in out["message"]
    assert ctrl.reads_after_update == 24


def test_a_stale_read_back_that_catches_up_is_a_revoke(monkeypatch):
    from app import deployment_handler

    table = _two_on_one_gateway()
    ctrl = _GatewayCtrl(
        ["client-2", "client-1"],
        after=[("READY", ["client-2", "client-1"]), ("READY", ["client-2", "client-1"]), ("READY", ["client-2"])],
    )
    monkeypatch.setattr(deployment_handler.time, "sleep", lambda _s: None)
    _wire_revoke(monkeypatch, table, ctrl)

    out = _delete_as_the_product_does("rt-1", table)

    assert out["status"] == "deleted", out["message"]
    assert ctrl.reads_after_update == 3


@pytest.mark.parametrize("status", ["FAILED", "UPDATE_UNSUCCESSFUL"])
def test_a_failed_update_fails_at_once(monkeypatch, status):
    from app import deployment_handler

    table = _two_on_one_gateway()
    ctrl = _GatewayCtrl(["client-2", "client-1"], after=[(status, ["client-2"])])
    monkeypatch.setattr(deployment_handler.time, "sleep", lambda _s: None)
    _wire_revoke(monkeypatch, table, ctrl)

    out = _delete_as_the_product_does("rt-1", table)

    assert out["status"] == "delete_failed", out["message"]
    assert "gateway gw-shared still allows client client-1: GatewayUpdateFailed" in out["message"]
    assert ctrl.reads_after_update == 1


def test_adoption_replaces_the_marker_and_never_retires_it():
    from app.services import gateway_deployer
    from app.services.gateway_update import NO_CLIENT_ALLOWED

    jwt = {
        "discoveryUrl": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool/.well-known/openid-configuration"
    }
    previous = {"customJWTAuthorizer": {**jwt, "allowedClients": [NO_CLIENT_ALLOWED]}}
    new = {"customJWTAuthorizer": {**jwt, "allowedClients": ["client-3"]}}
    merged = gateway_deployer._adoption_authorizer
    out = merged(
        "g",
        "gw-shared",
        previous,
        new,
        owner_sub="o",
        gateway_consumers=lambda _g, _p: [{"owner_sub": "o", "client_ids": []}],
    )
    assert out["customJWTAuthorizer"]["allowedClients"] == ["client-3"]

    empty_new = {"customJWTAuthorizer": {**jwt, "allowedClients": []}}
    out = merged(
        "g",
        "gw-shared",
        previous,
        empty_new,
        owner_sub="o",
        gateway_consumers=lambda _g, _p: [{"owner_sub": "o", "client_ids": []}],
    )
    assert out["customJWTAuthorizer"]["allowedClients"] == [NO_CLIENT_ALLOWED]

    cognito = MagicMock()
    with patch.object(gateway_deployer, "classify_user_pool", return_value=gateway_deployer.POOL_SHARED_EXACT):
        gateway_deployer._retire_stale_gateway_clients(previous, new, cognito)
    cognito.delete_user_pool_client.assert_not_called()


def test_a_client_the_gateway_no_longer_lists_needs_no_update(monkeypatch):
    table = _two_on_one_gateway()
    ctrl = _GatewayCtrl(["client-2"])
    _wire_revoke(monkeypatch, table, ctrl)

    assert _delete_as_the_product_does("rt-1", table)["status"] == "deleted"
    assert ctrl.events == ["delete_client client-1"]


def test_a_failed_revoke_still_deletes_the_client_and_fails_the_teardown_for_a_retry(monkeypatch):
    table = _two_on_one_gateway()
    leak = "customJWTAuthorizer discoveryUrl=https://issuer allowedClients=[client-2]"
    ctrl = _GatewayCtrl(
        ["client-2", "client-1"],
        update_error=ClientError({"Error": {"Code": "AccessDeniedException", "Message": leak}}, "UpdateGateway"),
    )
    deleted = _wire_revoke(monkeypatch, table, ctrl)

    out = _delete_as_the_product_does("rt-1", table)

    assert out["status"] == "delete_failed"
    assert ("cognito_app_client", "client-1") in deleted
    assert "gateway gw-shared still allows client client-1: AccessDeniedException" in out["message"]
    assert leak not in out["message"] and "issuer" not in out["message"]

    # The retry revokes, because the gateway is handed off again.
    ctrl.update_error = None
    ctrl.after = [("READY", ["client-2"])]
    assert _delete_as_the_product_does("rt-1", table)["status"] == "deleted"
    assert ctrl.updates[-1]["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] == ["client-2"]


def test_a_gateway_that_is_already_gone_needs_no_revoke(monkeypatch):
    table = _two_on_one_gateway()
    ctrl = _GatewayCtrl(["client-2", "client-1"], get_error=_missing("ResourceNotFoundException"))
    _wire_revoke(monkeypatch, table, ctrl)

    out = _delete_as_the_product_does("rt-1", table)

    assert out["status"] == "deleted", out["message"]
    assert "update_gateway" not in ctrl.events


def test_a_gateway_that_never_returns_to_ready_is_a_failure(monkeypatch):
    from app import deployment_handler

    table = _two_on_one_gateway()
    ctrl = _GatewayCtrl(["client-2", "client-1"])
    _wire_revoke(monkeypatch, table, ctrl)
    monkeypatch.setattr(deployment_handler.time, "sleep", lambda _s: None)
    real_update = ctrl.update_gateway

    def _update(**kwargs):
        real_update(**kwargs)
        ctrl.detail = {**ctrl.detail, "status": "UPDATING"}

    ctrl.update_gateway = _update

    out = _delete_as_the_product_does("rt-1", table)

    assert out["status"] == "delete_failed"
    assert "still allows client client-1: GatewayUpdateUnconfirmed" in out["message"]
