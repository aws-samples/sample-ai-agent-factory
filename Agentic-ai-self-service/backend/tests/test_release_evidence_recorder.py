"""Release evidence is append-only, exact-commit-bound, and region-complete."""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "release_evidence.py"
CONTROLS_PATH = PROJECT_ROOT / "release" / "production-readiness-controls.json"
REGIONS_PATH = PROJECT_ROOT / "release" / "emea-agentcore-regions.json"
TRACEABILITY_PATH = PROJECT_ROOT / "release" / "production-readiness-traceability.json"
TEST_ACCOUNT_ID = "123456789012"

SPEC = importlib.util.spec_from_file_location("release_evidence", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
release_evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_evidence)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-P", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


@pytest.fixture
def evidence_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    repo = tmp_path / "source"
    project = repo / "Agentic-ai-self-service"
    release_dir = project / "release"
    scripts_dir = project / "scripts"
    release_dir.mkdir(parents=True)
    scripts_dir.mkdir()
    shutil.copy2(CONTROLS_PATH, release_dir / CONTROLS_PATH.name)
    shutil.copy2(REGIONS_PATH, release_dir / REGIONS_PATH.name)
    shutil.copy2(TRACEABILITY_PATH, release_dir / TRACEABILITY_PATH.name)
    shutil.copy2(SCRIPT_PATH, scripts_dir / SCRIPT_PATH.name)
    (project / ".gitignore").write_text("build/\n", encoding="utf-8")
    (project / "README.md").write_text("# Evidence fixture\n", encoding="utf-8")

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Evidence Test")
    _git(repo, "config", "user.email", "evidence@example.invalid")
    _git(repo, "add", "Agentic-ai-self-service")
    _git(repo, "commit", "-m", "test: create evidence fixture")

    fixture_controls = release_dir / CONTROLS_PATH.name
    fixture_regions = release_dir / REGIONS_PATH.name
    fixture_traceability = release_dir / TRACEABILITY_PATH.name
    monkeypatch.setattr(release_evidence, "PROJECT_ROOT", project)
    monkeypatch.setattr(release_evidence, "CONTROLS_PATH", fixture_controls)
    monkeypatch.setattr(release_evidence, "REGIONS_PATH", fixture_regions)
    monkeypatch.setattr(release_evidence, "TRACEABILITY_PATH", fixture_traceability)
    monkeypatch.setattr(
        release_evidence,
        "DEFAULT_OUTPUT_ROOT",
        tmp_path / "default-evidence",
    )
    return {
        "repo": repo,
        "project": project,
        "controls_path": fixture_controls,
        "regions_path": fixture_regions,
        "traceability_path": fixture_traceability,
        "script_path": scripts_dir / SCRIPT_PATH.name,
        "output_root": tmp_path / "evidence",
        "tmp_path": tmp_path,
    }


def _event_region(kind: str) -> str | None:
    if kind in release_evidence._REGION_REQUIRED_KINDS:
        return "eu-west-1"
    return None


def _record_complete_run(run_dir: Path) -> list[dict]:
    controls = json.loads(release_evidence.CONTROLS_PATH.read_text(encoding="utf-8"))
    regions = json.loads(release_evidence.REGIONS_PATH.read_text(encoding="utf-8"))
    traceability = json.loads(release_evidence.TRACEABILITY_PATH.read_text(encoding="utf-8"))
    traced_by_id = {item["id"]: item["evidence"] for item in traceability["controls"]}
    events = []
    for control in controls["controls"]:
        control_id = control["id"]
        traced = traced_by_id[control_id]
        for kind in control["required_kinds"]:
            if kind.startswith("offline_"):
                for suite_id in sorted({item["suite"] for item in traced[kind]}):
                    events.append(
                        release_evidence.record_event(
                            run_dir,
                            control_id=control_id,
                            kind=kind,
                            status="pass",
                            suite_id=suite_id,
                            details={"test_case": f"{control_id}:{kind}:{suite_id}"},
                        )
                    )
            else:
                for scenario_id in traced[kind]:
                    events.append(
                        release_evidence.record_event(
                            run_dir,
                            control_id=control_id,
                            kind=kind,
                            status="pass",
                            region=_event_region(kind),
                            account_id=(TEST_ACCOUNT_ID if kind in release_evidence._REGION_REQUIRED_KINDS else None),
                            scenario_id=scenario_id,
                            details={"test_case": f"{control_id}:{kind}:{scenario_id}"},
                        )
                    )
        regional_kind = control.get("required_region_kind")
        selector = control.get("region_selector")
        if regional_kind and selector:
            for entry in regions["regions"]:
                if entry[selector]:
                    for scenario_id in traced[regional_kind]:
                        events.append(
                            release_evidence.record_event(
                                run_dir,
                                control_id=control_id,
                                kind=regional_kind,
                                status="pass",
                                region=entry["region"],
                                account_id=TEST_ACCOUNT_ID,
                                scenario_id=scenario_id,
                                details={"test_case": f"{control_id}:{entry['region']}:{scenario_id}"},
                            )
                        )
    return events


