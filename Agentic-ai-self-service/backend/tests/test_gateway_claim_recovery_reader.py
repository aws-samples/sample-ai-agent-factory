"""A gateway recorded only on its name claim is found, and torn down, by both dispatchers.

When every manifest write for a standing gateway failed, ``promote(recovery=...)``
leaves ``recovery_gateway_id`` and ``recovery_deployment_id`` on the durable claim, so
the claim is the last handle on it. Those fields were write-only: a later teardown
with no manifest row and no ``gateway_result`` saw nothing, and the failure path
even recorded "no resources were created". These run the real readers and the real
dispatch loops against the conftest's in-memory claim table, whose every read is a
strongly consistent GetItem by key: the reader has no index to lag behind.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.services import gateway_name_claim as gnc
from app.services.resource_ownership import ResourceDeletionRefused
from botocore.exceptions import ClientError

from tests.test_gateway_graph_stays_whole import _Finalizer, _record, _wire_delete

ACCOUNT = "111122223333"
REGION = "us-east-1"
GW = "orders-gw-abc123"


def _leave_recovery(table, *, deployment_id, owner="owner-1", account=ACCOUNT, region=REGION, name="orders"):
    claims = gnc.GatewayNameClaims(table)
    assert claims.acquire(
        account=account, region=region, name=name, owner_sub=owner, deployment_id=deployment_id, token="tok-step"
    )
    assert claims.promote(
        account=account,
        region=region,
        name=name,
        owner_sub=owner,
        token="tok-step",
        recovery={"recovery_gateway_id": GW, "recovery_deployment_id": deployment_id},
    )
    return gnc.claim_key(account, region, name)


def _read(table, deployment_id="d-1", owner="owner-1", **kw):
    return gnc.recovered_gateway_rows(
        deployment_id=deployment_id,
        owner_sub=owner,
        account_for=kw.pop("account_for", lambda: ACCOUNT),
        region=kw.pop("region", REGION),
        claims=gnc.GatewayNameClaims(table),
        **kw,
    )


# --- the reader ------------------------------------------------------------------


def test_a_recovery_claim_reads_back_as_the_gateway_row(gateway_lock_table):
    _leave_recovery(gateway_lock_table, deployment_id="d-1")
    assert _read(gateway_lock_table) == [{"type": "gateway", "id": GW, "name": "orders", "region": REGION}]


def test_an_ordinary_claim_is_not_in_the_index(gateway_lock_table):
    claims = gnc.GatewayNameClaims(gateway_lock_table)
    claims.acquire(account=ACCOUNT, region=REGION, name="orders", owner_sub="owner-1", deployment_id="d-1", token="t")
    claims.promote(account=ACCOUNT, region=REGION, name="orders", owner_sub="owner-1", token="t")
    assert _read(gateway_lock_table) == []


@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("another owner", {"owner": "owner-2"}),
        ("no owner on record", {"owner": None}),
        ("another account", {"account_for": lambda: "999999999999"}),
        ("another region", {"region": "eu-west-1"}),
        ("keyed in another region", {"leave": {"region": "eu-west-1"}}),
    ],
)
def test_a_recovery_claim_outside_this_deployment_refuses_the_teardown(gateway_lock_table, case, kwargs):
    """Found under this deployment's own id, so a mismatch is corrupt or foreign
    evidence: skipping it could let a teardown succeed and orphan the gateway."""
    _leave_recovery(gateway_lock_table, deployment_id=kwargs.pop("seed", "d-1"), **kwargs.pop("leave", {}))
    with pytest.raises(gnc.GatewayNameClaimRefused, match="nothing was deleted"):
        _read(gateway_lock_table, **kwargs)


def _corrupt_claim(table, key, **corrupt):
    """The claim as the strongly consistent read returns it, with *corrupt* applied."""
    real = table.get_item

    def _get(Key, **kw):  # noqa: N803
        got = real(Key=Key, **kw)
        return {"Item": dict(got["Item"], **corrupt)} if Key["claim_key"] == key else got

    table.get_item = _get


@pytest.mark.parametrize(
    ("case", "corrupt"),
    [
        ("provisional", {"provisional": True}),
        ("no gateway id", {"recovery_gateway_id": ""}),
        ("a path, not an id", {"recovery_gateway_id": "../x"}),
        ("a two-part key", {"claim_key": "111122223333#orders"}),
        ("an empty key part", {"claim_key": "111122223333##orders"}),
    ],
)
def test_a_malformed_recovery_item_refuses_the_teardown(gateway_lock_table, case, corrupt):
    key = _leave_recovery(gateway_lock_table, deployment_id="d-1")
    _corrupt_claim(gateway_lock_table, key, **corrupt)
    with pytest.raises(gnc.GatewayNameClaimRefused):
        _read(gateway_lock_table)


@pytest.mark.parametrize(
    ("case", "pointer"),
    [
        ("no claim_keys", {}),
        ("an empty set", {"claim_keys": set()}),
        ("a list, not a set", {"claim_keys": ["111122223333#us-east-1#orders"]}),
        ("a string", {"claim_keys": "111122223333#us-east-1#orders"}),
        ("a non-string key", {"claim_keys": {7}}),
        ("an empty key", {"claim_keys": {""}}),
    ],
)
def test_a_malformed_pointer_is_corruption_not_absence(gateway_lock_table, case, pointer):
    """d0: only an absent pointer reads as "nothing recorded here"."""
    key = gnc.recovery_pointer_key("d-1")
    gateway_lock_table.items[key] = {"claim_key": key, **pointer}
    with pytest.raises(gnc.GatewayNameClaimRefused, match="malformed recovery pointer"):
        _read(gateway_lock_table)


def test_no_pointer_is_one_read_and_nothing(gateway_lock_table):
    assert _read(gateway_lock_table) == []
    assert gateway_lock_table.calls == [("get_item", gnc.recovery_pointer_key("d-1"))]


def test_every_read_is_strongly_consistent_and_by_key(gateway_lock_table):
    """e4 #1: the fake asserts ConsistentRead on every GetItem, and has no Query or Scan."""
    key = _leave_recovery(gateway_lock_table, deployment_id="d-1")
    gateway_lock_table.calls.clear()
    assert [r["id"] for r in _read(gateway_lock_table)] == [GW]
    assert gateway_lock_table.calls == [("get_item", gnc.recovery_pointer_key("d-1")), ("get_item", key)]
    assert not hasattr(gateway_lock_table, "query") and not hasattr(gateway_lock_table, "scan")


