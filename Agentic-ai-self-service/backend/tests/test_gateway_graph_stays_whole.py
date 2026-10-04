"""A gateway that survives its own delete keeps its whole graph, on both dispatchers.

The gateway's authorizer pins this deployment's app client and scope, its targets
call the tool Lambdas through the credential providers, and a later DELETE needs all
of it to finish the gateway. Both teardown loops used to catch the gateway's failure
and carry on through that graph, so a retained gateway was left unreachable and a
failed one half-dismantled. These tests run the real loops: after any gateway is
retained or fails (only a hand-off to another deployment is not a stop), nothing in
its graph is mutated, while the unrelated runtime and memory rows are still cleaned up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.models.deployment_models import DeploymentState, DeploymentStatusEnum
from app.services.deployment_state_store import (
    CO_RESIDENT_REFUSAL,
    GATEWAY_GRAPH_FIELD,
    DeploymentStateStore,
    gateway_graph_membership,
)
from app.services.resource_ownership import ResourceDeletionRefused
from botocore.exceptions import ClientError

REGION = "us-east-1"
GW_ID = "gw-graph-1"


def _row(rtype: str, rid: str, *, graph: bool = False, created: bool = True, **extra) -> dict:
    row = {"type": rtype, "id": rid, "region": REGION, "created_by_deployment": created, **extra}
    if graph:
        row[GATEWAY_GRAPH_FIELD] = True
    return row


def _manifest(*, gateway_created: bool = True, tagged: bool = True) -> list[dict]:
    g = tagged
    return [
        _row("agent_runtime", "rt-graph-1"),
        _row("gateway", GW_ID, name="orders", created=gateway_created),
        _row("memory", "mem-graph-1"),
        _row("lambda", "orders-tool", name="orders-tool", graph=g),
        _row("oauth2_credential_provider", "orders-oauth", name="orders-oauth", graph=g),
        _row("s3_object", "s3://artifacts/specs/orders.json", graph=g),
        _row("cognito_app_client", "client-graph-1", pool_id="us-east-1_Shared", graph=g),
        _row("cognito_resource_server", "agentcore-orders", pool_id="us-east-1_Shared", graph=g),
        _row("iam_role", "AgentCoreGateway-orders", name="AgentCoreGateway-orders", graph=g),
        _row("secret", "arn:aws:secretsmanager:us-east-1:111122223333:secret:gw-conn-AbCdEf", graph=g),
        # Unrelated: the memory's role and a runtime secret carry no graph tag.
        _row("iam_role", "AgentCoreMemory-graph1", name="AgentCoreMemory-graph1"),
        _row("secret", "arn:aws:secretsmanager:us-east-1:111122223333:secret:rt-key-ZyXwVu"),
    ]


GRAPH = {(r["type"], r["id"]) for r in _manifest() if gateway_graph_membership(_manifest())(r)}
UNRELATED = {("agent_runtime", "rt-graph-1"), ("memory", "mem-graph-1"), ("iam_role", "AgentCoreMemory-graph1")}
UNRELATED |= {("secret", "arn:aws:secretsmanager:us-east-1:111122223333:secret:rt-key-ZyXwVu")}


def test_the_manifest_under_test_has_both_populations():
    # Guards the fixture itself: a graph set that swallowed everything would pass
    # every "unrelated rows still go" assertion below vacuously.
    assert len(GRAPH) == 7 and not (GRAPH & UNRELATED)


def test_the_gateway_step_tags_every_graph_row_it_records():
    from app.step_handlers.gateway_step import _gateway_manifest_resources

    rows = _gateway_manifest_resources(
        REGION,
        {
            "gateway_id": GW_ID,
            "gateway_name": "orders",
            "gateway_created_by_deployment": True,
            "gateway_role_name": "AgentCoreGateway-orders",
            "gateway_role_created_by_deployment": True,
            "client_info": {
                "user_pool_id": "us-east-1_Shared",
                "shared_pool": True,
                "client_id": "client-graph-1",
                "scope": "agentcore-orders/invoke",
                "minted_client_secret_ref": "arn:aws:secretsmanager:us-east-1:111122223333:secret:c-AbCdEf",
            },
            "custom_tool_lambdas": ["orders-tool"],
            "custom_tool_roles": ["orders-tool-role"],
            "connector_secret_arns": ["arn:aws:secretsmanager:us-east-1:111122223333:secret:gw-conn-AbCdEf"],
            "connector_credential_providers": ["OAUTH:orders-oauth"],
            "connector_spec_s3_uris": ["s3://artifacts/specs/orders.json"],
        },
    )
    graph = [r for r in rows if r["type"] != "gateway"]
    assert len(graph) == 9
    assert all(r.get(GATEWAY_GRAPH_FIELD) is True for r in graph), graph
    assert GATEWAY_GRAPH_FIELD not in next(r for r in rows if r["type"] == "gateway")


def test_a_manifest_with_no_tag_at_all_counts_every_graph_capable_type():
    """A legacy manifest cannot tell the gateway's secret from the runtime's, so both
    count: retaining an unrelated row is recoverable, breaking a live gateway is not."""
    legacy = _manifest(tagged=False)
    member = gateway_graph_membership(legacy)
    kept = {(r["type"], r["id"]) for r in legacy if member(r)}
    assert kept == GRAPH | {("iam_role", "AgentCoreMemory-graph1")} | {
        ("secret", "arn:aws:secretsmanager:us-east-1:111122223333:secret:rt-key-ZyXwVu")
    }
    assert not member(_row("agent_runtime", "rt-graph-1")) and not member(_row("memory", "mem-graph-1"))


def test_the_gateway_steps_journal_and_bound_secret_rows_are_tagged():
    from app.services.gateway_deployer import manifest_secret_journal

    recorded: list[dict] = []
    store = SimpleNamespace(record_resource_strict=lambda _d, row: recorded.append(row))
    manifest_secret_journal(store, "d-1", None, extra={GATEWAY_GRAPH_FIELD: True})("agentcore-gateway/x", REGION)
    manifest_secret_journal(store, "d-1", None)("runtime/x", REGION)
    assert recorded[0][GATEWAY_GRAPH_FIELD] is True
    assert GATEWAY_GRAPH_FIELD not in recorded[1], "only the gateway step's journal tags its rows"


# --------------------------------------------------------------- the DELETE path


class _Table:
    def __init__(self, items: dict[str, dict]):
        self.items = items

    def scan(self, **_kwargs):
        return {"Items": [dict(v) for v in self.items.values()]}


DISCOVERED = _row("secret", "arn:aws:secretsmanager:us-east-1:111122223333:secret:lost-AbCdEf")


def _wire_delete(monkeypatch, record: dict, gateway_outcome, *, discovered=(), hand_off=False, refuse=None):
    from app import deployment_handler
    from app.services import gateway_deployer, step_clients

    monkeypatch.setattr(
        gateway_deployer, "unrecorded_deployment_secret_rows", lambda **kw: ([dict(r) for r in discovered], [])
    )
    if hand_off or refuse:
        monkeypatch.setattr(
            deployment_handler,
            "manifest_delete_refusal",
            lambda _s, _d, res, **kw: (refuse or CO_RESIDENT_REFUSAL) if res["type"] == "gateway" else None,
        )

    table = _Table({"rt-graph-1": record})
    store = DeploymentStateStore.__new__(DeploymentStateStore)
    store._table = table
    monkeypatch.setattr(deployment_handler, "_get_state_store", lambda: store)
    monkeypatch.setattr(
        deployment_handler, "_scan_for_runtime", lambda _t, rid: {**table.items[rid], "deployment_id": ""}
    )
    monkeypatch.setattr(step_clients, "session_for_event", lambda _e: MagicMock())
    mutated: list[tuple[str, str]] = []

    def _delete(res, *_a, **_k):
        if res["type"] == "gateway":
            return gateway_outcome(res)
        mutated.append((res["type"], res["id"]))
        return f"[manifest] {res['type']} {res['id']} deleted"

    monkeypatch.setattr(deployment_handler, "_delete_managed_resource", _delete)
    monkeypatch.setattr(deployment_handler, "_revoke_client_on_kept_gateways", lambda *a, **k: ([], False))
    monkeypatch.setattr(
        deployment_handler, "destroy_runtime", MagicMock(side_effect=AssertionError("the manifest owns the runtime"))
    )
    return mutated


def _record(rows: list[dict]) -> dict:
    return {
        "deployment_id": "rt-graph-1",
        "user_id": "owner-1",
        "runtime_id": "rt-graph-1",
        "target_region": REGION,
        "resource_manifest_complete": True,
        "created_resources": rows,
    }


def _refused(_res):
    raise ResourceDeletionRefused("gateway ownership could not be proven")


def _failed(_res):
    raise ClientError({"Error": {"Code": "ValidationException", "Message": "busy"}}, "DeleteGateway")


def _left(res):
    return f"Gateway {res['id']} left in place (protected): in use"


def _deleted(res):
    return f"[manifest] gateway {res['id']} deleted"


def _silent(_res):
    return None


UNPROVEN = "the deployment table could not prove that no other live deployment references it (ClientError)"


@pytest.mark.parametrize(
    "outcome", [_refused, _failed, _left, _silent], ids=["refused", "failed", "left-in-place", "no-report"]
)
def test_delete_path_a_surviving_gateway_keeps_its_graph(monkeypatch, outcome):
    from app import deployment_handler

    rows = _manifest()
    mutated = _wire_delete(monkeypatch, _record(rows), outcome)
    result = deployment_handler._run_delete_cleanup("rt-graph-1", "owner-1")
    assert not (set(mutated) & GRAPH), f"graph mutated after the gateway survived: {set(mutated) & GRAPH}"
    assert UNRELATED <= set(mutated), "unrelated rows must still be cleaned up"
    assert result.success is False
    assert f"gateway {GW_ID} was not deleted, so its graph is left intact" in result.message
    # The rows stay in the manifest for the retry.
    assert rows == _manifest()


def test_delete_path_an_unprovable_reference_scan_keeps_the_graph(monkeypatch):
    """The one refusal that is not a hand-off: nobody can say who else uses it."""
    from app import deployment_handler

    mutated = _wire_delete(monkeypatch, _record(_manifest()), _deleted, refuse=UNPROVEN)
    deployment_handler._run_delete_cleanup("rt-graph-1", "owner-1")
    assert not (set(mutated) & GRAPH) and UNRELATED <= set(mutated)


def test_delete_path_the_retry_after_the_gateway_goes_finishes_the_graph(monkeypatch):
    from app import deployment_handler

    record = _record(_manifest())
    _wire_delete(monkeypatch, record, _failed)
    deployment_handler._run_delete_cleanup("rt-graph-1", "owner-1")
    mutated = _wire_delete(monkeypatch, record, _deleted)
    result = deployment_handler._run_delete_cleanup("rt-graph-1", "owner-1")
    assert GRAPH <= set(mutated) and result.success is True, result.message


def test_delete_path_a_deleted_gateway_lets_the_graph_go(monkeypatch):
    """The happy path: without it, a stop that froze every graph would pass the above."""
    from app import deployment_handler

    mutated = _wire_delete(monkeypatch, _record(_manifest()), _deleted)
    result = deployment_handler._run_delete_cleanup("rt-graph-1", "owner-1")
    assert GRAPH | UNRELATED <= set(mutated) and result.success is True, result.message


def test_delete_path_a_handed_off_gateway_does_not_stop_revoking_our_client(monkeypatch):
    """Another deployment's teardown owns a handed-off gateway, and our own app client
    on it is exactly the credential this teardown must revoke (F-66b)."""
    from app import deployment_handler

    mutated = _wire_delete(monkeypatch, _record(_manifest(gateway_created=False)), _deleted, hand_off=True)
    deployment_handler._run_delete_cleanup("rt-graph-1", "owner-1")
    assert ("cognito_app_client", "client-graph-1") in mutated


@pytest.mark.parametrize("created", [False, None], ids=["adopted-last-reference", "legacy-no-provenance"])
def test_delete_path_any_gateway_whose_delete_fails_keeps_its_graph(monkeypatch, created):
    from app import deployment_handler

    rows = _manifest()
    rows[1] = {k: v for k, v in rows[1].items() if k != "created_by_deployment"}
    if created is not None:
        rows[1]["created_by_deployment"] = created
    mutated = _wire_delete(monkeypatch, _record(rows), _failed)
    deployment_handler._run_delete_cleanup("rt-graph-1", "owner-1")
    assert not (set(mutated) & GRAPH)


def test_delete_path_a_secret_found_by_its_tags_is_kept_with_the_graph(monkeypatch):
    """Discovery runs before the loop; the gateway then fails. The discovered secret
    may be its connector's, so it must not go either."""
    from app import deployment_handler

    mutated = _wire_delete(monkeypatch, _record(_manifest()), _failed, discovered=[DISCOVERED])
    deployment_handler._run_delete_cleanup("rt-graph-1", "owner-1")
    assert ("secret", DISCOVERED["id"]) not in mutated
    mutated = _wire_delete(monkeypatch, _record(_manifest()), _deleted, discovered=[DISCOVERED])
    deployment_handler._run_delete_cleanup("rt-graph-1", "owner-1")
    assert ("secret", DISCOVERED["id"]) in mutated, "a deleted gateway must let the discovered secret go"