def _copy_run(run_dir: Path, parent: Path) -> Path:
    destination = parent / run_dir.name
    shutil.copytree(run_dir, destination)
    return destination


def test_release_contract_requires_positive_adversarial_and_mutation_twins():
    controls = json.loads(CONTROLS_PATH.read_text(encoding="utf-8"))
    assert len(controls["controls"]) == len({item["id"] for item in controls["controls"]})
    for control in controls["controls"]:
        assert {
            "offline_positive",
            "offline_adversarial",
            "offline_mutation",
        } <= set(control["required_kinds"])
    assert set(controls["global_required_kinds"]) == {
        "clean_clone",
        "exact_commit",
        "ui_cloudfront",
        "rollback",
        "teardown_zero",
    }


def test_traceability_maps_every_required_kind_to_an_existing_check():
    controls = json.loads(CONTROLS_PATH.read_text(encoding="utf-8"))
    traceability = json.loads(TRACEABILITY_PATH.read_text(encoding="utf-8"))
    suite_map, trace_map = release_evidence._validate_traceability(controls, traceability)
    assert set(trace_map) == {control["id"] for control in controls["controls"]}
    for suite in suite_map.values():
        assert all(version for version in suite.get("required_tools", {}).values())
        working_directory = PROJECT_ROOT / suite["working_directory"]
        for command in suite["commands"]:
            for part in command:
                if part.endswith(".py"):
                    assert (working_directory / part).is_file(), f"{suite['id']}: {part}"

    for traced in trace_map.values():
        for kind, requirements in traced["evidence"].items():
            if not kind.startswith("offline_"):
                continue
            for requirement in requirements:
                suite = suite_map[requirement["suite"]]
                selector = requirement["selector"]
                if selector in suite.get("check_ids", []):
                    continue
                path_text, test_name = selector.split("::", 1)
                test_path = PROJECT_ROOT / path_text
                assert test_path.is_file(), selector
                source = test_path.read_text(encoding="utf-8")
                if suite["selector_type"] == "pytest":
                    functions = {
                        node.name
                        for node in ast.walk(ast.parse(source))
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    }
                    assert test_name in functions, selector
                else:
                    assert test_name in source, selector


def test_emea_matrix_matches_documented_feature_boundaries():
    matrix = json.loads(REGIONS_PATH.read_text(encoding="utf-8"))
    by_region = {entry["region"]: entry for entry in matrix["regions"]}
    assert set(by_region) == {
        "eu-central-1",
        "eu-west-1",
        "eu-west-2",
        "eu-south-1",
        "eu-west-3",
        "eu-south-2",
        "eu-north-1",
    }
    assert all(entry["core"] for entry in by_region.values())
    assert {region for region, entry in by_region.items() if entry["production_release"]} == {
        "eu-central-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
    }
    controls = json.loads(CONTROLS_PATH.read_text(encoding="utf-8"))
    regional_control = next(item for item in controls["controls"] if item["id"] == "regional.emea_core")
    assert regional_control["region_selector"] == "production_release"
    assert {region for region, entry in by_region.items() if entry["memory"]} == {
        "eu-central-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "eu-north-1",
    }
    assert {region for region, entry in by_region.items() if entry["agent_registry"]} == {"eu-west-1"}
    assert {region for region, entry in by_region.items() if entry["web_search"]} == {"eu-west-1"}
    assert matrix["production_envelope"]["a2a"] is False


