"""Contract tests for release documents and their CI workflow only.

These tests intentionally import no product implementation. They keep the seven-Region
and downloadable-asset release promises executable without duplicating generator tests.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = PROJECT_ROOT.parent
CONTROLS_PATH = PROJECT_ROOT / "release" / "production-readiness-controls.json"
TRACEABILITY_PATH = PROJECT_ROOT / "release" / "production-readiness-traceability.json"
REGIONS_PATH = PROJECT_ROOT / "release" / "agentcore-regions.json"
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "agentic-ai-self-service-ci.yml"
THIS_TEST = "tests/test_release_documentation_contract.py"

CERTIFIED_REGIONS = {
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "eu-central-1",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
}
FULL_REGIONS = {"us-east-1", "us-east-2", "us-west-2", "eu-west-1"}
REDUCED_REGIONS = {"eu-central-1", "eu-west-2", "eu-west-3"}
DOWNLOAD_REQUIRED_KINDS = {
    "offline_positive",
    "offline_adversarial",
    "offline_mutation",
    "clean_clone",
    "exact_commit",
    "live_positive",
    "live_adversarial",
    "ui_cloudfront",
    "rollback",
    "teardown_zero",
}
DOWNLOAD_LIVE_REGION_SCENARIOS = {
    "api-authenticated-cfn-download-clean-recipient-deploy-invoke-update-rollback-teardown",
    "ui-authenticated-python-download-clean-recipient-lock-install-compile-start-invoke",
}
DOWNLOAD_LIVE_ADVERSARIAL_SCENARIOS = {
    "tampered-archive-or-manifest-rejected-before-use",
    "python-missing-or-incompatible-dependency-rejected",
    "cfn-wrong-account-or-region-refused",
    "generated-ci-defaults-remain-inert",
}
DOWNLOAD_SUITE_FILES = {
    "tests/test_downloadable_asset_contract.py",
    "tests/test_python_exporter.py",
    "tests/test_cfn_export_contract.py",
    "tests/test_the_exported_template_can_actually_deploy.py",
    "tests/test_export_bundle_staging.py",
    "tests/test_export_bundle_isolation.py",
    "tests/test_litellm_secret_region_is_enforced_in_the_template.py",
    THIS_TEST,
    "tests/test_ci_workflow_gates.py",
}
AUTHORITATIVE_PATHS = (
    CONTROLS_PATH,
    TRACEABILITY_PATH,
    PROJECT_ROOT / "release" / "README.md",
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "CHANGELOG.md",
    PROJECT_ROOT / "docs" / "DEPLOYMENT_INTERNALS.md",
    WORKFLOW_PATH,
)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _control(document: dict, control_id: str) -> dict:
    return next(item for item in document["controls"] if item["id"] == control_id)


def _suite(traceability: dict, suite_id: str) -> dict:
    return next(item for item in traceability["suites"] if item["id"] == suite_id)


def _workflow_steps(job: dict) -> str:
    return "\n".join(step.get("run") or "" for step in job["steps"])


def test_release_contract_documents_are_internally_consistent():
    controls = _json(CONTROLS_PATH)
    traceability = _json(TRACEABILITY_PATH)
    matrix = _json(REGIONS_PATH)

    control_ids = [item["id"] for item in controls["controls"]]
    traced_ids = [item["id"] for item in traceability["controls"]]
    assert len(control_ids) == len(set(control_ids))
    assert len(traced_ids) == len(set(traced_ids))
    assert set(traced_ids) == set(control_ids)
    assert matrix["schema_version"] == 2
    assert "delivery.downloadable_assets" in control_ids
    assert "regional.certified_core" in control_ids

    for reference in (
        PROJECT_ROOT / "release" / "README.md",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "docs" / "DEPLOYMENT_INTERNALS.md",
        REGIONS_PATH,
    ):
        assert reference.is_file(), reference


def test_legacy_scope_cannot_return_to_authoritative_documents():
    forbidden_literals = {
        "regional." + "emea_core",
        "emea-agentcore-" + "regions.json",
        "production-supported-" + "emea-regions-cdk-nag",
    }
    forbidden_wording = re.compile(
        r"(?:four\s+(?:explicit\s+)?emea|all\s+four\s+selected\s+regions|"
        r"four\s+release\s+regions|emea\s+support\s+envelope)",
        re.IGNORECASE,
    )
    for path in AUTHORITATIVE_PATHS:
        text = path.read_text(encoding="utf-8")
        assert not (forbidden_literals & set(re.findall(r"[A-Za-z0-9_.-]+", text))), path
        assert forbidden_wording.search(text) is None, path


def test_certified_region_matrix_and_runtime_envelope_are_exact():
    matrix = _json(REGIONS_PATH)
    regions = {item["region"]: item for item in matrix["regions"]}

    assert matrix["schema_version"] == 2
    assert matrix["partition"] == "aws"
    assert {region for region, item in regions.items() if item["production_release"]} == CERTIFIED_REGIONS
    assert {region for region, item in regions.items() if item["profile"] == "full"} == FULL_REGIONS
    assert {region for region, item in regions.items() if item["profile"] == "reduced"} == REDUCED_REGIONS

    for region in FULL_REGIONS:
        assert regions[region]["agent_registry"] is True
        assert regions[region]["web_search"] is True
    for region in {"eu-central-1", "eu-west-2"}:
        assert regions[region]["agent_registry"] is True
        assert regions[region]["web_search"] is False
    assert regions["eu-west-3"]["agent_registry"] is False
    assert regions["eu-west-3"]["web_search"] is False

    envelope = matrix["production_envelope"]
    assert envelope["partition"] == "aws"
    assert envelope["deployment_mode"] == "runtime"
    assert envelope["harness"] is False
    assert envelope["a2a"] is False


def test_uncertified_regions_remain_excluded():
    matrix = _json(REGIONS_PATH)
    regions = {item["region"]: item for item in matrix["regions"]}
    northern_california = regions["us-west-1"]
    assert northern_california["name"] == "US West (N. California)"
    assert northern_california["profile"] == "excluded"
    assert northern_california["production_release"] is False
    assert all(
        item["profile"] == "excluded" and item["production_release"] is False
        for region, item in regions.items()
        if region not in CERTIFIED_REGIONS
    )


def test_strict_synth_covers_every_certified_region():
    traceability = _json(TRACEABILITY_PATH)
    suite = _suite(traceability, "strict-synth")
    assert suite["check_ids"] == ["production-certified-regions-cdk-nag"]
    assert len(suite["commands"]) == len(CERTIFIED_REGIONS)

    command_regions = set()
    output_regions = set()
    for command in suite["commands"]:
        assert command[:4] == ["{cdk}", "synth", "--strict", "--quiet"]
        assert "environment_name=production" in command
        assert "project_name=acf" in command
        region = next(part.removeprefix("aws_region=") for part in command if part.startswith("aws_region="))
        output = command[command.index("--output") + 1]
        command_regions.add(region)
        assert output == f"{{artifact_dir}}/cdk-{region}"
        output_regions.add(output.removeprefix("{artifact_dir}/cdk-"))
    assert command_regions == CERTIFIED_REGIONS
    assert output_regions == CERTIFIED_REGIONS
    assert {"us-east-1", "us-east-2", "us-west-2"} <= command_regions


def test_downloadable_control_requires_every_evidence_kind_and_region_scenario():
    controls = _json(CONTROLS_PATH)
    traceability = _json(TRACEABILITY_PATH)
    control = _control(controls, "delivery.downloadable_assets")
    traced = _control(traceability, "delivery.downloadable_assets")["evidence"]

    assert set(control["required_kinds"]) == DOWNLOAD_REQUIRED_KINDS
    assert control["required_region_kind"] == "live_region"
    assert control["region_selector"] == "production_release"
    assert set(traced) == DOWNLOAD_REQUIRED_KINDS | {"live_region"}
    assert all(traced[kind] for kind in traced)
    assert set(traced["live_region"]) == DOWNLOAD_LIVE_REGION_SCENARIOS
    assert set(traced["live_adversarial"]) == DOWNLOAD_LIVE_ADVERSARIAL_SCENARIOS
    assert traced["ui_cloudfront"] == ["deployed-cloudfront-ui-authenticated-cfn-python-downloads-verified"]
    assert traced["rollback"] == ["downloaded-cfn-update-rollback-restores-prior-behavior"]
    assert traced["teardown_zero"] == ["downloaded-cfn-teardown-direct-service-inventories-zero"]

    selected = {item["region"] for item in _json(REGIONS_PATH)["regions"] if item[control["region_selector"]]}
    assert selected == CERTIFIED_REGIONS


def test_downloadable_offline_suite_is_exact_and_tool_pinned():
    traceability = _json(TRACEABILITY_PATH)
    suite = _suite(traceability, "export-gate")
    command_parts = {part for command in suite["commands"] for part in command}

    assert suite["working_directory"] == "backend"
    assert suite["selector_type"] == "pytest"
    assert DOWNLOAD_SUITE_FILES <= command_parts
    assert suite["required_tools"] == {
        "cfn-lint": "1.57.0",
        "checkov": "3.3.1",
        "terraform": "1.14.6",
        "shellcheck": "0.11.0",
        "actionlint": "1.7.12",
    }
    assert ["{actionlint}", "../../.github/workflows/agentic-ai-self-service-ci.yml"] in suite["commands"]
    for relative in DOWNLOAD_SUITE_FILES:
        assert (PROJECT_ROOT / "backend" / relative).is_file(), relative


def test_ci_cannot_skip_release_documentation_or_downloadable_contracts():
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    release_job = jobs["release-contract"]
    release_commands = _workflow_steps(release_job)
    assert "release/production-readiness-controls.json" in release_commands
    assert "release/production-readiness-traceability.json" in release_commands
    assert "release/agentcore-regions.json" in release_commands
    assert "backend/tests/test_release_documentation_contract.py" in release_commands

    export_job = jobs["export-gate"]
    export_commands = _workflow_steps(export_job)
    for relative in DOWNLOAD_SUITE_FILES:
        assert relative in export_commands
    assert "releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz" in export_commands
    assert "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8" in export_commands
    assert "actionlint .github/workflows/agentic-ai-self-service-ci.yml" in export_commands
    assert "$RUNNER_TEMP/export-gate.xml" in export_commands
    assert "/tmp/export-gate.xml" not in export_commands
    upload_step = next(step for step in jobs["export-gate"]["steps"] if step.get("name") == "Upload results")
    assert upload_step["with"]["path"] == "${{ runner.temp }}/export-gate.xml"
    assert "not downloadable" not in export_commands


def test_customer_docs_explain_integrity_execution_and_bounded_claim():
    root = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    internals = (PROJECT_ROOT / "docs" / "DEPLOYMENT_INTERNALS.md").read_text(encoding="utf-8")
    release = (PROJECT_ROOT / "release" / "README.md").read_text(encoding="utf-8")
    combined = "\n".join((root, internals, release))

    for literal in (
        "artifact-manifest.json",
        "SHA256SUMS",
        "verify-artifact.py",
        "archive_sha256",
        "archive_bytes",
        "127.0.0.1:8080:8080",
        "docker.io/library/python:3.13.13-slim@sha256:",
        "pip check",
        "installed-closure",
        "wheel bytes are not hash-locked",
        "delivery.downloadable_assets",
        "regional.certified_core",
        "actionlint 1.7.12",
    ):
        assert literal in combined, literal
    assert "bounded claim" in combined.lower()
    assert "future aws behavior" in combined.lower()
    assert "defect-free operation" in combined.lower()
    assert "100% future guarantee" not in combined.lower()


def test_workflow_schema_has_only_runnable_jobs_and_steps():
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert workflow["name"] == "Agentic-ai-self-service CI"
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]
    for job_id, job in workflow["jobs"].items():
        assert re.fullmatch(r"[a-z][a-z0-9-]*", job_id), job_id
        assert isinstance(job.get("runs-on"), str) and job["runs-on"]
        steps = job.get("steps")
        assert isinstance(steps, list) and steps
        for step in steps:
            assert isinstance(step, dict)
            assert bool(step.get("uses")) != bool(step.get("run")), (job_id, step)
            if "working-directory" in step:
                directory = step["working-directory"]
                assert isinstance(directory, str) and directory
                assert not Path(directory).is_absolute()
                assert ".." not in Path(directory).parts
