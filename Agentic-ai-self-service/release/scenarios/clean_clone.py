"""``clean_clone`` evidence — an independent, detached clean clone re-derives the candidate.

Implements release/README.md "Independent clean-clone verification" as two scenario ids:

* **independent-detached-clone-full-gates** — a second local clone (``git clone --no-hardlinks
  --no-local``) detached at the run's exact ``source.commit``; environments installed only from
  the committed locks (``backend/uv.lock`` via ``uv sync --frozen``, ``infra/requirements*.txt``,
  ``infra``/``frontend`` ``package-lock.json`` via ``npm ci``); Lambda dependencies and AgentCore
  bundles rebuilt with the committed scripts; **every** offline suite of
  ``production-readiness-traceability.json`` re-run in the clone through the recorder's own
  ``run-suite`` (no missing-tool skips, the recorder checks pinned tool versions); and the clone's
  rebuilt ``backend-package`` (wheel + sdist) and ``strict-synth`` (templates + asset manifests for
  all selected Regions) must be **byte-identical** to the primary run's artifacts.
* **downloadable-assets-independent-detached-clean-clone** — the same detached clone, lock-only
  backend environment, re-running the ``export-gate`` suite (the downloadable-assets offline
  evidence) plus the ``backend-package`` rebuild identity.

Both are offline (no account, no Region). The report lists the clone identity, each step's exit
status, each suite's recorder outcome and every artifact digest comparison; any mismatch, failed
step or failed/blocked suite fails the scenario closed. The primary run must already hold the
artifacts being compared (its ``backend-package``/``strict-synth`` suites ran first); otherwise
the scenario records ``blocked`` rather than inventing a comparison.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from _live import PROJECT_ROOT, ScenarioBlocked, ScenarioFailed, write_json

FULL_GATES_ID = "independent-detached-clone-full-gates"
DOWNLOADABLE_ID = "downloadable-assets-independent-detached-clean-clone"
TRACEABILITY = "release/production-readiness-traceability.json"
RECORDER = "scripts/release_evidence.py"
PLACEHOLDER = re.compile(r"\{([a-zA-Z0-9_-]+)\}")
RECORDER_PLACEHOLDERS = {"junit_xml", "artifact_dir"}
REBUILD_SCRIPTS = ("scripts/install-lambda-deps.sh", "scripts/install-agentcore-deps.sh")
STEP_TIMEOUT = 3600


def _run(
    cmd: list[str], *, cwd: Path, log: Path, env: dict[str, str] | None = None, timeout: int = STEP_TIMEOUT
) -> int:
    """Run one step, append its output to ``log``, return the exit status (never raises on failure)."""
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ (cd {cwd}) {' '.join(cmd)}\n")
        handle.flush()
        try:
            completed = subprocess.run(
                cmd, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            handle.write(f"[timeout after {timeout}s]\n")
            return 124
    return completed.returncode


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False, timeout=600
    )
    if completed.returncode != 0:
        raise ScenarioFailed(f"git {' '.join(args)} failed: {completed.stderr.strip()[:300]}")
    return completed.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_tree(root: Path, patterns: tuple[str, ...]) -> dict[str, str]:
    """Relative path -> sha256 for files matching ``patterns`` under ``root`` (sorted, stable)."""
    out: dict[str, str] = {}
    for pattern in patterns:
        for path in sorted(root.rglob(pattern)):
            if path.is_file():
                out[str(path.relative_to(root))] = _sha256(path)
    return out


def _primary_artifacts(run_dir: Path, suite_id: str, marker: str) -> Path:
    """The primary run's artifact root for ``suite_id`` (identified by a file/dir it must contain)."""
    candidates = [p for p in run_dir.rglob(marker) if suite_id in str(p.relative_to(run_dir))]
    if not candidates:
        raise ScenarioBlocked(f"the primary run has no {suite_id} artifacts ({marker}) yet; run that suite first")
    return sorted(candidates, key=lambda p: len(str(p)))[0].parent


