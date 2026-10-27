"""Release evidence is executed, receipt-backed, exact-commit-bound, and region-complete.

Nothing in a run is asserted: every event is written by the recorder after it ran a
contract command, and its status is re-derived from the receipt on every load.
"""

from __future__ import annotations

import ast
import hashlib
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
REGIONS_PATH = PROJECT_ROOT / "release" / "agentcore-regions.json"
LEGACY_REGIONS_PATH = PROJECT_ROOT / "release" / "emea-agentcore-regions.json"
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


ROOT_WORKFLOW_PATH = PROJECT_ROOT.parent / release_evidence.ROOT_WORKFLOW_RELATIVE
DISPATCHER_PATH = PROJECT_ROOT / "release" / "scenarios" / "run_scenario.py"
SELECTED_REGIONS = ("eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3", "us-east-1", "us-east-2", "us-west-2")
TOOLS = {"backend_python": sys.executable}

# ---------------------------------------------------------------------------------------------
# Fixture: a real git repository holding the REAL controls/regions contracts and a traceability
# contract whose suites and scenarios run small local scripts. Those scripts emit genuine JUnit
# reports and provenance documents, so the recorder's parsing, digesting, and binding logic is
# exercised end to end through real subprocesses -- nothing inside the recorder is monkeypatched.
# ---------------------------------------------------------------------------------------------

FIXTURE_SUITE = r"""
import json, sys
from pathlib import Path
from xml.sax.saxutils import quoteattr
suite_id, traceability_path, junit_path, behavior_path = sys.argv[1:5]
behavior = json.loads(Path(behavior_path).read_text()) if Path(behavior_path).exists() else {}
exit_code = behavior.get("suite_exit", {}).get(suite_id, 0)
dropped = set(behavior.get("suite_drop", {}).get(suite_id, []))
trace = json.loads(Path(traceability_path).read_text())
suite = next(s for s in trace["suites"] if s["id"] == suite_id)
selectors = sorted({r["selector"] for c in trace["controls"] for k, reqs in c["evidence"].items()
                    if k.startswith("offline_") for r in reqs if r["suite"] == suite_id})
if suite["selector_type"] != "command":
    root = suite["working_directory"].rstrip("/")
    def rel(path_text):
        return path_text[len(root) + 1:] if root != "." else path_text
    cases = []
    for selector in selectors:
        if selector in suite.get("check_ids", []):
            continue
        path_text, node = selector.split("::", 1)
        relative = rel(path_text)
        failure = '<failure message="injected"/>' if selector in dropped else ""
        if suite["selector_type"] == "pytest":
            parts = node.split("::")
            classname = relative[:-3].replace("/", ".") + ("." + ".".join(parts[:-1]) if len(parts) > 1 else "")
            cases.append(f'<testcase classname={quoteattr(classname)} name={quoteattr(parts[-1] + "[param]")} '
                         f'file={quoteattr(relative)} line="1" time="0.01">{failure}</testcase>')
        else:
            cases.append(f'<testcase classname={quoteattr(relative)} name={quoteattr("Suite > " + node)} '
                         f'time="0.01">{failure}</testcase>')
    Path(junit_path).write_text('<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="fixture" '
                                'tests="%d">%s</testsuite></testsuites>' % (len(cases), "".join(cases)))
print(f"fixture suite {suite_id} ran")
raise SystemExit(exit_code)
"""

FIXTURE_SCENARIO = r"""
import argparse, json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("scenario_id")
parser.add_argument("--run-dir", type=Path, required=True)
parser.add_argument("--artifact-dir", type=Path, required=True)
parser.add_argument("--region")
parser.add_argument("--account-id")
parser.add_argument("--behavior", type=Path, required=True)
args = parser.parse_args()
behavior = json.loads(args.behavior.read_text()) if args.behavior.exists() else {}
mine = behavior.get("scenario", {}).get(args.scenario_id, {})
run = json.loads((args.run_dir / "run.json").read_text())
args.artifact_dir.mkdir(parents=True, exist_ok=True)
omit = set(mine.get("omit", []))
if "report" not in omit:
    (args.artifact_dir / "report").write_text(json.dumps({"scenario": args.scenario_id, "region": args.region}) + "\n")
if args.region is not None and "provenance" not in omit:
    provenance = {
        "account_id": args.account_id, "region": args.region, "stack_id": f"acf-production-{args.region}",
        "deployed_commit": run["source"]["commit"], "deployed_project_tree": run["source"]["project_tree"],
        "endpoint": f"https://api.{args.region}.example.invalid/", "archive_sha256": "0" * 64,
    }
    provenance.update(mine.get("provenance_overrides", {}))
    (args.artifact_dir / "provenance").write_text(json.dumps(provenance) + "\n")
if mine.get("extra_file"):
    (args.artifact_dir / "extra.txt").write_text("extra\n")
raise SystemExit(mine.get("exit", 0))
"""


