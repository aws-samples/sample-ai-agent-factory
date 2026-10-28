#!/usr/bin/env python3 -I
"""Scenario dispatcher for release evidence.

`scripts/release_evidence.py run-scenario` executes exactly this file for every
scenario the traceability contract names. The recorder decides the status from
what happens here, never from a label:

* exit 0 and every required artifact written under ``--artifact-dir`` -> pass
* exit 3 -> ``blocked`` (not implemented here, or a prerequisite is missing)
* any other exit, a timeout, or a missing required artifact -> ``fail``

A live scenario must also write ``provenance`` (JSON) naming the AWS account,
Region, stack, deployed commit, and deployed project tree it actually observed;
the recorder refuses provenance that disagrees with the run's bound account,
the requested Region, or the candidate commit/tree.

Unknown or unimplemented scenarios are BLOCKED, never faked. Registering an
implementation is a reviewed source change: the contract fingerprints and the
project tree change, so an in-progress evidence run is invalidated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

BLOCKED = 3
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# `python -I` (isolated mode) does not put this script's directory on sys.path; the sibling
# modules here are tracked project code, so add exactly this directory and nothing else.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
# Never write bytecode into the tree being certified: a __pycache__ would make it dirty.
sys.dont_write_bytecode = True
ROOT_WORKFLOW_RELATIVE = ".github/workflows/agentic-ai-self-service-ci.yml"
CONTRACTS = (
    "release/production-readiness-controls.json",
    "release/agentcore-regions.json",
    "release/production-readiness-traceability.json",
)


from _live import ScenarioBlocked, ScenarioFailed  # noqa: E402  -- shared with every family module


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-P", *args], cwd=repo, check=False, capture_output=True, text=True, timeout=60)
    if completed.returncode != 0:
        raise ScenarioFailed(f"git {' '.join(args[:2])} failed")
    return completed.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_commit_tree_and_contract_digests_match(args: argparse.Namespace, artifact_dir: Path) -> dict:
    """exact_commit: re-derive the candidate identity INDEPENDENTLY and compare it to the run.

    The recorder bound the run to a commit, project tree, CI-workflow blob, and contract
    file digests when it started. This scenario recomputes all of them from the working
    repository with its own git/hash calls and fails if any disagree -- so a run whose
    recorder was fed a stale or edited identity cannot pass this gate.
    """
    run = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
    source = run["source"]
    repo = Path(_git(PROJECT_ROOT, "rev-parse", "--show-toplevel")).resolve()
    project_rel = PROJECT_ROOT.resolve().relative_to(repo).as_posix()
    commit = _git(repo, "rev-parse", "HEAD")
    observed = {
        "commit": commit,
        "project_tree": _git(repo, "rev-parse", f"{commit}:{project_rel}"),
        "root_workflow_blob": _git(repo, "rev-parse", f"{commit}:{ROOT_WORKFLOW_RELATIVE}"),
        "dirty": _git(
            repo, "status", "--porcelain=v1", "--untracked-files=all", "--", project_rel, ROOT_WORKFLOW_RELATIVE
        ),
        "contracts": {name: _sha256(PROJECT_ROOT / name) for name in CONTRACTS},
        "root_workflow_sha256": _sha256(repo / ROOT_WORKFLOW_RELATIVE),
    }
    expected = {
        "commit": source["commit"],
        "project_tree": source["project_tree"],
        "root_workflow_blob": source["root_workflow_blob"],
        "contracts": {
            "release/production-readiness-controls.json": run["contracts"]["controls"]["sha256"],
            "release/agentcore-regions.json": run["contracts"]["regions"]["sha256"],
            "release/production-readiness-traceability.json": run["contracts"]["traceability"]["sha256"],
        },
        "root_workflow_sha256": run["contracts"]["root_workflow"]["sha256"],
    }
    mismatches = [
        key
        for key in ("commit", "project_tree", "root_workflow_blob", "contracts", "root_workflow_sha256")
        if observed[key] != expected[key]
    ]
    if observed["dirty"]:
        mismatches.append("working_tree_dirty")
    report = {"scenario": args.scenario_id, "observed": observed, "expected": expected, "mismatches": mismatches}
    _write_json(artifact_dir / "report", report)
    if mismatches:
        raise ScenarioFailed(f"candidate identity mismatch: {mismatches}")
    return report


SCENARIOS: dict[str, Callable[[argparse.Namespace, Path], dict]] = {
    "source-commit-tree-and-contract-digests-match": source_commit_tree_and_contract_digests_match,
}


def _register_live_families() -> None:
    """Live families import boto3 and the frontend toolchain; load them only when dispatching."""
    import regional_core

    SCENARIOS["core-runtime-gateway-identity-policy-evaluation"] = (
        regional_core.core_runtime_gateway_identity_policy_evaluation
    )


_register_live_families()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario_id")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--region")
    parser.add_argument("--account-id")
    args = parser.parse_args(argv)
    implementation = SCENARIOS.get(args.scenario_id)
    if implementation is None:
        print(f"scenario {args.scenario_id} has no reviewed implementation; recording blocked", file=sys.stderr)
        return BLOCKED
    try:
        implementation(args, args.artifact_dir)
    except ScenarioBlocked as exc:
        print(f"scenario {args.scenario_id} blocked: {exc}", file=sys.stderr)
        return BLOCKED
    except ScenarioFailed as exc:
        print(f"scenario {args.scenario_id} failed: {exc}", file=sys.stderr)
        return 1
    print(f"scenario {args.scenario_id} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
