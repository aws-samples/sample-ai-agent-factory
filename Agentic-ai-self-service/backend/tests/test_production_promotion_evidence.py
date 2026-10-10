"""Production promotion is authorized only by version-bound AgentCore evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import boto3
import pytest
from app.services.agent_versions_store import (
    AgentVersion,
    AgentVersionsStore,
    PromotionEvidenceConflict,
)
from app.services.promotion_evidence import (
    PromotionEvidenceError,
    PromotionEvidencePending,
    PromotionPolicyConfigurationError,
    load_promotion_policy,
    require_promotion_evidence,
    start_batch_evaluation,
    validate_batch_evaluation_for_promotion,
    validate_frozen_promotion_policy,
)
from moto import mock_aws

RUNTIME_NAME = "release_bot"
AGENTCORE_RUNTIME_NAME = "release_bot_deadbeef"
VERSION_ID = "01a0d26eb4459a9516d12064ac4a8687"
DEPLOYMENT_ID = "dep-release-1"
RUNTIME_ID = "release_runtime_1"
ACCOUNT = "123456789012"
REGION = "eu-west-1"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}"
BATCH_ID = "batch_release_1"
BATCH_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:batch-evaluate/{BATCH_ID}"
CREATED = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
FINISHED = CREATED + timedelta(minutes=10)
NOW = FINISHED + timedelta(minutes=1)


def _policy(monkeypatch) -> dict:
    monkeypatch.delenv("PRODUCTION_EVALUATION_POLICY_JSON", raising=False)
    return load_promotion_policy()


def _integer_numbers_to_floats(value):
    """Model the old DynamoDB reader that converted every number to float."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, dict):
        return {key: _integer_numbers_to_floats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_integer_numbers_to_floats(item) for item in value]
    return value


def _scenarios(count: int = 5) -> list[dict]:
    return [
        {
            "session_id": f"release-session-{index}",
            "test_scenario_id": f"scenario-{index}",
            "assertions": ["The response satisfies the release acceptance criteria."],
            "expected_tool_names": ["catalog___lookup"],
            "turns": [],
        }
        for index in range(count)
    ]


def _result(policy: dict, **overrides) -> dict:
    session_ids = [item["session_id"] for item in _scenarios()]
    result = {
        "batchEvaluationId": BATCH_ID,
        "batchEvaluationArn": BATCH_ARN,
        "batchEvaluationName": "release_01a0d26eb445_deadbeef0000",
        "status": "COMPLETED",
        "createdAt": CREATED,
        "updatedAt": FINISHED,
        "evaluators": [{"evaluatorId": evaluator_id} for evaluator_id in policy["evaluators"]],
        "dataSourceConfig": {
            "cloudWatchLogs": {
                "serviceNames": [f"{AGENTCORE_RUNTIME_NAME}.DEFAULT"],
                "logGroupNames": ["aws/spans"],
                "filterConfig": {"sessionIds": session_ids},
            }
        },
        "evaluationResults": {
            "numberOfSessionsCompleted": len(session_ids),
            "numberOfSessionsInProgress": 0,
            "numberOfSessionsFailed": 0,
            "totalNumberOfSessions": len(session_ids),
            "numberOfSessionsIgnored": 0,
            "evaluatorSummaries": [
                {
                    "evaluatorId": evaluator_id,
                    "statistics": {"averageScore": max(0.91, threshold)},
                    "totalEvaluated": len(session_ids),
                    "totalFailed": 0,
                }
                for evaluator_id, threshold in policy["evaluators"].items()
            ],
        },
        "errorDetails": [],
    }
    result.update(overrides)
    return result


def _evidence(policy: dict, **overrides) -> dict:
    result = _result(policy, **overrides)
    return validate_batch_evaluation_for_promotion(
        result,
        runtime_name=RUNTIME_NAME,
        agentcore_runtime_name=AGENTCORE_RUNTIME_NAME,
        version_id=VERSION_ID,
        deployment_id=DEPLOYMENT_ID,
        runtime_id=RUNTIME_ID,
        runtime_arn=RUNTIME_ARN,
        version_created_at=(CREATED - timedelta(minutes=1)).isoformat(),
        policy=policy,
        now=NOW,
    )


