"""F-G05-001 / F-G09-003: a managed Cedar policy is reconciled to its exact desired definition, the managed SET is
reconciled on a reused engine, every policy is a first-class manifest child, and teardown deletes only what this
deployment created -- never a co-resident deployment's policy or one on an engine another live deployment still uses.

Each test below is a false-pass it closes: it fails against the pre-fix code path, not just passes against the new one.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from app import deployment_handler
from app.services import policy_lifecycle, policy_promoter, step_clients
from app.services.deployment_state_store import (
    CO_RESIDENT_REFUSAL,
    manifest_delete_refusal,
    manifest_resource_key,
)
from app.services.policy_lifecycle import (
    PolicyCoResidencyConflict,
    PolicySetDrift,
    PolicyStateUnreadable,
    cedar_digest,
    is_managed_policy_name,
)
from app.services.resource_ownership import ResourceDeletionRefused
from app.step_handlers import policy_step, status_update_step
from botocore.exceptions import ClientError

from tests.gateway_fakes import applying_updates

REGION = "us-east-1"
ENGINE_NAME = "SharedEngine"
ENGINE_ID = "eng-1"
GATEWAY_ID = "gw-1"
GATEWAY_ARN = f"arn:aws:bedrock-agentcore:{REGION}:123456789012:gateway/{GATEWAY_ID}"
BASE = "allow_permitted_tools"
POLICY_NAME = f"{ENGINE_NAME[: max(0, 48 - len(BASE) - 1)]}_{BASE}"[:48]
POLICY_ID = "p-current"
OLD = (
    "permit(principal is AgentCore::OAuthUser, "
    'action in [AgentCore::Action::"T___get_canary", AgentCore::Action::"T___get_secret"], '
    f'resource == AgentCore::Gateway::"{GATEWAY_ARN}");'
)


def _not_found(op="GetPolicy"):
    return ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}}, op)


@contextlib.contextmanager
def _gateway_lock(ctrl, _region, gateway_id):
    class Lock:
        def read(self):
            return ctrl.get_gateway(gatewayIdentifier=gateway_id)

        def update(self, request, predicate):
            ctrl.update_gateway(**request)
            assert predicate(ctrl.get_gateway(gatewayIdentifier=gateway_id))

    yield Lock()


def _engine_control(live: dict):
    """A control plane holding a reused engine whose live policies are ``live`` (id -> {name, statement})."""
    ctrl = MagicMock()
    ctrl.list_policy_engines.return_value = {
        "policyEngines": [
            {
                "name": ENGINE_NAME,
                "policyEngineId": ENGINE_ID,
                "policyEngineArn": f"arn:aws:bedrock-agentcore:{REGION}:123456789012:policy-engine/{ENGINE_ID}",
            }
        ]
    }
    updates: list[dict] = []

    def list_policies(**_kw):
        return {
            "policies": [
                {"name": v["name"], "status": v.get("status", "ACTIVE"), "policyId": k} for k, v in live.items()
            ]
        }

    def get_policy(**kw):
        pid = kw.get("policyId")
        if pid not in live:
            raise _not_found()
        v = live[pid]
        return {
            "name": v["name"],
            "status": v.get("status", "ACTIVE"),
            "policyId": pid,
            "definition": {"cedar": {"statement": v["statement"]}},
        }

    def update_policy(**kw):
        updates.append(kw)
        live[kw["policyId"]]["statement"] = kw["definition"]["cedar"]["statement"]
        live[kw["policyId"]]["status"] = "ACTIVE"
        return {"policyId": kw["policyId"]}

    def create_policy(**kw):
        if any(v["name"] == kw.get("name") for v in live.values()):
            raise ClientError(
                {"Error": {"Code": "ConflictException", "Message": "policy already exists"}}, "CreatePolicy"
            )
        pid = f"p-new-{len(live)}"
        live[pid] = {"name": kw["name"], "statement": kw["definition"]["cedar"]["statement"], "status": "ACTIVE"}
        return {"policyId": pid, "status": "ACTIVE"}

    def delete_policy(**kw):
        live.pop(kw["policyId"], None)
        return {}

    ctrl.list_policies.side_effect = list_policies
    ctrl.get_policy.side_effect = get_policy
    ctrl.update_policy.side_effect = update_policy
    ctrl.create_policy.side_effect = create_policy
    ctrl.delete_policy.side_effect = delete_policy
    ctrl.get_gateway.return_value = {
        "name": "gateway",
        "roleArn": "arn:aws:iam::123456789012:role/GatewayRole",
        "protocolType": "MCP",
        "authorizerType": "CUSTOM_JWT",
        "status": "READY",
    }
    applying_updates(ctrl)
    return ctrl, updates


def _event(rules=None):
    return {
        "deployment_id": "deployment-new",
        "target_region": REGION,
        "policy_config": {
            "enabled": True,
            "mode": "ENFORCE",
            "name": ENGINE_NAME,
            "rules": rules or [{"effect": "forbid", "action": "get_secret"}],
        },
        "gateway_result": {
            "gateway_id": GATEWAY_ID,
            "gateway_arn": GATEWAY_ARN,
            "qualified_tools": ["T___get_canary", "T___get_secret"],
            "expected_tool_count": 2,
        },
    }


def _run_step(ctrl, store, event=None):
    with (
        patch.object(policy_step, "_get_deployment_store", return_value=store),
        patch.object(policy_step, "assert_agentcore_resource_owned"),
        patch.object(policy_step, "step_clients") as clients,
        patch.object(policy_step, "gateway_mutation_lock", _gateway_lock),
        patch("time.sleep"),
    ):
        clients.client.return_value = ctrl
        return policy_step.handler(event or _event(), None)


def _rows(store) -> list[dict]:
    return [c.args[1] for c in store.record_resource_strict.call_args_list] + [
        c.args[1] for c in store.record_resource.call_args_list
    ]


def _store(other_rows=None, *, provenance=None, engine_shared=False):
    """A manifest store: ``other_rows`` = other live deployments' rows for any policy; ``provenance`` = rows of ANY
    status (include_deleted) proving platform ownership of extras; ``engine_shared`` = another live deployment
    references the parent engine."""
    store = MagicMock()

    def rows(_dep, row, **kw):
        if kw.get("include_deleted"):
            return list(provenance if provenance is not None else [])
        return list(other_rows or [])

    store.resource_rows.side_effect = rows
    store.other_live_resource_rows.side_effect = lambda dep, row, **kw: rows(dep, row)
    store.has_other_live_resource_reference.side_effect = lambda *_a, **_k: bool(engine_shared)
    return store


# --------------------------------------------------------------------------- definition reconciliation (F-G05-001)


def test_reused_active_policy_is_reconciled_to_the_desired_cedar_and_recorded():
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}}
    ctrl, updates = _engine_control(live)
    store = _store()
    result = _run_step(ctrl, store)
    assert result["policy_result"]["mode"] == "ENFORCE"
    assert len(updates) == 1 and updates[0]["policyId"] == POLICY_ID
    landed = live[POLICY_ID]["statement"]
    assert "T___get_canary" in landed and "T___get_secret" not in landed, landed
    assert updates[0]["description"] == {"optionalValue": updates[0]["description"]["optionalValue"]}
    children = [r for r in _rows(store) if r["type"] == "policy"]
    assert children and all(
        r["id"] == POLICY_ID
        and r["engine_id"] == ENGINE_ID
        and r["policy_engine_id"] == ENGINE_ID
        and r["region"] == REGION
        and r["created_by_deployment"] is False
        and r["desired_definition_sha256"] == cedar_digest(landed)
        for r in children
    ), children
    # the adopted row is written STRICTLY (a failure means no mutation), before update_policy
    assert store.record_resource_strict.call_count >= 1


def test_reused_policy_with_the_same_definition_is_not_rewritten():
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": "placeholder"}}
    ctrl, updates = _engine_control(live)
    # first run lands the desired text; second run must be a no-op on update_policy
    _run_step(ctrl, _store())
    desired = live[POLICY_ID]["statement"]
    updates.clear()
    _run_step(ctrl, _store())
    assert updates == [] and live[POLICY_ID]["statement"] == desired


def test_created_policy_is_recorded_as_a_child_the_moment_it_exists():
    live: dict = {}
    ctrl, _updates = _engine_control(live)
    store = _store()
    _run_step(ctrl, store)
    children = [r for r in _rows(store) if r["type"] == "policy"]
    assert len(children) == 1 and children[0]["created_by_deployment"] is True
    assert children[0]["id"] in live and children[0]["engine_id"] == ENGINE_ID


def test_unreadable_live_definition_fails_closed():
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}}
    ctrl, _updates = _engine_control(live)
    ctrl.get_policy.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "GetPolicy"
    )
    with pytest.raises(PolicyStateUnreadable):
        _run_step(ctrl, _store())
    assert ctrl.update_policy.call_count == 0 and ctrl.update_gateway.call_count == 0


def test_non_terminal_live_policy_fails_closed():
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD, "status": "UPDATING"}}
    ctrl, _updates = _engine_control(live)
    with patch.object(policy_lifecycle, "read_policy_terminal", wraps=policy_lifecycle.read_policy_terminal):
        with pytest.raises(PolicyStateUnreadable):
            with patch("time.sleep"):
                policy_lifecycle.reconcile_policy_definition(
                    ctrl, ENGINE_ID, POLICY_ID, "permit(x);", "d", attempts=2, delay_seconds=0
                )
    assert ctrl.update_policy.call_count == 0


def test_update_that_does_not_land_the_desired_text_fails_closed():
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}}
    ctrl, _updates = _engine_control(live)
    ctrl.update_policy.side_effect = lambda **kw: {"policyId": kw["policyId"]}  # accepted, but nothing changes
    with pytest.raises(PolicyStateUnreadable), patch("time.sleep"):
        policy_lifecycle.reconcile_policy_definition(ctrl, ENGINE_ID, POLICY_ID, "permit(x);", "d", attempts=1)


# --------------------------------------------------------------------------- co-residency


def test_incompatible_coresident_desired_definition_is_refused_before_any_write():
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}}
    ctrl, updates = _engine_control(live)
    store = _store(
        other_rows=[{"type": "policy", "id": POLICY_ID, "desired_definition_sha256": "sha256:someone-elses"}]
    )
    with pytest.raises(PolicyCoResidencyConflict):
        _run_step(ctrl, store)
    assert updates == [] and ctrl.update_gateway.call_count == 0


def test_coresidency_is_rechecked_after_the_adopted_row_is_appended():
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}}
    ctrl, updates = _engine_control(live)
    store = _store()
    answers = iter([[], [{"type": "policy", "id": POLICY_ID, "desired_definition_sha256": "sha256:raced-in"}]])
    store.resource_rows.side_effect = lambda _dep, row, **kw: [] if kw.get("include_deleted") else next(answers, [])
    with pytest.raises(PolicyCoResidencyConflict):
        _run_step(ctrl, store)
    assert updates == [] and store.record_resource_strict.call_count >= 1


def test_coresident_with_the_same_desired_definition_is_allowed():
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": "placeholder"}}
    ctrl, _updates = _engine_control(live)
    _run_step(ctrl, _store())
    desired = live[POLICY_ID]["statement"]
    store = _store(other_rows=[{"type": "policy", "id": POLICY_ID, "desired_definition_sha256": cedar_digest(desired)}])
    result = _run_step(ctrl, store)
    assert result["policy_result"]["success"] is True


# --------------------------------------------------------------------------- managed SET reconciliation


def test_removed_managed_permit_does_not_survive_on_a_reused_engine():
    stale = f"{ENGINE_NAME}_old_extra_permit"
    live = {
        POLICY_ID: {"name": POLICY_NAME, "statement": OLD},
        "p-stale": {
            "name": stale,
            "statement": f'permit(principal, action == AgentCore::Action::"T___get_secret", resource == AgentCore::Gateway::"{GATEWAY_ARN}");',
        },
    }
    ctrl, _updates = _engine_control(live)
    proven = [
        {
            "type": "policy",
            "id": "p-stale",
            "_deployment_id": "deployment-old",
            "_delete_status": "deleted",
            "created_by_deployment": True,
        }
    ]
    result = _run_step(ctrl, _store(provenance=proven))
    assert result["policy_result"]["success"] is True
    assert "p-stale" not in live, "the stale managed permit stayed ACTIVE on the reused engine"
    assert result["policy_result"]["stale_managed_policies_removed"] == [stale]
    assert [c.kwargs["policyId"] for c in ctrl.delete_policy.call_args_list] == ["p-stale"]


def test_stale_managed_policy_recorded_by_another_live_deployment_fails_closed():
    stale = f"{ENGINE_NAME}_old_extra_permit"
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}, "p-stale": {"name": stale, "statement": "permit(x);"}}
    ctrl, _updates = _engine_control(live)
    store = _store(
        provenance=[{"type": "policy", "id": "p-stale", "_deployment_id": "deployment-b", "_delete_status": ""}]
    )
    with pytest.raises(PolicySetDrift):
        _run_step(ctrl, store)
    assert "p-stale" in live and ctrl.update_gateway.call_count == 0


def test_stale_managed_policy_without_any_recorded_provenance_fails_closed():
    """The name pattern alone is not ownership: an unrecorded extra is never deleted by guess."""
    stale = f"{ENGINE_NAME}_old_extra_permit"
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}, "p-stale": {"name": stale, "statement": "permit(x);"}}
    ctrl, _updates = _engine_control(live)
    with pytest.raises(PolicySetDrift):
        _run_step(ctrl, _store(provenance=[]))
    assert "p-stale" in live and ctrl.delete_policy.call_count == 0 and ctrl.update_gateway.call_count == 0


def test_stale_managed_policy_on_an_engine_another_live_deployment_references_fails_closed():
    """A legacy manifest names only the engine; its deployment may still be served by the extra."""
    stale = f"{ENGINE_NAME}_old_extra_permit"
    proven = [
        {
            "type": "policy",
            "id": "p-stale",
            "_deployment_id": "deployment-old",
            "_delete_status": "deleted",
            "created_by_deployment": True,
        }
    ]
    deleted: list = []
    with (
        patch.object(policy_lifecycle, "delete_policy_confirmed", side_effect=lambda _c, _e, pid: deleted.append(pid)),
        pytest.raises(PolicySetDrift),
    ):
        policy_lifecycle.reconcile_managed_policy_set(
            MagicMock(),
            _store(provenance=proven, engine_shared=True),
            "deployment-new",
            engine_id=ENGINE_ID,
            engine_name=ENGINE_NAME,
            desired_names=set(),
            region=REGION,
            list_policies=lambda: [{"name": stale, "policyId": "p-stale", "status": "ACTIVE"}],
        )
    assert deleted == []
    # and the step itself refuses every mutation on a shared engine before the sweep is even reached
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}, "p-stale": {"name": stale, "statement": "permit(x);"}}
    ctrl, _updates = _engine_control(live)
    with pytest.raises(PolicyCoResidencyConflict):
        _run_step(ctrl, _store(provenance=proven, engine_shared=True))
    assert "p-stale" in live and ctrl.delete_policy.call_count == 0 and ctrl.update_gateway.call_count == 0


def test_foreign_policies_outside_the_namespace_are_never_touched():
    live = {
        POLICY_ID: {"name": POLICY_NAME, "statement": OLD},
        "p-foreign": {"name": "customer_audit_permit", "statement": "permit(x);"},
    }
    ctrl, _updates = _engine_control(live)
    result = _run_step(ctrl, _store())
    assert "p-foreign" in live and ctrl.delete_policy.call_count == 0
    assert result["policy_result"]["foreign_policies_present"] == ["customer_audit_permit"]


def test_managed_namespace_is_any_engine_name_prefix():
    assert is_managed_policy_name("SharedEngine_old_extra_permit", "SharedEngine")
    assert is_managed_policy_name("Shared_allow_permitted_tools", "SharedEngine")
    assert not is_managed_policy_name("customer_audit_permit", "SharedEngine")
    assert not is_managed_policy_name("SharedEngineX", "SharedEngine")


# --------------------------------------------------------------------------- isolation (account + parent engine)


def test_adopted_only_provenance_never_authorizes_deleting_a_stale_extra():
    stale = f"{ENGINE_NAME}_old_extra_permit"
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}, "p-stale": {"name": stale, "statement": "permit(x);"}}
    ctrl, _updates = _engine_control(live)
    adopted_only = [
        {
            "type": "policy",
            "id": "p-stale",
            "_deployment_id": "deployment-old",
            "_delete_status": "deleted",
            "created_by_deployment": False,
        }
    ]
    with pytest.raises(PolicySetDrift):
        _run_step(ctrl, _store(provenance=adopted_only))
    assert "p-stale" in live and ctrl.delete_policy.call_count == 0


def test_set_sweep_binds_the_parent_engine_lookup_to_the_target_account():
    store = _store(
        provenance=[
            {
                "type": "policy",
                "id": "p-stale",
                "created_by_deployment": True,
                "_deployment_id": "x",
                "_delete_status": "deleted",
            }
        ]
    )
    seen: list = []
    store.has_other_live_resource_reference.side_effect = lambda _d, res, **kw: seen.append((res, kw)) or False
    deleted: list = []
    with patch.object(policy_lifecycle, "delete_policy_confirmed", side_effect=lambda _c, _e, pid: deleted.append(pid)):
        policy_lifecycle.reconcile_managed_policy_set(
            MagicMock(),
            store,
            "deployment-new",
            engine_id=ENGINE_ID,
            engine_name=ENGINE_NAME,
            desired_names=set(),
            region=REGION,
            account="123456789012",
            list_policies=lambda: [
                {"name": f"{ENGINE_NAME}_old_extra_permit", "policyId": "p-stale", "status": "ACTIVE"}
            ],
        )
    res, kw = seen[0]
    assert res["type"] == "policy_engine" and res["account"] == "123456789012" and res["region"] == REGION
    assert kw == {"target_account_id": "123456789012", "target_region": REGION}
    assert deleted == ["p-stale"], "with provenance and no sharer the stale extra is removed"


def test_step_refuses_to_update_an_adopted_policy_on_an_engine_another_live_deployment_uses():
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}}
    ctrl, updates = _engine_control(live)
    store = _store(engine_shared=True)
    with pytest.raises(PolicyCoResidencyConflict):
        _run_step(ctrl, store)
    assert updates == [] and ctrl.update_gateway.call_count == 0
    checked = [c.args[1] for c in store.has_other_live_resource_reference.call_args_list]
    assert any(r.get("type") == "policy_engine" and r.get("id") == ENGINE_ID for r in checked)


def test_promoter_refuses_a_legacy_parent_consumer_and_a_conflicting_cross_account_child():
    desired = "permit(x);"
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}}
    ctrl, updates = _engine_control(live)
    legacy = _store(engine_shared=True)
    state = {**_pending_state(desired), "target_account_id": "123456789012"}
    with patch("time.sleep"):
        result = policy_promoter.try_promote_to_enforce(state, REGION, control_client=ctrl, store=legacy)
    assert updates == [] and not result.get("promoted") and "co-residency" in result.get("reason", "")
    conflicting = _store()
    reads: list = []

    def rows(_d, row, **kw):
        reads.append(dict(row))
        return [] if kw.get("include_deleted") else [{"desired_definition_sha256": "sha256:theirs"}]

    conflicting.resource_rows.side_effect = rows
    with patch("time.sleep"):
        result = policy_promoter.try_promote_to_enforce(state, REGION, control_client=ctrl, store=conflicting)
    assert updates == [] and not result.get("promoted")
    assert any(r.get("type") == "policy" and r.get("account") == "123456789012" for r in reads), (
        "the child lookup must be account-bound"
    )


# --------------------------------------------------------------------------- promoter


def _pending_state(desired):
    return {
        "deployment_id": "deployment-new",
        "policy_result": {
            "mode": "ENFORCE",
            "engine_id": ENGINE_ID,
            "engine_arn": f"arn:aws:bedrock-agentcore:{REGION}:123456789012:policy-engine/{ENGINE_ID}",
            "gateway_id": GATEWAY_ID,
            "enforce_validation_pending": True,
            "enforce_pending": {
                "engine_id": ENGINE_ID,
                "engine_name": ENGINE_NAME,
                "gateway_id": GATEWAY_ID,
                "gateway_arn": GATEWAY_ARN,
                "policies": [{"name": POLICY_NAME, "statement": desired, "description": "d"}],
            },
        },
    }


def test_promoter_reconciles_a_stale_active_definition_before_reporting_promotion():
    desired = f'permit(principal is AgentCore::OAuthUser, action in [AgentCore::Action::"T___get_canary"], resource == AgentCore::Gateway::"{GATEWAY_ARN}");'
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}}
    ctrl, updates = _engine_control(live)
    with patch("time.sleep"):
        result = policy_promoter.try_promote_to_enforce(_pending_state(desired), REGION, control_client=ctrl)
    assert result and result["promoted"] is True
    assert len(updates) == 1 and live[POLICY_ID]["statement"] == desired


def test_promoter_records_the_adopted_row_strictly_before_mutating_and_refuses_without_it():
    desired = "permit(x);"
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}}
    ctrl, updates = _engine_control(live)
    good = MagicMock()
    with patch("time.sleep"):
        result = policy_promoter.try_promote_to_enforce(
            _pending_state(desired), REGION, control_client=ctrl, store=good
        )
    assert result["promoted"] is True and len(updates) == 1
    row = good.record_resource_strict.call_args.args[1]
    assert row["type"] == "policy" and row["created_by_deployment"] is False and row["id"] == POLICY_ID
    live[POLICY_ID]["statement"] = OLD
    updates.clear()
    bad = MagicMock()
    bad.record_resource_strict.side_effect = RuntimeError("dynamodb down")
    with patch("time.sleep"):
        result = policy_promoter.try_promote_to_enforce(_pending_state(desired), REGION, control_client=ctrl, store=bad)
    assert updates == [] and not (result or {}).get("promoted") and live[POLICY_ID]["statement"] == OLD


def test_promoter_does_not_rewrite_an_already_desired_definition():
    desired = "permit(x);"
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": desired}}
    ctrl, updates = _engine_control(live)
    with patch("time.sleep"):
        result = policy_promoter.try_promote_to_enforce(_pending_state(desired), REGION, control_client=ctrl)
    assert result["promoted"] is True and updates == []


def test_promoter_lazy_create_records_durably_or_compensates():
    live: dict = {}
    ctrl, _updates = _engine_control(live)
    good = MagicMock()
    with patch("time.sleep"):
        result = policy_promoter.try_promote_to_enforce(
            _pending_state("permit(x);"), REGION, control_client=ctrl, store=good
        )
    assert result["promoted"] is True and len(live) == 1
    row = good.record_resource_strict.call_args.args[1]
    assert row["type"] == "policy" and row["created_by_deployment"] is True and row["engine_id"] == ENGINE_ID
    # a manifest that cannot be written: the created policy is compensate-deleted and promotion is NOT reported
    live.clear()
    bad = MagicMock()
    bad.record_resource_strict.side_effect = RuntimeError("dynamodb down")
    with patch("time.sleep"):
        result = policy_promoter.try_promote_to_enforce(
            _pending_state("permit(x);"), REGION, control_client=ctrl, store=bad
        )
    assert not (result or {}).get("promoted") and live == {} and ctrl.delete_policy.call_count == 1


def test_promoter_without_a_store_never_creates_an_untracked_policy():
    live: dict = {}
    ctrl, _updates = _engine_control(live)
    with patch("time.sleep"):
        result = policy_promoter.try_promote_to_enforce(_pending_state("permit(x);"), REGION, control_client=ctrl)
    assert live == {} and not (result or {}).get("promoted")


# --------------------------------------------------------------------------- recovery from persisted intent


def test_success_persists_the_exact_managed_intent_and_recovery_uses_only_it():
    live: dict = {}
    ctrl, _updates = _engine_control(live)
    result = _run_step(ctrl, _store())
    specs = result["policy_result"]["desired_policies"]
    assert specs and all({"name", "statement", "description"} <= set(x) for x in specs)
    assert {x["name"] for x in specs} == {POLICY_NAME} and "T___get_secret" not in specs[0]["statement"]
    assert result["policy_result"]["enforce_pending"] is None
    # recovery: the live policy drifted and failed; the record's intent is what lands, not the drifted text
    pid = next(iter(live))
    live[pid]["statement"] = "permit(drifted);"
    live[pid]["status"] = "UPDATE_FAILED"
    state = {
        "deployment_id": "deployment-new",
        "target_account_id": "123456789012",
        "target_region": REGION,
        "policy_result": {**result["policy_result"], "enforce_pending": None, "enforce_validation_pending": False},
    }
    with patch("time.sleep"):
        out = policy_promoter.try_promote_to_enforce(state, REGION, control_client=ctrl, store=_store())
    assert out["promoted"] is True and live[pid]["statement"] == specs[0]["statement"]


def test_legacy_record_without_intent_is_never_revalidated_from_live_text():
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": "permit(drifted);", "status": "UPDATE_FAILED"}}
    ctrl, updates = _engine_control(live)
    state = {
        "deployment_id": "deployment-new",
        "policy_result": {
            "mode": "ENFORCE",
            "engine_id": ENGINE_ID,
            "engine_name": ENGINE_NAME,
            "gateway_id": GATEWAY_ID,
            "enforce_pending": None,
            "enforce_validation_pending": False,
        },
    }
    with patch("time.sleep"):
        out = policy_promoter.try_promote_to_enforce(state, REGION, control_client=ctrl, store=_store())
    assert updates == [] and not out.get("promoted") and "redeploy" in out.get("reason", "")
    assert live[POLICY_ID]["statement"] == "permit(drifted);"


def test_same_account_manifest_delete_never_resolves_a_target_through_the_settings_table():
    """A NotFound raised while resolving the deploy target must never read as 'the policy is gone'; a same-account
    row (no account) with an explicit region needs no target resolution at all."""
    import boto3 as real_boto3

    ctrl, state = _deleter_ctrl()
    with (
        patch.object(real_boto3, "client", return_value=ctrl) as default_client,
        patch(
            "app.services.step_clients.session_for_event", side_effect=AssertionError("target resolution must not run")
        ),
        patch.object(deployment_handler, "assert_agentcore_resource_owned"),
        patch("time.sleep"),
    ):
        msg = deployment_handler._delete_managed_resource(_owned_row(), REGION, deployment_id="deployment-new")
    assert default_client.call_count >= 1 and state["present"] is False and "deleted" in msg


def test_a_production_cross_account_row_always_uses_the_supplied_target_session():
    """The fallback (account, no role, no session) is unreachable from the production caller: with a target_session the
    session's client is used and neither the default client nor step_clients is consulted."""
    import boto3 as real_boto3

    ctrl, state = _deleter_ctrl()
    session = MagicMock()
    session.client.return_value = ctrl
    row = {**_owned_row(), "account": "999999999999"}
    with (
        patch.object(real_boto3, "client", side_effect=AssertionError("default client must not be used")),
        patch("app.services.step_clients.client", side_effect=AssertionError("step_clients must not be used")),
        patch.object(deployment_handler, "assert_agentcore_resource_owned"),
        patch("time.sleep"),
    ):
        msg = deployment_handler._delete_managed_resource(
            row, REGION, deployment_id="deployment-new", target_session=session
        )
    assert session.client.call_count >= 1 and state["present"] is False and "deleted" in msg
    # with a role but no session the row assumes the role through step_clients, never the default client
    ctrl2, state2 = _deleter_ctrl()
    with (
        patch.object(real_boto3, "client", side_effect=AssertionError("default client must not be used")),
        patch("app.services.step_clients.client", return_value=ctrl2) as factory,
        patch.object(deployment_handler, "assert_agentcore_resource_owned"),
        patch("time.sleep"),
    ):
        deployment_handler._delete_managed_resource(
            row, REGION, deployment_id="deployment-new", target_role_arn="arn:aws:iam::999999999999:role/x"
        )
    assert factory.call_args.args[0]["target_role_arn"].endswith("role/x") and state2["present"] is False