def test_delete_path_a_legacy_untagged_graph_is_kept_whole(monkeypatch):
    from app import deployment_handler

    legacy = _manifest(tagged=False)
    mutated = _wire_delete(monkeypatch, _record(legacy), _failed)
    deployment_handler._run_delete_cleanup("rt-graph-1", "owner-1")
    member = gateway_graph_membership(legacy)
    assert not {k for k in mutated if member({"type": k[0], "id": k[1]})}
    assert {("agent_runtime", "rt-graph-1"), ("memory", "mem-graph-1")} <= set(mutated)


# ----------------------------------------------------------- the failure path


class _Finalizer:
    def __init__(self, rows: list[dict]):
        self._state = DeploymentState(
            deployment_id="d-graph",
            workflow_id="wf-1",
            user_id="owner-1",
            status=DeploymentStatusEnum.FAILED,
            started_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
            created_resources=rows,
            resource_manifest_version=1,
            resource_manifest_complete=True,
        )
        self.delete_status: list[tuple] = []

    def get(self, deployment_id):
        return self._state

    def update_delete_status(self, deployment_id, status, message=None, *, finalizer_token=None):
        self.delete_status.append((status, message))

    def reset_manifest_reference_cache(self, deployment_id):
        pass

    def has_other_live_resource_reference(self, *a, **kw):
        return False

    def record_resource_strict(self, deployment_id, resource, *, finalizer_token=None):
        pass

    def mark_resource_manifest_error(self, deployment_id, *, finalizer_token=None):
        pass


