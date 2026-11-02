"""``lifecycle.rollback`` -- production rolls back only to the exact prior version, and serves it.

Two scenario ids, one live journey (ported from the audited ``live-rollback-forward``
harness, now against the tracked integration helpers):

1. deploy version 1 of a runtime, promote it through the real AgentCore Batch Evaluation
   gate, invoke production and see version 1 answer;
2. deploy version 2, promote, invoke and see version 2; ``previous_production_version_id``
   is version 1;
3. ``POST /api/runtimes/{name}/rollback/{version1}`` -- an *arbitrary* target -- is refused
   with 404 and production is unchanged (``arbitrary-version-rollback-denied``);
4. ``POST /api/runtimes/{name}/rollback`` promotes exactly version 1 again and production
   serves it (``production-serves-exact-previous-version``);
5. forward promotion of version 2 restores it, proving the rollback left a recoverable state.

The journey runs once per deployment (see ``_live.family_journey``); each scenario checks its
own claim in the stored outcome. Budgets, deployments, the Transaction Search lease and the
synthetic user are all cleaned up in every exit path, and the synthetic user carries
``g-admins-cost`` only because budget authorization requires it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from _live import (
    BACKEND,
    REQUIRED_USER_GROUPS,
    BearerSession,
    ObservedStack,
    RunBinding,
    ScenarioFailed,
    boto3_session,
    bound_deployment,
    family_journey,
    redact,
    with_synthetic_user,
    write_json,
)

ROLLBACK_GROUPS = REQUIRED_USER_GROUPS | {"g-admins-cost"}
FAMILY = "runtime-slot-rollback"


def _runtime_config(runtime_name: str, marker: str) -> dict[str, Any]:
    return {
        "name": runtime_name,
        "entrypoint": "agent.py",
        "framework": "strands_agents",
        "model": {"modelId": "us.anthropic.claude-sonnet-5"},
        "systemPrompt": (
            "Answer ordinary factual release-certification questions concisely. "
            f"This immutable version marker is {marker}. Do not call external tools."
        ),
        "deploymentType": "direct_code_deploy",
        "pythonRuntime": "PYTHON_3_13",
        "protocol": "HTTP",
        "idleTimeout": 300,
        "maxLifetime": 3600,
        "enableOtel": False,
        "multiAgentPattern": "none",
    }


POLL_SECONDS = 20


def _degrade(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same real sessions judged against ground truth that contradicts what the agent said."""
    degraded = []
    for scenario in scenarios:
        copy = json.loads(json.dumps(scenario))
        copy["testScenarioId"] = copy["testScenarioId"].replace("trigger-release", "degraded-release")
        copy["assertions"] = ["The response is written entirely in French and refuses to answer the question."]
        for turn in copy.get("turns", []):
            turn["expectedResponse"] = "Je refuse de répondre."
        degraded.append(copy)
    return degraded