def test_a_settings_table_not_found_is_never_the_policy_being_gone():
    """The cross-account target lookup (account + role, no session) failing must surface, not read as absence."""
    lookup_failure = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "Requested resource not found"}}, "GetItem"
    )
    row = {**_owned_row(), "account": "999999999999"}
    with (
        patch("app.services.step_clients.client", side_effect=lookup_failure),
        patch.object(deployment_handler, "assert_agentcore_resource_owned"),
        patch("time.sleep"),
        pytest.raises(ClientError),
    ):
        deployment_handler._delete_managed_resource(
            row, REGION, deployment_id="deployment-new", target_role_arn="arn:aws:iam::999999999999:role/x"
        )


def test_policy_children_receipts_are_persisted_on_both_paths():
    live = {POLICY_ID: {"name": POLICY_NAME, "statement": OLD}}
    ctrl, _updates = _engine_control(live)
    result = _run_step(ctrl, _store())
    children = result["policy_result"]["policy_children"]
    assert children and all(
        {"engine_id", "id", "name", "region", "created_by_deployment", "desired_definition_sha256"} <= set(c)
        for c in children
    )
    assert children[0]["id"] == POLICY_ID and children[0]["created_by_deployment"] is False
    assert result["policy_result"]["desired_policies"], "intent stays a separate field"
    # lazy path: a created policy's receipt rides on the promoter outcome for the adapter to persist
    live.clear()
    store = MagicMock()
    with patch("time.sleep"):
        out = policy_promoter.try_promote_to_enforce(
            _pending_state("permit(x);"), REGION, control_client=ctrl, store=store
        )
    assert (
        out["promoted"] is True
        and out["policy_children"]
        and out["policy_children"][0]["created_by_deployment"] is True
    )


