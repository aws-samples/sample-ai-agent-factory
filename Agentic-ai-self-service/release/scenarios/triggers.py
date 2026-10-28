"""``trigger.webhook_idempotency`` -- facets of the Region's trigger-matrix journey.

The tracked ``tests/integration/test_trigger_delivery_matrix.py`` is one long live test that,
among the four real trigger sources, fires a signed webhook and then proves: the signed delivery
is accepted exactly once (202, ``duplicate: false``); an identical replay is accepted as the
idempotent duplicate (202, ``duplicate: true``); the same delivery id with a different body is
refused (409, exact detail); and eight concurrent identical deliveries of a fresh id yield exactly
one acceptance and seven duplicates with no conflict. Delivery rows carry no claim token once the
worker has completed them.

The regional core scenario runs that test as its ``triggers`` phase and caches the result as the
``core-triggers`` journey for the deployment. These four scenario ids re-attach that journey's
JUnit and log and verify the single test case passed; they block, never pass, when the journey has
not run on this exact deployment.
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from _live import JOURNEY_CACHE_ROOT, ScenarioBlocked, ScenarioFailed, bound_deployment, family_journey, write_json

MATRIX_TEST = "test_all_four_trigger_sources_invoke_and_cleanup"
FACETS = {
    "signed-delivery-executes-once": "a signed delivery is accepted once with duplicate=false and reaches a completed AgentCore delivery",
    "identical-replay-is-idempotent": "an identical replay of the same delivery id is accepted as duplicate=true and enqueues nothing new",
    "changed-body-same-id-conflicts": "the same delivery id with a different body is refused 409 'already used for a different payload'",
    "concurrent-claim-conflicts-and-recovers": "eight concurrent identical deliveries yield exactly one acceptance and seven duplicates; completed rows carry no claim token",
}


def _junit_case_passed(junit_path: Path, name: str) -> tuple[bool, str]:
    root = ET.parse(junit_path).getroot()
    for case in root.iter("testcase"):
        if case.get("name", "") == name or case.get("name", "").startswith(name):
            for child in case:
                if child.tag in ("failure", "error", "skipped"):
                    return False, f"{child.tag}: {(child.get('message') or '')[:300]}"
            return True, "passed"
    return False, f"{name} not present in {junit_path.name}"


def _scenario(args: argparse.Namespace, artifact_dir: Path, claim: str) -> dict:
    binding, observed, comparison = bound_deployment(args, artifact_dir)
    cache = JOURNEY_CACHE_ROOT / binding.commit / binding.region / "core-triggers"
    if not (cache / "journey.json").is_file():
        raise ScenarioBlocked(f"the core-triggers journey has not run for {binding.commit[:12]} in {binding.region}")

    def _never_runs(_: Path) -> dict[str, Any]:
        raise ScenarioBlocked("the trigger matrix runs only as the regional core scenario's triggers phase")

    journey = family_journey(binding, observed, family="core-triggers", run=_never_runs, artifact_dir=artifact_dir)
    junit = artifact_dir / "journey-core-triggers" / "junit-triggers.xml"
    if not junit.is_file():
        raise ScenarioFailed("the core-triggers journey carries no JUnit report")
    passed, detail = _junit_case_passed(junit, MATRIX_TEST)
    report = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "deployment_binding": comparison,
        "journey": {k: v for k, v in journey.items() if k != "outcome"},
        "claim": claim,
        "proved_by": f"{MATRIX_TEST}: {FACETS[claim]}",
        "junit_case": detail,
        "claim_holds": passed,
    }
    write_json(artifact_dir / "report", report)
    if not passed:
        raise ScenarioFailed(f"{claim}: {detail}")
    return report


def signed_delivery_executes_once(args, artifact_dir):
    return _scenario(args, artifact_dir, "signed-delivery-executes-once")


def identical_replay_is_idempotent(args, artifact_dir):
    return _scenario(args, artifact_dir, "identical-replay-is-idempotent")


def changed_body_same_id_conflicts(args, artifact_dir):
    return _scenario(args, artifact_dir, "changed-body-same-id-conflicts")


def concurrent_claim_conflicts_and_recovers(args, artifact_dir):
    return _scenario(args, artifact_dir, "concurrent-claim-conflicts-and-recovers")