def _fixture_traceability(real: dict, fixture_dir: Path, traceability_path: Path) -> dict:
    """The real control->selector/scenario mapping, with executable fixture commands."""
    trace = deepcopy(real)
    python = "{backend_python}"
    behavior = str(fixture_dir / "behavior.json")
    for suite in trace["suites"]:
        base = [python, str(fixture_dir / "fixture_suite.py"), suite["id"], str(traceability_path)]
        if suite["selector_type"] == "command":
            suite["commands"] = [base + ["-", behavior]]
        else:
            suite["commands"] = [base + ["{junit_xml}", behavior]]
        suite.pop("required_environment", None)
        suite.pop("required_tools", None)
    for scenario in trace["scenarios"]:
        command = [
            python,
            str(fixture_dir / "fixture_scenario.py"),
            "{scenario_id}",
            "--run-dir",
            "{run_dir}",
            "--artifact-dir",
            "{artifact_dir}",
            "--behavior",
            behavior,
        ]
        if scenario["provenance"]:
            command += ["--region", "{region}", "--account-id", "{account_id}"]
        scenario["command"] = command
        scenario["working_directory"] = "."
    return trace


@pytest.fixture
def evidence_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    repo = tmp_path / "source"
    project = repo / "Agentic-ai-self-service"
    release_dir = project / "release"
    scripts_dir = project / "scripts"
    fixture_dir = tmp_path / "fixture"
    for directory in (
        release_dir / "scenarios",
        scripts_dir,
        fixture_dir,
        project / "backend",
        project / "infra",
        project / "frontend",
        repo / ".github" / "workflows",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CONTROLS_PATH, release_dir / CONTROLS_PATH.name)
    shutil.copy2(REGIONS_PATH, release_dir / REGIONS_PATH.name)
    shutil.copy2(SCRIPT_PATH, scripts_dir / SCRIPT_PATH.name)
    shutil.copy2(DISPATCHER_PATH, release_dir / "scenarios" / DISPATCHER_PATH.name)
    shutil.copy2(ROOT_WORKFLOW_PATH, repo / release_evidence.ROOT_WORKFLOW_RELATIVE)
    (fixture_dir / "fixture_suite.py").write_text(FIXTURE_SUITE, encoding="utf-8")
    (fixture_dir / "fixture_scenario.py").write_text(FIXTURE_SCENARIO, encoding="utf-8")
    fixture_traceability = release_dir / TRACEABILITY_PATH.name
    real = json.loads(TRACEABILITY_PATH.read_text(encoding="utf-8"))
    fixture_traceability.write_text(
        json.dumps(_fixture_traceability(real, fixture_dir, fixture_traceability), indent=2) + "\n", encoding="utf-8"
    )
    for name in ("backend", "infra", "frontend"):
        (project / name / ".keep").write_text("", encoding="utf-8")
    (project / ".gitignore").write_text("build/\n", encoding="utf-8")
    (project / "README.md").write_text("# Evidence fixture\n", encoding="utf-8")

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Evidence Test")
    _git(repo, "config", "user.email", "evidence@example.invalid")
    _git(repo, "add", "Agentic-ai-self-service", ".github")
    _git(repo, "commit", "-m", "test: create evidence fixture")

    monkeypatch.setattr(release_evidence, "PROJECT_ROOT", project)
    monkeypatch.setattr(release_evidence, "CONTROLS_PATH", release_dir / CONTROLS_PATH.name)
    monkeypatch.setattr(release_evidence, "REGIONS_PATH", release_dir / REGIONS_PATH.name)
    monkeypatch.setattr(release_evidence, "TRACEABILITY_PATH", fixture_traceability)
    monkeypatch.setattr(release_evidence, "DEFAULT_OUTPUT_ROOT", tmp_path / "default-evidence")
    return {
        "repo": repo,
        "project": project,
        "fixture_dir": fixture_dir,
        "behavior": fixture_dir / "behavior.json",
        "controls_path": release_dir / CONTROLS_PATH.name,
        "regions_path": release_dir / REGIONS_PATH.name,
        "traceability_path": fixture_traceability,
        "script_path": scripts_dir / SCRIPT_PATH.name,
        "root_workflow": repo / release_evidence.ROOT_WORKFLOW_RELATIVE,
        "output_root": tmp_path / "evidence",
        "tmp_path": tmp_path,
    }


def _set_behavior(project: dict, behavior: dict) -> None:
    project["behavior"].write_text(json.dumps(behavior), encoding="utf-8")


def _start(project: dict, run_id: str, *, account_id: str | None = TEST_ACCOUNT_ID) -> Path:
    return release_evidence.start_run(project["output_root"], run_id, account_id=account_id)


def _scenario(run_dir: Path, scenario_id: str, region: str | None = "eu-west-1") -> list[dict]:
    return release_evidence.run_scenario(run_dir, scenario_id=scenario_id, region=region, tools=TOOLS)