@pytest.mark.parametrize(
    ("case", "change"),
    [
        ("erased", None),
        ("cleared by a later deployment", {"recovery_gateway_id": None, "recovery_deployment_id": None}),
        ("another deployment's evidence", {"recovery_deployment_id": "d-2"}),
    ],
)
def test_a_listed_claim_without_this_deployments_evidence_is_skipped(gateway_lock_table, case, change):
    """The pointer is never shrunk, so it may list a claim whose evidence is gone. The
    evidence is the claim's: without it there is nothing of this deployment's there."""
    key = _leave_recovery(gateway_lock_table, deployment_id="d-1")
    if change is None:
        del gateway_lock_table.items[key]
    else:
        item = gateway_lock_table.items[key]
        for attr, value in change.items():
            item.pop(attr) if value is None else item.__setitem__(attr, value)
    assert _read(gateway_lock_table) == []


def test_a_failed_read_raises_rather_than_reading_as_nothing(gateway_lock_table):
    gateway_lock_table.get_item = MagicMock(
        side_effect=ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetItem")
    )
    with pytest.raises(ClientError):
        _read(gateway_lock_table)


def test_a_failed_claim_read_after_the_pointer_raises(gateway_lock_table):
    key = _leave_recovery(gateway_lock_table, deployment_id="d-1")
    real = gateway_lock_table.get_item

    def _get(Key, **kw):  # noqa: N803
        if Key["claim_key"] == key:
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "GetItem")
        return real(Key=Key, **kw)

    gateway_lock_table.get_item = _get
    with pytest.raises(ClientError):
        _read(gateway_lock_table)


# --- the failure path ------------------------------------------------------------


