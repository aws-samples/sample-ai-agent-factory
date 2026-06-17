"""Phase 1 Gap 1C — evaluation router unit tests.

These tests use FastAPI's TestClient against a small app that mounts the
evaluations router, with the agent_versions_store + boto3 clients mocked.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from app.routers.evaluations import router as evaluations_router  # noqa: E402
from app.services.agent_versions_store import AgentVersion, RuntimeSlots  # noqa: E402
from app.services.auth import _LOCAL_DEV_SUB, get_caller_sub  # noqa: E402


@pytest.fixture
def app_with_router() -> FastAPI:
    app = FastAPI()
    app.include_router(evaluations_router)
    app.dependency_overrides[get_caller_sub] = lambda: _LOCAL_DEV_SUB
    return app


@pytest.fixture
def client(app_with_router: FastAPI) -> TestClient:
    return TestClient(app_with_router)


def test_evaluation_config_404_when_no_slot(client: TestClient):
    with patch(
        "app.routers.evaluations.get_slots_store"
    ) as slots_store_mock:
        slots_store_mock.return_value.get.return_value = None
        resp = client.get("/api/runtimes/myagent/evaluation-config")
    assert resp.status_code == 404


def test_evaluation_config_cross_tenant_returns_404(client: TestClient):
    """Different owner_sub on the slot row → 404 (existence non-disclosure)."""
    with patch(
        "app.routers.evaluations.get_slots_store"
    ) as slots_store_mock, patch(
        "app.routers.evaluations.get_versions_store"
    ) as versions_store_mock:
        slots_store_mock.return_value.get.return_value = RuntimeSlots(
            runtime_name="myagent",
            owner_sub="someone-else",
            production_version_id="v1",
        )
        # Even though we never reach versions_store.get, set up a return so a
        # bug that bypasses assert_owner can't accidentally pass the test.
        versions_store_mock.return_value.get.return_value = AgentVersion(
            runtime_name="myagent",
            version_id="v1",
            owner_sub="someone-else",
            created_at="2026-05-28T00:00:00+00:00",
            deployment_id="d1",
            agentcore_runtime_name="myagent_xyz",
            runtime_id="rt-xyz",
        )
        resp = client.get("/api/runtimes/myagent/evaluation-config")
    assert resp.status_code == 404


def test_evaluation_config_match_by_runtime_id_substring(client: TestClient):
    """Matched configs are returned with evaluators + sampling rate."""
    runtime_id = "myagent_abcd1234-runtime-abcd1234"
    with patch(
        "app.routers.evaluations.get_slots_store"
    ) as slots_store_mock, patch(
        "app.routers.evaluations.get_versions_store"
    ) as versions_store_mock, patch("boto3.client") as boto_mock:
        slots_store_mock.return_value.get.return_value = RuntimeSlots(
            runtime_name="myagent",
            owner_sub=_LOCAL_DEV_SUB,
            production_version_id="v1",
        )
        versions_store_mock.return_value.get.return_value = AgentVersion(
            runtime_name="myagent",
            version_id="v1",
            owner_sub=_LOCAL_DEV_SUB,
            created_at="2026-05-28T00:00:00+00:00",
            deployment_id="d1",
            agentcore_runtime_name="myagent_abcd1234",
            runtime_id=runtime_id,
        )
        ctrl_client = MagicMock()
        ctrl_client.list_online_evaluation_configs.return_value = {
            "onlineEvaluationConfigs": [
                {
                    "onlineEvaluationConfigName": f"eval_{runtime_id[:32]}",
                    "onlineEvaluationConfigId": "ec-1",
                }
            ]
        }
        ctrl_client.get_online_evaluation_config.return_value = {
            "onlineEvaluationConfigName": f"eval_{runtime_id[:32]}",
            "evaluators": [
                {"evaluatorId": "Builtin.GoalSuccessRate"},
                {"evaluatorId": "Builtin.Correctness"},
            ],
            "rule": {"samplingConfig": {"samplingPercentage": 50}},
            "status": "ENABLED",
        }
        boto_mock.return_value = ctrl_client

        resp = client.get("/api/runtimes/myagent/evaluation-config")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["evaluators"] == [
        "Builtin.GoalSuccessRate",
        "Builtin.Correctness",
    ]
    assert body["sampling_rate"] == 50
    assert body["status"] == "ENABLED"
    assert body["config_id"] == "ec-1"


def test_evaluation_config_404_when_no_match(client: TestClient):
    """No matching config → 404 (not 500)."""
    with patch(
        "app.routers.evaluations.get_slots_store"
    ) as slots_store_mock, patch(
        "app.routers.evaluations.get_versions_store"
    ) as versions_store_mock, patch("boto3.client") as boto_mock:
        slots_store_mock.return_value.get.return_value = RuntimeSlots(
            runtime_name="myagent",
            owner_sub=_LOCAL_DEV_SUB,
            production_version_id="v1",
        )
        versions_store_mock.return_value.get.return_value = AgentVersion(
            runtime_name="myagent",
            version_id="v1",
            owner_sub=_LOCAL_DEV_SUB,
            created_at="2026-05-28T00:00:00+00:00",
            deployment_id="d1",
            agentcore_runtime_name="myagent_abcd1234",
            runtime_id="myagent_abcd1234-rt-xyz",
        )
        ctrl_client = MagicMock()
        ctrl_client.list_online_evaluation_configs.return_value = {
            "onlineEvaluationConfigs": [
                {
                    "onlineEvaluationConfigName": "eval_unrelated_other_agent",
                    "onlineEvaluationConfigId": "ec-other",
                }
            ]
        }
        boto_mock.return_value = ctrl_client
        resp = client.get("/api/runtimes/myagent/evaluation-config")
    assert resp.status_code == 404


def test_invalid_runtime_name_rejected(client: TestClient):
    """Names that don't match the AgentCore regex are rejected at 400."""
    resp = client.get("/api/runtimes/has-hyphens/evaluation-config")
    assert resp.status_code == 400


def test_evaluation_results_handles_missing_log_group(client: TestClient):
    """If the runtime hasn't received traffic yet, the log group doesn't exist
    yet — treat that as "no results", not a 500."""
    with patch(
        "app.routers.evaluations.get_slots_store"
    ) as slots_store_mock, patch(
        "app.routers.evaluations.get_versions_store"
    ) as versions_store_mock, patch("boto3.client") as boto_mock:
        slots_store_mock.return_value.get.return_value = RuntimeSlots(
            runtime_name="myagent",
            owner_sub=_LOCAL_DEV_SUB,
            production_version_id="v1",
        )
        versions_store_mock.return_value.get.return_value = AgentVersion(
            runtime_name="myagent",
            version_id="v1",
            owner_sub=_LOCAL_DEV_SUB,
            created_at="2026-05-28T00:00:00+00:00",
            deployment_id="d1",
            agentcore_runtime_name="myagent_abcd1234",
            runtime_id="rt-xyz",
        )
        logs_client = MagicMock()

        class _RNF(Exception):
            pass

        logs_client.exceptions.ResourceNotFoundException = _RNF
        logs_client.start_query.side_effect = _RNF()
        boto_mock.return_value = logs_client

        resp = client.get("/api/runtimes/myagent/evaluations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] == []
    assert "No evaluation log group" in (body.get("message") or "")