def _run_complete_run(run_dir: Path) -> list[dict]:
    """Execute every suite and every scenario (regional ones in every selected Region)."""
    traceability = json.loads(release_evidence.TRACEABILITY_PATH.read_text(encoding="utf-8"))
    events: list[dict] = []
    for suite in traceability["suites"]:
        events += release_evidence.run_suite(run_dir, suite_id=suite["id"], tools=TOOLS)
    kind_of = {
        scenario: kind
        for traced in traceability["controls"]
        for kind, requirements in traced["evidence"].items()
        if not kind.startswith("offline_")
        for scenario in requirements
    }
    for scenario in traceability["scenarios"]:
        kind = kind_of[scenario["id"]]
        if kind == "live_region":
            for region in SELECTED_REGIONS:
                events += _scenario(run_dir, scenario["id"], region)
        elif kind in release_evidence._REGION_REQUIRED_KINDS:
            events += _scenario(run_dir, scenario["id"], "eu-west-1")
        else:
            events += _scenario(run_dir, scenario["id"], None)
    return events


def _copy_run(run_dir: Path, parent: Path) -> Path:
    destination = parent / run_dir.name
    shutil.copytree(run_dir, destination)
    return destination


def _rewrite_event(run_dir: Path, event: dict, mutate) -> None:
    """Forge an event file the way an attacker with disk access would: edit, then re-sign."""
    mutated = deepcopy(event)
    mutate(mutated)
    mutated.pop("event_digest", None)
    mutated["event_digest"] = release_evidence._digest(mutated)
    (run_dir / "events" / f"{event['event_id']}.json").write_text(json.dumps(mutated), encoding="utf-8")


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