def _failure(monkeypatch, *, owned=True, live_name="orders", gone=False, sealed=True, dep="d-graph", **event):
    from app.services import gateway_deployer
    from app.step_handlers import status_update_step as su

    cleaned: list[tuple[str, str]] = []
    monkeypatch.setattr(su, "_cleanup_resource", lambda res, _r, _e: cleaned.append((res["type"], res["id"])))
    monkeypatch.setattr(gateway_deployer, "unrecorded_deployment_secret_rows", lambda **kw: ([], []))
    sts = SimpleNamespace(get_caller_identity=lambda: {"Account": ACCOUNT})
    monkeypatch.setattr(su.step_clients, "client", lambda *a, **kw: sts)

    def _prove(_c, _t, gateway_id, _r):
        if gone:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetGateway")
        if not owned:
            raise ResourceDeletionRefused("not this platform's")
        return {"gatewayId": gateway_id, "name": live_name}

    monkeypatch.setattr(su, "assert_agentcore_resource_owned", _prove)
    finalizer = _Finalizer([])  # no manifest row, and the event carries no gateway_result
    finalizer._state.resource_manifest_complete = sealed
    su._auto_cleanup_on_failure(
        finalizer, dep, {"deployment_id": dep, "owner_sub": "owner-1", "target_region": REGION, **event}
    )
    return cleaned, finalizer.delete_status[-1]


def test_failure_path_tears_down_a_gateway_only_its_claim_records(monkeypatch, gateway_lock_table):
    key = _leave_recovery(gateway_lock_table, deployment_id="d-graph")
    cleaned, (status, message) = _failure(monkeypatch)
    assert cleaned == [("gateway", GW)]
    assert status == "deleted", message
    assert key not in gateway_lock_table.items, "the last handle goes with the gateway"


def test_failure_path_refuses_another_owners_recovery_claim(monkeypatch, gateway_lock_table):
    key = _leave_recovery(gateway_lock_table, deployment_id="d-graph", owner="owner-2")
    cleaned, (status, message) = _failure(monkeypatch)
    assert cleaned == []
    assert status == "delete_failed" and "another owner" in message, message
    assert gateway_lock_table.items[key]["owner_sub"] == "owner-2"


def test_failure_path_refuses_a_recovery_claim_keyed_in_another_account(monkeypatch, gateway_lock_table):
    _leave_recovery(gateway_lock_table, deployment_id="d-graph", account="999999999999")
    cleaned, (status, message) = _failure(monkeypatch)
    assert cleaned == [] and status == "delete_failed" and "target account" in message, message


def test_failure_path_refuses_a_recovery_claim_whose_gateway_lives_under_another_name(monkeypatch, gateway_lock_table):
    key = _leave_recovery(gateway_lock_table, deployment_id="d-graph")
    cleaned, (status, message) = _failure(monkeypatch, live_name="payments")
    assert cleaned == [] and status == "delete_failed", message
    assert gateway_lock_table.items[key]["recovery_gateway_id"] == GW


def test_failure_path_with_a_stale_recovery_claim_deletes_nothing_live(monkeypatch, gateway_lock_table):
    """The gateway is already gone: the row is still offered to the (idempotent)
    cleanup, which is what lets the claim be erased."""
    key = _leave_recovery(gateway_lock_table, deployment_id="d-graph")
    cleaned, (status, message) = _failure(monkeypatch, gone=True)
    assert status == "deleted", message
    assert key not in gateway_lock_table.items


def test_a_recovered_gateway_that_fails_still_freezes_a_legacy_graph(monkeypatch, gateway_lock_table):
    """The recovered row carries no graph tag: one tagged row would make every
    untagged row of a legacy manifest read as unrelated, and the freeze would then
    dismantle the graph of the very gateway that just survived."""
    from app.services import gateway_deployer
    from app.step_handlers import status_update_step as su

    _leave_recovery(gateway_lock_table, deployment_id="d-graph")
    mutated: list[str] = []

    def _cleanup(res, _r, _e):
        if res["type"] == "gateway":
            raise ClientError({"Error": {"Code": "ValidationException"}}, "DeleteGateway")
        mutated.append(res["type"])

    monkeypatch.setattr(su, "_cleanup_resource", _cleanup)
    monkeypatch.setattr(gateway_deployer, "unrecorded_deployment_secret_rows", lambda **kw: ([], []))
    monkeypatch.setattr(su.step_clients, "client", lambda *a, **kw: SimpleNamespace(get_caller_identity=dict))
    monkeypatch.setattr(su, "assert_agentcore_resource_owned", lambda _c, _t, g, _r: {"gatewayId": g, "name": "orders"})
    legacy = [
        {"type": "lambda", "id": "orders-tool", "name": "orders-tool", "region": REGION},
        {"type": "memory", "id": "mem-1", "region": REGION},
    ]
    finalizer = _Finalizer(legacy)
    finalizer._state.target_account_id = ACCOUNT
    su._auto_cleanup_on_failure(
        finalizer, "d-graph", {"deployment_id": "d-graph", "owner_sub": "owner-1", "target_region": REGION}
    )
    assert mutated == ["memory"], "the legacy lambda is graph-capable and must be left with its gateway"