def test_start_rejects_dirty_source(evidence_project: dict):
    (evidence_project["project"] / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(release_evidence.EvidenceError, match="dirty"):
        release_evidence.start_run(evidence_project["output_root"], "dirty-source")


def test_record_rechecks_source_and_contract_fingerprints(evidence_project: dict):
    run_dir = release_evidence.start_run(evidence_project["output_root"], "source-lock")
    (evidence_project["project"] / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(release_evidence.EvidenceError, match="dirty"):
        release_evidence.record_event(
            run_dir,
            control_id="release.integrity",
            kind="exact_commit",
            status="pass",
            scenario_id="source-commit-tree-and-contract-digests-match",
        )


def test_contract_byte_drift_is_rejected_even_if_git_status_hides_it(evidence_project: dict):
    run_dir = release_evidence.start_run(evidence_project["output_root"], "contract-lock")
    contract = evidence_project["controls_path"]
    contract.write_text(contract.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    relative = contract.relative_to(evidence_project["repo"])
    _git(evidence_project["repo"], "update-index", "--assume-unchanged", str(relative))
    assert release_evidence._source_identity()["clean"] is True
    with pytest.raises(release_evidence.EvidenceError, match="contracts changed"):
        release_evidence.record_event(
            run_dir,
            control_id="release.integrity",
            kind="exact_commit",
            status="pass",
            scenario_id="source-commit-tree-and-contract-digests-match",
        )


def test_artifacts_are_digest_only_and_sensitive_details_are_rejected(evidence_project: dict):
    run_dir = release_evidence.start_run(evidence_project["output_root"], "bounded-artifacts")
    artifact = evidence_project["tmp_path"] / "test-output.log"
    artifact.write_text("private test output that must not be copied\n", encoding="utf-8")
    event = release_evidence.record_event(
        run_dir,
        control_id="release.integrity",
        kind="exact_commit",
        status="pass",
        scenario_id="source-commit-tree-and-contract-digests-match",
        details={"exit_code": 0, "test_count": 12, "verified": True},
        artifacts={"test-output": artifact},
    )
    serialized = json.dumps(event)
    assert str(artifact) not in serialized
    assert "private test output" not in serialized
    assert event["artifacts"][0]["bytes"] == artifact.stat().st_size
    assert len(event["artifacts"][0]["sha256"]) == 64

    for details in (
        {"access_token": "redacted"},
        {"result": "AKIA" + "A" * 16},
        {"ratio": float("nan")},
        {"count": 10**19},
    ):
        with pytest.raises(release_evidence.EvidenceError, match="Unsafe"):
            release_evidence.record_event(
                run_dir,
                control_id="release.integrity",
                kind="offline_positive",
                status="pass",
                suite_id="backend",
                details=details,
            )


def test_symlink_and_excess_artifacts_are_rejected(evidence_project: dict):
    run_dir = release_evidence.start_run(evidence_project["output_root"], "artifact-boundary")
    artifact = evidence_project["tmp_path"] / "artifact.txt"
    artifact.write_text("safe\n", encoding="utf-8")
    link = evidence_project["tmp_path"] / "artifact-link.txt"
    link.symlink_to(artifact)
    with pytest.raises(release_evidence.EvidenceError, match="regular single-link"):
        release_evidence.record_event(
            run_dir,
            control_id="release.integrity",
            kind="exact_commit",
            status="pass",
            scenario_id="source-commit-tree-and-contract-digests-match",
            artifacts={"linked": link},
        )
    with pytest.raises(release_evidence.EvidenceError, match="At most 32"):
        release_evidence.record_event(
            run_dir,
            control_id="release.integrity",
            kind="exact_commit",
            status="pass",
            scenario_id="source-commit-tree-and-contract-digests-match",
            artifacts={f"artifact-{index}": artifact for index in range(33)},
        )


def test_failure_remains_open_until_explicitly_superseded(evidence_project: dict):
    run_dir = release_evidence.start_run(evidence_project["output_root"], "supersession")
    failed = release_evidence.record_event(
        run_dir,
        control_id="release.integrity",
        kind="offline_positive",
        status="fail",
        suite_id="backend",
        details={"reason_code": "intentional-break"},
    )
    assert len(release_evidence.status_run(run_dir)["coverage"]["open_events"]) == 1

    def supersede(reason: str):
        try:
            return release_evidence.record_event(
                run_dir,
                control_id="release.integrity",
                kind="offline_positive",
                status="pass",
                suite_id="backend",
                supersedes=failed["event_id"],
                details={"reason_code": reason},
            )
        except release_evidence.EvidenceError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(supersede, ("fixed-a", "fixed-b")))
    passed = [result for result in results if isinstance(result, dict)]
    refused = [result for result in results if isinstance(result, release_evidence.EvidenceError)]
    assert len(passed) == 1
    assert len(refused) == 1
    assert "inactive" in str(refused[0])
    status = release_evidence.status_run(run_dir)
    assert status["coverage"]["open_events"] == []
    assert status["coverage"]["superseded_event_count"] == 1
    assert passed[0]["supersedes"] == failed["event_id"]


def test_live_region_requires_a_selected_matrix_region(evidence_project: dict):
    run_dir = release_evidence.start_run(evidence_project["output_root"], "region-boundary")
    with pytest.raises(release_evidence.EvidenceError, match="not selected"):
        release_evidence.record_event(
            run_dir,
            control_id="regional.emea_core",
            kind="live_region",
            status="pass",
            scenario_id="core-runtime-gateway-identity-policy-evaluation",
            region="eu-north-1",
            account_id=TEST_ACCOUNT_ID,
        )
    with pytest.raises(release_evidence.EvidenceError, match="12-digit AWS account ID"):
        release_evidence.record_event(
            run_dir,
            control_id="regional.emea_core",
            kind="live_region",
            status="pass",
            scenario_id="core-runtime-gateway-identity-policy-evaluation",
            region="eu-west-1",
        )
    with pytest.raises(release_evidence.EvidenceError, match="12-digit AWS account ID"):
        release_evidence.record_event(
            run_dir,
            control_id="regional.emea_core",
            kind="live_region",
            status="pass",
            scenario_id="core-runtime-gateway-identity-policy-evaluation",
            region="eu-west-1",
            account_id="12345678901",
        )
    with pytest.raises(release_evidence.EvidenceError, match="requires an AWS Region"):
        release_evidence.record_event(
            run_dir,
            control_id="regional.emea_core",
            kind="live_region",
            status="pass",
            scenario_id="core-runtime-gateway-identity-policy-evaluation",
        )
    accepted = {
        release_evidence.record_event(
            run_dir,
            control_id="regional.emea_core",
            kind="live_region",
            status="pass",
            scenario_id="core-runtime-gateway-identity-policy-evaluation",
            region=region,
            account_id=TEST_ACCOUNT_ID,
        )["region"]
        for region in ("eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3")
    }
    assert accepted == {"eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3"}


def test_unmapped_suite_and_scenario_are_rejected(evidence_project: dict):
    run_dir = release_evidence.start_run(evidence_project["output_root"], "trace-boundary")
    with pytest.raises(release_evidence.EvidenceError, match="mapped suite"):
        release_evidence.record_event(
            run_dir,
            control_id="release.integrity",
            kind="offline_positive",
            status="pass",
            suite_id="unmapped",
        )
    with pytest.raises(release_evidence.EvidenceError, match="mapped scenario"):
        release_evidence.record_event(
            run_dir,
            control_id="release.integrity",
            kind="exact_commit",
            status="pass",
            scenario_id="generic-pass",
        )


def test_parallel_records_are_not_lost(evidence_project: dict):
    run_dir = release_evidence.start_run(evidence_project["output_root"], "parallel-append")

    def append(index: int) -> str:
        event = release_evidence.record_event(
            run_dir,
            control_id="release.integrity",
            kind="offline_positive",
            status="pass",
            suite_id="backend",
            details={"worker": index},
        )
        return event["event_id"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        event_ids = list(executor.map(append, range(8)))
    assert len(set(event_ids)) == 8
    assert release_evidence.status_run(run_dir)["coverage"]["total_event_count"] == 8


def test_complete_run_seals_verifies_and_is_portable_to_clean_clone(
    evidence_project: dict, monkeypatch: pytest.MonkeyPatch
):
    run_dir = release_evidence.start_run(evidence_project["output_root"], "complete-run")
    events = _record_complete_run(run_dir)
    status = release_evidence.status_run(run_dir)
    assert status["coverage"]["complete"] is True

    def finish_run():
        return "final", release_evidence.finalize_run(run_dir)

    def append_during_finish():
        try:
            return "event", release_evidence.record_event(
                run_dir,
                control_id="release.integrity",
                kind="exact_commit",
                status="pass",
                scenario_id="source-commit-tree-and-contract-digests-match",
                details={"race_probe": True},
            )
        except release_evidence.EvidenceError as exc:
            return "refused", exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        race_results = list(executor.map(lambda operation: operation(), (finish_run, append_during_finish)))
    final = next(value for kind, value in race_results if kind == "final")
    assert final["status"] == "pass"
    assert release_evidence.verify_run(run_dir)["final_digest"] == final["final_digest"]
    non_final = next((kind, value) for kind, value in race_results if kind != "final")
    if non_final[0] == "refused":
        assert "immutable" in str(non_final[1])
    else:
        assert non_final[1]["event_id"] in {
            event["event_id"]
            for event in release_evidence._load_events(
                run_dir,
                release_evidence._load_run(run_dir),
                release_evidence._load_contract(release_evidence.CONTROLS_PATH),
                release_evidence._load_contract(release_evidence.REGIONS_PATH),
                release_evidence._load_contract(release_evidence.TRACEABILITY_PATH),
            )[0]
        }

    mutation_run = _copy_run(run_dir, evidence_project["tmp_path"] / "region-mutation")
    (mutation_run / "final.json").unlink()
    ireland_event = next(
        event
        for event in events
        if event["control_id"] == "regional.emea_core"
        and event["kind"] == "live_region"
        and event["region"] == "eu-west-1"
    )
    (mutation_run / "events" / f"{ireland_event['event_id']}.json").unlink()
    mutated_status = release_evidence.status_run(mutation_run)
    assert {
        "scope": "region",
        "control_id": "regional.emea_core",
        "kind": "live_region",
        "region": "eu-west-1",
        "suite_id": None,
        "scenario_id": "core-runtime-gateway-identity-policy-evaluation",
    } in mutated_status["coverage"]["missing"]
    with pytest.raises(release_evidence.EvidenceError, match="incomplete"):
        release_evidence.finalize_run(mutation_run)

    suite_mutation_run = _copy_run(run_dir, evidence_project["tmp_path"] / "suite-mutation")
    (suite_mutation_run / "final.json").unlink()
    frontend_event = next(
        event
        for event in events
        if event["control_id"] == "release.integrity"
        and event["kind"] == "offline_positive"
        and event["suite_id"] == "frontend"
    )
    (suite_mutation_run / "events" / f"{frontend_event['event_id']}.json").unlink()
    suite_status = release_evidence.status_run(suite_mutation_run)
    assert {
        "scope": "suite",
        "control_id": "release.integrity",
        "kind": "offline_positive",
        "region": None,
        "suite_id": "frontend",
        "scenario_id": None,
    } in suite_status["coverage"]["missing"]
    with pytest.raises(release_evidence.EvidenceError, match="incomplete"):
        release_evidence.finalize_run(suite_mutation_run)

    tampered_run = _copy_run(run_dir, evidence_project["tmp_path"] / "digest-mutation")
    event_path = next((tampered_run / "events").glob("*.json"))
    tampered = json.loads(event_path.read_text(encoding="utf-8"))
    tampered["details"]["mutated"] = True
    event_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(release_evidence.EvidenceError, match="digest does not match"):
        release_evidence.verify_run(tampered_run)

    clone = evidence_project["tmp_path"] / "independent-clone"
    _git(evidence_project["tmp_path"], "clone", "-q", str(evidence_project["repo"]), str(clone))
    clone_project = clone / "Agentic-ai-self-service"
    _git(clone, "checkout", "--detach", "HEAD")
    monkeypatch.setattr(release_evidence, "PROJECT_ROOT", clone_project)
    monkeypatch.setattr(
        release_evidence,
        "CONTROLS_PATH",
        clone_project / "release" / CONTROLS_PATH.name,
    )
    monkeypatch.setattr(
        release_evidence,
        "REGIONS_PATH",
        clone_project / "release" / REGIONS_PATH.name,
    )
    monkeypatch.setattr(
        release_evidence,
        "TRACEABILITY_PATH",
        clone_project / "release" / TRACEABILITY_PATH.name,
    )
    assert release_evidence.verify_run(run_dir)["final_digest"] == final["final_digest"]


def test_cli_records_bounded_event_and_refuses_incomplete_finalization(evidence_project: dict):
    output_root = evidence_project["tmp_path"] / "cli-evidence"
    start = subprocess.run(
        [
            sys.executable,
            str(evidence_project["script_path"]),
            "start",
            "--output-root",
            str(output_root),
            "--run-id",
            "cli-run",
        ],
        cwd=evidence_project["project"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    run_dir = Path(json.loads(start.stdout)["run_dir"])
    record = subprocess.run(
        [
            sys.executable,
            str(evidence_project["script_path"]),
            "record",
            "--run-dir",
            str(run_dir),
            "--control",
            "release.integrity",
            "--kind",
            "exact_commit",
            "--status",
            "pass",
            "--scenario-id",
            "source-commit-tree-and-contract-digests-match",
            "--detail",
            "exit_code=0",
        ],
        cwd=evidence_project["project"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(record.stdout)["details"] == {"exit_code": 0}

    record_command = [
        sys.executable,
        str(evidence_project["script_path"]),
        "record",
        "--run-dir",
        str(run_dir),
        "--control",
        "release.integrity",
        "--kind",
        "offline_positive",
        "--status",
        "pass",
        "--suite-id",
        "backend",
    ]
    processes = [
        subprocess.Popen(
            [*record_command, "--detail", f"worker={index}"],
            cwd=evidence_project["project"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(4)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        assert json.loads(stdout)["status"] == "pass"

    status = subprocess.run(
        [
            sys.executable,
            str(evidence_project["script_path"]),
            "status",
            "--run-dir",
            str(run_dir),
        ],
        cwd=evidence_project["project"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    status_document = json.loads(status.stdout)
    assert status_document["finalized"] is False
    assert status_document["coverage"]["total_event_count"] == 5
    final = subprocess.run(
        [
            sys.executable,
            str(evidence_project["script_path"]),
            "finalize",
            "--run-dir",
            str(run_dir),
        ],
        cwd=evidence_project["project"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert final.returncode == 2
    assert "incomplete" in final.stderr


def test_contract_validator_rejects_missing_region_selector():
    controls = json.loads(CONTROLS_PATH.read_text(encoding="utf-8"))
    regions = json.loads(REGIONS_PATH.read_text(encoding="utf-8"))
    broken = deepcopy(regions)
    broken["regions"][0].pop("production_release")
    with pytest.raises(release_evidence.EvidenceError, match="lacks boolean selector production_release"):
        release_evidence._validate_contracts(controls, broken)


def test_duplicate_json_keys_and_tampered_lock_are_rejected(evidence_project: dict):
    run_dir = release_evidence.start_run(evidence_project["output_root"], "parser-boundary")
    forged = run_dir / "events" / "event-forged.json"
    forged.write_text('{"event_id":"event-forged","event_id":"event-other"}', encoding="utf-8")
    with pytest.raises(release_evidence.EvidenceError, match="Duplicate JSON key"):
        release_evidence.status_run(run_dir)
    forged.unlink()

    (run_dir / ".lock").write_text("mutated", encoding="utf-8")
    with pytest.raises(release_evidence.EvidenceError, match="empty regular"):
        release_evidence.status_run(run_dir)
