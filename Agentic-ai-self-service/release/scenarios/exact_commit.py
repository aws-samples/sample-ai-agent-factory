"""Exact-commit evidence: the recorded run identity still matches the repository executing it.

``source-commit-tree-and-contract-digests-match`` is the ``exact_commit`` evidence kind of the
``release.integrity`` control. A sealed evidence run records its source identity (commit, project
tree, repository CI workflow blob), the contract fingerprints (bytes, sha256 and canonical document
digest of the controls, Region matrix and traceability documents, plus the CI workflow bytes), the
recorder's own identity, and a self digest. This scenario recomputes every one of them from the
repository and the files on disk -- through the recorder's own functions, so drift in *how* they
are computed is caught as well -- and fails closed on any mismatch or on a dirty project tree or
CI workflow.

Offline by contract: no AWS identity and no provenance (``provenance: false``). It writes ``report``
with every check, its expected and observed value, and the verdict, before raising on failure so a
failed run still leaves its evidence.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from _live import PROJECT_ROOT, ScenarioFailed, write_json

SCENARIO_ID = "source-commit-tree-and-contract-digests-match"
RECORDER_PATH = PROJECT_ROOT / "scripts" / "release_evidence.py"
SOURCE_KEYS = ("project_rel", "commit", "project_tree", "root_workflow", "root_workflow_blob")
CONTRACT_KEYS = ("controls", "regions", "traceability", "root_workflow")
RUNNER_KEYS = ("tool", "version", "sha256", "bytes")


def _load_recorder() -> ModuleType:
    """Import the evidence recorder from its real path so its PROJECT_ROOT-relative constants resolve."""
    spec = importlib.util.spec_from_file_location("acfpr_release_evidence", RECORDER_PATH)
    if spec is None or spec.loader is None:
        raise ScenarioFailed(f"evidence recorder is not importable: {RECORDER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_run(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run.json"
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioFailed(f"run.json is unreadable: {type(error).__name__}") from error
    if not isinstance(run, dict) or run.get("schema_version") != 2:
        raise ScenarioFailed("run.json is not a schema_version 2 evidence run")
    return run


def _check(checks: list[dict[str, Any]], name: str, expected: Any, observed: Any) -> None:
    checks.append({"check": name, "expected": expected, "observed": observed, "match": expected == observed})


def _guarded(checks: list[dict[str, Any]], name: str, recorder: ModuleType, compute: Any) -> Any:
    """Run one recorder computation; a recorder-side refusal becomes a failed check, never a crash."""
    try:
        return compute()
    except recorder.EvidenceError as error:
        _check(checks, name, "computed", f"EvidenceError: {error}")
        return None


def _verify(run: dict[str, Any], recorder: ModuleType) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    # 1. The run is internally sealed: its digest covers every other field.
    sealed = _guarded(
        checks,
        "run_digest.self_consistent",
        recorder,
        lambda: recorder._check_self_digest(run, "run_digest", "Evidence run") or True,
    )
    if sealed is not None:
        _check(checks, "run_digest.self_consistent", True, True)

    # 2. Source identity: HEAD commit, project tree and CI workflow blob are the recorded ones, and clean.
    current = _guarded(checks, "source.identity", recorder, recorder._source_identity) or {}
    expected_source = run.get("source") if isinstance(run.get("source"), dict) else {}
    for key in SOURCE_KEYS:
        _check(checks, f"source.{key}", expected_source.get(key), current.get(key))
    _check(checks, "source.clean", True, current.get("clean"))

    # 3. Contract fingerprints: bytes, sha256 and canonical document digest, recomputed from disk.
    bundle = _guarded(checks, "contracts.bundle", recorder, recorder._load_contract_bundle)
    fingerprints = bundle[3] if bundle else {}
    expected_contracts = run.get("contracts") if isinstance(run.get("contracts"), dict) else {}
    for key in CONTRACT_KEYS:
        _check(checks, f"contracts.{key}", expected_contracts.get(key), fingerprints.get(key))

    # 4. Runner identity: the recorder that sealed the run is byte-identical to the one on disk.
    runner = _guarded(checks, "runner.identity", recorder, recorder._runner_identity) or {}
    expected_runner = run.get("runner") if isinstance(run.get("runner"), dict) else {}
    for key in RUNNER_KEYS:
        _check(checks, f"runner.{key}", expected_runner.get(key), runner.get(key))

    mismatches = [item["check"] for item in checks if not item["match"]]
    return {
        "scenario": SCENARIO_ID,
        "run_id": run.get("run_id"),
        "commit": current.get("commit"),
        "branch": current.get("branch"),
        "checks": checks,
        # Contract shape pinned by backend/tests/test_release_evidence_recorder.py: the list of
        # mismatching checks (empty on success) and the identity actually observed in this tree.
        "mismatches": mismatches,
        "observed": {
            "commit": current.get("commit"),
            "project_tree": current.get("project_tree"),
            "root_workflow_blob": current.get("root_workflow_blob"),
            "clean": current.get("clean"),
        },
        "expected": {
            "commit": expected_source.get("commit"),
            "project_tree": expected_source.get("project_tree"),
            "root_workflow_blob": expected_source.get("root_workflow_blob"),
        },
        "passed": bool(checks) and not mismatches,
        "environment": {"python": runner.get("python"), "platform": runner.get("platform")},
    }


def source_commit_tree_and_contract_digests_match(args: argparse.Namespace, artifact_dir: Path) -> dict[str, Any]:
    recorder = _load_recorder()
    run = _load_run(Path(args.run_dir))
    report = _verify(run, recorder)
    write_json(artifact_dir / "report", report)
    if not report["passed"]:
        failed = [item["check"] for item in report["checks"] if not item["match"]]
        raise ScenarioFailed(f"exact-commit evidence does not match the repository: {failed}")
    return report