def _promotion_gate(
    session: Any,
    logs: Any,
    runtime_name: str,
    status: dict[str, Any],
    *,
    adversarial_against: dict[str, Any] | None,
) -> dict[str, Any]:
    """The customer promotion gate, step by step, recording what the API returned at each step."""
    import time

    from tests.integration.test_trigger_delivery_matrix import _release_scenarios, _wait_for_evaluation_sessions

    base = session.base_url
    version_id, runtime_id = status["version_id"], status["runtime_id"]
    record: dict[str, Any] = {"version_id": version_id, "denials": {}}

    def slots() -> dict[str, Any]:
        response = session.get(f"{base}/api/runtimes/{runtime_name}/slots", timeout=60)
        response.raise_for_status()
        return response.json()

    def production_unchanged(expected: str | None) -> bool:
        return slots().get("production_version_id") == expected

    deadline = time.monotonic() + 600
    while slots().get("staging_version_id") != version_id:
        if time.monotonic() > deadline:
            raise ScenarioFailed(f"version {version_id} never reached the staging slot")
        time.sleep(10)
    versions = session.get(f"{base}/api/runtimes/{runtime_name}/versions", timeout=60)
    versions.raise_for_status()
    version_record = next(item for item in versions.json() if item.get("version_id") == version_id)
    agentcore_runtime_name = version_record["agentcore_runtime_name"]
    evidence = session.get(f"{base}/api/runtimes/{runtime_name}/versions/{version_id}/promotion-evidence", timeout=60)
    evidence.raise_for_status()
    policy = evidence.json().get("promotion_policy") or {}
    if not evidence.json().get("production_gate_required"):
        raise ScenarioFailed("the production gate is not required; the policy under test is not in force")
    minimum_sessions = int(policy.get("minimum_sessions") or 0)
    record["minimum_sessions"] = minimum_sessions
    production_before = slots().get("production_version_id")

    if adversarial_against is not None:
        stale_batch = adversarial_against["batch_evaluation_id"]
        stale = session.post(
            f"{base}/api/runtimes/{runtime_name}/versions/{version_id}/promotion-evaluation/attest",
            json={"batchEvaluationId": stale_batch},
            timeout=90,
        )
        record["denials"]["cross_version_stale_evidence"] = {
            "status": stale.status_code,
            "detail": stale.text[:300],
            "production_unchanged": production_unchanged(production_before),
        }
        if stale.status_code != 409:
            raise ScenarioFailed(f"stale cross-version evidence attested with {stale.status_code}, expected 409")
        unattested = session.post(
            f"{base}/api/runtimes/{runtime_name}/versions/{version_id}/promote", json={"slot": "production"}, timeout=90
        )
        record["denials"]["promote_without_attestation"] = {
            "status": unattested.status_code,
            "detail": unattested.text[:300],
            "production_unchanged": production_unchanged(production_before),
        }
        if unattested.status_code != 409:
            raise ScenarioFailed(f"promotion without attestation returned {unattested.status_code}, expected 409")

    scenarios, session_ids = _release_scenarios(
        session, runtime_name=runtime_name, version_id=version_id, count=minimum_sessions
    )
    _wait_for_evaluation_sessions(logs, agentcore_runtime_name=agentcore_runtime_name, session_ids=session_ids)

    def run_batch(payload: list[dict[str, Any]], label: str) -> str:
        start = session.post(
            f"{base}/api/runtimes/{runtime_name}/versions/{version_id}/promotion-evaluation/start",
            json={"scenarios": payload},
            timeout=90,
        )
        if start.status_code != 200:
            raise ScenarioFailed(f"starting the {label} evaluation returned {start.status_code}: {start.text[:300]}")
        batch_id = start.json()["batch_evaluation_id"]
        # Real AgentCore batch evaluations in acf-production run longer than the original 30 min
        # ceiling (measured 2026-10-29: the degraded eval was still IN_PROGRESS at 1800s). Raise to
        # 60 min so a legitimately-slow terminal state is observed; a still-IN_PROGRESS eval at 3600s
        # indicates a genuine service stall, not a too-short harness budget.
        poll_deadline = time.monotonic() + 3600
        last = ""
        while time.monotonic() < poll_deadline:
            batch = session.get(
                f"{base}/api/runtimes/{runtime_name}/versions/{version_id}/promotion-evaluation/{batch_id}", timeout=60
            )
            batch.raise_for_status()
            last = str(batch.json().get("status") or "")
            if last == "COMPLETED":
                return batch_id
            if last not in {"PENDING", "IN_PROGRESS", "STOPPING"}:
                raise ScenarioFailed(f"the {label} evaluation ended {last}")
            time.sleep(POLL_SECONDS)
        raise ScenarioFailed(f"the {label} evaluation did not complete in time (last {last})")

    if adversarial_against is not None:
        degraded_batch = run_batch(_degrade(scenarios), "degraded")
        degraded = session.post(
            f"{base}/api/runtimes/{runtime_name}/versions/{version_id}/promotion-evaluation/attest",
            json={"batchEvaluationId": degraded_batch},
            timeout=90,
        )
        record["denials"]["degraded_result"] = {
            "batch_evaluation_id": degraded_batch,
            "status": degraded.status_code,
            "detail": degraded.text[:300],
            "production_unchanged": production_unchanged(production_before),
        }
        if degraded.status_code != 409:
            raise ScenarioFailed(f"a degraded batch result was attested with {degraded.status_code}, expected 409")

    batch_id = run_batch(scenarios, "genuine")
    attest = session.post(
        f"{base}/api/runtimes/{runtime_name}/versions/{version_id}/promotion-evaluation/attest",
        json={"batchEvaluationId": batch_id},
        timeout=90,
    )
    if attest.status_code != 200:
        raise ScenarioFailed(f"attesting the genuine evaluation returned {attest.status_code}: {attest.text[:300]}")
    verdict = (attest.json().get("promotion_evidence") or {}).get("verdict")
    record.update({"batch_evaluation_id": batch_id, "attest_status": 200, "verdict": verdict})
    if verdict != "pass":
        raise ScenarioFailed(f"genuine evaluation verdict is {verdict!r}")
    promote = session.post(
        f"{base}/api/runtimes/{runtime_name}/versions/{version_id}/promote", json={"slot": "production"}, timeout=90
    )
    if promote.status_code != 200 or promote.json().get("promoted_version_id") != version_id:
        raise ScenarioFailed(f"promotion returned {promote.status_code}: {promote.text[:300]}")
    record["promoted"] = True
    record["runtime_id"] = runtime_id
    return record


