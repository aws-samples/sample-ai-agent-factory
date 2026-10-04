"""A gateway row whose append failed must still reach failure cleanup through the Catch.

The gateway step records its partial inventory and raises. The state machine's Catch then
keeps only the step's input and ``error_info``, so a row that DynamoDB refused is named
nowhere unless the exception itself carries it. These tests run the real step, turn its
exception into the payload Lambda and Step Functions actually produce, and hand that to the
real ``_auto_cleanup_on_failure``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from app.models.deployment_models import DeploymentState, DeploymentStatusEnum
from app.services import failure_inventory
from app.services.deployment_state_store import DeploymentStateStore

REGION = "us-east-1"
ACCOUNT = "111122223333"
GW_ID = "gw-lost-1"
POOL_ID = "us-east-1_LostPool"


class _Session:
    def client(self, service, **kw):
        if service == "secretsmanager":
            return object()
        assert service == "sts", service

        class _Sts:
            def get_caller_identity(self):
                return {"Account": ACCOUNT}

        return _Sts()


class _Store(DeploymentStateStore):
    """The real record_resource over an append that fails for chosen row types."""

    def __init__(self, fail_types: set[str]):
        self.fail_types = fail_types
        self.rows: list[dict] = []
        self.marked = 0

    def update_step(self, *a, **kw):
        pass

    def record_resource_strict(self, deployment_id, resource, *, finalizer_token=None):
        if resource.get("type") in self.fail_types:
            raise ConnectionError("dynamodb unreachable")
        self.rows.append(dict(resource))

    def mark_resource_manifest_error(self, deployment_id, *, finalizer_token=None):
        self.marked += 1


def _failed_result() -> dict:
    return {
        "success": False,
        "error": "CreateGatewayTarget failed",
        "gateway_id": GW_ID,
        "gateway_name": "lost-gw",
        "gateway_created_by_deployment": True,
        "client_info": {"user_pool_id": POOL_ID, "client_id": "client-lost", "client_secret": "fake-not-a-secret"},
    }


def _run_failed_step(monkeypatch, store: _Store) -> tuple[dict, Exception]:
    from app.step_handlers import gateway_step

    monkeypatch.setenv("APP_AWS_REGION", REGION)
    monkeypatch.setattr(gateway_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(gateway_step, "resolve_gateway_provider", lambda config: "agentcore")
    monkeypatch.setattr(gateway_step.step_clients, "session_for_event", lambda event: _Session())
    monkeypatch.setattr(gateway_step, "deploy_gateway", lambda **kw: _failed_result())
    event = {"deployment_id": "d-1", "owner_sub": "owner-a", "gateway_config": {"name": "lost-gw"}}
    with pytest.raises(RuntimeError, match="CreateGatewayTarget failed") as info:
        gateway_step.handler(event, None)
    return event, info.value


def _catch(event: dict, exc: Exception) -> dict:
    """What the next state receives: Lambda's error document as Cause, input kept."""
    cause = json.dumps(
        {
            "errorMessage": str(exc),
            "errorType": type(exc).__name__,
            "requestId": "00000000-0000-0000-0000-000000000000",
            "stackTrace": ['  File "/var/task/app/step_handlers/gateway_step.py", line 1, in handler\n'],
        }
    )
    return {**event, "error_info": {"Error": type(exc).__name__, "Cause": cause}}


class _Finalizer:
    """The store status_update reads: the manifest as the failed step left it."""

    def __init__(self, rows: list[dict]):
        self._state = DeploymentState(
            deployment_id="d-1",
            workflow_id="wf-1",
            user_id="owner-a",
            status=DeploymentStatusEnum.FAILED,
            started_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
            created_resources=rows,
            resource_manifest_version=1,
            resource_manifest_complete=False,
            resource_manifest_error=True,
        )
        self.recorded: list[dict] = []
        self.delete_status: list[tuple] = []

    def get(self, deployment_id):
        return self._state

    def update_delete_status(self, deployment_id, status, message=None, *, finalizer_token=None):
        self.delete_status.append((status, message))

    def reset_manifest_reference_cache(self, deployment_id):
        pass

    def has_other_live_resource_reference(self, *a, **kw):
        return False

    def record_resource(self, deployment_id, resource):
        self.recorded.append(dict(resource))

    def record_resource_strict(self, deployment_id, resource, *, finalizer_token=None):
        # The write-back must know which rows landed, so it is the strict append.
        self.recorded.append(dict(resource))

    def mark_resource_manifest_error(self, deployment_id, *, finalizer_token=None):
        pass


def _cleanup(monkeypatch, finalizer: _Finalizer, catch_event: dict) -> list[dict]:
    from app.services import gateway_deployer
    from app.step_handlers import status_update_step

    attempted: list[dict] = []
    monkeypatch.setattr(status_update_step, "_cleanup_resource", lambda res, region, event: attempted.append(res))
    monkeypatch.setattr(gateway_deployer, "unrecorded_deployment_secret_rows", lambda **kw: ([], []))
    # The teardown name hold proves each gateway ours and live under its name (F-66f).
    sts = SimpleNamespace(get_caller_identity=lambda: {"Account": "111122223333"})
    monkeypatch.setattr(status_update_step.step_clients, "client", lambda *a, **kw: sts)
    monkeypatch.setattr(
        status_update_step,
        "assert_agentcore_resource_owned",
        lambda _c, _t, gateway_id, _r: {"gatewayId": gateway_id, "name": "lost-gw"},
    )
    status_update_step._auto_cleanup_on_failure(finalizer, "d-1", catch_event)
    return attempted