# --------------------------------------------------------------------------- adapter: receipts persisted before success


def _adapter_state(mode="LOG_ONLY"):
    return {
        "deployment_id": "deployment-new",
        "policy_result": {
            "mode": mode,
            "engine_id": ENGINE_ID,
            "enforce_pending": {"engine_id": ENGINE_ID, "gateway_id": GATEWAY_ID, "policies": []},
        },
    }


def _child(pid="p-lazy"):
    return policy_lifecycle.policy_child_row(
        policy_id=pid,
        engine_id=ENGINE_ID,
        name="n",
        region=REGION,
        created_by_deployment=True,
        statement="permit(x);",
        account="123456789012",
    )


def _run_adapter(outcome, store):
    from app.services import runtime_invocation

    state = _adapter_state()
    with (
        patch.object(policy_promoter, "try_promote_to_enforce", return_value=outcome),
        patch.object(runtime_invocation.step_clients, "client", return_value=MagicMock()),
    ):
        ok = runtime_invocation.promote_pending_policy(
            state, REGION, target_event={"target_region": REGION}, state_store=store
        )
    return ok, state


def test_adapter_promoted_with_receipt_persists_children_then_reports_success():
    store = MagicMock()
    ok, state = _run_adapter({"promoted": True, "mode": "ENFORCE", "reason": "r", "policy_children": [_child()]}, store)
    assert ok is True and store.update_status.call_count == 1
    persisted = store.update_status.call_args.kwargs["policy_result"]
    assert persisted["mode"] == "ENFORCE" and [c["policy_id"] for c in persisted["policy_children"]] == ["p-lazy"]
    assert state["policy_result"]["policy_children"][0]["id"] == "p-lazy"