def _journey(binding: RunBinding, observed: ObservedStack, artifacts: Path) -> dict[str, Any]:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    import requests
    from tests.integration.conftest import DeploymentCleanupTracker, RuntimeBudgetTracker, wait_for_deployment_terminal
    from tests.integration.transaction_search import TransactionSearchLease

    base = observed.cloudfront_url
    session_aws = boto3_session(binding.region)
    logs = session_aws.client("logs")
    xray = session_aws.client("xray")
    sts = session_aws.client("sts")
    previous_env = {k: os.environ.get(k) for k in ("AWS_REGION", "AWS_DEFAULT_REGION")}
    os.environ.update(
        {
            "AWS_REGION": binding.region,
            "AWS_DEFAULT_REGION": binding.region,
            "INTEGRATION_EVALUATION_LOG_TIMEOUT_SECONDS": "900",
            "INTEGRATION_EVALUATION_INDEX_SETTLE_SECONDS": "180",
            "INTEGRATION_PROMOTION_TIMEOUT_SECONDS": "1800",
        }
    )
    outcome: dict[str, Any] = {"region": binding.region, "customer_path": base}
    cleanup_errors: list[str] = []
    try:
        with with_synthetic_user(observed, label="rollback", groups=ROLLBACK_GROUPS) as user:
            session = BearerSession(user, base)
            session.headers["Content-Type"] = "application/json"
            tracker = DeploymentCleanupTracker(session, logs_client=logs)
            budgets = RuntimeBudgetTracker(session)
            runtime_name = f"it_rollback_{uuid.uuid4().hex[:12]}"
            outcome["runtime_name"] = runtime_name
            try:
                budgets.authorize(runtime_name)
                lease = TransactionSearchLease(
                    account_id=binding.account_id,
                    region=binding.region,
                    manage=True,
                    state_path=artifacts / "transaction-search-state.json",
                    xray_client=xray,
                    logs_client=logs,
                    sts_client=sts,
                )
                with lease:

                    def deploy(marker: str):
                        node_id = f"it-rollback-{marker}-{uuid.uuid4().hex[:12]}"
                        try:
                            response = session.post(
                                f"{base}/api/deploy",
                                json={"nodeId": node_id, "config": _runtime_config(runtime_name, marker)},
                                timeout=90,
                            )
                        except requests.RequestException:
                            tracker.recover_by_node_id(node_id)
                            raise
                        if response.status_code != 202:
                            raise ScenarioFailed(
                                f"deploy {marker} returned {response.status_code}: {response.text[:300]}"
                            )
                        deployment_id = response.json()["deploymentId"]
                        record = tracker.track(deployment_id)
                        status = wait_for_deployment_terminal(session, deployment_id, timeout=20 * 60)
                        record.last_status = status
                        if status.get("status") != "succeeded":
                            raise ScenarioFailed(f"deploy {marker} ended {status.get('status')}")
                        tracker.bind_runtime(record, status["runtime_id"])
                        return status

                    def slots() -> dict[str, Any]:
                        response = session.get(f"{base}/api/runtimes/{runtime_name}/slots", timeout=60)
                        response.raise_for_status()
                        return response.json()

                    def promote(status: dict[str, Any], *, adversarial_against: dict[str, Any] | None = None) -> str:
                        """Promote through the real Batch Evaluation gate, recording the batch id and verdict.

                        With ``adversarial_against`` (the previous version's gate record), first prove the
                        denials: that version's batch id attested against THIS version (cross-version stale
                        evidence), promotion without any attestation, and a REAL batch run on deliberately
                        degraded ground truth -- each an exact 409 with production unchanged.
                        """
                        version_id = status["version_id"]
                        gate = _promotion_gate(
                            session, logs, runtime_name, status, adversarial_against=adversarial_against
                        )
                        outcome.setdefault("promotion_gates", []).append(gate)
                        if slots().get("production_version_id") != version_id:
                            raise ScenarioFailed("promotion did not make the version production")
                        return version_id

                    def invoke_expect(version_id: str, label: str) -> None:
                        response = session.post(
                            f"{base}/api/runtimes/{runtime_name}/invoke",
                            json={
                                "input": "What is 3 plus 4? Answer concisely.",
                                "sessionId": f"rollback-{uuid.uuid4()}",
                            },
                            timeout=180,
                        )
                        if response.status_code != 200:
                            raise ScenarioFailed(
                                f"invoke ({label}) returned {response.status_code}: {response.text[:300]}"
                            )
                        body = response.json()
                        if body.get("success") is not True or body.get("slot") != "production":
                            raise ScenarioFailed(f"invoke ({label}) did not hit production: {str(body)[:300]}")
                        if body.get("versionId") != version_id or not str(body.get("response") or "").strip():
                            raise ScenarioFailed(
                                f"invoke ({label}) served {body.get('versionId')}, expected {version_id}"
                            )

                    first_status = deploy("first")
                    first_version = promote(first_status)
                    invoke_expect(first_version, "first")
                    second_status = deploy("second")
                    second_version = promote(second_status, adversarial_against=outcome["promotion_gates"][0])
                    if second_version == first_version:
                        raise ScenarioFailed("second deployment reused the first version id")
                    if slots().get("previous_production_version_id") != first_version:
                        raise ScenarioFailed(
                            "previous_production_version_id is not version 1 after promoting version 2"
                        )
                    invoke_expect(second_version, "second")

                    arbitrary = session.post(f"{base}/api/runtimes/{runtime_name}/rollback/{first_version}", timeout=60)
                    if arbitrary.status_code != 404:
                        raise ScenarioFailed(
                            f"arbitrary-version rollback returned {arbitrary.status_code}, expected 404"
                        )
                    if slots().get("production_version_id") != second_version:
                        raise ScenarioFailed("arbitrary-version rollback attempt moved production")
                    outcome["arbitrary_version_rollback"] = {
                        "status": arbitrary.status_code,
                        "production_unchanged": True,
                    }

                    rolled = session.post(f"{base}/api/runtimes/{runtime_name}/rollback", timeout=90)
                    if rolled.status_code != 200 or rolled.json().get("promoted_version_id") != first_version:
                        raise ScenarioFailed(f"rollback returned {rolled.status_code}: {rolled.text[:300]}")
                    if slots().get("production_version_id") != first_version:
                        raise ScenarioFailed("rollback did not make version 1 production")
                    invoke_expect(first_version, "after-rollback")
                    outcome["rollback"] = {
                        "status": 200,
                        "promoted_version_id": first_version,
                        "served_exact_previous": True,
                    }

                    forward = session.post(
                        f"{base}/api/runtimes/{runtime_name}/versions/{second_version}/promote",
                        json={"slot": "production"},
                        timeout=90,
                    )
                    if forward.status_code != 200 or forward.json().get("promoted_version_id") != second_version:
                        raise ScenarioFailed(f"forward promotion returned {forward.status_code}: {forward.text[:300]}")
                    invoke_expect(second_version, "forward")
                    outcome["forward_recovery"] = {"status": 200, "promoted_version_id": second_version, "served": True}
                    outcome["versions"] = {"first": first_version, "second": second_version}
            finally:
                try:
                    budgets.cleanup()
                except Exception as exc:  # noqa: BLE001
                    cleanup_errors.append("budget cleanup: " + redact(f"{type(exc).__name__}: {exc}", *user.secrets()))
                for record in reversed(tracker.records):
                    try:
                        tracker.cleanup(record)
                    except Exception as exc:  # noqa: BLE001
                        cleanup_errors.append(
                            "deployment cleanup: " + redact(f"{type(exc).__name__}: {exc}", *user.secrets())
                        )
                session.close()
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    outcome["cleanup_errors"] = cleanup_errors
    write_json(artifacts / "journey-outcome.json", outcome)
    if cleanup_errors:
        raise ScenarioFailed("rollback journey left residue: " + "; ".join(cleanup_errors)[:800])
    return outcome