def test_certified_matrix_matches_documented_feature_boundaries():
    matrix = json.loads(REGIONS_PATH.read_text(encoding="utf-8"))
    by_region = {entry["region"]: entry for entry in matrix["regions"]}
    assert matrix["schema_version"] == 2
    assert matrix["partition"] == "aws"
    assert matrix["source_url"] == (
        "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html"
    )
    assert matrix["accessed_at"] == "2026-10-27"
    assert set(by_region) == {
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
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
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
    assert {region for region, entry in by_region.items() if entry["profile"] == "full"} == {
        "eu-west-1",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
    assert {region for region, entry in by_region.items() if entry["profile"] == "reduced"} == {
        "eu-central-1",
        "eu-west-2",
        "eu-west-3",
    }
    assert {region for region, entry in by_region.items() if entry["profile"] == "excluded"} == {
        "eu-north-1",
        "eu-south-1",
        "eu-south-2",
        "us-west-1",
    }
    controls = json.loads(CONTROLS_PATH.read_text(encoding="utf-8"))
    regional_control = next(item for item in controls["controls"] if item["id"] == "regional.certified_core")
    assert regional_control["region_selector"] == "production_release"
    assert {region for region, entry in by_region.items() if entry["memory"]} == {
        "eu-central-1",
        "eu-north-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
    assert {region for region, entry in by_region.items() if entry["agent_registry"]} == {
        "eu-central-1",
        "eu-west-1",
        "eu-west-2",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
    assert {region for region, entry in by_region.items() if entry["web_search"]} == {
        "eu-west-1",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
    assert {region for region, entry in by_region.items() if entry["harness"]} == {
        "eu-central-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
    assert by_region["us-west-1"]["production_release"] is False
    assert by_region["us-west-1"]["profile"] == "excluded"
    envelope = matrix["production_envelope"]
    assert envelope["partition"] == "aws"
    assert envelope["deployment_mode"] == "runtime"
    assert envelope["harness"] is False
    assert envelope["a2a"] is False


def test_legacy_emea_matrix_filename_is_absent():
    assert REGIONS_PATH.is_file()
    assert not LEGACY_REGIONS_PATH.exists()


# ---------------------------------------------------------------------------------------------
# Executed evidence: receipts, binding, and the refusal matrix
# ---------------------------------------------------------------------------------------------


def test_start_rejects_dirty_source(evidence_project: dict):
    (evidence_project["project"] / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(release_evidence.EvidenceError, match="dirty"):
        _start(evidence_project, "dirty-source")
    (evidence_project["project"] / "uncommitted.txt").unlink()
    workflow = evidence_project["root_workflow"]
    workflow.write_text(workflow.read_text() + "# drift\n")
    with pytest.raises(release_evidence.EvidenceError, match="dirty"):
        _start(evidence_project, "dirty-workflow")


def test_run_binds_commit_tree_root_workflow_account_and_runner(evidence_project: dict):
    run_dir = _start(evidence_project, "bound-run")
    run = json.loads((run_dir / "run.json").read_text())
    assert run["schema_version"] == 2 and run["account_id"] == TEST_ACCOUNT_ID
    assert set(run["contracts"]) == {"controls", "regions", "traceability", "root_workflow"}
    assert run["source"]["root_workflow"] == release_evidence.ROOT_WORKFLOW_RELATIVE
    expected_blob = _git(evidence_project["repo"], "rev-parse", f"HEAD:{release_evidence.ROOT_WORKFLOW_RELATIVE}")
    assert run["source"]["root_workflow_blob"] == expected_blob
    assert run["runner"]["tool"] == "release_evidence.py"
    assert run["runner"]["version"] == release_evidence.RECORDER_VERSION
    assert (run_dir / "artifacts").is_dir()


def test_there_is_no_way_to_assert_a_pass():
    assert not hasattr(release_evidence, "record_event")
    assert not hasattr(release_evidence, "_record_event_unlocked")
    parser = release_evidence._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["record", "--run-dir", "x", "--control", "c", "--kind", "k", "--status", "pass"])
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert source.count('"status": _derived_status(') == 1, "status is written from the receipt and nowhere else"


def test_suite_run_credits_only_selectors_the_junit_report_shows_passing(evidence_project: dict):
    run_dir = _start(evidence_project, "suite-run")
    events = release_evidence.run_suite(run_dir, suite_id="export-gate", tools=TOOLS)
    assert events and all(event["status"] == "pass" for event in events)
    receipt = events[0]["receipt"]
    assert receipt["commands"][0]["exit_status"] == 0 and receipt["selectors_unverified"] == []
    assert receipt["tools"]["backend_python"]["path"] == str(Path(sys.executable).resolve())
    assert {artifact["label"] for artifact in events[0]["artifacts"]} >= {"stdout-0.log", "stderr-0.log", "junit.xml"}
    assert (run_dir / "artifacts" / events[0]["event_id"] / "junit.xml").exists()

    traceability = json.loads(evidence_project["traceability_path"].read_text())
    control_id, kind, selector = next(
        (traced["id"], kind, req["selector"])
        for traced in traceability["controls"]
        for kind, reqs in traced["evidence"].items()
        if kind.startswith("offline_")
        for req in reqs
        if req["suite"] == "export-gate"
    )
    _set_behavior(evidence_project, {"suite_drop": {"export-gate": [selector]}})
    rerun = release_evidence.run_suite(run_dir, suite_id="export-gate", tools=TOOLS)
    failed = [event for event in rerun if event["status"] == "fail"]
    assert failed and all(selector in event["receipt"]["selectors_unverified"] for event in failed)
    assert (control_id, kind) in {(event["control_id"], event["kind"]) for event in failed}
    assert all(event["supersedes"] is not None for event in rerun), "a rerun supersedes the earlier scope"


def test_suite_exit_failure_records_fail_and_blocks_finalization(evidence_project: dict):
    run_dir = _start(evidence_project, "suite-exit")
    _set_behavior(evidence_project, {"suite_exit": {"static": 1}})
    events = release_evidence.run_suite(run_dir, suite_id="static", tools=TOOLS)
    assert all(event["status"] == "fail" for event in events)
    assert all(event["receipt"]["commands"][0]["exit_status"] == 1 for event in events)
    status = release_evidence.status_run(run_dir)
    assert status["coverage"]["complete"] is False and status["coverage"]["open_events"]
    with pytest.raises(release_evidence.EvidenceError, match="incomplete"):
        release_evidence.finalize_run(run_dir)
    _set_behavior(evidence_project, {})
    rerun = release_evidence.run_suite(run_dir, suite_id="static", tools=TOOLS)
    assert all(event["status"] == "pass" and event["supersedes"] is not None for event in rerun)
    status = release_evidence.status_run(run_dir)
    assert status["coverage"]["superseded_event_count"] == len(events)
    assert status["coverage"]["total_event_count"] == len(events) + len(rerun)


def test_blocked_exit_records_blocked_and_never_credits(evidence_project: dict):
    run_dir = _start(evidence_project, "blocked-scenario")
    _set_behavior(evidence_project, {"scenario": {"prompt-attack-blocked": {"exit": 3}}})
    events = _scenario(run_dir, "prompt-attack-blocked")
    assert events and all(event["status"] == "blocked" for event in events)
    coverage = release_evidence.status_run(run_dir)["coverage"]
    assert coverage["active_pass_count"] == 0
    assert any(item["scenario_id"] == "prompt-attack-blocked" for item in coverage["missing"])


def _dispatch(
    project: dict, run_dir: Path, scenario_id: str, artifact_dir: Path, *live: str
) -> subprocess.CompletedProcess:
    dispatcher = project["project"] / "release" / "scenarios" / "run_scenario.py"
    argv = [
        sys.executable,
        "-I",
        str(dispatcher),
        scenario_id,
        "--run-dir",
        str(run_dir),
        "--artifact-dir",
        str(artifact_dir),
    ]
    return subprocess.run([*argv, *live], capture_output=True, text=True, timeout=60)


def test_the_real_dispatcher_blocks_unimplemented_scenarios_and_passes_exact_commit(evidence_project: dict, tmp_path):
    run_dir = _start(evidence_project, "dispatcher")
    blocked = _dispatch(evidence_project, run_dir, "prompt-attack-blocked", tmp_path / "a", "--region", "eu-west-1")
    assert blocked.returncode == release_evidence.BLOCKED_EXIT_STATUS
    assert "no reviewed implementation" in blocked.stderr
    passed = _dispatch(evidence_project, run_dir, "source-commit-tree-and-contract-digests-match", tmp_path / "b")
    assert passed.returncode == 0, passed.stdout + passed.stderr
    report = json.loads((tmp_path / "b" / "report").read_text())
    run = json.loads((run_dir / "run.json").read_text())
    assert report["mismatches"] == [] and report["observed"]["commit"] == run["source"]["commit"]
    controls_path = evidence_project["controls_path"]
    original = controls_path.read_bytes()
    controls_path.write_bytes(original + b"\n")
    try:
        tampered = _dispatch(evidence_project, run_dir, "source-commit-tree-and-contract-digests-match", tmp_path / "c")
    finally:
        controls_path.write_bytes(original)
    assert tampered.returncode == 1 and "mismatch" in tampered.stderr


def test_a_hand_written_pass_without_a_passing_receipt_is_rejected_on_load(evidence_project: dict):
    run_dir = _start(evidence_project, "forged-pass")
    _set_behavior(evidence_project, {"suite_exit": {"static": 1}})
    [event] = release_evidence.run_suite(run_dir, suite_id="static", tools=TOOLS)
    assert event["status"] == "fail"

    def relabel(doc):
        doc["status"] = "pass"

    _rewrite_event(run_dir, event, relabel)
    with pytest.raises(release_evidence.EvidenceError, match="status does not follow from its receipt"):
        release_evidence.status_run(run_dir)


def test_a_pass_with_no_artifacts_is_rejected_on_load(evidence_project: dict):
    run_dir = _start(evidence_project, "no-artifacts")
    [event] = release_evidence.run_suite(run_dir, suite_id="static", tools=TOOLS)
    assert event["status"] == "pass" and event["artifacts"]

    def strip(doc):
        doc["artifacts"] = []

    _rewrite_event(run_dir, event, strip)
    with pytest.raises(release_evidence.EvidenceError, match="status does not follow from its receipt"):
        release_evidence.status_run(run_dir)


def test_artifact_tampering_after_recording_is_detected(evidence_project: dict):
    run_dir = _start(evidence_project, "tamper-artifact")
    [event] = release_evidence.run_suite(run_dir, suite_id="static", tools=TOOLS)
    log = run_dir / "artifacts" / event["event_id"] / "stdout-0.log"
    log.write_text(log.read_text() + "edited\n")
    with pytest.raises(release_evidence.EvidenceError, match="no longer matches its recorded digest"):
        release_evidence.status_run(run_dir)


def test_live_scenario_requires_an_account_bound_run_and_matching_provenance(evidence_project: dict):
    unbound = _start(evidence_project, "unbound", account_id=None)
    with pytest.raises(release_evidence.EvidenceError, match="not bound to an AWS account"):
        _scenario(unbound, "prompt-attack-blocked")

    run_dir = _start(evidence_project, "bound")
    overrides = {"scenario": {"prompt-attack-blocked": {"provenance_overrides": {"account_id": "999999999999"}}}}
    _set_behavior(evidence_project, overrides)
    with pytest.raises(release_evidence.EvidenceError, match="different AWS account"):
        _scenario(run_dir, "prompt-attack-blocked")
    overrides["scenario"]["prompt-attack-blocked"]["provenance_overrides"] = {"region": "us-east-1"}
    _set_behavior(evidence_project, overrides)
    with pytest.raises(release_evidence.EvidenceError, match="different Region"):
        _scenario(run_dir, "prompt-attack-blocked")
    assert release_evidence.status_run(run_dir)["coverage"]["total_event_count"] == 0, (
        "a refused scenario records nothing"
    )


def test_a_stale_deployed_build_cannot_be_recorded(evidence_project: dict):
    run_dir = _start(evidence_project, "stale-build")
    overrides = {"scenario": {"prompt-attack-blocked": {"provenance_overrides": {"deployed_commit": "0" * 40}}}}
    _set_behavior(evidence_project, overrides)
    with pytest.raises(release_evidence.EvidenceError, match="not the candidate commit"):
        _scenario(run_dir, "prompt-attack-blocked")
    overrides["scenario"]["prompt-attack-blocked"]["provenance_overrides"] = {"deployed_project_tree": "f" * 40}
    _set_behavior(evidence_project, overrides)
    with pytest.raises(release_evidence.EvidenceError, match="not the candidate tree"):
        _scenario(run_dir, "prompt-attack-blocked")


def test_a_mixed_account_event_forged_on_disk_is_rejected(evidence_project: dict):
    run_dir = _start(evidence_project, "mixed-account")
    [event, *_] = _scenario(run_dir, "prompt-attack-blocked")
    assert event["status"] == "pass"

    def other_account(doc):
        doc["account_id"] = "999999999999"
        doc["receipt"]["provenance"]["account_id"] = "999999999999"

    _rewrite_event(run_dir, event, other_account)
    with pytest.raises(release_evidence.EvidenceError, match="AWS account this run is not bound to"):
        release_evidence.status_run(run_dir)


def test_missing_required_artifact_turns_a_zero_exit_into_fail(evidence_project: dict):
    run_dir = _start(evidence_project, "missing-report")
    _set_behavior(evidence_project, {"scenario": {"prompt-attack-blocked": {"omit": ["report"]}}})
    events = _scenario(run_dir, "prompt-attack-blocked")
    assert all(event["status"] == "fail" for event in events)
    assert all(event["details"]["missing_required_artifacts"] == "report" for event in events)


def test_root_workflow_drift_after_start_blocks_every_later_operation(evidence_project: dict):
    run_dir = _start(evidence_project, "workflow-drift")
    workflow = evidence_project["root_workflow"]
    workflow.write_text(workflow.read_text() + "# edited after start\n")
    with pytest.raises(release_evidence.EvidenceError, match="dirty"):
        release_evidence.run_suite(run_dir, suite_id="static", tools=TOOLS)
    _git(evidence_project["repo"], "add", release_evidence.ROOT_WORKFLOW_RELATIVE)
    _git(evidence_project["repo"], "commit", "-m", "test: change ci workflow")
    with pytest.raises(release_evidence.EvidenceError, match="Source identity changed at commit"):
        release_evidence.run_suite(run_dir, suite_id="static", tools=TOOLS)


def test_contract_byte_drift_is_rejected_even_if_git_status_hides_it(evidence_project: dict):
    run_dir = _start(evidence_project, "contract-drift")
    controls = evidence_project["controls_path"]
    original = controls.read_bytes()
    controls.write_bytes(original + b"\n")
    _git(evidence_project["repo"], "update-index", "--assume-unchanged", str(controls))
    try:
        with pytest.raises(release_evidence.EvidenceError, match="dirty|contracts changed"):
            release_evidence.run_suite(run_dir, suite_id="static", tools=TOOLS)
    finally:
        _git(evidence_project["repo"], "update-index", "--no-assume-unchanged", str(controls))
        controls.write_bytes(original)


def test_live_region_scenario_is_refused_outside_the_selected_matrix(evidence_project: dict):
    run_dir = _start(evidence_project, "region-matrix")
    scenario = "core-runtime-gateway-identity-policy-evaluation"
    with pytest.raises(release_evidence.EvidenceError, match="not selected"):
        _scenario(run_dir, scenario, "us-west-1")
    with pytest.raises(release_evidence.EvidenceError, match="requires --region"):
        _scenario(run_dir, scenario, None)
    events = _scenario(run_dir, scenario, "eu-west-3")
    assert all(event["status"] == "pass" and event["region"] == "eu-west-3" for event in events)


def test_unknown_suite_scenario_and_tool_are_rejected(evidence_project: dict):
    run_dir = _start(evidence_project, "unknown")
    with pytest.raises(release_evidence.EvidenceError, match="Unknown release suite"):
        release_evidence.run_suite(run_dir, suite_id="nope", tools=TOOLS)
    with pytest.raises(release_evidence.EvidenceError, match="Unknown scenario"):
        _scenario(run_dir, "nope", None)
    with pytest.raises(release_evidence.EvidenceError, match="pass --tool backend_python"):
        release_evidence.run_suite(run_dir, suite_id="static", tools={})
    with pytest.raises(release_evidence.EvidenceError, match="must be an absolute path"):
        release_evidence.run_suite(run_dir, suite_id="static", tools={"backend_python": "python3"})


def test_required_tool_versions_are_verified_against_the_executable_actually_used(evidence_project: dict):
    trace = json.loads(evidence_project["traceability_path"].read_text())
    for suite in trace["suites"]:
        if suite["id"] == "static":
            suite["required_tools"] = {"actionlint": "1.7.12"}
    evidence_project["traceability_path"].write_text(json.dumps(trace, indent=2) + "\n")
    _git(evidence_project["repo"], "add", "-A")
    _git(evidence_project["repo"], "commit", "-m", "test: pin a tool")
    run_dir = _start(evidence_project, "tool-version")
    shim = evidence_project["fixture_dir"] / "actionlint"
    shim.write_text("#!/bin/sh\necho 'v1.7.11'\n")
    shim.chmod(0o755)
    with pytest.raises(release_evidence.EvidenceError, match="must report version 1.7.12"):
        release_evidence.run_suite(run_dir, suite_id="static", tools={**TOOLS, "actionlint": str(shim)})
    shim.write_text("#!/bin/sh\necho 'v1.7.12'\n")
    [event] = release_evidence.run_suite(run_dir, suite_id="static", tools={**TOOLS, "actionlint": str(shim)})
    assert event["receipt"]["tool_versions"] == {"actionlint": "v1.7.12"}
    assert event["receipt"]["tools"]["actionlint"]["sha256"] == hashlib.sha256(shim.read_bytes()).hexdigest()


def test_sensitive_details_are_rejected_and_bounded_ones_are_kept(evidence_project: dict):
    run_dir = _start(evidence_project, "details")
    with pytest.raises(release_evidence.EvidenceError, match="Unsafe evidence detail key"):
        release_evidence.run_suite(run_dir, suite_id="static", tools=TOOLS, details={"api_token": "x"})
    with pytest.raises(release_evidence.EvidenceError, match="Unsafe evidence detail value"):
        release_evidence.run_suite(run_dir, suite_id="static", tools=TOOLS, details={"note": "AKIA" + "A" * 16})
    [event] = release_evidence.run_suite(run_dir, suite_id="static", tools=TOOLS, details={"operator_note": "ok"})
    assert event["details"] == {"operator_note": "ok"}


def test_symlinks_inside_an_artifact_tree_are_rejected(evidence_project: dict):
    run_dir = _start(evidence_project, "symlink-tree")
    _set_behavior(evidence_project, {"scenario": {"prompt-attack-blocked": {"extra_file": True}}})
    [event, *_] = _scenario(run_dir, "prompt-attack-blocked")
    output = run_dir / "artifacts" / event["event_id"] / "output"
    assert (output / "extra.txt").exists()
    (output / "escape").symlink_to(evidence_project["controls_path"])
    with pytest.raises(release_evidence.EvidenceError, match="contains a symlink"):
        release_evidence.status_run(run_dir)


def test_parallel_suite_runs_are_serialized_and_none_are_lost(evidence_project: dict):
    run_dir = _start(evidence_project, "parallel")
    suites = ["static", "secret-scan", "backend-package", "strict-synth"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda suite: release_evidence.run_suite(run_dir, suite_id=suite, tools=TOOLS), suites))
    written = sum(len(events) for events in results)
    assert release_evidence.status_run(run_dir)["coverage"]["total_event_count"] == written


def test_complete_run_seals_verifies_and_is_portable_to_clean_clone(
    evidence_project: dict, tmp_path: Path, monkeypatch
):
    run_dir = _start(evidence_project, "complete")
    events = _run_complete_run(run_dir)
    assert events and all(event["status"] == "pass" for event in events)
    status = release_evidence.status_run(run_dir)
    assert status["coverage"]["complete"] is True, status["coverage"]["missing"][:5]
    assert status["coverage"]["active_pass_by_kind"]["live_region"] == 3 * len(SELECTED_REGIONS)
    final = release_evidence.finalize_run(run_dir)
    run = json.loads((run_dir / "run.json").read_text())
    assert final["status"] == "pass" and final["account_id"] == TEST_ACCOUNT_ID and final["runner"] == run["runner"]
    assert release_evidence.verify_run(run_dir)["final_digest"] == final["final_digest"]
    with pytest.raises(release_evidence.EvidenceError, match="immutable"):
        release_evidence.run_suite(run_dir, suite_id="static", tools=TOOLS)

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(evidence_project["repo"]), str(clone))
    project = clone / "Agentic-ai-self-service"
    monkeypatch.setattr(release_evidence, "PROJECT_ROOT", project)
    monkeypatch.setattr(release_evidence, "CONTROLS_PATH", project / "release" / CONTROLS_PATH.name)
    monkeypatch.setattr(release_evidence, "REGIONS_PATH", project / "release" / REGIONS_PATH.name)
    monkeypatch.setattr(release_evidence, "TRACEABILITY_PATH", project / "release" / TRACEABILITY_PATH.name)
    copied = _copy_run(run_dir, tmp_path / "portable")
    assert release_evidence.verify_run(copied)["final_digest"] == final["final_digest"]
    (project / "README.md").write_text("# changed\n")
    _git(clone, "config", "user.name", "Evidence Test")
    _git(clone, "config", "user.email", "evidence@example.invalid")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "drift")
    with pytest.raises(release_evidence.EvidenceError, match="Source identity changed"):
        release_evidence.verify_run(copied)


def test_cli_executes_suites_and_refuses_incomplete_finalization(evidence_project: dict, capsys):
    root = str(evidence_project["output_root"])
    assert (
        release_evidence.main(["start", "--output-root", root, "--run-id", "cli-run", "--account-id", TEST_ACCOUNT_ID])
        == 0
    )
    run_dir = Path(json.loads(capsys.readouterr().out)["run_dir"])
    tool = f"backend_python={sys.executable}"
    assert release_evidence.main(["run-suite", "--run-dir", str(run_dir), "--suite-id", "static", "--tool", tool]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["events"] and all(event["status"] == "pass" for event in summary["events"])
    assert release_evidence.main(["finalize", "--run-dir", str(run_dir)]) == 2
    assert "incomplete" in capsys.readouterr().err
    assert (
        release_evidence.main(["run-suite", "--run-dir", str(run_dir), "--suite-id", "static", "--tool", "other=/y"])
        == 2
    )
    assert "Unknown tool placeholder" in capsys.readouterr().err


def test_contract_requires_executable_scenarios_junit_routing_and_tool_placeholders():
    controls = json.loads(CONTROLS_PATH.read_text(encoding="utf-8"))
    traceability = json.loads(TRACEABILITY_PATH.read_text(encoding="utf-8"))
    suites, trace_map = release_evidence._validate_traceability(controls, traceability)
    scenarios = release_evidence._validate_scenarios(traceability)
    referenced = {
        scenario
        for traced in trace_map.values()
        for kind, requirements in traced["evidence"].items()
        if not kind.startswith("offline_")
        for scenario in requirements
    }
    assert set(scenarios) == referenced and len(scenarios) == 59
    for suite in suites.values():
        junit = sum(any("{junit_xml}" in part for part in command) for command in suite["commands"])
        assert junit == (1 if suite["selector_type"] in {"pytest", "javascript"} else 0), suite["id"]

    def drop_junit(doc):
        for suite in doc["suites"]:
            if suite["id"] == "infra":
                suite["commands"][0].remove("--junit-xml={junit_xml}")

    mutations = {
        "scenario without definition": lambda doc: doc["scenarios"].pop(),
        "orphan scenario": lambda doc: doc["scenarios"].append({**doc["scenarios"][0], "id": "orphan-scenario"}),
        "suite without junit": drop_junit,
        "bare executable": lambda doc: doc["suites"][0]["commands"][0].__setitem__(0, "python3"),
        "unknown placeholder": lambda doc: doc["suites"][0]["commands"][0].append("{shell}"),
        "provenance without artifact": lambda doc: doc["scenarios"][0].__setitem__("required_artifacts", ["report"]),
    }
    for name, mutate in mutations.items():
        broken = deepcopy(traceability)
        mutate(broken)
        with pytest.raises(release_evidence.EvidenceError):
            release_evidence._validate_traceability(controls, broken)
        assert name


def test_contract_validator_rejects_missing_region_selector():
    controls = json.loads(CONTROLS_PATH.read_text(encoding="utf-8"))
    regions = json.loads(REGIONS_PATH.read_text(encoding="utf-8"))
    broken = deepcopy(regions)
    broken["regions"][0].pop("production_release")
    with pytest.raises(release_evidence.EvidenceError, match="lacks boolean selector production_release"):
        release_evidence._validate_contracts(controls, broken)


@pytest.mark.parametrize(
    ("region", "field", "value", "message"),
    [
        ("us-east-2", "web_search", False, "full profile"),
        ("eu-central-1", "web_search", True, "reduced profile"),
        ("us-west-1", "production_release", True, "excluded profile"),
    ],
)
def test_contract_validator_rejects_stale_profile_flags(region, field, value, message):
    controls = json.loads(CONTROLS_PATH.read_text(encoding="utf-8"))
    regions = json.loads(REGIONS_PATH.read_text(encoding="utf-8"))
    broken = deepcopy(regions)
    entry = next(item for item in broken["regions"] if item["region"] == region)
    entry[field] = value

    with pytest.raises(release_evidence.EvidenceError, match=message):
        release_evidence._validate_contracts(controls, broken)


def test_contract_validator_rejects_unreviewed_partition_region():
    controls = json.loads(CONTROLS_PATH.read_text(encoding="utf-8"))
    regions = json.loads(REGIONS_PATH.read_text(encoding="utf-8"))
    broken = deepcopy(regions)
    gov = deepcopy(next(item for item in broken["regions"] if item["region"] == "us-west-2"))
    gov["region"] = "us-gov-west-1"
    broken["regions"].append(gov)

    with pytest.raises(release_evidence.EvidenceError, match="outside the reviewed commercial aws partition"):
        release_evidence._validate_contracts(controls, broken)


def test_contract_bundle_rejects_the_legacy_matrix_filename(evidence_project: dict):
    legacy = evidence_project["regions_path"].with_name("emea-agentcore-regions.json")
    legacy.write_text(evidence_project["regions_path"].read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(release_evidence.EvidenceError, match="Legacy EMEA Region matrix must be removed"):
        release_evidence._load_contract_bundle()


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