def test_default_policy_is_digest_bound_and_requires_five_scenarios(monkeypatch):
    policy = _policy(monkeypatch)
    assert policy["minimum_sessions"] == 5
    assert set(policy["evaluators"]) == {
        "Builtin.GoalSuccessRate",
        "Builtin.Correctness",
        "Builtin.Helpfulness",
    }
    assert validate_frozen_promotion_policy(policy) == policy


def test_all_floats_deserialization_mutation_breaks_policy_and_evidence_digests(monkeypatch):
    policy = _policy(monkeypatch)
    mutated_policy = _integer_numbers_to_floats(policy)
    with pytest.raises(PromotionEvidenceError, match="policy is invalid"):
        validate_frozen_promotion_policy(mutated_policy)

    evidence = _evidence(policy)
    mutated_evidence = _integer_numbers_to_floats(evidence)
    with pytest.raises(PromotionEvidenceError, match="digest is invalid"):
        require_promotion_evidence(
            mutated_evidence,
            runtime_name=RUNTIME_NAME,
            version_id=VERSION_ID,
            deployment_id=DEPLOYMENT_ID,
            runtime_id=RUNTIME_ID,
            policy=policy,
            now_epoch=int(NOW.timestamp()),
        )


def test_invalid_operator_policy_fails_closed(monkeypatch):
    monkeypatch.setenv("PRODUCTION_EVALUATION_POLICY_JSON", '{"schema_version":1,"evaluators":{}}')
    with pytest.raises(PromotionPolicyConfigurationError, match="unsupported|must"):
        load_promotion_policy()


def test_start_wrapper_builds_exact_version_bound_batch_request(monkeypatch):
    policy = _policy(monkeypatch)
    client = MagicMock()
    client.start_batch_evaluation.return_value = {
        "batchEvaluationId": BATCH_ID,
        "batchEvaluationArn": BATCH_ARN,
        "status": "PENDING",
    }

    started = start_batch_evaluation(
        client,
        runtime_name=RUNTIME_NAME,
        agentcore_runtime_name=AGENTCORE_RUNTIME_NAME,
        version_id=VERSION_ID,
        deployment_id=DEPLOYMENT_ID,
        runtime_id=RUNTIME_ID,
        runtime_arn=RUNTIME_ARN,
        scenarios=_scenarios(),
        policy=policy,
    )

    assert started["batch_evaluation_id"] == BATCH_ID
    request = client.start_batch_evaluation.call_args.kwargs
    assert request["dataSourceConfig"]["cloudWatchLogs"] == {
        "serviceNames": [f"{AGENTCORE_RUNTIME_NAME}.DEFAULT"],
        "logGroupNames": ["aws/spans"],
        "filterConfig": {"sessionIds": [item["session_id"] for item in _scenarios()]},
    }
    assert {item["evaluatorId"] for item in request["evaluators"]} == set(policy["evaluators"])
    assert len(request["evaluationMetadata"]["sessionMetadata"]) == policy["minimum_sessions"]
    assert request["clientToken"] == started["request_digest"]


def test_start_wrapper_rejects_too_few_scenarios_before_calling_aws(monkeypatch):
    policy = _policy(monkeypatch)
    client = MagicMock()
    with pytest.raises(PromotionEvidenceError, match="At least 5"):
        start_batch_evaluation(
            client,
            runtime_name=RUNTIME_NAME,
            agentcore_runtime_name=AGENTCORE_RUNTIME_NAME,
            version_id=VERSION_ID,
            deployment_id=DEPLOYMENT_ID,
            runtime_id=RUNTIME_ID,
            runtime_arn=RUNTIME_ARN,
            scenarios=_scenarios(4),
            policy=policy,
        )
    client.start_batch_evaluation.assert_not_called()