def _run_failure_cleanup(monkeypatch, rows: list[dict], gateway_raises, *, discovered=(), hand_off=False, refuse=None):
    from app.services import gateway_deployer
    from app.step_handlers import status_update_step as su

    mutated: list[tuple[str, str]] = []
    if hand_off or refuse:
        monkeypatch.setattr(
            su,
            "manifest_delete_refusal",
            lambda _s, _d, res, **kw: (refuse or CO_RESIDENT_REFUSAL) if res["type"] == "gateway" else None,
        )

    def _cleanup(res, _region, _event):
        if res["type"] == "gateway" and gateway_raises is not None:
            raise gateway_raises(res)
        mutated.append((res["type"], res["id"]))

    monkeypatch.setattr(su, "_cleanup_resource", _cleanup)
    monkeypatch.setattr(
        gateway_deployer, "unrecorded_deployment_secret_rows", lambda **kw: ([dict(r) for r in discovered], [])
    )
    sts = SimpleNamespace(get_caller_identity=lambda: {"Account": "111122223333"})
    monkeypatch.setattr(su.step_clients, "client", lambda *a, **kw: sts)
    monkeypatch.setattr(
        su,
        "assert_agentcore_resource_owned",
        lambda _c, _t, gateway_id, _r: {"gatewayId": gateway_id, "name": "orders"},
    )
    finalizer = _Finalizer(rows)
    su._auto_cleanup_on_failure(finalizer, "d-graph", {"deployment_id": "d-graph", "owner_sub": "owner-1"})
    return mutated, finalizer.delete_status[-1]


