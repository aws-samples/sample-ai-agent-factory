"""The CI workflow is an artifact too, and nothing was checking it.

Two jobs in it were broken in ways no test could see, and both failed in the same
shape: a red check whose redness said nothing about the thing it was supposed to be
gating.

``cdk assertions`` installed ``infra/requirements.txt`` — the three packages the stack
synthesizes with, no pytest — and then ran ``python3 -m pytest tests/``. It died on
``No module named pytest`` in 28 seconds on every run since it was added, so the 118
CDK assertions it exists to run had never run once.

``backend tests`` ran the whole suite including ``TestPolicyScanner``, which needs
checkov, which that job deliberately does not install (checkov pins an older boto3 and
co-installing it silently swaps the rule set the accepted-findings baseline was recorded
against). ``_require_scanner`` escalates a missing scanner to a hard failure whenever
``CI`` is set — correct for the job that owns the gate, guaranteed failure for the job
that cannot — so five errors appeared under the export gate's own test file, reading as
"the emitted template has a policy problem" when the truth was "this runner was never
able to look".

Both are one-line fixes and neither is self-evident from reading the YAML, which is why
they are pinned here rather than left to the next person to rediscover from a red check.
"""

from pathlib import Path

import pytest
import yaml

WORKFLOW_NAME = "agentic-ai-self-service-ci.yml"


def _workflow_path():
    """Walk up for the workflow. It lives at the monorepo root, not in the sample.

    Deliberately a failure and not a skip when it is missing. A gate that skips itself
    when it cannot find its subject is how the two defects above survived: the whole
    point is to notice that something moved.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".github" / "workflows" / WORKFLOW_NAME
        if candidate.is_file():
            return candidate
    pytest.fail(f"{WORKFLOW_NAME} not found in any parent's .github/workflows")


@pytest.fixture(scope="module")
def jobs():
    return yaml.safe_load(_workflow_path().read_text())["jobs"]


def _job_by_name(jobs, name):
    for job in jobs.values():
        if job.get("name") == name:
            return job
    pytest.fail(f"no job named {name!r}; jobs are {sorted(j.get('name') for j in jobs.values())}")


def _steps_text(job):
    return "\n".join(step.get("run") or "" for step in job["steps"])


class TestTheCdkJobCanRunPytest:
    def test_the_install_step_provides_pytest(self, jobs):
        """Whatever the file is called, what it installs has to include pytest.

        Asserted through the file's contents rather than its name so a rename does not
        break the test and a rename *back* to requirements.txt does.
        """
        job = _job_by_name(jobs, "cdk assertions")
        run = _steps_text(job)
        assert "pytest tests/" in run, "this job no longer runs the CDK assertions"

        # Resolved through each step's own working-directory rather than a hardcoded
        # infra/ path, so a typo in working-directory fails here too — that is the other
        # way this job can install a file that does not exist.
        repo_root = _workflow_path().parents[2]
        installed = [
            (repo_root / (step.get("working-directory") or "") / line.split("-r", 1)[1].strip().strip("'\""))
            for step in job["steps"]
            for line in (step.get("run") or "").splitlines()
            if "pip install" in line and "-r" in line
        ]
        assert installed, f"the job installs no requirements file: {run!r}"

        seen = set()
        contents = ""

        def _read(path):
            path = path.resolve()
            if path in seen:
                return
            assert path.is_file(), f"the job installs {path}, which does not exist"
            seen.add(path)
            nonlocal contents
            text = path.read_text()
            contents += text
            for line in text.splitlines():
                if line.strip().startswith("-r"):
                    _read(path.parent / line.strip()[2:].strip())

        for path in installed:
            _read(path)

        assert "pytest" in contents, f"{[p.name for p in seen]} declare no pytest: the job cannot run"

    def test_the_cache_key_follows_the_file_that_is_installed(self, jobs):
        """Otherwise the cache is keyed on a file whose changes cannot invalidate it."""
        job = _job_by_name(jobs, "cdk assertions")
        installed = [
            line.split("-r", 1)[1].strip().strip("'\"")
            for line in _steps_text(job).splitlines()
            if "pip install" in line and "-r" in line
        ]
        assert installed, "the job installs no requirements file"
        cache_paths = [
            (step.get("with") or {}).get("cache-dependency-path", "")
            for step in job["steps"]
            if "setup-python" in (step.get("uses") or "")
        ]
        assert cache_paths, "the job does not set up python with a cache"
        assert any(name in path for name in installed for path in cache_paths), (
            f"cache-dependency-path {cache_paths} does not name any installed file {installed}"
        )


class TestOnlyTheJobWithCheckovRunsTheCheckovGate:
    """One job installs checkov; exactly that job must be the one running the scan."""

    def test_the_general_backend_job_deselects_the_policy_scan(self, jobs):
        job = _job_by_name(jobs, "backend tests")
        run = _steps_text(job)
        assert "pipx install" not in run and "checkov" not in run, (
            "this job now installs checkov; if that is intended, the boto3 standoff in "
            "_require_scanner has to be re-argued, and this test updated with it"
        )
        assert "not policy_scan" in run, (
            "the backend job has no checkov, and _require_scanner turns that into a hard "
            "failure under CI, so it must deselect the marker"
        )

    def test_the_export_gate_job_installs_checkov_and_does_not_deselect_it(self, jobs):
        job = _job_by_name(jobs, "exported template (cfn-lint + checkov)")
        run = _steps_text(job)
        assert "checkov" in run, "the job that owns the policy gate no longer installs checkov"
        # The inverse of the assertion above, and the half that matters: deselecting the
        # marker in *both* jobs would turn CI green while the scan stopped running
        # anywhere at all, which is the failure mode this whole class exists to prevent.
        assert "not policy_scan" not in run, "the only job that can run the policy scan is skipping it"
        assert "test_cfn_export_contract.py" in run


class TestTheMarkerIsRegisteredAndUsedWhereItIsNeeded:
    def test_the_marker_is_declared(self):
        """An unregistered marker is a warning, and under ``-W error`` an unrelated
        failure; either way ``-m "not policy_scan"`` would silently match nothing.
        """
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        assert "policy_scan:" in pyproject

    def test_every_checkov_call_site_is_under_the_marker(self):
        """The gap this closes: a new checkov-needing test added without the marker
        re-breaks the backend job, and its error appears in the export gate's file where
        it reads as a template problem.
        """
        contract = (Path(__file__).resolve().parent / "test_cfn_export_contract.py").read_text()
        lines = contract.splitlines()
        call_sites = [i for i, line in enumerate(lines) if '_require_scanner("checkov"' in line]
        assert call_sites, "no checkov call site found; has the policy gate been removed?"

        for index in call_sites:
            # Walk back to the enclosing class and check the decorator above it.
            enclosing = next(
                (i for i in range(index, -1, -1) if lines[i].startswith("class ")),
                None,
            )
            assert enclosing is not None, f"line {index + 1} is not inside a class"
            decorators = []
            cursor = enclosing - 1
            while cursor >= 0 and lines[cursor].startswith("@"):
                decorators.append(lines[cursor])
                cursor -= 1
            assert any("policy_scan" in d for d in decorators), (
                f"{lines[enclosing].strip()} calls _require_scanner('checkov') at line "
                f"{index + 1} but is not marked policy_scan; it will hard-fail the backend job"
            )