def test_complete_passing_result_produces_self_verifying_evidence(monkeypatch):
    policy = _policy(monkeypatch)
    evidence = _evidence(policy)
    assert evidence["verdict"] == "pass"
    assert evidence["session_summary"] == {
        "total": 5,
        "completed": 5,
        "failed": 0,
        "ignored": 0,
        "session_ids_digest": evidence["session_summary"]["session_ids_digest"],
    }
    assert (
        require_promotion_evidence(
            evidence,
            runtime_name=RUNTIME_NAME,
            version_id=VERSION_ID,
            deployment_id=DEPLOYMENT_ID,
            runtime_id=RUNTIME_ID,
            policy=policy,
            now_epoch=int(NOW.timestamp()),
        )
        == evidence
    )


def test_pending_result_cannot_be_attested(monkeypatch):
    policy = _policy(monkeypatch)
    with pytest.raises(PromotionEvidencePending, match="still IN_PROGRESS"):
        _evidence(policy, status="IN_PROGRESS")


def test_low_score_is_an_exact_release_denial(monkeypatch):
    policy = _policy(monkeypatch)
    result = _result(policy)
    result["evaluationResults"]["evaluatorSummaries"][0]["statistics"]["averageScore"] = 0.1
    with pytest.raises(PromotionEvidenceError, match="production requires at least"):
        validate_batch_evaluation_for_promotion(
            result,
            runtime_name=RUNTIME_NAME,
            agentcore_runtime_name=AGENTCORE_RUNTIME_NAME,
            version_id=VERSION_ID,
            deployment_id=DEPLOYMENT_ID,
            runtime_id=RUNTIME_ID,
            runtime_arn=RUNTIME_ARN,
            version_created_at=(CREATED - timedelta(minutes=1)).isoformat(),
            policy=policy,
            now=NOW,
        )


def test_wrong_runtime_source_cannot_authorize_this_version(monkeypatch):
    policy = _policy(monkeypatch)
    result = _result(policy)
    result["dataSourceConfig"]["cloudWatchLogs"]["serviceNames"] = ["another_runtime"]
    with pytest.raises(PromotionEvidenceError, match="not exactly this version"):
        validate_batch_evaluation_for_promotion(
            result,
            runtime_name=RUNTIME_NAME,
            agentcore_runtime_name=AGENTCORE_RUNTIME_NAME,
            version_id=VERSION_ID,
            deployment_id=DEPLOYMENT_ID,
            runtime_id=RUNTIME_ID,
            runtime_arn=RUNTIME_ARN,
            version_created_at=(CREATED - timedelta(minutes=1)).isoformat(),
            policy=policy,
            now=NOW,
        )


def test_tampering_with_stored_evidence_breaks_its_digest(monkeypatch):
    policy = _policy(monkeypatch)
    evidence = _evidence(policy)
    evidence["evaluator_summaries"][0]["average_score"] = 1.0
    with pytest.raises(PromotionEvidenceError, match="digest is invalid"):
        require_promotion_evidence(
            evidence,
            runtime_name=RUNTIME_NAME,
            version_id=VERSION_ID,
            deployment_id=DEPLOYMENT_ID,
            runtime_id=RUNTIME_ID,
            policy=policy,
            now_epoch=int(NOW.timestamp()),
        )


