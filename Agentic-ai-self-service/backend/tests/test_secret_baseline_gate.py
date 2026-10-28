"""The release secret scan covers every tracked sample file with an exact baseline."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "verify_secret_baseline.py"

SPEC = importlib.util.spec_from_file_location("verify_secret_baseline", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
secret_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(secret_gate)


def test_baseline_paths_resolve_from_repository_root():
    repo, project_rel = secret_gate._repo_identity()
    assert repo / project_rel == PROJECT_ROOT
    secret_gate._validate_baseline(project_rel)


def test_tracked_file_scan_is_bounded_to_the_sample():
    repo, project_rel = secret_gate._repo_identity()
    files = secret_gate._tracked_files(repo, project_rel)
    assert f"{project_rel}/scripts/release_evidence.py" in files
    assert all(path.startswith(f"{project_rel}/") for path in files)
    assert not any("/build/" in path or "/.kiro/" in path for path in files)


def test_mutation_with_repository_relative_baseline_key_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mutated = tmp_path / "baseline.json"
    mutated.write_text(json.dumps({"results": {"README.md": []}}), encoding="utf-8")
    monkeypatch.setattr(secret_gate, "BASELINE_PATH", mutated)
    with pytest.raises(secret_gate.SecretScanError, match="outside Agentic-ai-self-service/"):
        secret_gate._validate_baseline("Agentic-ai-self-service")


def test_scanner_version_is_exactly_pinned():
    assert secret_gate.REQUIRED_VERSION == "1.5.0"
