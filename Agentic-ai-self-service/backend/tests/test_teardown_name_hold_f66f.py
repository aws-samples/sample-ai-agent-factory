"""F-66f through the real teardown dispatcher: what a teardown leaves on the name claim.

A deployment from before name claims existed has none, so its teardown CREATES one to
hold the name. That claim is derived from a manifest row, which proves nothing: a
stale or foreign row must not leave a durable owner behind. But once the gateway is
proven ours live, the claim is the only guard left on whatever part of the graph a
failed teardown could not delete (a deploy's pre-flight lists live gateways only, so
once the gateway is gone nothing else stops another owner creating on the name).
"""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from app.services import gateway_name_claim as gnc
from app.services.resource_ownership import ResourceDeletionRefused

ACCOUNT = "111111111111"
REGION = "us-east-1"
RUNTIME_ID = "runtime-legacy-gw"
KEY = gnc.claim_key(ACCOUNT, REGION, "orders")


@pytest.fixture(autouse=True)
def _pin_the_home_region(monkeypatch):
    import app.deployment_handler as deployment_handler

    monkeypatch.setattr(deployment_handler, "config", replace(deployment_handler.config, aws_region=REGION))


def _teardown(monkeypatch, *, owned: bool, gateway_log: list[str], runtimes: list[str] | None = None):
    """Run _run_delete_cleanup over one id-only legacy gateway; return what it deleted."""
    import app.deployment_handler as deployment_handler
    from app.services import step_clients

    record = {
        # Empty: skips the unrelated deterministic KB-tool branch (see the phantom-error tests).
        "deployment_id": "",
        "user_id": "sub-1",
        "runtime_id": RUNTIME_ID,
        "deployment_mode": "runtime",
        "target_account_id": ACCOUNT,
        "gateway_result": {"gateway_id": "gateway-1"},
        "created_resources": [],
    }
    control = MagicMock()
    control.get_gateway.return_value = {"gatewayId": "gateway-1", "name": "orders", "status": "READY"}
    session = MagicMock()
    session.client.return_value = control
    store = MagicMock()
    store._table = object()
    deleted: list[str] = []

    def _owned(_client, resource_type, resource_id, _region):
        assert resource_type == "gateway" and resource_id == "gateway-1"
        if not owned:
            raise ResourceDeletionRefused("gateway gateway-1 is not this platform's")
        return {"gatewayId": resource_id, "name": "orders"}

    def _cleanup(**_kw):
        # The real cleanup proves ownership itself before any delete.
        if not owned:
            return ["Gateway gateway-1 left in place (protected): not this platform's"]
        deleted.append("gateway-1")
        return list(gateway_log)

    monkeypatch.setattr(deployment_handler, "_get_state_store", lambda: store)
    monkeypatch.setattr(deployment_handler, "_scan_for_runtime", lambda table, requested: record)
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: session)
    monkeypatch.setattr(deployment_handler, "assert_agentcore_resource_owned", _owned)
    monkeypatch.setattr(deployment_handler, "cleanup_gateway_resources", _cleanup)

    def _destroy_runtime(*a, **kw):
        if runtimes is not None:
            runtimes.append(RUNTIME_ID)
        return {"success": True, "message": f"Runtime {RUNTIME_ID} deleted"}

    monkeypatch.setattr(deployment_handler, "destroy_runtime", _destroy_runtime)
    result = deployment_handler._run_delete_cleanup(RUNTIME_ID, "sub-1")
    return result, deleted


def _another_owner_can_claim(table) -> bool:
    try:
        gnc.GatewayNameClaims(table).acquire(
            account=ACCOUNT,
            region=REGION,
            name="orders",
            owner_sub="real-owner",
            deployment_id="d-real",
            token="tok-real",
        )
    except gnc.GatewayNameClaimRefused:
        return False
    return True


def _left_free(table) -> bool:
    """The hold's own provisional claim, abandoned: still an item, but expired and
    marked for collection, so it names no owner anyone has to get past."""
    item = table.items.get(KEY)
    return item is None or (item.get("provisional") is True and item.get("holder_expires_at") == 0)


def test_a_foreign_legacy_gateway_deletes_nothing_and_leaves_no_claim(monkeypatch, gateway_lock_table):
    """An unprovable gateway row does not refuse the whole teardown: the user's own
    runtime still goes, the gateway's graph is left untouched, and no claim is left
    that would squat the name for its real owner."""
    runtimes: list[str] = []
    result, deleted = _teardown(monkeypatch, owned=False, gateway_log=[], runtimes=runtimes)
    assert deleted == []
    assert runtimes == [RUNTIME_ID], "one unprovable row must not make the runtime undeletable"
    assert result.success is False
    assert _left_free(gateway_lock_table)
    assert _another_owner_can_claim(gateway_lock_table)


def test_an_owned_legacy_gateway_whose_coupled_cleanup_fails_keeps_its_claim(monkeypatch, gateway_lock_table):
    result, deleted = _teardown(
        monkeypatch,
        owned=True,
        gateway_log=["Gateway gateway-1 deleted", "Gateway role AgentCoreGateway-orders error: AccessDenied"],
    )
    assert deleted == ["gateway-1"] and result.success is False
    item = gateway_lock_table.items[KEY]
    assert item["owner_sub"] == "sub-1" and "holder_deployment_id" not in item
    assert not _another_owner_can_claim(gateway_lock_table)


def test_an_owned_legacy_gateway_fully_removed_erases_its_claim(monkeypatch, gateway_lock_table):
    result, deleted = _teardown(monkeypatch, owned=True, gateway_log=["Gateway gateway-1 deleted"])
    assert deleted == ["gateway-1"] and result.success is True, result.message
    assert _left_free(gateway_lock_table)
    assert _another_owner_can_claim(gateway_lock_table)


def test_a_partial_teardown_then_a_retry_that_finishes_erases_the_claim(monkeypatch, gateway_lock_table):
    _teardown(
        monkeypatch,
        owned=True,
        gateway_log=["Gateway gateway-1 deleted", "Gateway role AgentCoreGateway-orders error: AccessDenied"],
    )
    assert KEY in gateway_lock_table.items
    result, _ = _teardown(monkeypatch, owned=True, gateway_log=["Gateway role AgentCoreGateway-orders deleted"])
    assert result.success is True, result.message
    assert _left_free(gateway_lock_table)
    assert _another_owner_can_claim(gateway_lock_table)