def test_evidence_write_is_identity_fenced_immutable_and_idempotent(monkeypatch):
    policy = _policy(monkeypatch)
    evidence = _evidence(policy)
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName="AgentVersions",
            KeySchema=[
                {"AttributeName": "runtime_name", "KeyType": "HASH"},
                {"AttributeName": "version_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "runtime_name", "AttributeType": "S"},
                {"AttributeName": "version_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        store = AgentVersionsStore("AgentVersions", REGION)
        store.put(
            AgentVersion(
                runtime_name=RUNTIME_NAME,
                version_id=VERSION_ID,
                owner_sub="owner-1",
                created_at=(CREATED - timedelta(minutes=1)).isoformat(),
                deployment_id=DEPLOYMENT_ID,
                agentcore_runtime_name=AGENTCORE_RUNTIME_NAME,
                runtime_id=RUNTIME_ID,
                runtime_arn=RUNTIME_ARN,
                status="succeeded",
                production_gate_required=True,
                promotion_policy=policy,
            )
        )

        persisted = store.get(RUNTIME_NAME, VERSION_ID, consistent=True)
        assert persisted is not None
        assert persisted.promotion_policy is not None
        for key in (
            "schema_version",
            "minimum_sessions",
            "maximum_failed_sessions",
            "maximum_ignored_sessions",
            "maximum_evaluator_failures",
            "max_evidence_age_seconds",
        ):
            assert type(persisted.promotion_policy[key]) is int
        assert validate_frozen_promotion_policy(persisted.promotion_policy) == policy

        recorded = store.record_promotion_evidence(
            RUNTIME_NAME,
            VERSION_ID,
            expected_owner_sub="owner-1",
            expected_deployment_id=DEPLOYMENT_ID,
            expected_runtime_id=RUNTIME_ID,
            expected_runtime_arn=RUNTIME_ARN,
            evidence=evidence,
        )
        assert recorded.promotion_evidence == evidence
        assert (
            store.record_promotion_evidence(
                RUNTIME_NAME,
                VERSION_ID,
                expected_owner_sub="owner-1",
                expected_deployment_id=DEPLOYMENT_ID,
                expected_runtime_id=RUNTIME_ID,
                expected_runtime_arn=RUNTIME_ARN,
                evidence=evidence,
            ).promotion_evidence
            == evidence
        )

        persisted_evidence = store.get(RUNTIME_NAME, VERSION_ID, consistent=True)
        assert persisted_evidence is not None
        assert persisted_evidence.promotion_policy is not None
        assert persisted_evidence.promotion_evidence is not None
        assert type(persisted_evidence.promotion_evidence["expires_at_epoch"]) is int
        assert type(persisted_evidence.promotion_evidence["session_summary"]["total"]) is int
        assert (
            require_promotion_evidence(
                persisted_evidence.promotion_evidence,
                runtime_name=RUNTIME_NAME,
                version_id=VERSION_ID,
                deployment_id=DEPLOYMENT_ID,
                runtime_id=RUNTIME_ID,
                policy=persisted_evidence.promotion_policy,
                now_epoch=int(NOW.timestamp()),
            )
            == evidence
        )

        replacement = dict(evidence)
        replacement["evidence_digest"] = "f" * 64
        with pytest.raises(PromotionEvidenceConflict):
            store.record_promotion_evidence(
                RUNTIME_NAME,
                VERSION_ID,
                expected_owner_sub="owner-1",
                expected_deployment_id=DEPLOYMENT_ID,
                expected_runtime_id=RUNTIME_ID,
                expected_runtime_arn=RUNTIME_ARN,
                evidence=replacement,
            )


def test_production_batch_refuses_a_missing_customer_managed_key(monkeypatch):
    policy = _policy(monkeypatch)
    monkeypatch.delenv("EVALUATION_EVIDENCE_KMS_KEY_ARN", raising=False)
    client = MagicMock()

    with pytest.raises(PromotionPolicyConfigurationError, match="is required"):
        start_batch_evaluation(
            client,
            runtime_name=RUNTIME_NAME,
            agentcore_runtime_name=AGENTCORE_RUNTIME_NAME,
            version_id=VERSION_ID,
            deployment_id=DEPLOYMENT_ID,
            runtime_id=RUNTIME_ID,
            runtime_arn=RUNTIME_ARN,
            scenarios=_scenarios(),
            policy=policy,
            kms_required=True,
        )
    client.start_batch_evaluation.assert_not_called()
