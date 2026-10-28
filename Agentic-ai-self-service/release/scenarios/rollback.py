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
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from _live import (
    BACKEND,
    REQUIRED_USER_GROUPS,
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


def _journey(binding: RunBinding, observed: ObservedStack, artifacts: Path) -> dict[str, Any]:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    import requests
    from tests.integration.conftest import DeploymentCleanupTracker, RuntimeBudgetTracker, wait_for_deployment_terminal
    from tests.integration.test_trigger_delivery_matrix import _promote_staging_version_for_triggers
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
            session = requests.Session()
            session.headers.update(
                {
                    "Authorization": f"Bearer {user.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            )
            session.base_url = base  # type: ignore[attr-defined]
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

                    def promote(status: dict[str, Any]) -> str:
                        version_id = status["version_id"]
                        _promote_staging_version_for_triggers(session, logs, runtime_name=runtime_name, status=status)
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
                    second_version = promote(second_status)
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
