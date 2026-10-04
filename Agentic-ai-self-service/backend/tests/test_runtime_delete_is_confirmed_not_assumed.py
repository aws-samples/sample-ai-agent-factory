"""F-08: ``DeleteAgentRuntime`` is an acceptance, not a deletion.

``destroy_runtime`` treated a 200 from ``delete_agent_runtime`` as "gone": it then deleted the
execution role while the runtime was still ``DELETING`` and returned ``success: True``, which the
teardown turned into a terminal ``deleted`` tombstone that nothing ever retries. AgentCore parks a
delete in ``DELETE_FAILED`` the same way it does for gateways (measured for DeleteGateway at
``status_update_step._confirm_gateway_deleted``), so the runtime survived with no role, a
``deleted`` row and no retry handle. A permanent orphan.

The fix is the convention every other asynchronous delete in this codebase already follows
(``destroy_harness``, ``delete_memory_confirmed``, ``_confirm_gateway_deleted``): poll the read API
until it answers not-found, treat a terminal ``*FAILED`` as a failure with the service's reason,
treat an exhausted budget or an unreadable state as "not confirmed" (retained), and touch the
execution role only after the runtime is proven gone. ``deleted`` is written only on confirmation.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from app.services import runtime_deployer as mod
from app.services.deletion_confirmation import DeletionFailedAfterAccept
from app.services.resource_ownership import ResourceDeletionRefused
from botocore.exceptions import ClientError

REGION = "us-east-1"
RUNTIME_ID = "agent_x-AbCdEfGhIj"
ROLE_NAME = "AgentCoreRuntime-agent_x"
ROLE_ARN = f"arn:aws:iam::123456789012:role/{ROLE_NAME}"


def _err(code: str, op: str = "GetAgentRuntime") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} (fake)"}}, op)


def _live() -> dict:
    return {
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeName": "agent_x",
        "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{RUNTIME_ID}",
        "roleArn": ROLE_ARN,
        "status": "READY",
    }


class _Ctrl:
    """A runtime that is READY until ``delete_agent_runtime`` is called, then answers each
    subsequent ``get_agent_runtime`` from *after_delete* (the last entry repeats forever)."""

    def __init__(self, after_delete: list, log: list) -> None:
        self._after = list(after_delete)
        self.log = log
        self.deleted = False

    def get_agent_runtime(self, *, agentRuntimeId: str):  # noqa: N803 - boto3 casing
        assert agentRuntimeId == RUNTIME_ID
        if not self.deleted:
            return _live()
        self.log.append("get")
        item = self._after.pop(0) if len(self._after) > 1 else self._after[0]
        if isinstance(item, Exception):
            raise item
        return item

    def delete_agent_runtime(self, *, agentRuntimeId: str):  # noqa: N803 - boto3 casing
        assert agentRuntimeId == RUNTIME_ID
        self.deleted = True
        self.log.append("delete")
        return {}

    def list_online_evaluation_configs(self, **_kw):
        return {"onlineEvaluationConfigs": []}


DELETING = {"agentRuntimeId": RUNTIME_ID, "status": "DELETING"}
DELETE_FAILED = {"agentRuntimeId": RUNTIME_ID, "status": "DELETE_FAILED", "statusReasons": ["FAKE: endpoint busy"]}


@pytest.fixture
def destroy(monkeypatch):
    """Drive the real ``destroy_runtime`` against a fake control plane; return (result, log)."""
    monkeypatch.delenv("SHARED_RUNTIME_ROLE_ARN", raising=False)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    log: list = []
    # Ownership is proven by the same read; this test is about what happens AFTER the delete.
    monkeypatch.setattr(
        mod,
        "assert_agentcore_resource_owned",
        lambda ctrl, rtype, rid, region: ctrl.get_agent_runtime(agentRuntimeId=rid),
    )
    monkeypatch.setattr(mod, "delete_owned_iam_role", lambda iam, name, region=None: log.append(("delete_role", name)))
    monkeypatch.setattr(mod, "_resolve_runtime_name_for_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_list_all_online_evaluation_configs", lambda ctrl: [])
    import app.services.observability_dashboard as dash

    monkeypatch.setattr(dash, "delete_dashboard_for_runtime", lambda *a, **k: True)

    def _run(after_delete: list, **kwargs):
        ctrl = _Ctrl(after_delete, log)
        factory = lambda service, **_kw: ctrl if service == "bedrock-agentcore-control" else MagicMock()  # noqa: E731
        result = mod.destroy_runtime(RUNTIME_ID, REGION, client_factory=factory, **kwargs)
        return result, log

    return _run


# --------------------------------------------------------------------------- destroy_runtime


def test_a_confirmed_delete_succeeds_and_the_role_goes_only_after_the_runtime_is_gone(destroy):
    result, log = destroy([DELETING, DELETING, _err("ResourceNotFoundException")])
    assert result["success"] is True, result
    assert not result.get("retained")
    assert log[:5] == ["delete", "get", "get", "get", ("delete_role", ROLE_NAME)], log
    # Every role deletion (the execution role, then the eval sidecar role) follows the last read.
    assert all(entry == "get" or entry == "delete" for entry in log[: log.index(("delete_role", ROLE_NAME))]), log


def test_delete_failed_is_a_failure_with_the_services_reason_and_the_role_is_kept(destroy):
    result, log = destroy([DELETE_FAILED])
    assert result["success"] is False, result
    assert not result.get("retained"), "a terminal failure is a failure, not an unknown"
    assert "DELETE_FAILED" in result["message"] and "endpoint busy" in result["message"]
    assert ("delete_role", ROLE_NAME) not in log, "the role must outlive a runtime that still exists"


def test_still_deleting_at_the_end_of_the_budget_is_retained_not_deleted(destroy):
    result, log = destroy([DELETING], confirmation_attempts=3, confirmation_interval=0.0)
    assert result["success"] is False, result
    assert result["retained"] is True
    assert "not confirmed" in result["message"].lower()
    assert log.count("get") == 3
    assert ("delete_role", ROLE_NAME) not in log


def test_an_unreadable_state_after_the_delete_is_retained(destroy):
    result, log = destroy([_err("AccessDeniedException")])
    assert result["success"] is False and result["retained"] is True, result
    assert ("delete_role", ROLE_NAME) not in log


def test_an_expired_deadline_confirms_nothing(destroy):
    result, log = destroy([DELETING], confirmation_deadline=time.monotonic() - 1.0)
    assert result["success"] is False and result["retained"] is True, result
    assert ("delete_role", ROLE_NAME) not in log


def test_an_already_absent_runtime_is_still_idempotent_success(destroy, monkeypatch):
    """The other direction: not-found on the very first read is the documented no-op, and role
    cleanup by convention-derived names still runs (that path is tested elsewhere)."""

    class _Gone:
        def get_agent_runtime(self, **_kw):
            raise _err("ResourceNotFoundException")

        def delete_agent_runtime(self, **_kw):
            raise _err("ResourceNotFoundException", "DeleteAgentRuntime")

    monkeypatch.setattr(mod, "assert_agentcore_resource_owned", lambda ctrl, *a, **k: ctrl.get_agent_runtime())
    result = mod.destroy_runtime(
        RUNTIME_ID, REGION, client_factory=lambda service, **_kw: _Gone() if "agentcore" in service else MagicMock()
    )
    assert result["success"] is True


# --------------------------------------------------------------------------- the manifest arm


@pytest.fixture
def arm(monkeypatch):
    import app.deployment_handler as dh

    monkeypatch.setattr(dh, "boto3", MagicMock())
    monkeypatch.setattr(dh, "assert_agentcore_resource_owned", lambda *a, **k: None)

    def _run(verdict: dict, **kwargs):
        captured: dict = {}

        def _destroy(rid, region, **kw):
            captured.update(kw)
            return verdict

        monkeypatch.setattr(dh, "destroy_runtime", _destroy)
        msg = dh._delete_managed_resource(
            {"type": "agent_runtime", "id": RUNTIME_ID, "region": REGION}, REGION, **kwargs
        )
        return msg, captured

    return _run, dh


def test_the_manifest_arm_turns_retained_into_a_retention_not_a_failure(arm):
    run, _ = arm
    with pytest.raises(ResourceDeletionRefused, match="not confirmed"):
        run({"success": False, "retained": True, "message": "deletion is not confirmed"})


def test_the_manifest_arm_turns_delete_failed_into_a_failure(arm):
    run, _ = arm
    with pytest.raises(RuntimeError, match="DELETE_FAILED"):
        run({"success": False, "message": "Runtime destroy error: entered DELETE_FAILED"})


def test_the_manifest_arm_reports_a_confirmed_delete(arm):
    run, _ = arm
    msg, _ = run({"success": True, "message": f"Runtime {RUNTIME_ID} deleted"})
    assert msg.startswith(f"[manifest] runtime {RUNTIME_ID}:")


def test_the_manifest_arm_hands_the_async_deadline_to_destroy_runtime(arm):
    """Inside the async teardown the shared confirmation deadline bounds the poll; inline (no
    deadline) the arm passes nothing extra, so callers with a strict fake signature are unaffected."""
    run, dh = arm
    _, inline = run({"success": True, "message": "ok"})
    assert "confirmation_deadline" not in inline and "confirmation_attempts" not in inline

    deadline = time.monotonic() + 120.0
    token = dh._memory_confirm_deadline.set(deadline)
    try:
        _, async_kwargs = run({"success": True, "message": "ok"})
    finally:
        dh._memory_confirm_deadline.reset(token)
    assert async_kwargs["confirmation_deadline"] == deadline
    assert async_kwargs["confirmation_attempts"] >= 2


# --------------------------------------------------------------------------- the failed-deploy arm


@pytest.fixture
def failed_deploy_arm(monkeypatch):
    from app.services import step_clients
    from app.step_handlers import status_update_step as sus

    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setattr(sus, "assert_agentcore_resource_owned", lambda *a, **k: _live())
    log: list = []

    def _run(after_delete: list, deadline: float | None):
        ctrl = _Ctrl(after_delete, log)
        monkeypatch.setattr(
            step_clients,
            "client",
            lambda _event, service, **_kw: ctrl if service == "bedrock-agentcore-control" else MagicMock(),
        )
        event = {"deployment_id": "d1"}
        if deadline is not None:
            event["_cleanup_deadline_monotonic"] = deadline
        sus._cleanup_resource({"type": "agent_runtime", "id": RUNTIME_ID, "region": REGION}, REGION, event)
        return log

    return _run, sus


def test_the_failed_deploy_arm_confirms_absence_before_counting_the_runtime_cleaned(failed_deploy_arm):
    run, _ = failed_deploy_arm
    log = run([DELETING, _err("ResourceNotFoundException")], deadline=time.monotonic() + 60)
    assert log == ["delete", "get", "get"]


def test_the_failed_deploy_arm_surfaces_delete_failed(failed_deploy_arm):
    run, _ = failed_deploy_arm
    with pytest.raises(DeletionFailedAfterAccept, match="DELETE_FAILED"):
        run([DELETE_FAILED], deadline=time.monotonic() + 60)


def test_the_failed_deploy_arm_retains_an_unconfirmed_runtime(failed_deploy_arm):
    run, sus = failed_deploy_arm
    with pytest.raises(sus._ResourceRetained) as ei:
        run([DELETING], deadline=time.monotonic() - 1.0)
    assert ei.value.rtype == "agent_runtime" and ei.value.rid == RUNTIME_ID
    assert "not confirmed" in ei.value.reason.lower()