def test_failure_path_leaves_nothing_when_the_claims_cannot_be_read(monkeypatch, gateway_lock_table):
    _leave_recovery(gateway_lock_table, deployment_id="d-graph")
    gateway_lock_table.get_item = MagicMock(
        side_effect=ClientError({"Error": {"Code": "ThrottlingException"}}, "GetItem")
    )
    cleaned, (status, message) = _failure(monkeypatch)
    assert cleaned == [] and status == "delete_failed", message


# --- the DELETE path -------------------------------------------------------------


def _delete(monkeypatch, *, owned=True, live_name="orders"):
    from app import deployment_handler

    deleted: list[str] = []

    def _gateway(res):
        # The real gateway arm proves ownership live itself, before any delete.
        if not owned:
            raise ResourceDeletionRefused("not this platform's")
        deleted.append(res["id"])
        return f"[manifest] gateway {res['id']} deleted"

    record = {**_record([]), "target_account_id": ACCOUNT, "resource_manifest_complete": False}
    _wire_delete(monkeypatch, record, _gateway)
    monkeypatch.setattr(deployment_handler, "destroy_runtime", lambda *a, **k: {"success": True, "message": "gone"})

    def _prove(_c, _t, gateway_id, _r):
        if not owned:
            raise ResourceDeletionRefused("not this platform's")
        return {"gatewayId": gateway_id, "name": live_name}

    monkeypatch.setattr(deployment_handler, "assert_agentcore_resource_owned", _prove)
    return deployment_handler._run_delete_cleanup("rt-graph-1", "owner-1"), deleted


def test_delete_path_tears_down_a_gateway_only_its_claim_records(monkeypatch, gateway_lock_table):
    key = _leave_recovery(gateway_lock_table, deployment_id="rt-graph-1")
    result, deleted = _delete(monkeypatch)
    assert deleted == [GW], result.message
    assert key not in gateway_lock_table.items, result.message


def test_delete_path_refuses_another_owners_recovery_claim(monkeypatch, gateway_lock_table):
    key = _leave_recovery(gateway_lock_table, deployment_id="rt-graph-1", owner="owner-2")
    result, deleted = _delete(monkeypatch)
    assert deleted == [] and result.success is False and "another owner" in result.message
    assert gateway_lock_table.items[key]["recovery_gateway_id"] == GW


def test_delete_path_refuses_a_recovery_claim_keyed_in_another_account(monkeypatch, gateway_lock_table):
    _leave_recovery(gateway_lock_table, deployment_id="rt-graph-1", account="999999999999")
    result, deleted = _delete(monkeypatch)
    assert deleted == [] and result.success is False and "target account" in result.message


def test_delete_path_of_a_foreign_recovered_gateway_deletes_nothing(monkeypatch, gateway_lock_table):
    """Ours on the claim, not ours live: the name hold's proof, not the claim, decides."""
    _leave_recovery(gateway_lock_table, deployment_id="rt-graph-1")
    _result, deleted = _delete(monkeypatch, owned=False)
    assert deleted == []


def test_delete_path_refuses_a_recovery_claim_whose_gateway_lives_under_another_name(monkeypatch, gateway_lock_table):
    key = _leave_recovery(gateway_lock_table, deployment_id="rt-graph-1")
    result, deleted = _delete(monkeypatch, live_name="payments")
    assert deleted == [] and result.success is False
    assert "different name" in result.message
    assert gateway_lock_table.items[key]["recovery_gateway_id"] == GW


def test_delete_path_deletes_nothing_when_the_claims_cannot_be_read(monkeypatch, gateway_lock_table):
    _leave_recovery(gateway_lock_table, deployment_id="rt-graph-1")
    gateway_lock_table.get_item = MagicMock(
        side_effect=ClientError({"Error": {"Code": "ThrottlingException"}}, "GetItem")
    )
    result, deleted = _delete(monkeypatch)
    assert deleted == [] and result.success is False
    assert "nothing was deleted" in result.message and "Throttling" not in result.message


