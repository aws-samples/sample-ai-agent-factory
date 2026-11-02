"""Supply-chain and strict-synthesis contracts for the repository CI workflow.

The workflow is executable release infrastructure. These tests pin the exact action
commits and scanner artifacts it trusts, and mutation-test the controls most likely
to be weakened by an apparently harmless CI edit.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
import yaml

WORKFLOW_NAME = "agentic-ai-self-service-ci.yml"
EXPECTED_ACTION_PINS = {
    "actions/checkout": ("v7.0.1", "3d3c42e5aac5ba805825da76410c181273ba90b1"),  # pragma: allowlist secret
    "actions/setup-python": ("v7.1.0", "6d4fd22b20465ccc0f7354476588e8df7c9ca0e0"),  # pragma: allowlist secret
    "actions/setup-node": ("v7.1.0", "949feb2413d6458794dcd2491c4babbbce0c15c1"),  # pragma: allowlist secret
    "actions/upload-artifact": ("v7.0.2", "cf430e030ddbb5b0abf93d22962f4752f3646cd9"),  # pragma: allowlist secret
    "hashicorp/setup-terraform": ("v4.0.1", "dfe3c3f87815947d99a8997f908cb6525fc44e9e"),  # pragma: allowlist secret
}
EXPECTED_REGIONS = [
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "eu-central-1",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
]
SHELLCHECK_VERSION = "0.11.0"
SHELLCHECK_SHA256 = "b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6"  # pragma: allowlist secret
SHELLCHECK_URL = (
    "https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.linux.x86_64.tar.gz"
)


def _workflow_path() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".github" / "workflows" / WORKFLOW_NAME
        if candidate.is_file():
            return candidate
    pytest.fail(f"{WORKFLOW_NAME} not found in any parent's .github/workflows")


@pytest.fixture
def workflow_text() -> str:
    return _workflow_path().read_text()


@pytest.fixture
def workflow(workflow_text: str) -> dict:
    return yaml.safe_load(workflow_text)


def _iter_uses(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses":
                yield value
            yield from _iter_uses(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_uses(value)


def _step(job: dict, name: str) -> dict:
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1, f"expected one {name!r} step, found {len(matches)}"
    return matches[0]


def _assert_action_pins(document: dict) -> None:
    references = list(_iter_uses(document))
    assert references, "workflow invokes no actions"

    seen = set()
    for reference in references:
        match = re.fullmatch(r"([^@\s]+)@([0-9a-f]{40})", reference or "")
        assert match, f"action reference is not pinned to a full lowercase 40-hex commit: {reference!r}"
        repository, commit = match.groups()
        assert repository in EXPECTED_ACTION_PINS, f"unreviewed action repository: {repository}"
        expected_tag, expected_commit = EXPECTED_ACTION_PINS[repository]
        assert commit == expected_commit, (
            f"{repository} must remain at {expected_tag} commit {expected_commit}, got {commit}"
        )
        seen.add(repository)

    assert seen == set(EXPECTED_ACTION_PINS), f"expected action set {sorted(EXPECTED_ACTION_PINS)}, got {sorted(seen)}"


def _assert_shellcheck_pin(document: dict) -> None:
    job = document["jobs"]["export-gate"]
    step = _step(job, "Install ShellCheck 0.11.0 from its verified official artifact")
    run = step["run"]

    assert "apt-get" not in run and "sudo" not in run
    assert SHELLCHECK_URL in run
    assert SHELLCHECK_SHA256 in run
    assert "sha256sum --check --status" in run
    assert run.index(SHELLCHECK_SHA256) < run.index("tar -xzf")
    assert run.index("tar -xzf") < run.index("actual_version=")
    assert f'[[ "$actual_version" == "{SHELLCHECK_VERSION}" ]]' in run
    assert "|| true" not in run
    assert step.get("continue-on-error") is not True


def _assert_strict_synth(document: dict) -> None:
    job = document["jobs"]["strict-synth"]
    assert job["name"] == "strict synth (${{ matrix.region }})"
    assert "if" not in job
    assert job.get("continue-on-error") is not True
    assert job["strategy"] == {
        "fail-fast": False,
        "matrix": {"region": EXPECTED_REGIONS},
    }

    python_setup = next(step for step in job["steps"] if (step.get("uses") or "").startswith("actions/setup-python@"))
    assert python_setup["with"] == {
        "python-version": "3.13",
        "cache": "pip",
        "cache-dependency-path": "Agentic-ai-self-service/infra/requirements.txt",
    }

    node_setup = next(step for step in job["steps"] if (step.get("uses") or "").startswith("actions/setup-node@"))
    assert node_setup["with"] == {
        "node-version": "20",
        "cache": "npm",
        "cache-dependency-path": "Agentic-ai-self-service/infra/package-lock.json",
    }

    python_install = _step(job, "Install exact Python synth dependencies")
    assert python_install["working-directory"] == "Agentic-ai-self-service/infra"
    assert python_install["run"].strip() == "python3 -m pip install --requirement requirements.txt"

    cdk_install = _step(job, "Install the repository-pinned CDK CLI")
    assert cdk_install["working-directory"] == "Agentic-ai-self-service/infra"
    assert cdk_install["run"].strip() == "npm ci"

    synth = _step(job, "Strict production synth without AWS lookups")
    assert synth["working-directory"] == "Agentic-ai-self-service/infra"
    assert synth["env"] == {
        "AWS_EC2_METADATA_DISABLED": "true",
        "CDK_DISABLE_VERSION_CHECK": "true",
    }
    assert synth.get("continue-on-error") is not True
    run = synth["run"]
    required_fragments = [
        "set -euo pipefail",
        'region="${{ matrix.region }}"',
        'output="$RUNNER_TEMP/strict-synth-$region"',
        'isolated_home="$RUNNER_TEMP/strict-synth-home-$region"',
        'isolated_tmp="$RUNNER_TEMP/strict-synth-tmp-$region"',
        "./node_modules/.bin/cdk synth",
        "--strict",
        "--quiet",
        "--no-lookups",
        '--output "$output"',
        "-c environment_name=production",
        '-c aws_region="$region"',
        "-c project_name=acf",
        'test -f "$output/acf-production.template.json"',
    ]
    for fragment in required_fragments:
        assert fragment in run, f"strict synth is missing {fragment!r}"
    assert "npx" not in run, "npx may resolve a CDK CLI other than infra/package-lock.json's pin"
    assert "|| true" not in run
    assert not re.search(r"(^|[\n;&|])\s*aws(?:\s|$)", run), "strict synth must make no AWS CLI call"


def _assert_export_tools_are_mandatory(document: dict) -> None:
    assert document["env"]["CI"] == "true"
    job = document["jobs"]["export-gate"]
    assert job.get("continue-on-error") is not True

    for step in job["steps"]:
        if step.get("name") != "Upload results":
            assert "if" not in step, f"mandatory export step is conditional: {step.get('name') or step.get('uses')}"
            assert step.get("continue-on-error") is not True

    checks = _step(job, "Confirm the scanners are present")["run"]
    for command in (
        "cfn-lint --version",
        "checkov --version",
        "terraform version",
        "shellcheck --version",
        "actionlint -version",
    ):
        assert command in checks

    gate = _step(job, "Contract, lint and policy gates over the emitted bundle")["run"]
    assert "tests/test_ci_supply_chain_and_strict_synth.py" in gate
    for marker in ("policy_scan", "terraform_scan", "shell_scan"):
        assert f"not {marker}" not in gate, f"export gate must not skip its mandatory {marker} tests"
    assert "|| true" not in gate


def test_every_action_is_an_exact_reviewed_commit(workflow: dict, workflow_text: str) -> None:
    _assert_action_pins(workflow)
    for repository, (tag, commit) in EXPECTED_ACTION_PINS.items():
        assert re.search(
            rf"^\s*-?\s*uses:\s*{re.escape(repository)}@{commit}\s+#\s+{re.escape(tag)}\s*$",
            workflow_text,
            re.MULTILINE,
        ), f"{repository}@{commit} must retain its reviewed exact-tag annotation {tag}"


def test_shellcheck_is_verified_before_extraction_and_version_checked(workflow: dict) -> None:
    _assert_shellcheck_pin(workflow)


def test_strict_synth_is_a_real_exact_seven_region_matrix(workflow: dict) -> None:
    _assert_strict_synth(workflow)


def test_strict_synth_dependency_inputs_are_exact_repository_pins() -> None:
    infra = _workflow_path().parents[2] / "Agentic-ai-self-service" / "infra"
    requirements = [
        line.strip()
        for line in (infra / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert requirements
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^=\s]+", line) for line in requirements), requirements

    lock = json.loads((infra / "package-lock.json").read_text())
    declared = lock["packages"][""]["devDependencies"]["aws-cdk"]
    resolved = lock["packages"]["node_modules/aws-cdk"]["version"]
    assert declared == resolved == "2.1138.0"


def test_export_gate_tools_are_mandatory(workflow: dict) -> None:
    _assert_export_tools_are_mandatory(workflow)


def test_workflow_uses_runner_temp_not_shared_tmp(workflow_text: str) -> None:
    assert "/tmp" not in workflow_text
    assert "$RUNNER_TEMP" in workflow_text


def test_mutation_changing_an_action_commit_is_rejected(workflow: dict) -> None:
    mutated = copy.deepcopy(workflow)
    checkout = next(_iter_uses(mutated))
    replacement = checkout[:-1] + ("0" if checkout[-1] != "0" else "1")

    def replace_first(node) -> bool:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "uses" and value == checkout:
                    node[key] = replacement
                    return True
                if replace_first(value):
                    return True
        elif isinstance(node, list):
            for value in node:
                if replace_first(value):
                    return True
        return False

    assert replace_first(mutated)
    with pytest.raises(AssertionError, match="must remain"):
        _assert_action_pins(mutated)


@pytest.mark.parametrize("missing_region", EXPECTED_REGIONS)
def test_mutation_removing_any_region_is_rejected(workflow: dict, missing_region: str) -> None:
    mutated = copy.deepcopy(workflow)
    mutated["jobs"]["strict-synth"]["strategy"]["matrix"]["region"].remove(missing_region)
    with pytest.raises(AssertionError):
        _assert_strict_synth(mutated)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (SHELLCHECK_SHA256, "0" * 64),
        ('[[ "$actual_version" == "0.11.0" ]]', "true"),
    ],
)
def test_mutation_weakening_shellcheck_integrity_is_rejected(workflow: dict, old: str, new: str) -> None:
    mutated = copy.deepcopy(workflow)
    step = _step(
        mutated["jobs"]["export-gate"],
        "Install ShellCheck 0.11.0 from its verified official artifact",
    )
    assert old in step["run"]
    step["run"] = step["run"].replace(old, new, 1)
    with pytest.raises(AssertionError):
        _assert_shellcheck_pin(mutated)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("--strict", "--no-strict"),
        ('test -f "$output/acf-production.template.json"', "true"),
    ],
)
def test_mutation_bypassing_strict_synth_is_rejected(workflow: dict, old: str, new: str) -> None:
    mutated = copy.deepcopy(workflow)
    step = _step(mutated["jobs"]["strict-synth"], "Strict production synth without AWS lookups")
    assert old in step["run"]
    step["run"] = step["run"].replace(old, new, 1)
    with pytest.raises(AssertionError):
        _assert_strict_synth(mutated)


def test_mutation_skipping_a_mandatory_export_tool_is_rejected(workflow: dict) -> None:
    mutated = copy.deepcopy(workflow)
    step = _step(
        mutated["jobs"]["export-gate"],
        "Contract, lint and policy gates over the emitted bundle",
    )
    step["run"] += '\n-m "not shell_scan"\n'
    with pytest.raises(AssertionError, match="must not skip"):
        _assert_export_tools_are_mandatory(mutated)
