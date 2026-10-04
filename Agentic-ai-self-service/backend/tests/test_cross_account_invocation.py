"""Cross-account deployments must also be invoked in the target account.

Deployment-time target awareness is not sufficient: the sync API, the browser
SSE API, the long-running Function URL, Harness invocation, ARN synthesis, and
lazy policy promotion each create their own AWS clients after deployment.
These tests make the home-account client a hard failure and prove every user
surface consumes the target context frozen on the deployment record.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from app import deployment_handler as dh
from app import stream_handler as sh
from app.services import policy_promoter, step_clients
from fastapi.testclient import TestClient

OWNER = "owner-sub"
RUNTIME_ID = "target_runtime_123"
TARGET_ACCOUNT = "123456789012"
TARGET_REGION = "eu-west-1"
TARGET_ROLE_ARN = f"arn:aws:iam::{TARGET_ACCOUNT}:role/AgentCoreFlowsDeploymentRole"
TARGET_EVENT = {
    "target_account_id": TARGET_ACCOUNT,
    "target_region": TARGET_REGION,
    "target_role_arn": TARGET_ROLE_ARN,
}


class _Store:
    _table = object()


class _TargetSession:
    def __init__(self):
        self.agentcore = MagicMock(name="target-agentcore-data")
        self.agentcore.invoke_agent_runtime.return_value = {
            "response": json.dumps({"response": "target account answered"}).encode(),
            "runtimeSessionId": "target-session",
            "statusCode": 200,
        }
        self.sts = MagicMock(name="target-sts")
        self.sts.get_caller_identity.return_value = {"Account": TARGET_ACCOUNT}
        self.client_calls: list[tuple[str, dict]] = []

    def client(self, service: str, **kwargs):
        self.client_calls.append((service, kwargs))
        if service == "bedrock-agentcore":
            return self.agentcore
        if service == "sts":
            return self.sts
        raise AssertionError(f"unexpected target-account service: {service}")


def _record(*, mode: str = "runtime", runtime_arn: str = "") -> dict:
    record = {
        "deployment_id": "dep-target",
        "runtime_id": RUNTIME_ID,
        "runtime_arn": runtime_arn,
        "user_id": OWNER,
        "deployment_mode": mode,
        **TARGET_EVENT,
    }
    if mode == "harness":
        record["harness_arn"] = f"arn:aws:bedrock-agentcore:{TARGET_REGION}:{TARGET_ACCOUNT}:harness/h-1"
    return record


def _install_api_path(monkeypatch, record: dict, session: _TargetSession):
    events: list[dict] = []
    promotions: list[tuple[dict, str]] = []

    monkeypatch.setattr(dh, "_get_state_store", lambda: _Store())
    monkeypatch.setattr(
        dh,
        "_scan_for_runtime",
        lambda _table, runtime_id: dict(record) if runtime_id == RUNTIME_ID else None,
    )
    monkeypatch.setattr(dh, "_get_user_id", lambda _request: OWNER)
    monkeypatch.setattr(
        step_clients,
        "session_for_event",
        lambda event: events.append(dict(event)) or session,
    )
    monkeypatch.setattr(
        dh,
        "_maybe_promote_policy",
        lambda state, region: promotions.append((state, region)) or False,
    )
    monkeypatch.setattr(
        dh.boto3,
        "client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("home-account boto3.client must not be used")),
    )
    return TestClient(dh.deployment_app), events, promotions


def test_sync_runtime_uses_target_session_region_and_sts_account(monkeypatch):
    session = _TargetSession()
    client, events, promotions = _install_api_path(
        monkeypatch,
        _record(runtime_arn=""),
        session,
    )

    response = client.post(
        "/api/test-runtime",
        json={
            "runtimeId": RUNTIME_ID,
            "input": "hello",
            "sessionId": "user-session",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["response"] == "target account answered"
    assert events == [TARGET_EVENT]
    assert promotions and promotions[0][1] == TARGET_REGION
    session.sts.get_caller_identity.assert_called_once_with()
    _, invoke = session.agentcore.invoke_agent_runtime.call_args
    assert invoke["agentRuntimeArn"] == (
        f"arn:aws:bedrock-agentcore:{TARGET_REGION}:{TARGET_ACCOUNT}:runtime/{RUNTIME_ID}"
    )
    assert json.loads(invoke["payload"])["session_id"] == "user-session"
    assert ("sts", {"region_name": TARGET_REGION}) in session.client_calls
    assert any(
        service == "bedrock-agentcore" and kwargs["region_name"] == TARGET_REGION
        for service, kwargs in session.client_calls
    )


def test_browser_stream_runtime_uses_target_session_and_region(monkeypatch):
    session = _TargetSession()
    client, events, promotions = _install_api_path(
        monkeypatch,
        _record(runtime_arn=""),
        session,
    )

    response = client.post(
        "/api/test-runtime-stream",
        json={"runtimeId": RUNTIME_ID, "input": "hello"},
    )

    assert response.status_code == 200, response.text
    assert "target account answered" in response.text
    assert events == [TARGET_EVENT]
    assert promotions and promotions[0][1] == TARGET_REGION
    _, invoke = session.agentcore.invoke_agent_runtime.call_args
    assert invoke["agentRuntimeArn"].startswith(f"arn:aws:bedrock-agentcore:{TARGET_REGION}:{TARGET_ACCOUNT}:")


@pytest.mark.parametrize(
    "path",
    ["/api/test-runtime", "/api/test-runtime-stream"],
)
def test_api_harness_paths_use_the_target_data_client_and_promote_policy(
    monkeypatch,
    path,
):
    session = _TargetSession()
    client, events, promotions = _install_api_path(
        monkeypatch,
        _record(mode="harness"),
        session,
    )
    harness_calls: list[tuple] = []

    def _invoke_harness(
        region,
        harness_arn,
        prompt,
        session_id,
        *,
        agentcore_data_client=None,
    ):
        harness_calls.append(
            (
                region,
                harness_arn,
                prompt,
                session_id,
                agentcore_data_client,
            )
        )
        return {
            "success": True,
            "output": "target harness answered",
            "trace_id": "a" * 32,
        }

    monkeypatch.setattr(dh, "invoke_harness", _invoke_harness)

    response = client.post(
        path,
        json={"runtimeId": RUNTIME_ID, "input": "hello"},
    )

    assert response.status_code == 200, response.text
    assert events == [TARGET_EVENT]
    assert promotions and promotions[0][1] == TARGET_REGION
    assert len(harness_calls) == 1
    assert harness_calls[0][0] == TARGET_REGION
    assert harness_calls[0][4] is session.agentcore


def _install_function_url_path(
    monkeypatch,
    record: dict,
    session: _TargetSession,
) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setattr(sh, "_get_state_store", lambda: _Store())
    monkeypatch.setattr(sh, "_scan_for_runtime", lambda _table, _runtime_id: dict(record))
    monkeypatch.setattr(
        sh.step_clients,
        "session_for_event",
        lambda event: events.append(dict(event)) or session,
    )
    monkeypatch.setattr(
        sh.boto3,
        "client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("home-account boto3.client must not be used")),
    )
    return events


def test_function_url_runtime_uses_target_session_region_and_sts_account(
    monkeypatch,
):
    session = _TargetSession()
    events = _install_function_url_path(
        monkeypatch,
        _record(runtime_arn=""),
        session,
    )
    written: list[bytes] = []

    sh._stream_invoke(
        written.append,
        {"runtimeId": RUNTIME_ID, "input": "hello"},
        OWNER,
    )

    body = b"".join(written).decode()
    assert "target account answered" in body
    assert events == [TARGET_EVENT]
    _, invoke = session.agentcore.invoke_agent_runtime.call_args
    assert invoke["agentRuntimeArn"] == (
        f"arn:aws:bedrock-agentcore:{TARGET_REGION}:{TARGET_ACCOUNT}:runtime/{RUNTIME_ID}"
    )
    assert any(
        service == "bedrock-agentcore" and kwargs["region_name"] == TARGET_REGION
        for service, kwargs in session.client_calls
    )


def test_function_url_harness_uses_the_target_data_client(monkeypatch):
    session = _TargetSession()
    events = _install_function_url_path(
        monkeypatch,
        _record(mode="harness"),
        session,
    )
    calls: list[dict] = []

    def _invoke_harness(
        region,
        harness_arn,
        prompt,
        session_id,
        *,
        agentcore_data_client=None,
    ):
        calls.append(
            {
                "region": region,
                "harness_arn": harness_arn,
                "client": agentcore_data_client,
            }
        )
        return {"success": True, "output": "target harness answered"}

    monkeypatch.setattr(sh, "invoke_harness", _invoke_harness)
    written: list[bytes] = []

    sh._stream_invoke(
        written.append,
        {"runtimeId": RUNTIME_ID, "input": "hello"},
        OWNER,
    )

    assert "target harness answered" in b"".join(written).decode()
    assert events == [TARGET_EVENT]
    assert calls == [
        {
            "region": TARGET_REGION,
            "harness_arn": _record(mode="harness")["harness_arn"],
            "client": session.agentcore,
        }
    ]


def test_policy_promotion_builds_its_control_client_from_the_target_context(
    monkeypatch,
):
    state = _record(runtime_arn=(f"arn:aws:bedrock-agentcore:{TARGET_REGION}:{TARGET_ACCOUNT}:runtime/{RUNTIME_ID}"))
    state["policy_result"] = {
        "mode": "LOG_ONLY",
        "enforce_pending": {"engine_id": "eng-1", "gateway_id": "gw-1"},
    }
    target_control = object()
    durable_store = object()  # the manifest store the promoter must record lazy policy children into (F-G09-003)
    client_calls: list[tuple[dict, str, dict]] = []
    promoter_calls: list[tuple[dict, str, object, object]] = []

    monkeypatch.setattr(dh, "_get_state_store", lambda: durable_store)
    monkeypatch.setattr(
        step_clients,
        "client",
        lambda event, service, **kwargs: client_calls.append((dict(event), service, kwargs)) or target_control,
    )
    monkeypatch.setattr(
        policy_promoter,
        "try_promote_to_enforce",
        lambda deployment_state, region, *, control_client=None, store=None: (
            promoter_calls.append((deployment_state, region, control_client, store)) or {"promoted": False}
        ),
    )

    assert dh._maybe_promote_policy(state, "us-east-1") is False
    assert client_calls == [
        (
            TARGET_EVENT,
            "bedrock-agentcore-control",
            {"region_name": TARGET_REGION},
        )
    ]
    # Both halves of the contract: the TARGET account's control client, and the exact durable store, are forwarded.
    assert promoter_calls == [(state, TARGET_REGION, target_control, durable_store)]
    assert promoter_calls[0][3] is durable_store


def test_enforce_policy_reconciliation_is_reachable_after_pending_is_cleared(
    monkeypatch,
):
    state = _record()
    state["policy_result"] = {"mode": "ENFORCE", "engine_id": "eng-1"}
    target_control = object()
    called: list[object] = []

    monkeypatch.setattr(
        step_clients,
        "client",
        lambda *_args, **_kwargs: target_control,
    )
    monkeypatch.setattr(
        policy_promoter,
        "try_promote_to_enforce",
        lambda *_args, control_client=None, **_kwargs: called.append(control_client) or None,
    )

    assert dh._maybe_promote_policy(state, "us-east-1") is False
    assert called == [target_control]
