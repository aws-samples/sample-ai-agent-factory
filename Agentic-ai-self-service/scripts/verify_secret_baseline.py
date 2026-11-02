#!/usr/bin/env python3
"""Run the pinned detect-secrets baseline against every tracked sample file."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = PROJECT_ROOT / ".secrets.baseline"
REQUIRED_VERSION = "1.5.0"


class SecretScanError(RuntimeError):
    """The deterministic secret-scan preconditions are not satisfied."""


def _git(repo: Path, *args: str, text: bool = True):
    result = subprocess.run(
        ["git", "-P", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=text,
        timeout=60,
    )
    if result.returncode != 0:
        raise SecretScanError("Git tracked-file discovery failed")
    return result.stdout


def _repo_identity() -> tuple[Path, str]:
    repo = Path(_git(PROJECT_ROOT, "rev-parse", "--show-toplevel").strip()).resolve()
    project_rel = PROJECT_ROOT.resolve().relative_to(repo).as_posix()
    return repo, project_rel


def _validate_baseline(project_rel: str) -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    results = baseline.get("results")
    if not isinstance(results, dict):
        raise SecretScanError("Secret baseline results must be an object")
    prefix = f"{project_rel}/"
    invalid = sorted(path for path in results if not path.startswith(prefix))
    if invalid:
        raise SecretScanError(f"Secret baseline has {len(invalid)} path(s) outside {prefix}; first={invalid[0]}")


def _tracked_files(repo: Path, project_rel: str) -> list[str]:
    raw = _git(repo, "ls-files", "-z", "--", project_rel, text=False)
    files = [os.fsdecode(item) for item in raw.split(b"\0") if item]
    if not files:
        raise SecretScanError("No tracked project files were discovered")
    prefix = f"{project_rel}/"
    if any(not path.startswith(prefix) for path in files):
        raise SecretScanError("Tracked-file discovery escaped the project subtree")
    return files


def _detect_secrets_tool() -> str:
    tool = shutil.which("detect-secrets-hook")
    if tool is None:
        raise SecretScanError(
            f"detect-secrets-hook {REQUIRED_VERSION} is required; install detect-secrets=={REQUIRED_VERSION}"
        )
    version = subprocess.run(
        [tool, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if version.returncode != 0 or version.stdout.strip() != REQUIRED_VERSION:
        raise SecretScanError(
            f"detect-secrets-hook version must be exactly {REQUIRED_VERSION}, got {version.stdout.strip() or 'unknown'}"
        )
    return tool


def main() -> int:
    try:
        repo, project_rel = _repo_identity()
        _validate_baseline(project_rel)
        files = _tracked_files(repo, project_rel)
        tool = _detect_secrets_tool()
        result = subprocess.run(
            [tool, "--baseline", str(BASELINE_PATH.relative_to(repo)), *files],
            cwd=repo,
            check=False,
            timeout=900,
        )
        return result.returncode
    except (SecretScanError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"verify_secret_baseline: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
