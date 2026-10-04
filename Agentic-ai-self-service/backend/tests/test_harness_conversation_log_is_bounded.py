"""A harness's conversation log is bounded like every runtime's, fail-closed.

AgentCore hosts each harness on a runtime it creates itself (``GetHarness`` ->
``environment.agentCoreRuntimeEnvironment.agentRuntimeId``), and that runtime's DEFAULT log
group holds every conversation the harness serves. The service creates the group with no
retention. Measured 2026-10-02: all 60 ``/aws/bedrock-agentcore/runtimes/harness_*-DEFAULT``
groups in the platform account were set to never expire, the one left by that morning's
harness deploy-and-delete included. The runtime path has the rule this enforces
(``govern_default_runtime_log_group``): a deployment does not report success while its
conversation log is unbounded.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest
from app.services import harness_deployer
from app.services.runtime_deployer import RUNTIME_LOG_RETENTION_DAYS
from botocore.exceptions import ClientError

BACKING = "harness_audit_1a2b3c4d-AbCdEf1234"
GROUP = f"/aws/bedrock-agentcore/runtimes/{BACKING}-DEFAULT"
REGION = "eu-west-1"


class _Ctrl:
    def __init__(self, harness: dict):
        self.harness = harness

    def get_harness(self, harnessId):  # noqa: N803 -- boto3's keyword
        return {"harness": {"harnessId": harnessId, **self.harness}}


def test_the_ready_harness_reports_its_backing_runtime():
    ready = harness_deployer.wait_for_harness_ready(
        _Ctrl(
            {
                "status": "READY",
                "arn": "arn:aws:bedrock-agentcore:eu-west-1:123456789012:harness/audit-AbCdEf1234",
                "environment": {"agentCoreRuntimeEnvironment": {"agentRuntimeId": BACKING}},
            }
        ),
        "audit-AbCdEf1234",
        timeout=5,
    )

    assert ready["success"] is True and ready["backing_runtime_id"] == BACKING


def test_a_ready_harness_without_an_environment_reports_none():
    ready = harness_deployer.wait_for_harness_ready(_Ctrl({"status": "READY", "arn": "a"}), "audit-x", timeout=5)

    assert ready["success"] is True and ready["backing_runtime_id"] == ""


class _Store:
    def __init__(self):
        self.resources: list[dict] = []
        self.order: list[str] = []

    def update_step(self, *a, **kw):
        pass

    def update_status(self, *a, **kw):
        pass

    def record_resource(self, _deployment_id, resource):
        self.resources.append(resource)
        self.order.append(f"record:{resource['type']}")


def _run(monkeypatch, *, ready, logs=None):
    from app.step_handlers import harness_step

    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    monkeypatch.delenv("SHARED_HARNESS_ROLE_ARN", raising=False)
    store = _Store()
    logs = logs if logs is not None else MagicMock()
    regions: list = []

    def _client(_event, service, **kwargs):
        if service == "logs":
            regions.append(kwargs.get("region_name"))
            store.order.append("logs-client")
            return logs
        return MagicMock()

    monkeypatch.setattr(harness_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(harness_step.step_clients, "client", _client)
    monkeypatch.setattr(
        harness_step.harness_deployer,
        "get_shared_or_new_harness_role",
        lambda *a, **kw: "arn:aws:iam::123456789012:role/AgentCoreHarness-audit",
    )
    monkeypatch.setattr(
        harness_step.harness_deployer,
        "create_harness",
        lambda *a, **kw: {"harness_id": "audit-AbCdEf1234", "arn": "harness-arn", "created_by_deployment": True},
    )
    monkeypatch.setattr(harness_step.harness_deployer, "wait_for_harness_ready", lambda *a, **kw: ready)
    event = {
        "deployment_id": "dep-harness-logs",
        "target_region": REGION,
        "config": {"name": "audit", "model": {"modelId": "us.anthropic.claude-sonnet-5"}},
    }
    return harness_step.handler, event, store, logs, regions


def test_the_backing_runtime_log_group_gets_the_platform_retention(monkeypatch):
    handler, event, store, logs, regions = _run(
        monkeypatch, ready={"success": True, "arn": "harness-arn", "backing_runtime_id": BACKING}
    )

    result = handler(event, None)

    assert logs.mock_calls == [
        call.create_log_group(logGroupName=GROUP),
        call.put_retention_policy(logGroupName=GROUP, retentionInDays=RUNTIME_LOG_RETENTION_DAYS),
    ]
    assert regions == [REGION], "the group lives in the deployment's target region"
    assert result["harness_result"]["backing_runtime_id"] == BACKING
    assert result["harness_result"]["runtime_log_group"] == GROUP
    # The harness is in the manifest before anything that can fail, so a failure still tears it down.
    assert store.order.index("record:harness") < store.order.index("logs-client")


def test_an_existing_group_is_adopted_and_bounded(monkeypatch):
    logs = MagicMock()
    logs.create_log_group.side_effect = ClientError(
        {"Error": {"Code": "ResourceAlreadyExistsException", "Message": "exists"}}, "CreateLogGroup"
    )
    handler, event, _store, logs, _regions = _run(
        monkeypatch, ready={"success": True, "arn": "harness-arn", "backing_runtime_id": BACKING}, logs=logs
    )

    handler(event, None)

    logs.put_retention_policy.assert_called_once_with(logGroupName=GROUP, retentionInDays=RUNTIME_LOG_RETENTION_DAYS)


def test_a_harness_with_no_backing_runtime_does_not_report_success(monkeypatch):
    handler, event, store, logs, _regions = _run(monkeypatch, ready={"success": True, "arn": "harness-arn"})

    with pytest.raises(RuntimeError, match="no backing runtime"):
        handler(event, None)

    assert logs.mock_calls == []
    assert [r["type"] for r in store.resources] == ["harness", "iam_role"], "the delete path still has its rows"


def test_a_retention_denial_fails_the_deploy(monkeypatch):
    logs = MagicMock()
    denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "PutRetentionPolicy")
    logs.put_retention_policy.side_effect = denied
    handler, event, _store, _logs, _regions = _run(
        monkeypatch, ready={"success": True, "arn": "harness-arn", "backing_runtime_id": BACKING}, logs=logs
    )

    with pytest.raises(ClientError) as raised:
        handler(event, None)

    assert raised.value is denied


def test_a_malformed_backing_runtime_id_is_refused_before_any_call(monkeypatch):
    handler, event, _store, logs, _regions = _run(
        monkeypatch, ready={"success": True, "arn": "harness-arn", "backing_runtime_id": "../other"}
    )

    with pytest.raises(ValueError, match="invalid runtime id"):
        handler(event, None)

    assert logs.mock_calls == []
