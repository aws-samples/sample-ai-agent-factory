"""HTTP contract for version-bound production evaluation and attestation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.routers import evaluations
from app.services.agent_versions_store import AgentVersion
from app.services.auth import get_caller_sub
from app.services.promotion_evidence import load_promotion_policy
from fastapi import FastAPI
from fastapi.testclient import TestClient

OWNER = "release-owner"
RUNTIME_NAME = "release_bot"
VERSION_ID = "01a0d26eb4459a9516d12064ac4a8687"
DEPLOYMENT_ID = "dep-release-1"
RUNTIME_ID = "release_runtime_1"
REGION = "eu-west-1"
ACCOUNT = "123456789012"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}"
BATCH_ID = "batch_release_1"
BATCH_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:batch-evaluate/{BATCH_ID}"


def _scenarios() -> list[dict]:
    return [
        {
            "sessionId": f"session-{index}",
            "testScenarioId": f"scenario-{index}",
            "assertions": ["The response meets the release acceptance criteria."],
            "expectedToolNames": ["catalog___lookup"],
        }
        for index in range(5)
    ]


def _batch_result(policy: dict, *, status: str = "COMPLETED") -> dict:
    created = datetime.now(UTC) - timedelta(minutes=2)
    completed = created + timedelta(minutes=1)
    return {
        "batchEvaluationId": BATCH_ID,
        "batchEvaluationArn": BATCH_ARN,
        "batchEvaluationName": "release_batch",
        "status": status,
        "createdAt": created,
        "updatedAt": completed,
        "evaluators": [{"evaluatorId": evaluator_id} for evaluator_id in policy["evaluators"]],
        "dataSourceConfig": {
            "cloudWatchLogs": {
                "serviceNames": [RUNTIME_ID],
                "logGroupNames": [f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT"],
                "filterConfig": {"sessionIds": [f"session-{index}" for index in range(5)]},
            }
        },
        "evaluationResults": {
            "numberOfSessionsCompleted": 5,
            "numberOfSessionsInProgress": 0,
            "numberOfSessionsFailed": 0,
            "totalNumberOfSessions": 5,
            "numberOfSessionsIgnored": 0,
            "evaluatorSummaries": [
                {
                    "evaluatorId": evaluator_id,
                    "statistics": {"averageScore": 0.95},
                    "totalEvaluated": 5,
                    "totalFailed": 0,
                }
                for evaluator_id in policy["evaluators"]
            ],
        },
        "errorDetails": [],
    }


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setenv(
        "EVALUATION_EVIDENCE_KMS_KEY_ARN",
        f"arn:aws:kms:{REGION}:{ACCOUNT}:key/12345678-1234-1234-1234-1234567890ab",
    )
    policy = load_promotion_policy()
    version = AgentVersion(
        runtime_name=RUNTIME_NAME,
        version_id=VERSION_ID,
        owner_sub=OWNER,
        created_at=(datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
        deployment_id=DEPLOYMENT_ID,
        agentcore_runtime_name=RUNTIME_NAME,
        runtime_id=RUNTIME_ID,
        runtime_arn=RUNTIME_ARN,
        status="succeeded",
        production_gate_required=True,
        promotion_policy=policy,
    )
    agentcore = MagicMock()
    agentcore.start_batch_evaluation.return_value = {
        "batchEvaluationId": BATCH_ID,
        "batchEvaluationArn": BATCH_ARN,
        "status": "PENDING",
    }
    agentcore.get_batch_evaluation.return_value = _batch_result(policy)
    target = SimpleNamespace(
        deployment_id=DEPLOYMENT_ID,
        runtime_id=RUNTIME_ID,
        runtime_arn=RUNTIME_ARN,
        client=lambda service: agentcore if service == "bedrock-agentcore" else None,
    )
    store = MagicMock()
    store.get.return_value = version

    def _record(*_args, evidence, **_kwargs):
        version.promotion_evidence = evidence
        return version

    store.record_promotion_evidence.side_effect = _record
    monkeypatch.setattr(evaluations, "resolve_owned_runtime_version_target", lambda *args, **kwargs: target)
    monkeypatch.setattr(evaluations, "get_versions_store", lambda: store)
    monkeypatch.setattr("app.services.rbac.has_scopes", lambda request, required: True)

    app = FastAPI()
    app.include_router(evaluations.router)
    app.dependency_overrides[get_caller_sub] = lambda: OWNER
    return SimpleNamespace(
        client=TestClient(app),
        agentcore=agentcore,
        store=store,
        version=version,
        policy=policy,
    )


def test_start_endpoint_uses_the_version_target_and_frozen_policy(harness):
    response = harness.client.post(
        f"/api/runtimes/{RUNTIME_NAME}/versions/{VERSION_ID}/promotion-evaluation/start",
        json={"scenarios": _scenarios()},
    )

    assert response.status_code == 200, response.text
    assert response.json()["batch_evaluation_id"] == BATCH_ID
    request = harness.agentcore.start_batch_evaluation.call_args.kwargs
    assert request["dataSourceConfig"]["cloudWatchLogs"]["serviceNames"] == [RUNTIME_ID]
    assert {item["evaluatorId"] for item in request["evaluators"]} == set(harness.policy["evaluators"])
    assert request["kmsKeyArn"].endswith("12345678-1234-1234-1234-1234567890ab")


def test_attest_endpoint_records_only_server_verified_evidence(harness):
    response = harness.client.post(
        f"/api/runtimes/{RUNTIME_NAME}/versions/{VERSION_ID}/promotion-evaluation/attest",
        json={"batchEvaluationId": BATCH_ID},
    )

    assert response.status_code == 200, response.text
    evidence = response.json()["promotion_evidence"]
    assert evidence["verdict"] == "pass"
    assert evidence["runtime_id"] == RUNTIME_ID
    harness.store.record_promotion_evidence.assert_called_once()


def test_pending_batch_cannot_create_evidence(harness):
    harness.agentcore.get_batch_evaluation.return_value = _batch_result(harness.policy, status="IN_PROGRESS")

    response = harness.client.post(
        f"/api/runtimes/{RUNTIME_NAME}/versions/{VERSION_ID}/promotion-evaluation/attest",
        json={"batchEvaluationId": BATCH_ID},
    )

    assert response.status_code == 409, response.text
    assert "still IN_PROGRESS" in response.json()["detail"]
    harness.store.record_promotion_evidence.assert_not_called()


def test_evidence_endpoint_returns_only_the_owner_checked_version_record(harness):
    response = harness.client.get(f"/api/runtimes/{RUNTIME_NAME}/versions/{VERSION_ID}/promotion-evidence")

    assert response.status_code == 200, response.text
    assert response.json()["production_gate_required"] is True
    assert response.json()["promotion_evidence"] is None
