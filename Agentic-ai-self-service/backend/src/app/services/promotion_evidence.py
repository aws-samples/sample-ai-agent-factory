"""Fail-closed AgentCore batch-evaluation evidence for production promotion.

The browser never asserts that an evaluation passed.  It supplies only the
session/ground-truth corpus and, later, the opaque batch-evaluation id.  This
module calls AgentCore through a target-bound client, validates the service
result against the version's frozen platform policy, and emits a small
canonical evidence record suitable for an immutable DynamoDB write.

AWS API shapes are pinned to the boto3 ``bedrock-agentcore`` data-plane
``start_batch_evaluation`` and ``get_batch_evaluation`` operations.  Keep every
API call behind the two wrappers below so model/version drift has one seam.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import UTC, datetime
from typing import Any

from app.services.production_safety import is_production_like_environment

DEFAULT_PROMOTION_POLICY: dict[str, Any] = {
    "schema_version": 1,
    "minimum_sessions": 5,
    "maximum_failed_sessions": 0,
    "maximum_ignored_sessions": 0,
    "maximum_evaluator_failures": 0,
    "max_evidence_age_seconds": 86_400,
    "evaluators": {
        "Builtin.GoalSuccessRate": 0.70,
        "Builtin.Correctness": 0.70,
        "Builtin.Helpfulness": 0.70,
    },
}

_POLICY_ENV = "PRODUCTION_EVALUATION_POLICY_JSON"
_GATE_ENV = "PRODUCTION_EVALUATION_GATE"
_ALLOWED_POLICY_KEYS = frozenset(DEFAULT_PROMOTION_POLICY)
_EVALUATOR_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_BATCH_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_RUNTIME_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):bedrock-agentcore:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):runtime/(?P<runtime>[A-Za-z0-9_-]+)$"
)
_BATCH_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):bedrock-agentcore:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):batch-evaluate/(?P<batch>[A-Za-z0-9_-]+)$"
)
_BATCH_EVALUATION_SPANS_LOG_GROUP = "aws/spans"
_KMS_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):kms:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):key/[0-9a-fA-F-]{36}$"
)


class PromotionPolicyConfigurationError(RuntimeError):
    """The operator-supplied platform policy is invalid; fail closed."""


class PromotionEvidenceError(ValueError):
    """A batch result cannot authorize production promotion."""


class PromotionEvidencePending(PromotionEvidenceError):
    """The named batch evaluation has not reached a terminal success state."""


def agentcore_runtime_service_name(agentcore_runtime_name: str) -> str:
    """Return the CloudWatch service name documented for Runtime endpoints."""

    normalized = str(agentcore_runtime_name or "").strip()
    if not normalized or re.fullmatch(r"[A-Za-z0-9_]{1,48}", normalized) is None:
        raise PromotionEvidenceError("The version has no valid AgentCore runtime name for evaluation")
    return f"{normalized}.DEFAULT"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strict_bool(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise PromotionPolicyConfigurationError(f"{name} must be true or false")


def production_evaluation_gate_required() -> bool:
    """Return the environment's non-bypassable production-slot policy.

    Production-like environments always require staged evaluation. An explicit
    false value is accepted only for development-like environments; attempting to
    disable the gate in production is a configuration failure, not an escape hatch.
    """

    production_like = is_production_like_environment()
    explicit = os.environ.get(_GATE_ENV)
    if explicit is not None and explicit.strip():
        enabled = _strict_bool(_GATE_ENV, explicit)
        if production_like and not enabled:
            raise PromotionPolicyConfigurationError(f"{_GATE_ENV} cannot be disabled in a production-like environment")
        return enabled
    return production_like


def _bounded_int(document: dict[str, Any], key: str, *, minimum: int, maximum: int) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PromotionPolicyConfigurationError(f"{_POLICY_ENV}.{key} must be an integer in {minimum}..{maximum}")
    return value


def load_promotion_policy() -> dict[str, Any]:
    """Load and strictly validate the platform-owned production policy."""

    raw = os.environ.get(_POLICY_ENV, "").strip()
    if raw:
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PromotionPolicyConfigurationError(f"{_POLICY_ENV} must be valid JSON") from exc
    else:
        document = dict(DEFAULT_PROMOTION_POLICY)
        document["evaluators"] = dict(DEFAULT_PROMOTION_POLICY["evaluators"])

    if not isinstance(document, dict):
        raise PromotionPolicyConfigurationError(f"{_POLICY_ENV} must be a JSON object")
    unknown = sorted(set(document) - _ALLOWED_POLICY_KEYS)
    if unknown:
        raise PromotionPolicyConfigurationError(f"{_POLICY_ENV} contains unsupported field(s): {unknown}")
    if document.get("schema_version") != 1:
        raise PromotionPolicyConfigurationError(f"{_POLICY_ENV}.schema_version must be 1")

    evaluators = document.get("evaluators")
    if not isinstance(evaluators, dict) or not 1 <= len(evaluators) <= 10:
        raise PromotionPolicyConfigurationError(f"{_POLICY_ENV}.evaluators must contain 1..10 entries")
    normalized_evaluators: dict[str, float] = {}
    for evaluator_id, threshold in sorted(evaluators.items()):
        if not isinstance(evaluator_id, str) or _EVALUATOR_ID.fullmatch(evaluator_id) is None:
            raise PromotionPolicyConfigurationError(f"{_POLICY_ENV} contains an invalid evaluator id")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise PromotionPolicyConfigurationError(f"threshold for {evaluator_id} must be numeric")
        numeric = float(threshold)
        if not 0.0 <= numeric <= 1.0:
            raise PromotionPolicyConfigurationError(f"threshold for {evaluator_id} must be in 0..1")
        normalized_evaluators[evaluator_id] = numeric

    policy = {
        "schema_version": 1,
        "minimum_sessions": _bounded_int(document, "minimum_sessions", minimum=1, maximum=100),
        "maximum_failed_sessions": _bounded_int(document, "maximum_failed_sessions", minimum=0, maximum=100),
        "maximum_ignored_sessions": _bounded_int(document, "maximum_ignored_sessions", minimum=0, maximum=100),
        "maximum_evaluator_failures": _bounded_int(
            document,
            "maximum_evaluator_failures",
            minimum=0,
            maximum=100,
        ),
        "max_evidence_age_seconds": _bounded_int(
            document,
            "max_evidence_age_seconds",
            minimum=300,
            maximum=604_800,
        ),
        "evaluators": normalized_evaluators,
    }
    return {**policy, "policy_digest": _sha256(policy)}


def evaluation_kms_key_arn(
    value: str | None,
    *,
    target_region: str,
    target_account_id: str,
    required: bool = False,
) -> str | None:
    """Validate a frozen target-bound CMK ARN; production evidence requires one."""

    resolved = (value or "").strip()
    if not resolved:
        if required:
            raise PromotionPolicyConfigurationError("A frozen target evaluation KMS key is required")
        return None
    match = _KMS_ARN.fullmatch(resolved)
    if match is None:
        raise PromotionPolicyConfigurationError("The frozen evaluation KMS key must be a KMS key ARN")
    if match.group("region") != target_region or match.group("account") != target_account_id:
        raise PromotionPolicyConfigurationError(
            "The frozen evaluation KMS key must belong to the runtime account and region"
        )
    return resolved


def _as_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise PromotionEvidenceError(f"AgentCore batch evaluation omitted {field}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _runtime_identity(runtime_arn: str, runtime_id: str) -> re.Match[str]:
    match = _RUNTIME_ARN.fullmatch(runtime_arn)
    if match is None or match.group("runtime") != runtime_id:
        raise PromotionEvidenceError("The version has no canonical AgentCore runtime identity")
    return match


def _scenario_ground_truth(scenario: dict[str, Any]) -> dict[str, Any]:
    inline: dict[str, Any] = {}
    assertions = scenario.get("assertions") or []
    if assertions:
        inline["assertions"] = [{"text": str(text)} for text in assertions]
    expected_tools = scenario.get("expected_tool_names") or []
    if expected_tools:
        inline["expectedTrajectory"] = {"toolNames": [str(name) for name in expected_tools]}
    turns = scenario.get("turns") or []
    if turns:
        inline["turns"] = [
            {
                "input": {"prompt": str(turn["prompt"])},
                "expectedResponse": {"text": str(turn["expected_response"])},
            }
            for turn in turns
        ]
    if not inline:
        raise PromotionEvidenceError("Every release scenario needs ground truth or an expected trajectory")
    return {"inline": inline}


def require_transaction_search(xray_client: Any) -> None:
    """Require the account-level CloudWatch span path Batch Evaluation consumes."""

    try:
        destination = xray_client.get_trace_segment_destination()
        indexing_response = xray_client.get_indexing_rules()
        if not isinstance(destination, dict) or not isinstance(indexing_response, dict):
            raise ValueError("X-Ray Transaction Search response is not an object")
        rules = indexing_response.get("IndexingRules", [])
        if not isinstance(rules, list):
            raise ValueError("X-Ray indexing rules are not a list")
        raw_indexing = next(
            (
                (item.get("Rule") or {}).get("Probabilistic", {}).get("DesiredSamplingPercentage", 0.0)
                for item in rules
                if isinstance(item, dict) and item.get("Name") == "Default"
            ),
            0.0,
        )
        if isinstance(raw_indexing, bool):
            raise ValueError("X-Ray indexing percentage is boolean")
        indexing = float(raw_indexing)
        if not math.isfinite(indexing):
            raise ValueError("X-Ray indexing percentage is not finite")
    except Exception as exc:
        raise PromotionPolicyConfigurationError("CloudWatch Transaction Search state could not be read") from exc
    if destination.get("Destination") != "CloudWatchLogs" or destination.get("Status") != "ACTIVE" or indexing <= 0:
        raise PromotionEvidenceError(
            "Production evaluation requires CloudWatch Transaction Search in the "
            "runtime account and Region (ACTIVE/CloudWatchLogs with indexing above 0%)"
        )


def start_batch_evaluation(
    client: Any,
    *,
    runtime_name: str,
    agentcore_runtime_name: str,
    version_id: str,
    deployment_id: str,
    runtime_id: str,
    runtime_arn: str,
    scenarios: list[dict[str, Any]],
    policy: dict[str, Any],
    kms_key_arn: str | None = None,
    kms_required: bool = False,
) -> dict[str, Any]:
    """Start one idempotent, version-bound AgentCore batch evaluation."""

    identity = _runtime_identity(runtime_arn, runtime_id)
    session_ids = [str(item["session_id"]) for item in scenarios]
    if len(set(session_ids)) != len(session_ids):
        raise PromotionEvidenceError("Release scenario session ids must be unique")
    if len(session_ids) < int(policy["minimum_sessions"]):
        raise PromotionEvidenceError(
            f"At least {policy['minimum_sessions']} release scenarios are required by platform policy"
        )

    service_name = agentcore_runtime_service_name(agentcore_runtime_name)
    request_identity = {
        "runtime_name": runtime_name,
        "agentcore_runtime_name": agentcore_runtime_name,
        "service_name": service_name,
        "version_id": version_id,
        "deployment_id": deployment_id,
        "runtime_id": runtime_id,
        "session_ids": sorted(session_ids),
        "scenario_digest": _sha256(scenarios),
        "policy_digest": policy["policy_digest"],
    }
    token = _sha256(request_identity)
    request: dict[str, Any] = {
        "batchEvaluationName": f"release_{version_id[:12]}_{token[:12]}"[:48],
        "evaluators": [{"evaluatorId": evaluator_id} for evaluator_id in policy["evaluators"]],
        "dataSourceConfig": {
            "cloudWatchLogs": {
                "serviceNames": [service_name],
                "logGroupNames": [_BATCH_EVALUATION_SPANS_LOG_GROUP],
                "filterConfig": {"sessionIds": session_ids},
            }
        },
        "clientToken": token,
        "evaluationMetadata": {
            "sessionMetadata": [
                {
                    "sessionId": str(item["session_id"]),
                    "testScenarioId": str(item["test_scenario_id"]),
                    "groundTruth": _scenario_ground_truth(item),
                    "metadata": {
                        "version_id": version_id,
                        "deployment_id": deployment_id,
                        "policy_digest": str(policy["policy_digest"]),
                    },
                }
                for item in scenarios
            ]
        },
        "description": f"Production promotion evidence for version {version_id}",
        "tags": {
            "ManagedBy": "agentcore-workflow",
            "VersionId": version_id[:64],
            "PolicyDigest": str(policy["policy_digest"])[:64],
        },
        "outputConfig": {"cloudWatchConfig": {"resultDestination": "DEDICATED_LOG_GROUP"}},
    }
    kms_key = evaluation_kms_key_arn(
        kms_key_arn,
        target_region=identity.group("region"),
        target_account_id=identity.group("account"),
        required=kms_required,
    )
    if kms_key:
        request["kmsKeyArn"] = kms_key

    response = client.start_batch_evaluation(**request)
    batch_id = str(response.get("batchEvaluationId") or "")
    batch_arn = str(response.get("batchEvaluationArn") or "")
    if _BATCH_ID.fullmatch(batch_id) is None:
        raise PromotionEvidenceError("AgentCore returned no valid batch evaluation id")
    arn_match = _BATCH_ARN.fullmatch(batch_arn)
    if (
        arn_match is None
        or arn_match.group("batch") != batch_id
        or arn_match.group("region") != identity.group("region")
        or arn_match.group("account") != identity.group("account")
    ):
        raise PromotionEvidenceError("AgentCore returned a batch evaluation outside the runtime target")
    return {
        "batch_evaluation_id": batch_id,
        "batch_evaluation_arn": batch_arn,
        "status": str(response.get("status") or "PENDING"),
        "policy": policy,
        "scenario_count": len(scenarios),
        "request_digest": token,
    }


def get_batch_evaluation(client: Any, batch_evaluation_id: str) -> dict[str, Any]:
    """Read one batch evaluation through the sole SDK wrapper."""

    if _BATCH_ID.fullmatch(batch_evaluation_id or "") is None:
        raise PromotionEvidenceError("Invalid batch evaluation id")
    response = client.get_batch_evaluation(batchEvaluationId=batch_evaluation_id)
    if not isinstance(response, dict):
        raise PromotionEvidenceError("AgentCore returned an invalid batch evaluation result")
    return response


def summarize_batch_evaluation(result: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded, non-sensitive status view for the owning caller."""

    summary = result.get("evaluationResults") or {}
    evaluator_summaries = []
    for item in summary.get("evaluatorSummaries") or []:
        evaluator_summaries.append(
            {
                "evaluator_id": str(item.get("evaluatorId") or ""),
                "average_score": float((item.get("statistics") or {}).get("averageScore") or 0.0),
                "total_evaluated": int(item.get("totalEvaluated") or 0),
                "total_failed": int(item.get("totalFailed") or 0),
            }
        )
    return {
        "batch_evaluation_id": str(result.get("batchEvaluationId") or ""),
        "status": str(result.get("status") or ""),
        "created_at": _as_utc(result.get("createdAt"), "createdAt").isoformat(),
        "updated_at": _as_utc(result.get("updatedAt"), "updatedAt").isoformat(),
        "total_sessions": int(summary.get("totalNumberOfSessions") or 0),
        "completed_sessions": int(summary.get("numberOfSessionsCompleted") or 0),
        "failed_sessions": int(summary.get("numberOfSessionsFailed") or 0),
        "ignored_sessions": int(summary.get("numberOfSessionsIgnored") or 0),
        "evaluator_summaries": sorted(evaluator_summaries, key=lambda item: item["evaluator_id"]),
    }