def _retained(res):
    from app.step_handlers.status_update_step import _ResourceRetained

    return _ResourceRetained("gateway", res["id"], "in use")


def _rejected(res):
    from app.step_handlers.status_update_step import _DeleteRejectedAfterAccept

    return _DeleteRejectedAfterAccept("gateway", res["id"], ["kms:Decrypt denied"])


def _error(_res):
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "DeleteGateway")


@pytest.mark.parametrize("raises", [_retained, _rejected, _error], ids=["retained", "rejected-after-accept", "failed"])
def test_failure_path_a_surviving_gateway_keeps_its_graph(monkeypatch, raises):
    mutated, (status, message) = _run_failure_cleanup(monkeypatch, _manifest(), raises)
    assert not (set(mutated) & GRAPH), f"graph mutated after the gateway survived: {set(mutated) & GRAPH}"
    assert UNRELATED <= set(mutated), "unrelated rows must still be cleaned up"
    assert status != "deleted"
    assert f"Gateway {GW_ID} was not deleted, so {len(GRAPH)} resource(s) of its graph were left intact." in message


def test_failure_path_an_unprovable_reference_scan_keeps_the_graph(monkeypatch):
    mutated, (status, _m) = _run_failure_cleanup(monkeypatch, _manifest(), None, refuse=UNPROVEN)
    assert not (set(mutated) & GRAPH) and UNRELATED <= set(mutated)
    assert status == "delete_retained"