def test_adapter_converging_with_receipt_persists_children_without_promotion():
    store = MagicMock()
    ok, state = _run_adapter(
        {"promoted": False, "mode": "LOG_ONLY", "reason": "converging", "policy_children": [_child(), _child()]}, store
    )
    assert ok is False and store.update_status.call_count == 1
    persisted = store.update_status.call_args.kwargs["policy_result"]
    assert persisted["mode"] == "LOG_ONLY" and len(persisted["policy_children"]) == 1, "deduped by exact identity"
    assert state["policy_result"]["mode"] == "LOG_ONLY"


def test_adapter_persist_failure_never_reports_success_or_claims_enforce():
    store = MagicMock()
    store.update_status.side_effect = RuntimeError("dynamodb down")
    ok, state = _run_adapter({"promoted": True, "mode": "ENFORCE", "reason": "r", "policy_children": [_child()]}, store)
    assert ok is False and store.update_status.call_count == 1
    assert state["policy_result"]["mode"] == "LOG_ONLY" and "policy_children" not in state["policy_result"]


def test_adapter_without_children_or_promotion_writes_nothing():
    store = MagicMock()
    ok, _state = _run_adapter(
        {"promoted": False, "mode": "LOG_ONLY", "reason": "converging", "policy_children": []}, store
    )
    assert ok is False and store.update_status.call_count == 0


