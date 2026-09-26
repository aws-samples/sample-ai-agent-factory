"""Live AWS tools must never silently select a Region."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE_ROOTS = (
    ROOT / "scripts",
    ROOT / "tests" / "adversarial",
    ROOT / "tests" / "integration",
    ROOT / "tests" / "smoke",
    ROOT / "tests" / "teardown",
)
PYTHON_DEFAULTS = (
    re.compile(r'add_argument\(["\']--region["\'],\s*default=["\']us-'),
    re.compile(r'get\(["\']AWS_REGION["\'],\s*["\']us-'),
    re.compile(r'get\(["\']AWS_DEFAULT_REGION["\'],\s*["\']us-'),
)
SHELL_DEFAULT = re.compile(r'(?:AWS|CDK_DEFAULT)_REGION[^\n]*:-us-')


def live_files(suffix: str):
    for root in LIVE_ROOTS:
        for path in root.rglob(f"*{suffix}"):
            if any(part in {".venv", "node_modules", "__pycache__"} for part in path.parts):
                continue
            yield path


def test_python_live_tools_have_no_implicit_us_region() -> None:
    offenders: list[str] = []
    for path in live_files(".py"):
        if path.name.startswith("test_"):
            continue
        source = path.read_text()
        if any(pattern.search(source) for pattern in PYTHON_DEFAULTS):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_shell_live_tools_have_no_implicit_us_region() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in live_files(".sh")
        if SHELL_DEFAULT.search(path.read_text())
    ]
    assert offenders == []


def test_destructive_and_release_gates_require_region_explicitly() -> None:
    required_python = [
        ROOT / "scripts" / "final_teardown.py",
        ROOT / "scripts" / "gap_closure_live_verify.py",
    ]
    required_python.extend(
        path
        for path in (ROOT / "scripts").glob("live-*/*.py")
        if not path.name.startswith("test_")
        and 'add_argument("--region"' in path.read_text()
    )
    for path in required_python:
        source = path.read_text()
        assert 'add_argument("--region", required=True)' in source, path

    evaluation = (ROOT / "scripts" / "evaluation_gate.py").read_text()
    assert "EVAL_REGION must identify" in evaluation

    smoke = (ROOT / "tests" / "smoke" / "smoke.py").read_text()
    assert "AWS_REGION or AWS_DEFAULT_REGION is required" in smoke