def test_a_claim_left_by_failed_writes_is_found_by_a_later_manual_delete(monkeypatch, gateway_lock_table):
    """Codex 85b end to end: the failure pass cannot write any manifest row or handle
    and the gateway survives it, so only the claim records it; a DELETE issued later
    finds it through its recovery pointer and removes it."""
    key = _leave_recovery(gateway_lock_table, deployment_id="rt-graph-1")
    # A failure pass whose gateway delete keeps failing leaves the claim as it was.
    from app.services import gateway_deployer
    from app.step_handlers import status_update_step as su

    def _still_failing(res, _r, _e):
        raise ClientError({"Error": {"Code": "ThrottlingException"}}, "DeleteGateway")

    monkeypatch.setattr(su, "_cleanup_resource", _still_failing)
    monkeypatch.setattr(gateway_deployer, "unrecorded_deployment_secret_rows", lambda **kw: ([], []))
    monkeypatch.setattr(su.step_clients, "client", lambda *a, **kw: SimpleNamespace(get_caller_identity=dict))
    monkeypatch.setattr(su, "assert_agentcore_resource_owned", lambda _c, _t, g, _r: {"gatewayId": g, "name": "orders"})
    finalizer = _Finalizer([])
    finalizer._state.resource_manifest_complete = False
    finalizer._state.target_account_id = ACCOUNT
    su._auto_cleanup_on_failure(
        finalizer, "rt-graph-1", {"deployment_id": "rt-graph-1", "owner_sub": "owner-1", "target_region": REGION}
    )
    assert finalizer.delete_status[-1][0] == "delete_failed"
    assert gateway_lock_table.items[key]["recovery_gateway_id"] == GW
    assert "holder_token" not in gateway_lock_table.items[key], "the failed pass released its lease"
    assert "gc_after" not in _pointer(gateway_lock_table, "rt-graph-1"), "a failed pass keeps its pointer"

    result, deleted = _delete(monkeypatch)
    assert deleted == [GW], result.message
    assert key not in gateway_lock_table.items
    assert result.success is True and "gc_after" in _pointer(gateway_lock_table, "rt-graph-1")


# --- the recovery pointer's reclaim --------------------------------------------------
#
# A pointer is only ever removed by the table's TTL, so each dispatcher marks it once
# nothing it lists still carries this deployment's evidence, and a teardown may call
# itself "deleted" only once that mark landed (or there is no pointer).


def _pointer(table, dep):
    return table.items.get(gnc.recovery_pointer_key(dep))


def _fail_mark(table, monkeypatch, code):
    """The pointer mark (SET gc_after) fails with ``code``; every other write lands."""
    real = table.update_item

    def _update(**kw):
        if kw["UpdateExpression"] == "SET gc_after = :exp":
            raise ClientError({"Error": {"Code": code}}, "UpdateItem")
        return real(**kw)

    monkeypatch.setattr(table, "update_item", _update)
    return lambda: monkeypatch.setattr(table, "update_item", real)


def _fail_pointer_reread(table, monkeypatch, dep):
    """The reclaim's read of the pointer fails; the reader's (the first) succeeds."""
    real, reads = table.get_item, []

    def _get(**kw):
        if kw["Key"]["claim_key"] == gnc.recovery_pointer_key(dep):
            reads.append(1)
            if len(reads) > 1:
                raise ClientError({"Error": {"Code": "ThrottlingException"}}, "GetItem")
        return real(**kw)

    monkeypatch.setattr(table, "get_item", _get)
    return lambda: monkeypatch.setattr(table, "get_item", real)


def test_failure_path_marks_the_pointer_after_a_full_teardown(monkeypatch, gateway_lock_table):
    _leave_recovery(gateway_lock_table, deployment_id="d-graph")
    cleaned, (status, message) = _failure(monkeypatch)
    assert status == "deleted", message
    assert "gc_after" in _pointer(gateway_lock_table, "d-graph")


def test_delete_path_marks_the_pointer_after_a_full_teardown(monkeypatch, gateway_lock_table):
    _leave_recovery(gateway_lock_table, deployment_id="rt-graph-1")
    result, _deleted = _delete(monkeypatch)
    assert result.success is True, result.message
    assert "gc_after" in _pointer(gateway_lock_table, "rt-graph-1")


#: The server-authored proof the empty-manifest branch needs before it says "deleted".
_NOTHING_CREATED = {"no_resources_created": {"proven": True, "reason": "rejected at ValidateWorkflow"}}