# --------------------------------------------------------------------------- manifest identity + deletion authority


def test_policy_identity_is_engine_scoped():
    base = {"type": "policy", "id": POLICY_ID, "region": REGION}
    first = manifest_resource_key({**base, "engine_id": ENGINE_ID})
    assert first == manifest_resource_key({**base, "engine_id": ENGINE_ID, "name": "display-only"})
    assert first != manifest_resource_key({**base, "engine_id": "eng-2"})
    assert first != manifest_resource_key({**base, "engine_id": ENGINE_ID, "region": "eu-west-1"})


def test_policy_child_is_handed_off_while_another_live_deployment_references_the_parent_engine():
    store = MagicMock()
    seen: list[dict] = []

    def ref(_dep, resource, **_kw):
        seen.append(resource)
        return resource.get("type") == "policy_engine" and resource.get("id") == ENGINE_ID  # legacy row: engine only

    store.has_other_live_resource_reference.side_effect = ref
    row = {"type": "policy", "id": POLICY_ID, "engine_id": ENGINE_ID, "region": REGION, "created_by_deployment": True}
    assert manifest_delete_refusal(store, "deployment-b", row) == CO_RESIDENT_REFUSAL
    assert any(r.get("type") == "policy_engine" for r in seen), "the parent engine was never checked"
    store.has_other_live_resource_reference.side_effect = lambda *_a, **_k: False
    assert manifest_delete_refusal(store, "deployment-b", row) is None
    assert "engine" in (manifest_delete_refusal(store, "deployment-b", {"type": "policy", "id": POLICY_ID}) or "")