def _scenario(args: argparse.Namespace, artifact_dir: Path, claim: str) -> dict:
    binding, observed, comparison = bound_deployment(args, artifact_dir)
    journey = family_journey(
        binding,
        observed,
        family=FAMILY,
        run=lambda artifacts: _journey(binding, observed, artifacts),
        artifact_dir=artifact_dir,
    )
    outcome = journey["outcome"]
    if claim == "arbitrary-version-rollback-denied":
        facet = outcome.get("arbitrary_version_rollback") or {}
        holds = facet.get("status") == 404 and facet.get("production_unchanged") is True
    elif claim == "production-serves-exact-previous-version":
        facet = outcome.get("rollback") or {}
        holds = facet.get("served_exact_previous") is True and facet.get("promoted_version_id") == (
            outcome.get("versions") or {}
        ).get("first")
    elif claim == "batch-evaluation-pass-and-production-promotion":
        gates = outcome.get("promotion_gates") or []
        facet = {"gates": gates}
        holds = len(gates) == 2 and all(
            g.get("verdict") == "pass" and g.get("promoted") is True and g.get("batch_evaluation_id") for g in gates
        )
    elif claim == "forged-or-stale-evidence-denied":
        gates = outcome.get("promotion_gates") or []
        denials = (gates[1].get("denials") if len(gates) == 2 else None) or {}
        facet = {k: denials.get(k) for k in ("cross_version_stale_evidence", "promote_without_attestation")}
        holds = all(
            (facet.get(k) or {}).get("status") == 409 and (facet.get(k) or {}).get("production_unchanged") is True
            for k in ("cross_version_stale_evidence", "promote_without_attestation")
        )
    elif claim == "degraded-result-denied":
        gates = outcome.get("promotion_gates") or []
        facet = ((gates[1].get("denials") if len(gates) == 2 else None) or {}).get("degraded_result") or {}
        holds = (
            facet.get("status") == 409
            and facet.get("production_unchanged") is True
            and bool(facet.get("batch_evaluation_id"))
        )
    else:
        raise ScenarioFailed(f"unknown rollback claim {claim}")
    report = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "deployment_binding": comparison,
        "journey": {k: v for k, v in journey.items() if k != "outcome"},
        "claim": claim,
        "claim_holds": holds,
        "facet": facet,
        "versions": outcome.get("versions"),
    }
    write_json(artifact_dir / "report", report)
    if not holds:
        raise ScenarioFailed(f"{claim} does not hold in the journey outcome")
    return report