def test_failure_path_the_two_row_repro(monkeypatch):
    """Codex's exact case: a gateway that raises _ResourceRetained, then its client."""
    rows = [_row("gateway", GW_ID, name="orders"), _row("cognito_app_client", "client-graph-1", graph=True)]
    mutated, (status, _message) = _run_failure_cleanup(monkeypatch, rows, _retained)
    assert mutated == [] and status == "delete_retained"


def test_failure_path_a_deleted_gateway_lets_the_graph_go(monkeypatch):
    mutated, (status, message) = _run_failure_cleanup(monkeypatch, _manifest(), None)
    assert GRAPH | UNRELATED <= set(mutated)
    assert status == "deleted" and "left intact" not in message


def test_failure_path_a_handed_off_gateway_does_not_stop_revoking_our_client(monkeypatch):
    mutated, _ = _run_failure_cleanup(monkeypatch, _manifest(gateway_created=False), None, hand_off=True)
    assert ("cognito_app_client", "client-graph-1") in mutated


@pytest.mark.parametrize("created", [False, None], ids=["adopted-last-reference", "legacy-no-provenance"])
def test_failure_path_any_gateway_whose_delete_fails_keeps_its_graph(monkeypatch, created):
    rows = _manifest()
    rows[1] = {k: v for k, v in rows[1].items() if k != "created_by_deployment"}
    if created is not None:
        rows[1]["created_by_deployment"] = created
    mutated, _ = _run_failure_cleanup(monkeypatch, rows, _error)
    assert not (set(mutated) & GRAPH)


def test_failure_path_a_secret_found_by_its_tags_is_kept_with_the_graph(monkeypatch):
    mutated, _ = _run_failure_cleanup(monkeypatch, _manifest(), _error, discovered=[DISCOVERED])
    assert ("secret", DISCOVERED["id"]) not in mutated
    mutated, _ = _run_failure_cleanup(monkeypatch, _manifest(), None, discovered=[DISCOVERED])
    assert ("secret", DISCOVERED["id"]) in mutated


def test_failure_path_a_legacy_untagged_graph_is_kept_whole(monkeypatch):
    legacy = _manifest(tagged=False)
    mutated, _ = _run_failure_cleanup(monkeypatch, legacy, _retained)
    member = gateway_graph_membership(legacy)
    assert not {k for k in mutated if member({"type": k[0], "id": k[1]})}
    assert {("agent_runtime", "rt-graph-1"), ("memory", "mem-graph-1")} <= set(mutated)


# ------------------------------------------- a gateway that lost some of its targets


class _GatewayCtrl:
    """The real gateway arms run against this: two targets that delete, then a
    gateway that does not go away in the way *outcome* names."""

    def __init__(self, outcome: str):
        self.outcome = outcome
        self.targets = ["tgt-b", "tgt-a"]
        self.deleted_targets: list[str] = []

    def list_gateway_targets(self, **_kw):
        return {"items": [{"targetId": t} for t in self.targets]}

    def delete_gateway_target(self, *, gatewayIdentifier, targetId):
        self.targets.remove(targetId)
        self.deleted_targets.append(targetId)

    def delete_gateway(self, **_kw):
        if self.outcome == "delete-denied":
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "DeleteGateway")

    def get_gateway(self, **_kw):
        # Named, so the teardown name hold reads it as the recorded gateway.
        status = "FAILED" if self.outcome == "failed-after-accept" else "DELETING"
        return {"gatewayId": GW_ID, "name": "orders", "status": status, "statusReasons": ["kms"]}