def validate_batch_evaluation_for_promotion(
    result: dict[str, Any],
    *,
    runtime_name: str,
    agentcore_runtime_name: str,
    version_id: str,
    deployment_id: str,
    runtime_id: str,
    runtime_arn: str,
    version_created_at: str,
    policy: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a terminal AgentCore result and build immutable evidence."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    runtime_identity = _runtime_identity(runtime_arn, runtime_id)
    batch_id = str(result.get("batchEvaluationId") or "")
    batch_arn = str(result.get("batchEvaluationArn") or "")
    batch_identity = _BATCH_ARN.fullmatch(batch_arn)
    if (
        _BATCH_ID.fullmatch(batch_id) is None
        or batch_identity is None
        or batch_identity.group("batch") != batch_id
        or batch_identity.group("region") != runtime_identity.group("region")
        or batch_identity.group("account") != runtime_identity.group("account")
    ):
        raise PromotionEvidenceError("The batch evaluation is not bound to the runtime account and region")

    status = str(result.get("status") or "")
    if status in {"PENDING", "IN_PROGRESS", "STOPPING"}:
        raise PromotionEvidencePending(f"Batch evaluation is still {status}")
    if status != "COMPLETED":
        raise PromotionEvidenceError(f"Batch evaluation status {status or 'UNKNOWN'} cannot authorize promotion")
    if result.get("errorDetails"):
        raise PromotionEvidenceError("Batch evaluation completed with job-level errors")

    created_at = _as_utc(result.get("createdAt"), "createdAt")
    updated_at = _as_utc(result.get("updatedAt"), "updatedAt")
    try:
        version_created = datetime.fromisoformat(version_created_at.replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, AttributeError) as exc:
        raise PromotionEvidenceError("The version has no valid creation timestamp") from exc
    if created_at < version_created:
        raise PromotionEvidenceError("The batch evaluation predates this immutable version")
    if updated_at < created_at or updated_at > current:
        raise PromotionEvidenceError("The batch evaluation timestamps are inconsistent")
    expires_at_epoch = int(updated_at.timestamp()) + int(policy["max_evidence_age_seconds"])
    if expires_at_epoch <= int(current.timestamp()) + 60:
        raise PromotionEvidenceError("The batch evaluation evidence is stale")

    logs_source = ((result.get("dataSourceConfig") or {}).get("cloudWatchLogs")) or {}
    expected_service = agentcore_runtime_service_name(agentcore_runtime_name)
    if {str(value) for value in (logs_source.get("serviceNames") or [])} != {expected_service} or {
        str(value) for value in (logs_source.get("logGroupNames") or [])
    } != {_BATCH_EVALUATION_SPANS_LOG_GROUP}:
        raise PromotionEvidenceError("The batch evaluation source is not exactly this version's runtime")
    filter_config = logs_source.get("filterConfig") or {}
    session_ids = [str(value) for value in (filter_config.get("sessionIds") or [])]
    if len(session_ids) != len(set(session_ids)):
        raise PromotionEvidenceError("The batch evaluation source contains duplicate sessions")

    summary = result.get("evaluationResults") or {}
    total = int(summary.get("totalNumberOfSessions") or 0)
    completed = int(summary.get("numberOfSessionsCompleted") or 0)
    failed = int(summary.get("numberOfSessionsFailed") or 0)
    ignored = int(summary.get("numberOfSessionsIgnored") or 0)
    in_progress = int(summary.get("numberOfSessionsInProgress") or 0)
    if total < int(policy["minimum_sessions"]):
        raise PromotionEvidenceError(
            f"Only {total} sessions were evaluated; policy requires {policy['minimum_sessions']}"
        )
    if len(session_ids) != total:
        raise PromotionEvidenceError("The completed session count does not match the exact requested session set")
    if completed != total or in_progress:
        raise PromotionEvidenceError("Not every release scenario completed")
    if failed > int(policy["maximum_failed_sessions"]):
        raise PromotionEvidenceError(f"{failed} release scenario(s) failed")
    if ignored > int(policy["maximum_ignored_sessions"]):
        raise PromotionEvidenceError(f"{ignored} release scenario(s) were ignored")

    by_id: dict[str, dict[str, Any]] = {}
    for item in summary.get("evaluatorSummaries") or []:
        evaluator_id = str(item.get("evaluatorId") or "")
        if evaluator_id in by_id:
            raise PromotionEvidenceError(f"AgentCore returned duplicate summary for {evaluator_id}")
        by_id[evaluator_id] = item

    normalized_evaluators = []
    for evaluator_id, threshold in policy["evaluators"].items():
        item = by_id.get(evaluator_id)
        if item is None:
            raise PromotionEvidenceError(f"Required evaluator {evaluator_id} is missing")
        average = float((item.get("statistics") or {}).get("averageScore") or 0.0)
        total_evaluated = int(item.get("totalEvaluated") or 0)
        total_failed = int(item.get("totalFailed") or 0)
        if total_evaluated < int(policy["minimum_sessions"]):
            raise PromotionEvidenceError(f"Evaluator {evaluator_id} did not score every required session")
        if total_failed > int(policy["maximum_evaluator_failures"]):
            raise PromotionEvidenceError(f"Evaluator {evaluator_id} failed {total_failed} time(s)")
        if average < float(threshold):
            raise PromotionEvidenceError(
                f"Evaluator {evaluator_id} scored {average:.4f}; production requires at least {float(threshold):.4f}"
            )
        normalized_evaluators.append(
            {
                "evaluator_id": evaluator_id,
                "average_score": average,
                "minimum_score": float(threshold),
                "total_evaluated": total_evaluated,
                "total_failed": total_failed,
            }
        )

    evidence = {
        "schema_version": 1,
        "verdict": "pass",
        "runtime_name": runtime_name,
        "version_id": version_id,
        "deployment_id": deployment_id,
        "runtime_id": runtime_id,
        "batch_evaluation_id": batch_id,
        "batch_evaluation_arn": batch_arn,
        "policy_digest": policy["policy_digest"],
        "verified_at": current.isoformat(),
        "completed_at": updated_at.isoformat(),
        "expires_at_epoch": expires_at_epoch,
        "session_summary": {
            "total": total,
            "completed": completed,
            "failed": failed,
            "ignored": ignored,
            "session_ids_digest": _sha256(sorted(session_ids)),
        },
        "evaluator_summaries": sorted(normalized_evaluators, key=lambda item: item["evaluator_id"]),
    }
    return {**evidence, "evidence_digest": _sha256(evidence)}


def require_promotion_evidence(
    evidence: Any,
    *,
    runtime_name: str,
    version_id: str,
    deployment_id: str,
    runtime_id: str | None,
    policy: dict[str, Any],
    now_epoch: int | None = None,
) -> dict[str, Any]:
    """Validate stored evidence immediately before the atomic slot write."""

    policy = validate_frozen_promotion_policy(policy)
    if not isinstance(evidence, dict):
        raise PromotionEvidenceError("Production promotion requires verified batch-evaluation evidence")
    expected = {
        "schema_version": 1,
        "verdict": "pass",
        "runtime_name": runtime_name,
        "version_id": version_id,
        "deployment_id": deployment_id,
        "runtime_id": runtime_id,
        "policy_digest": policy["policy_digest"],
    }
    for key, value in expected.items():
        if evidence.get(key) != value:
            raise PromotionEvidenceError(f"Stored promotion evidence has a stale {key} binding")
    digest = str(evidence.get("evidence_digest") or "")
    unsigned = {key: value for key, value in evidence.items() if key != "evidence_digest"}
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or digest != _sha256(unsigned):
        raise PromotionEvidenceError("Stored promotion evidence digest is invalid")
    expires_at = evidence.get("expires_at_epoch")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        raise PromotionEvidenceError("Stored promotion evidence has no valid expiry")
    minimum = (now_epoch if now_epoch is not None else int(datetime.now(UTC).timestamp())) + 60
    if expires_at < minimum:
        raise PromotionEvidenceError("Stored promotion evidence is expired or too close to expiry")
    return evidence


def validate_frozen_promotion_policy(policy: Any) -> dict[str, Any]:
    """Verify that a persisted policy is complete and digest-bound."""

    if not isinstance(policy, dict):
        raise PromotionEvidenceError("This version has no frozen production evaluation policy")
    digest = str(policy.get("policy_digest") or "")
    unsigned = {key: value for key, value in policy.items() if key != "policy_digest"}
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or digest != _sha256(unsigned):
        raise PromotionEvidenceError("This version's frozen production evaluation policy is invalid")
    required = {
        "schema_version",
        "minimum_sessions",
        "maximum_failed_sessions",
        "maximum_ignored_sessions",
        "maximum_evaluator_failures",
        "max_evidence_age_seconds",
        "evaluators",
    }
    if set(unsigned) != required or unsigned.get("schema_version") != 1:
        raise PromotionEvidenceError("This version's frozen production evaluation policy is incomplete")
    if not isinstance(unsigned.get("evaluators"), dict) or not unsigned["evaluators"]:
        raise PromotionEvidenceError("This version's frozen production evaluation policy has no evaluators")
    return policy


def assert_batch_evaluation_targets_runtime(
    result: dict[str, Any],
    *,
    agentcore_runtime_name: str,
    runtime_id: str,
    runtime_arn: str,
) -> None:
    """Refuse a batch record not scoped exactly to this immutable runtime."""

    runtime_identity = _runtime_identity(runtime_arn, runtime_id)
    batch_id = str(result.get("batchEvaluationId") or "")
    batch_identity = _BATCH_ARN.fullmatch(str(result.get("batchEvaluationArn") or ""))
    if (
        _BATCH_ID.fullmatch(batch_id) is None
        or batch_identity is None
        or batch_identity.group("batch") != batch_id
        or batch_identity.group("region") != runtime_identity.group("region")
        or batch_identity.group("account") != runtime_identity.group("account")
    ):
        raise PromotionEvidenceError("The batch evaluation is not bound to the runtime account and region")
    logs_source = ((result.get("dataSourceConfig") or {}).get("cloudWatchLogs")) or {}
    expected_service = agentcore_runtime_service_name(agentcore_runtime_name)
    if {str(value) for value in (logs_source.get("serviceNames") or [])} != {expected_service} or {
        str(value) for value in (logs_source.get("logGroupNames") or [])
    } != {_BATCH_EVALUATION_SPANS_LOG_GROUP}:
        raise PromotionEvidenceError("The batch evaluation source is not exactly this version's runtime")