def test_a_refused_teardown_leaves_the_pointer_unmarked(monkeypatch, gateway_lock_table):
    """Another owner's claim is refused, keeps its evidence, and so keeps the pointer."""
    _leave_recovery(gateway_lock_table, deployment_id="d-graph", owner="owner-2")
    _cleaned, (status, _message) = _failure(monkeypatch)
    assert status == "delete_failed"
    assert "gc_after" not in _pointer(gateway_lock_table, "d-graph")

    _leave_recovery(gateway_lock_table, deployment_id="rt-graph-1", owner="owner-2", name="payments")
    result, deleted = _delete(monkeypatch)
    assert result.success is False and deleted == []
    assert "gc_after" not in _pointer(gateway_lock_table, "rt-graph-1")


_UNFINISHED = [
    ("the mark is throttled", lambda t, m, dep: _fail_mark(t, m, "ThrottlingException"), "could not be checked"),
    (
        "a promote moved the generation",
        lambda t, m, dep: _fail_mark(t, m, "ConditionalCheckFailedException"),
        "appeared during cleanup",
    ),
    ("the pointer cannot be reread", _fail_pointer_reread, "could not be checked"),
]


@pytest.mark.parametrize(("case", "inject", "reason"), _UNFINISHED, ids=[c[0] for c in _UNFINISHED])
def test_failure_path_is_not_deleted_until_the_pointer_is_marked(monkeypatch, gateway_lock_table, case, inject, reason):
    """Nothing retries a "deleted" deployment, so an unmarked pointer must leave it
    delete_failed; the DELETE that retries it then marks the pointer."""
    key = _leave_recovery(gateway_lock_table, deployment_id="rt-graph-1")
    heal = inject(gateway_lock_table, monkeypatch, "rt-graph-1")
    cleaned, (status, message) = _failure(monkeypatch, dep="rt-graph-1")
    assert cleaned == [("gateway", GW)] and key not in gateway_lock_table.items
    assert status == "delete_failed", message
    assert 0 <= message.find(reason) < 120, f"the reason leads, inside the 1 KiB cap: {message}"
    assert "gc_after" not in _pointer(gateway_lock_table, "rt-graph-1")

    heal()
    result, deleted = _delete(monkeypatch)
    assert deleted == [] and result.success is True, result.message
    assert "gc_after" in _pointer(gateway_lock_table, "rt-graph-1")


@pytest.mark.parametrize(("case", "inject", "reason"), _UNFINISHED, ids=[c[0] for c in _UNFINISHED])
def test_delete_path_is_not_deleted_until_the_pointer_is_marked(monkeypatch, gateway_lock_table, case, inject, reason):
    _leave_recovery(gateway_lock_table, deployment_id="rt-graph-1")
    heal = inject(gateway_lock_table, monkeypatch, "rt-graph-1")
    result, deleted = _delete(monkeypatch)
    assert deleted == [GW]
    assert result.success is False and 0 <= result.message.find(reason) < 120, result.message
    assert "gc_after" not in _pointer(gateway_lock_table, "rt-graph-1")

    heal()
    result, deleted = _delete(monkeypatch)
    assert deleted == [] and result.success is True, result.message
    assert "gc_after" in _pointer(gateway_lock_table, "rt-graph-1")


def test_the_empty_manifest_branch_is_gated_on_the_mark(monkeypatch, gateway_lock_table):
    """No gateway left to find and a proof that nothing was created, but a pointer still
    unmarked: "no resources were created" must still wait for the mark."""
    claims = gnc.GatewayNameClaims(gateway_lock_table)
    _leave_recovery(gateway_lock_table, deployment_id="d-graph")
    assert claims.erase(account=ACCOUNT, region=REGION, name="orders", owner_sub="owner-1")
    heal = _fail_mark(gateway_lock_table, monkeypatch, "ThrottlingException")
    cleaned, (status, message) = _failure(monkeypatch, **_NOTHING_CREATED)
    assert cleaned == [] and status == "delete_failed", message
    assert "gc_after" not in _pointer(gateway_lock_table, "d-graph")
    heal()
    cleaned, (status, message) = _failure(monkeypatch, **_NOTHING_CREATED)
    assert cleaned == [] and status == "deleted", message
    assert "gc_after" in _pointer(gateway_lock_table, "d-graph")


def test_no_pointer_is_benign_to_both_dispatchers(monkeypatch, gateway_lock_table):
    _cleaned, (status, message) = _failure(monkeypatch, **_NOTHING_CREATED)
    assert status == "deleted", message
    result, _deleted = _delete(monkeypatch)
    assert result.success is True, result.message
    assert not any(k.startswith("recovery#") for k in gateway_lock_table.items)