PARTIAL = "gateway gw-graph-1 was NOT deleted after 2 of its targets were: tgt-a, tgt-b"
OUTCOMES = ["delete-denied", "failed-after-accept", "never-went-away"]


def _no_sleep(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_failure_path_reports_the_targets_it_deleted_before_the_gateway_failed(monkeypatch, outcome):
    from app.step_handlers import status_update_step as su

    _no_sleep(monkeypatch)
    monkeypatch.setattr(su, "_GATEWAY_DELETE_CONFIRM_BUDGET_S", 0.0)
    ctrl = _GatewayCtrl(outcome)
    real = su._cleanup_resource
    others: list[tuple[str, str]] = []

    def _cleanup(res, region, event):
        if res["type"] != "gateway":
            others.append((res["type"], res["id"]))
            return None
        return real(res, region, event)

    sts = SimpleNamespace(get_caller_identity=lambda: {"Account": "111122223333"})
    monkeypatch.setattr(su.step_clients, "client", lambda _e, svc, **kw: sts if svc == "sts" else ctrl)
    monkeypatch.setattr(
        su,
        "assert_agentcore_resource_owned",
        lambda _c, _t, gateway_id, _r: {"gatewayId": gateway_id, "name": "orders"},
    )
    from app.services import gateway_deployer

    monkeypatch.setattr(gateway_deployer, "unrecorded_deployment_secret_rows", lambda **kw: ([], []))
    monkeypatch.setattr(su, "_cleanup_resource", _cleanup)
    finalizer = _Finalizer(_manifest())
    su._auto_cleanup_on_failure(finalizer, "d-graph", {"deployment_id": "d-graph", "owner_sub": "owner-1"})
    status, message = finalizer.delete_status[-1]
    assert ctrl.deleted_targets == ["tgt-b", "tgt-a"], "the fixture must actually delete targets"
    assert PARTIAL in message, message
    assert status != "deleted"
    assert not (set(others) & GRAPH) and UNRELATED <= set(others), "the graph freeze still holds"


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_delete_path_reports_the_targets_it_deleted_before_the_gateway_failed(monkeypatch, outcome):
    from app import deployment_handler
    from app.services import step_clients

    _no_sleep(monkeypatch)
    ctrl = _GatewayCtrl(outcome)
    real = deployment_handler._delete_managed_resource

    def _gateway(res):
        return real(res, REGION, target_session=SimpleNamespace(client=lambda *_a, **_k: ctrl))

    mutated = _wire_delete(monkeypatch, _record(_manifest()), _gateway)
    monkeypatch.setattr(
        deployment_handler, "assert_agentcore_resource_owned", lambda *a, **k: {"gatewayId": GW_ID, "name": "orders"}
    )
    monkeypatch.setattr(step_clients, "session_for_event", lambda _e: MagicMock())
    result = deployment_handler._run_delete_cleanup("rt-graph-1", "owner-1")
    assert ctrl.deleted_targets == ["tgt-b", "tgt-a"], result.message
    assert PARTIAL in result.message, result.message
    assert result.success is False
    assert not (set(mutated) & GRAPH) and UNRELATED <= set(mutated)


def test_a_gateway_that_fails_with_no_target_deleted_claims_none():
    """The baseline for the sentence: it appears only when targets actually went."""
    from app.services.deployment_state_store import (
        describe_gateway_targets_deleted,
        gateway_targets_deleted,
        note_gateway_targets_deleted,
    )

    exc = RuntimeError("x")
    note_gateway_targets_deleted(exc, [])
    assert gateway_targets_deleted(exc) == []
    many = [f"t{i:02d}" for i in range(25)]
    text = describe_gateway_targets_deleted("gw", many)
    assert "after 25 of its targets" in text and text.endswith("t09 and 15 more") and len(text) < 200