def test_a_gateway_row_that_failed_to_append_is_still_cleaned_up(monkeypatch):
    store = _Store(fail_types={"gateway", "cognito_user_pool"})
    event, exc = _run_failed_step(monkeypatch, store)
    assert store.marked == 2, "the failed appends were not marked on the manifest"
    assert {r["type"] for r in store.rows}.isdisjoint({"gateway", "cognito_user_pool"})

    attempted = _cleanup(monkeypatch, _Finalizer(store.rows), _catch(event, exc))
    by_type = {r["type"]: r for r in attempted}
    assert by_type["gateway"]["id"] == GW_ID
    assert by_type["gateway"]["name"] == "lost-gw", "the claim erase needs the name"
    assert by_type["gateway"]["created_by_deployment"] is True
    assert by_type["cognito_user_pool"]["id"] == POOL_ID


def test_recovered_rows_are_written_back_for_a_later_delete(monkeypatch):
    """Auto-cleanup is best-effort; if it leaves the gateway, the user's DELETE reads
    only created_resources, so the recovered rows must land there too."""
    store = _Store(fail_types={"gateway"})
    event, exc = _run_failed_step(monkeypatch, store)
    finalizer = _Finalizer(store.rows)
    _cleanup(monkeypatch, finalizer, _catch(event, exc))
    assert [r["id"] for r in finalizer.recorded if r["type"] == "gateway"] == [GW_ID]
    assert {r["type"] for r in finalizer.recorded} == {"gateway"}


def test_a_retried_status_step_does_not_write_the_rows_back_twice(monkeypatch):
    """The status Lambda can run again for one failure; by then the first pass's
    write-back is in the manifest, and appending it again only grows the list."""
    store = _Store(fail_types={"gateway"})
    event, exc = _run_failed_step(monkeypatch, store)
    catch_event = _catch(event, exc)
    first = _Finalizer(store.rows)
    _cleanup(monkeypatch, first, catch_event)
    second = _Finalizer(store.rows + first.recorded)
    _cleanup(monkeypatch, second, catch_event)
    assert second.recorded == []


def test_the_carried_rows_hold_no_secret_and_are_stripped_from_the_text(monkeypatch):
    store = _Store(fail_types={"gateway", "cognito_user_pool", "cognito_app_client"})
    event, exc = _run_failed_step(monkeypatch, store)
    cause = _catch(event, exc)["error_info"]["Cause"]
    carried = failure_inventory.rows_from_error_info({"Cause": cause})
    # An owned pool's client is not a row of its own; it goes with the pool.
    assert {r["type"] for r in carried} == {"gateway", "cognito_user_pool"}
    decoded = json.dumps(carried)
    assert "fake-not-a-secret" not in decoded and "fake-not-a-secret" not in cause
    stripped = failure_inventory.strip(cause)
    assert failure_inventory.MARKER not in stripped
    assert "CreateGatewayTarget failed" in stripped
    # Stripping leaves a Cause that is still the JSON document it was.
    assert json.loads(stripped)["errorMessage"] == "Gateway deployment failed: CreateGatewayTarget failed"


def test_a_step_whose_appends_all_succeed_carries_nothing(monkeypatch):
    """The baseline: the marker only appears when a row was actually lost."""
    store = _Store(fail_types=set())
    _event, exc = _run_failed_step(monkeypatch, store)
    assert failure_inventory.MARKER not in str(exc)
    assert store.marked == 0


def test_the_status_step_logs_and_stores_the_failure_without_the_rows(monkeypatch, caplog):
    from app.step_handlers import status_update_step

    store = _Store(fail_types={"gateway"})
    event, exc = _run_failed_step(monkeypatch, store)
    catch_event = _catch(event, exc)

    class _StatusStore(_Finalizer):
        def acquire_finalizer_lease(self, deployment_id, *, seconds=None):
            return "test-finalizer-token"

        def release_finalizer_lease(self, deployment_id, token):
            pass

        def update_step(self, *a, **kw):
            pass

        def update_status(self, deployment_id, status, **kw):
            self.status_kw = kw

    status_store = _StatusStore(store.rows)
    monkeypatch.setattr(status_update_step, "_get_deployment_store", lambda: status_store)
    cleaned: list = []
    monkeypatch.setattr(status_update_step, "_auto_cleanup_on_failure", lambda s, d, e, **kw: cleaned.append(e))
    caplog.clear()  # the gateway step's own traceback, logged above, carries them
    with caplog.at_level(logging.ERROR, logger=status_update_step.logger.name):
        out = status_update_step.handler(catch_event, None)
    assert failure_inventory.MARKER not in status_store.status_kw["error_details"]
    assert failure_inventory.MARKER not in out["error_details"]
    assert failure_inventory.MARKER not in caplog.text
    # The cleanup still receives the event that carries them.
    assert failure_inventory.rows_from_error_info(cleaned[0]["error_info"])


def test_an_unreadable_carriage_is_reported_not_trusted(caplog):
    with caplog.at_level(logging.WARNING, logger=failure_inventory.logger.name):
        rows = failure_inventory.rows_from_error_info({"Cause": f"x\n{failure_inventory.MARKER}eyJ0eXBl"})
    assert rows == []
    assert "could not be read back" in caplog.text


def test_rows_without_provenance_are_dropped():
    exc = failure_inventory.StepFailedWithUnrecordedRows(
        "boom",
        [
            {"type": "gateway", "id": "gw-1"},
            {"type": "gateway", "id": "gw-2", "created_by_deployment": "yes"},
            {"type": "gateway", "id": "gw-3", "created_by_deployment": False},
        ],
    )
    assert [r["id"] for r in failure_inventory.rows_from_error_info({"Cause": str(exc)})] == ["gw-3"]