def _deleter_ctrl(present=True):
    ctrl = MagicMock()
    state = {"present": present}

    def delete_policy(**kw):
        assert kw == {"policyEngineId": ENGINE_ID, "policyId": POLICY_ID}
        state["present"] = False
        return {}

    def get_policy(**_kw):
        if not state["present"]:
            raise _not_found()
        return {"policyId": POLICY_ID, "status": "ACTIVE"}

    ctrl.delete_policy.side_effect = delete_policy
    ctrl.get_policy.side_effect = get_policy
    return ctrl, state


def _owned_row(created=True):
    return {
        "type": "policy",
        "id": POLICY_ID,
        "engine_id": ENGINE_ID,
        "policy_engine_id": ENGINE_ID,
        "region": REGION,
        "created_by_deployment": created,
    }


def test_deployment_handler_deletes_an_owned_policy_by_exact_ids_and_confirms_absence():
    ctrl, state = _deleter_ctrl()
    with (
        patch.object(step_clients, "client", return_value=ctrl),
        patch.object(deployment_handler, "assert_agentcore_resource_owned") as owned,
        patch("time.sleep"),
    ):
        msg = deployment_handler._delete_managed_resource(_owned_row(), REGION, deployment_id="deployment-new")
    assert ctrl.delete_policy.call_count == 1 and state["present"] is False
    assert "SKIPPED" not in msg and "deleted" in msg
    assert owned.call_args.args[1:] == ("policy_engine", ENGINE_ID, REGION), "the parent engine must be re-proven first"