def _which(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise ScenarioBlocked(f"required tool is not installed on this host: {name}")
    return found


def _suite_placeholders(suite: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for command in suite.get("commands") or []:
        for token in command:
            names.update(PLACEHOLDER.findall(str(token)))
    names.update(str(k) for k in (suite.get("required_tools") or {}).keys())
    return names - RECORDER_PLACEHOLDERS


def _clone(repo_root: Path, commit: str, project_rel: str, project_tree: str, dest: Path, log: Path) -> dict[str, Any]:
    if (
        _run(
            ["git", "clone", "--quiet", "--no-hardlinks", "--no-local", str(repo_root), str(dest)],
            cwd=repo_root.parent,
            log=log,
        )
        != 0
    ):
        raise ScenarioFailed("git clone of the repository failed")
    if _run(["git", "checkout", "--quiet", "--detach", commit], cwd=dest, log=log) != 0:
        raise ScenarioFailed(f"the clone cannot check out {commit[:12]} detached")
    head = _git(dest, "rev-parse", "HEAD")
    tree = _git(dest, "rev-parse", f"HEAD:{project_rel}")
    detached = _git(dest, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    clean = _git(dest, "status", "--porcelain") == ""
    identity = {"path": str(dest), "head": head, "project_tree": tree, "detached": detached, "clean": clean}
    if head != commit or tree != project_tree or not detached or not clean:
        raise ScenarioFailed(
            f"clone identity mismatch: {json.dumps(identity)} expected commit={commit} tree={project_tree}"
        )
    return identity


def _environments(project: Path, *, full: bool, log: Path) -> dict[str, Any]:
    """Install only from the committed locks; return the tool paths the suites need."""
    uv = _which("uv")
    steps: dict[str, int] = {}
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(project / "backend" / ".venv")}
    steps["backend uv sync --frozen"] = _run(
        [uv, "sync", "--frozen", "--all-extras"], cwd=project / "backend", log=log, env=env
    )
    tools = {"backend_python": str(project / "backend" / ".venv" / "bin" / "python"), "uv": uv, "npm": _which("npm")}
    if full:
        infra_python = project / "infra" / ".venv" / "bin" / "python"
        steps["infra venv"] = _run(
            ["python3", "-m", "venv", str(project / "infra" / ".venv")], cwd=project / "infra", log=log
        )
        steps["infra pip (locks)"] = _run(
            [
                str(infra_python),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--require-hashes" if _hashed(project / "infra" / "requirements.txt") else "--no-deps",
                "-r",
                "requirements.txt",
                "-r",
                "requirements-dev.txt",
            ],
            cwd=project / "infra",
            log=log,
        )
        steps["infra npm ci"] = _run(
            [tools["npm"], "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=project / "infra", log=log
        )
        steps["frontend npm ci"] = _run(
            [tools["npm"], "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=project / "frontend", log=log
        )
        tools.update(
            {
                "infra_python": str(infra_python),
                "cdk": str(project / "infra" / "node_modules" / ".bin" / "cdk"),
                "actionlint": _which("actionlint"),
                "cfn-lint": _which("cfn-lint"),
                "checkov": _which("checkov"),
                "terraform": _which("terraform"),
                "shellcheck": _which("shellcheck"),
                "detect-secrets-hook": _which("detect-secrets-hook"),
            }
        )
        for script in REBUILD_SCRIPTS:
            steps[script] = _run(["bash", script], cwd=project, log=log, timeout=5400)
    else:
        tools["actionlint"] = _which("actionlint")
    failed = {name: code for name, code in steps.items() if code != 0}
    if failed:
        raise ScenarioFailed(f"lock-only environment steps failed: {json.dumps(failed)}")
    return {"steps": steps, "tools": tools}


def _hashed(requirements: Path) -> bool:
    return requirements.is_file() and "--hash=" in requirements.read_text(encoding="utf-8")


def _recorder_run(
    project: Path, suites: list[dict[str, Any]], tools: dict[str, str], out_root: Path, log: Path
) -> dict[str, Any]:
    python = tools["backend_python"]
    start = subprocess.run(
        [
            python,
            "-I",
            RECORDER,
            "start",
            "--output-root",
            str(out_root),
            "--run-id",
            f"clean-clone-{int(time.time())}",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    log.open("a", encoding="utf-8").write(f"\n$ recorder start\n{start.stdout}\n{start.stderr}\n")
    if start.returncode != 0:
        raise ScenarioFailed("the recorder refused to start a run in the clone: " + start.stderr.strip()[-400:])
    run_dirs = sorted(p for p in out_root.iterdir() if p.is_dir())
    if len(run_dirs) != 1:
        raise ScenarioFailed(f"expected one recorder run directory in the clone, found {len(run_dirs)}")
    run_dir = run_dirs[0]
    results: dict[str, int] = {}
    for suite in suites:
        wanted = _suite_placeholders(suite)
        missing = sorted(name for name in wanted if name not in tools)
        if missing:
            raise ScenarioBlocked(f"suite {suite['id']} needs tools the clone did not resolve: {missing}")
        cmd = [python, "-I", RECORDER, "run-suite", "--run-dir", str(run_dir), "--suite-id", suite["id"]]
        for name in sorted(wanted):
            cmd += ["--tool", f"{name}={tools[name]}"]
        results[suite["id"]] = _run(
            cmd, cwd=project, log=log, timeout=int(suite.get("timeout_seconds") or STEP_TIMEOUT) + 300
        )
    status = subprocess.run(
        [python, "-I", RECORDER, "status", "--run-dir", str(run_dir)],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    try:
        status_doc: Any = json.loads(status.stdout)
    except ValueError:
        status_doc = {"raw": status.stdout[-2000:], "stderr": status.stderr[-800:]}
    return {"run_dir": str(run_dir), "suite_exit_codes": results, "status": status_doc}


def _status_has_active_problem(status_doc: Any) -> bool:
    text = json.dumps(status_doc, default=str).lower()
    return '"failed"' in text or '"blocked"' in text or '"error"' in text


def _compare(label: str, primary: dict[str, str], clone: dict[str, str]) -> dict[str, Any]:
    mismatched = sorted(k for k in set(primary) | set(clone) if primary.get(k) != clone.get(k))
    return {
        "label": label,
        "primary_files": len(primary),
        "clone_files": len(clone),
        "identical": not mismatched and bool(primary),
        "mismatched": mismatched[:40],
    }


def _journey(args: argparse.Namespace, artifacts: Path, *, full: bool) -> dict[str, Any]:
    run = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
    source = run["source"]
    commit, project_rel, project_tree = str(source["commit"]), str(source["project_rel"]), str(source["project_tree"])
    repo_root = Path(_git(PROJECT_ROOT, "rev-parse", "--show-toplevel"))
    # Fail closed before any heavy work: the primary artifacts we must compare against.
    primary_dist = _primary_artifacts(args.run_dir, "backend-package", "backend-dist")
    primary_synth = _primary_artifacts(args.run_dir, "strict-synth", "cdk-us-east-1") if full else None
    traceability = json.loads((PROJECT_ROOT / TRACEABILITY).read_text(encoding="utf-8"))
    suites = [s for s in traceability["suites"] if full or s["id"] in ("export-gate", "backend-package")]
    log = artifacts / "clean-clone.log"
    workspace = Path(tempfile.mkdtemp(prefix="acfpr-clean-clone-"))
    outcome: dict[str, Any] = {
        "commit": commit,
        "project_rel": project_rel,
        "workspace": str(workspace),
        "full_gates": full,
    }
    try:
        clone_root = workspace / "clone"
        outcome["clone"] = _clone(repo_root, commit, project_rel, project_tree, clone_root, log)
        project = clone_root / project_rel
        environment = _environments(project, full=full, log=log)
        outcome["environment_steps"] = environment["steps"]
        recorded = _recorder_run(project, suites, environment["tools"], workspace / "evidence", log)
        outcome["recorder"] = recorded
        clone_run = Path(recorded["run_dir"])
        comparisons = [
            _compare(
                "backend-package (wheel + sdist)",
                _digest_tree(primary_dist / "backend-dist", ("*.whl", "*.tar.gz")),
                _digest_tree(
                    _primary_artifacts(clone_run, "backend-package", "backend-dist") / "backend-dist",
                    ("*.whl", "*.tar.gz"),
                ),
            )
        ]
        if full and primary_synth is not None:
            clone_synth = _primary_artifacts(clone_run, "strict-synth", "cdk-us-east-1")
            comparisons.append(
                _compare(
                    "strict-synth (templates + asset manifests, all Regions)",
                    _digest_tree(primary_synth, ("cdk-*/*.template.json", "cdk-*/*.assets.json")),
                    _digest_tree(clone_synth, ("cdk-*/*.template.json", "cdk-*/*.assets.json")),
                )
            )
        outcome["comparisons"] = comparisons
        outcome["suites_passed"] = all(
            code == 0 for code in recorded["suite_exit_codes"].values()
        ) and not _status_has_active_problem(recorded["status"])
        outcome["identical"] = all(c["identical"] for c in comparisons)
    finally:
        write_json(artifacts / "journey-outcome.json", outcome)
        shutil.rmtree(workspace, ignore_errors=True)
    return outcome


def _scenario(args: argparse.Namespace, artifact_dir: Path, *, full: bool) -> dict:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    outcome = _journey(args, artifact_dir, full=full)
    holds = (
        outcome.get("clone", {}).get("clean") is True
        and outcome.get("suites_passed") is True
        and outcome.get("identical") is True
    )
    report = {
        "scenario": args.scenario_id,
        "claim": FULL_GATES_ID if full else DOWNLOADABLE_ID,
        "claim_holds": holds,
        "clone": outcome.get("clone"),
        "environment_steps": outcome.get("environment_steps"),
        "suite_exit_codes": (outcome.get("recorder") or {}).get("suite_exit_codes"),
        "recorder_status": (outcome.get("recorder") or {}).get("status"),
        "comparisons": outcome.get("comparisons"),
    }
    write_json(artifact_dir / "report", report)
    if not holds:
        raise ScenarioFailed(
            f"{report['claim']}: {json.dumps({k: report[k] for k in ('suite_exit_codes', 'comparisons')}, default=str)[:500]}"
        )
    return report


def independent_detached_clone_full_gates(args, artifact_dir):
    return _scenario(args, artifact_dir, full=True)


def downloadable_assets_independent_detached_clean_clone(args, artifact_dir):
    return _scenario(args, artifact_dir, full=False)
