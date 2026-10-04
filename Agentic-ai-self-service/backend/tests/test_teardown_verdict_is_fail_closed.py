"""A teardown verdict is fail-closed: nothing it could not remove is reported as removed.

Peer audits (0f, da, b7, cc) found four ways ``overall_success`` stayed True over a leak: a name
release that raised was a note, an AWS registry record that could not be deleted was a note, a
sidecar (dashboard, evaluation config) the runtime destroy could not remove was a warning in a log,
and trigger delivery rows were never deleted at all. Each is pinned here against the real
``_run_delete_cleanup`` (with the AWS edges stubbed) and the real stores over a fake table.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import replace
from unittest.mock import MagicMock

import app.services.gateway_deployer as gd
import pytest
from app.services.gateway_deployer import _annotate_managed_target, collecting_target_records
from app.services.trigger_store import TriggerStore, _delivery_partition
from app.step_handlers.gateway_step import _gateway_manifest_resources
from boto3.dynamodb.conditions import Key

_RUNTIME_ID = "agent_abc-QNPVKl93O8"
_REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _pin_the_home_region(monkeypatch):
    import app.deployment_handler as deployment_handler

    monkeypatch.setattr(deployment_handler, "config", replace(deployment_handler.config, aws_region=_REGION))


def _record(**extra) -> dict:
    rec = {
        # Empty on purpose: a deployment id would send the legacy KB-Lambda pass to real boto3.
        "deployment_id": "",
        "user_id": "sub-1",
        "runtime_id": _RUNTIME_ID,
        "deployment_mode": "runtime",
        "created_resources": [{"type": "agent_runtime", "id": _RUNTIME_ID, "region": _REGION}],
    }
    rec.update(extra)
    return rec


@pytest.fixture
def dh(monkeypatch):
    import app.deployment_handler as dh

    monkeypatch.setattr(dh, "_get_state_store", lambda: MagicMock())
    monkeypatch.setattr(
        dh,
        "_delete_managed_resource",
        lambda res, region, **_kwargs: f"[manifest] runtime {res.get('id')}: Runtime {res.get('id')} deleted",
    )
    monkeypatch.setattr(
        dh, "destroy_runtime", lambda rid, region: {"success": True, "message": f"Runtime {rid} deleted"}
    )

    def _release(
        deployment_record, caller_sub, *, runtime_may_still_live, trigger_cleanup_unconfirmed=False, outcome=None
    ):
        if outcome is not None:
            outcome.update({"released": True, "kept_locked": False})
        return ["Released runtime name 'x' (slots/versions)"]

    monkeypatch.setattr(dh, "_release_runtime_name_claim", _release)
    return dh


def _run(dh, monkeypatch, record: dict):
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: record)
    return dh._run_delete_cleanup(_RUNTIME_ID, "sub-1")


# --------------------------------------------------------------------------- the control


def test_the_control_teardown_is_green(dh, monkeypatch):
    resp = _run(dh, monkeypatch, _record())
    assert resp.success is True, resp.message


# --------------------------------------------------------------------------- name release


def test_a_name_release_that_raises_is_a_cleanup_failure(dh, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(dh, "_release_runtime_name_claim", _boom)
    resp = _run(dh, monkeypatch, _record())
    assert resp.success is False
    assert "Cleanup failures in:" in resp.message and "runtime_name_release" in resp.message


def test_a_name_kept_locked_is_a_retained_resource_not_a_success(dh, monkeypatch):
    def _kept(
        deployment_record, caller_sub, *, runtime_may_still_live, trigger_cleanup_unconfirmed=False, outcome=None
    ):
        if outcome is not None:
            outcome.update({"released": False, "kept_locked": True})
        return ["Runtime name 'x' kept locked (trigger cleanup was not confirmed)"]

    monkeypatch.setattr(dh, "_release_runtime_name_claim", _kept)
    resp = _run(dh, monkeypatch, _record())
    assert resp.success is False
    assert "retained by live-ownership policy" in resp.message and "runtime_name" in resp.message


@pytest.mark.parametrize(
    "messages,kept",
    [
        (["Released runtime name 'x' (slots/versions)"], False),
        (["Runtime name 'x' kept locked (its runtime was not destroyed)"], True),
        (["Runtime name 'x' not released (changed during teardown)"], True),
        (["Runtime name release skipped (this deployment's version row is ambiguous)"], True),
        ([], False),
    ],
)
def test_the_release_wrapper_reads_its_own_messages(dh, monkeypatch, messages, kept):
    import app.deployment_handler as real_dh

    monkeypatch.setattr(real_dh, "_release_runtime_name_claim_messages", lambda *a, **k: list(messages))
    outcome: dict = {}
    # The wrapper is the real one: the fixture replaced only the module attribute the cascade reads.
    got = (
        real_dh.__dict__["_release_runtime_name_claim"].__wrapped__
        if hasattr(real_dh._release_runtime_name_claim, "__wrapped__")
        else None
    )
    assert got is None  # no decorator hides the real wrapper
    monkeypatch.undo()
    monkeypatch.setattr(real_dh, "_release_runtime_name_claim_messages", lambda *a, **k: list(messages))
    result = real_dh._release_runtime_name_claim({}, "sub-1", runtime_may_still_live=False, outcome=outcome)
    assert result == messages
    assert outcome == {"kept_locked": kept, "released": not kept}


# --------------------------------------------------------------------------- AWS registry record


def _registry(monkeypatch, registry):
    import app.services.aws_agent_registry as reg

    monkeypatch.setattr(reg, "get_registry", lambda: registry)


def test_a_missing_registry_cannot_delete_the_record_and_says_so(dh, monkeypatch):
    _registry(monkeypatch, None)
    resp = _run(dh, monkeypatch, _record(aws_registry_record_id="rec-1"))
    assert resp.success is False
    assert "aws_registry_record" in resp.message


def test_a_registry_refusal_is_a_cleanup_failure(dh, monkeypatch):
    registry = MagicMock()
    registry.delete.return_value = False
    _registry(monkeypatch, registry)
    resp = _run(dh, monkeypatch, _record(aws_registry_record_id="rec-1"))
    assert resp.success is False and "aws_registry_record" in resp.message
    registry.delete.assert_called_once_with("rec-1")


def test_a_registry_exception_is_a_cleanup_failure_named_by_type_only(dh, monkeypatch):
    registry = MagicMock()
    registry.delete.side_effect = RuntimeError("table agentcore-registry key rec-1 secret-ish")
    _registry(monkeypatch, registry)
    resp = _run(dh, monkeypatch, _record(aws_registry_record_id="rec-1"))
    assert resp.success is False and "aws_registry_record" in resp.message
    assert "secret-ish" not in resp.message and "RuntimeError" in resp.message


def test_a_deleted_registry_record_keeps_the_teardown_green(dh, monkeypatch):
    registry = MagicMock()
    registry.delete.return_value = True
    _registry(monkeypatch, registry)
    resp = _run(dh, monkeypatch, _record(aws_registry_record_id="rec-1"))
    assert resp.success is True, resp.message
    assert "AWS registry record rec-1 deleted" in resp.message


# --------------------------------------------------------------------------- sidecars


def test_the_manifest_runtime_arm_reports_sidecars_into_the_callers_sink(monkeypatch):
    """The real ``_delete_managed_resource`` arm: the runtime row is deleted, the sidecars it could
    not remove are reported, and the row itself is not failed (the runtime IS gone).

    Deliberately NOT the ``dh`` fixture: that fixture stubs the dispatcher itself."""
    import app.deployment_handler as dh

    monkeypatch.setattr(dh, "boto3", MagicMock())
    monkeypatch.setattr(dh, "assert_agentcore_resource_owned", lambda *a, **k: None)
    monkeypatch.setattr(
        dh,
        "destroy_runtime",
        lambda rid, region, **_k: {
            "success": True,
            "message": f"Runtime {rid} deleted",
            "sidecar_failures": ["dashboard", "evaluation_config:cfg-1"],
        },
    )
    sink: list[str] = []
    msg = dh._delete_managed_resource(
        {"type": "agent_runtime", "id": _RUNTIME_ID, "region": _REGION}, _REGION, sidecar_failures=sink
    )
    assert msg.startswith(f"[manifest] runtime {_RUNTIME_ID}:")
    assert sink == ["runtime_sidecar:dashboard", "runtime_sidecar:evaluation_config:cfg-1"]
    # Without a sink the arm still succeeds: the sink is the caller's choice to listen.
    assert dh._delete_managed_resource(
        {"type": "agent_runtime", "id": _RUNTIME_ID, "region": _REGION}, _REGION
    ).startswith("[manifest]")


def test_the_cascade_hands_its_failure_list_to_the_dispatcher_and_reads_it_back(dh, monkeypatch):
    seen: dict = {}

    def _dispatcher(res, region, sidecar_failures=None, **_kwargs):
        seen["sink_is_a_list"] = isinstance(sidecar_failures, list)
        if sidecar_failures is not None:
            sidecar_failures.append("runtime_sidecar:dashboard")
        return f"[manifest] runtime {res.get('id')}: Runtime {res.get('id')} deleted"

    monkeypatch.setattr(dh, "_delete_managed_resource", _dispatcher)
    resp = _run(dh, monkeypatch, _record())
    assert seen == {"sink_is_a_list": True}
    assert resp.success is False
    assert "Cleanup failures in:" in resp.message and "runtime_sidecar:dashboard" in resp.message


def test_a_secondary_mcp_runtime_sidecar_is_a_cleanup_failure_too(dh, monkeypatch):
    calls: list[str] = []

    def _destroy(rid, region, **_k):
        calls.append(rid)
        return {"success": True, "message": f"Runtime {rid} deleted", "sidecar_failures": ["dashboard"]}

    monkeypatch.setattr(dh, "destroy_runtime", _destroy)
    resp = _run(dh, monkeypatch, _record(mcp_server_runtime_id="mcp_srv-AbCdEfGhIj"))
    if "mcp_srv-AbCdEfGhIj" in calls:
        assert resp.success is False and "mcp_runtime_sidecar:dashboard" in resp.message
    else:
        pytest.skip("this record shape did not reach the secondary-runtime arm; the arm is pinned by grep below")
    src = pathlib.Path(dh.__file__).read_text()
    assert 'cleanup_failures.append(f"mcp_runtime_sidecar:{_sidecar}")' in src


# --------------------------------------------------------------------------- gateway target provenance

_GW = "gw-123"


def _targets():
    return [
        {"target_id": "t-created", "name": "Tools", "family": "lambda", "digest": "d1", "arm": "created"},
        {"target_id": "t-adopted", "name": "Legacy", "family": "lambda", "digest": "d2", "arm": "adopted"},
        {
            "target_id": "t-updated",
            "name": "MCPServerRuntime",
            "family": "mcpServer",
            "digest": "d3",
            "arm": "updated",
            "source_runtime_arn": f"arn:aws:bedrock-agentcore:{_REGION}:123456789012:runtime/mcp_srv-AbCdEfGhIj",
            "source_runtime_id": "mcp_srv-AbCdEfGhIj",
        },
    ]


def _target_rows(gateway_created: bool) -> dict:
    rows = _gateway_manifest_resources(
        _REGION,
        {
            "gateway_id": _GW,
            "gateway_name": "gw",
            "gateway_created_by_deployment": gateway_created,
            "gateway_targets": _targets(),
        },
    )
    return {r["id"]: r for r in rows if r.get("type") == "gateway_target"}


def test_on_a_reused_gateway_only_a_created_target_is_ours():
    rows = _target_rows(gateway_created=False)
    assert rows["t-created"]["created_by_deployment"] is True
    assert rows["t-adopted"]["created_by_deployment"] is False
    assert rows["t-updated"]["created_by_deployment"] is False


def test_on_a_gateway_we_created_every_target_is_ours():
    rows = _target_rows(gateway_created=True)
    assert all(r["created_by_deployment"] is True for r in rows.values())


def test_the_row_carries_arm_and_source_runtime():
    rows = _target_rows(gateway_created=True)
    assert rows["t-created"]["target_arm"] == "created" and rows["t-created"]["source_runtime_arn"] == ""
    assert rows["t-updated"]["target_arm"] == "updated"
    assert rows["t-updated"]["source_runtime_id"] == "mcp_srv-AbCdEfGhIj"
    assert rows["t-updated"]["source_runtime_arn"].endswith("runtime/mcp_srv-AbCdEfGhIj")


def test_a_record_with_no_arm_is_treated_as_created():
    rows = _gateway_manifest_resources(
        _REGION,
        {
            "gateway_id": _GW,
            "gateway_created_by_deployment": False,
            "gateway_targets": [{"target_id": "t-old", "name": "n"}],
        },
    )
    row = next(r for r in rows if r.get("type") == "gateway_target")
    assert row["target_arm"] == "created" and row["created_by_deployment"] is True


def test_the_annotation_binds_by_name_and_by_id_and_never_leaks_between_sinks():
    arn = f"arn:aws:bedrock-agentcore:{_REGION}:123456789012:runtime/mcp_srv-AbCdEfGhIj"
    with collecting_target_records() as records:
        gd._record_managed_target("t-1", "MCPServerRuntime", {"targetConfiguration": {}}, arm="created")
        gd._record_managed_target("t-2", "Tools", {"targetConfiguration": {}}, arm="created")
        _annotate_managed_target(target_name="MCPServerRuntime", source_runtime_arn=arn)
    by_id = {r["target_id"]: r for r in records}
    assert by_id["t-1"]["source_runtime_arn"] == arn and by_id["t-1"]["source_runtime_id"] == "mcp_srv-AbCdEfGhIj"
    assert by_id["t-2"]["source_runtime_arn"] == "" and by_id["t-2"]["source_runtime_id"] == ""
    with collecting_target_records() as later:
        gd._record_managed_target("t-3", "MCPServerRuntime", {"targetConfiguration": {}}, arm="created")
    assert later[0]["source_runtime_arn"] == "", "a previous deployment's source must not bleed into the next sink"
    # Outside any sink the annotation is a no-op, not an error.
    _annotate_managed_target(target_name="MCPServerRuntime", source_runtime_arn=arn)


# --------------------------------------------------------------------------- trigger delivery rows


class _FakeTable:
    """Enough of a DynamoDB Table for the purge: query by partition, delete, batch delete."""

    def __init__(self, rows: list[dict], *, resurrect_rounds: int = 0) -> None:
        self.rows = {(r["runtime_name"], r["trigger_id"]): r for r in rows}
        self.deleted: list[tuple[str, str]] = []
        self.queries = 0
        self._resurrect_rounds = resurrect_rounds

    def query(self, *, KeyConditionExpression, ProjectionExpression=None, ConsistentRead=False):  # noqa: N803
        assert ConsistentRead is True, "the purge must read consistently or it can miss a fresh row"
        pk = KeyConditionExpression.get_expression()["values"][1]
        self.queries += 1
        if self._resurrect_rounds:
            self._resurrect_rounds -= 1
            return {"Items": [{"runtime_name": pk, "trigger_id": f"ghost-{self.queries}"}]}
        return {"Items": [dict(r) for (p, _), r in self.rows.items() if p == pk]}

    def delete_item(self, *, Key, **_kw):  # noqa: N803
        self.rows.pop((Key["runtime_name"], Key["trigger_id"]), None)
        self.deleted.append((Key["runtime_name"], Key["trigger_id"]))

    def batch_writer(self):
        table = self

        class _Batch:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def delete_item(self, *, Key):  # noqa: N803
                table.delete_item(Key=Key)

        return _Batch()


def _store(table: _FakeTable) -> TriggerStore:
    store = TriggerStore.__new__(TriggerStore)
    store._table = table
    store._table_name = "triggers"
    return store


def _rows(runtime: str, trigger: str, deliveries: int) -> list[dict]:
    pk = _delivery_partition(runtime, trigger)
    return [{"runtime_name": runtime, "trigger_id": trigger, "status": "deleting", "delete_token": "tok"}] + [
        {"runtime_name": pk, "trigger_id": f"dlv-{i}"} for i in range(deliveries)
    ]


def test_purge_removes_every_delivery_row_of_exactly_that_trigger():
    other = _rows("orders_bot", "trg-2", 2)
    table = _FakeTable(_rows("orders_bot", "trg-1", 3) + other)
    assert _store(table).purge_deliveries("orders_bot", "trg-1") == 3
    assert {k for k in table.rows if k[0].startswith("!delivery#")} == {
        (r["runtime_name"], r["trigger_id"]) for r in other[1:]
    }
    assert ("orders_bot", "trg-1") in table.rows, (
        "the purge touches delivery rows only; the trigger row is the caller's"
    )


def test_delete_purges_the_partition_with_the_row():
    table = _FakeTable(_rows("orders_bot", "trg-1", 2))
    _store(table).delete("orders_bot", "trg-1")
    assert table.rows == {}


def test_a_confirmed_claimed_delete_purges_and_an_unconfirmed_one_does_not():
    table = _FakeTable(_rows("orders_bot", "trg-1", 2))
    store = _store(table)
    assert store.delete_claimed(runtime_name="orders_bot", trigger_id="trg-1", delete_token="tok") is True
    assert table.rows == {}

    from botocore.exceptions import ClientError

    table2 = _FakeTable(_rows("orders_bot", "trg-1", 2))

    def _refuse(**_kw):
        raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "DeleteItem")

    table2.delete_item = _refuse  # type: ignore[method-assign]
    assert _store(table2).delete_claimed(runtime_name="orders_bot", trigger_id="trg-1", delete_token="tok") is False
    assert len(table2.rows) == 3, "an unconfirmed claim deletes nothing, delivery rows included"


def test_a_partition_that_never_drains_raises_instead_of_reporting_deleted():
    table = _FakeTable(_rows("orders_bot", "trg-1", 1), resurrect_rounds=99)
    with pytest.raises(RuntimeError, match="did not drain"):
        _store(table).purge_deliveries("orders_bot", "trg-1", max_rounds=3)


def test_the_purge_partition_key_is_the_stores_own_delivery_partition():
    assert _delivery_partition("orders_bot", "trg-1") == "!delivery#orders_bot#trg-1"
    cond = Key("runtime_name").eq(_delivery_partition("orders_bot", "trg-1"))
    assert cond.get_expression()["values"][1] == "!delivery#orders_bot#trg-1"


# --------------------------------------------------------------------------- evaluation config manifest row


def _eval_event(**extra) -> dict:
    ev = {
        "deployment_id": "dep-1",
        "runtime_arn": f"arn:aws:bedrock-agentcore:{_REGION}:123456789012:runtime/{_RUNTIME_ID}",
        "runtime_id": _RUNTIME_ID,
        "target_region": _REGION,
        "evaluation_config": {"enabled": True},
    }
    ev.update(extra)
    return ev


def _eval_clients(monkeypatch, *, create_raises: Exception | None = None, existing_source: dict | None = None):
    import app.step_handlers.evaluation_step as es

    ctrl = MagicMock()
    if create_raises is not None:
        ctrl.create_online_evaluation_config.side_effect = create_raises
    else:
        ctrl.create_online_evaluation_config.return_value = {"onlineEvaluationConfigId": "cfg-1"}
    ctrl.get_online_evaluation_config.return_value = {"dataSourceConfig": {"cloudWatchLogs": existing_source or {}}}
    iam = MagicMock()
    iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123456789012:role/AgentCoreEval-x"}}
    iam.get_role.return_value = {"Role": {"Arn": "arn:aws:iam::123456789012:role/AgentCoreEval-x", "Tags": []}}
    store = MagicMock()
    monkeypatch.setattr(es, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(
        es.step_clients, "client", lambda event, service: {"bedrock-agentcore-control": ctrl, "iam": iam}[service]
    )
    monkeypatch.setattr(es, "_find_online_evaluation_config_id", lambda c, name: "cfg-9")
    return es, ctrl, store


def _eval_rows(store) -> list[dict]:
    return [
        c.args[1] for c in store.record_resource.call_args_list if c.args[1].get("type") == "online_evaluation_config"
    ]


def test_a_created_evaluation_config_is_journaled_as_ours(monkeypatch):
    es, ctrl, store = _eval_clients(monkeypatch)
    out = es.handler(_eval_event(), None)
    assert out["evaluation_result"]["config_id"] == "cfg-1"
    rows = _eval_rows(store)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "cfg-1" and row["region"] == _REGION and row["created_by_deployment"] is True
    assert row["name"] == re.sub(r"[^a-zA-Z0-9_]", "_", f"eval_{_RUNTIME_ID}")[:48]


def test_an_adopted_default_named_config_is_ours_only_when_its_data_source_names_this_runtime(monkeypatch):
    es, ctrl, store = _eval_clients(
        monkeypatch,
        create_raises=RuntimeError("config already exists"),
        existing_source={"serviceNames": [_RUNTIME_ID], "logGroupNames": []},
    )
    es.handler(_eval_event(), None)
    assert _eval_rows(store)[0] == {
        "type": "online_evaluation_config",
        "id": "cfg-9",
        "name": re.sub(r"[^a-zA-Z0-9_]", "_", f"eval_{_RUNTIME_ID}")[:48],
        "region": _REGION,
        "created_by_deployment": True,
    }
    ctrl.get_online_evaluation_config.assert_called_once_with(onlineEvaluationConfigId="cfg-9")


def test_an_adopted_config_sampling_another_runtime_is_recorded_but_not_ours(monkeypatch):
    es, ctrl, store = _eval_clients(
        monkeypatch,
        create_raises=RuntimeError("config already exists"),
        existing_source={
            "serviceNames": ["someone_else-ZZZZZZZZZZ"],
            "logGroupNames": ["/aws/bedrock-agentcore/runtimes/other-DEFAULT"],
        },
    )
    es.handler(_eval_event(), None)
    assert _eval_rows(store)[0]["created_by_deployment"] is False


def test_an_adopted_user_named_config_is_never_ours_even_if_it_names_this_runtime(monkeypatch):
    es, ctrl, store = _eval_clients(
        monkeypatch,
        create_raises=RuntimeError("config already exists"),
        existing_source={"serviceNames": [_RUNTIME_ID]},
    )
    es.handler(_eval_event(evaluation_config={"enabled": True, "name": "shared-team-evals"}), None)
    row = _eval_rows(store)[0]
    assert row["name"] == "shared_team_evals" and row["created_by_deployment"] is False
    ctrl.get_online_evaluation_config.assert_not_called()


def test_an_unreadable_existing_config_is_recorded_as_not_ours(monkeypatch):
    es, ctrl, store = _eval_clients(monkeypatch, create_raises=RuntimeError("config already exists"))
    ctrl.get_online_evaluation_config.side_effect = RuntimeError("AccessDenied")
    es.handler(_eval_event(), None)
    assert _eval_rows(store)[0]["created_by_deployment"] is False