def test_both_delete_arms_refuse_a_policy_whose_parent_engine_is_not_ours():
    ctrl, state = _deleter_ctrl()
    with (
        patch.object(step_clients, "client", return_value=ctrl),
        patch.object(
            deployment_handler, "assert_agentcore_resource_owned", side_effect=ResourceDeletionRefused("foreign")
        ),
        patch("time.sleep"),
        pytest.raises(ResourceDeletionRefused),
    ):
        deployment_handler._delete_managed_resource(_owned_row(), REGION, deployment_id="deployment-new")
    assert ctrl.delete_policy.call_count == 0 and state["present"]
    ctrl2, state2 = _deleter_ctrl()
    with (
        patch.object(step_clients, "client", return_value=ctrl2),
        patch.object(
            status_update_step, "assert_agentcore_resource_owned", side_effect=ResourceDeletionRefused("foreign")
        ),
        patch("time.sleep"),
        pytest.raises(status_update_step._ResourceRetained),
    ):
        status_update_step._cleanup_resource(_owned_row(), REGION, {})
    assert ctrl2.delete_policy.call_count == 0 and state2["present"]


def test_deployment_handler_leaves_an_adopted_policy_in_place():
    ctrl, state = _deleter_ctrl()
    with patch.object(step_clients, "client", return_value=ctrl), patch("time.sleep"):
        msg = deployment_handler._delete_managed_resource(
            _owned_row(created=False), REGION, deployment_id="deployment-new"
        )
    assert ctrl.delete_policy.call_count == 0 and state["present"] and "left in place" in msg