def arbitrary_version_rollback_denied(args: argparse.Namespace, artifact_dir: Path) -> dict:
    return _scenario(args, artifact_dir, "arbitrary-version-rollback-denied")


def production_serves_exact_previous_version(args: argparse.Namespace, artifact_dir: Path) -> dict:
    return _scenario(args, artifact_dir, "production-serves-exact-previous-version")


def batch_evaluation_pass_and_production_promotion(args: argparse.Namespace, artifact_dir: Path) -> dict:
    """``release.evaluation_promotion``: both versions reached production only through a real,
    COMPLETED Batch Evaluation whose attested verdict was pass."""
    return _scenario(args, artifact_dir, "batch-evaluation-pass-and-production-promotion")


def forged_or_stale_evidence_denied(args: argparse.Namespace, artifact_dir: Path) -> dict:
    """``release.evaluation_promotion``: version 1's real batch attested against version 2, and a
    promotion with no attestation at all, are each refused 409 with production unmoved."""
    return _scenario(args, artifact_dir, "forged-or-stale-evidence-denied")


def degraded_result_denied(args: argparse.Namespace, artifact_dir: Path) -> dict:
    """``release.evaluation_promotion``: a REAL batch run on contradictory ground truth completes and
    is refused at attestation (409) with production unmoved."""
    return _scenario(args, artifact_dir, "degraded-result-denied")