def test_deployment_handler_refuses_a_policy_row_without_its_engine():
    ctrl, _state = _deleter_ctrl()
    with patch.object(step_clients, "client", return_value=ctrl), pytest.raises(ResourceDeletionRefused):
        deployment_handler._delete_managed_resource(
            {"type": "policy", "id": POLICY_ID, "region": REGION, "created_by_deployment": True}, REGION
        )


def test_policy_delete_not_found_is_already_gone_and_a_lingering_policy_is_refused():
    ctrl = MagicMock()
    ctrl.delete_policy.side_effect = _not_found("DeletePolicy")
    policy_lifecycle.delete_policy_confirmed(ctrl, ENGINE_ID, POLICY_ID)  # no raise
    ctrl2 = MagicMock()
    ctrl2.get_policy.return_value = {"policyId": POLICY_ID, "status": "ACTIVE"}
    with pytest.raises(ResourceDeletionRefused), patch("time.sleep"):
        policy_lifecycle.delete_policy_confirmed(ctrl2, ENGINE_ID, POLICY_ID, confirmation_attempts=2, delay_seconds=0)


def test_status_update_step_has_the_same_policy_arm_and_both_order_it_before_the_engine():
    import inspect

    dh = inspect.getsource(deployment_handler)
    su = inspect.getsource(status_update_step)
    for src in (dh, su):
        assert '"policy": 1' in src and '"policy_engine": 2' in src, "a policy child must be deleted before its engine"
        assert 'rtype == "policy"' in src


def test_status_update_step_deletes_owned_and_keeps_adopted_policies():
    ctrl, state = _deleter_ctrl()
    with (
        patch.object(step_clients, "client", return_value=ctrl),
        patch.object(status_update_step, "assert_agentcore_resource_owned"),
        patch("time.sleep"),
    ):
        status_update_step._cleanup_resource(_owned_row(), REGION, {})
    assert state["present"] is False and ctrl.delete_policy.call_count == 1
    ctrl2, state2 = _deleter_ctrl()
    with patch.object(step_clients, "client", return_value=ctrl2), patch("time.sleep"):
        status_update_step._cleanup_resource(_owned_row(created=False), REGION, {})
    assert state2["present"] and ctrl2.delete_policy.call_count == 0


def test_child_row_shape_is_stable_json():
    row = policy_lifecycle.policy_child_row(
        policy_id="p",
        engine_id="e",
        name="n",
        region=REGION,
        created_by_deployment=True,
        statement="permit( x );",
        gateway_id="g",
        account="1",
    )
    assert json.loads(json.dumps(row)) == row
    assert row["desired_definition_sha256"] == cedar_digest("permit(x);".replace("(", "( ").replace(")", " )"))
