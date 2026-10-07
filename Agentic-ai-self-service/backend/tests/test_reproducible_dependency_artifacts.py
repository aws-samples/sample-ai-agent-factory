"""Release artifacts must be dependency-locked and checkout-path neutral."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
PACKAGER_PATH = ROOT / "scripts" / "package-agentcore-deps.py"
AGENTCORE_INSTALLER = ROOT / "scripts" / "install-agentcore-deps.sh"
LAMBDA_INSTALLER = ROOT / "scripts" / "install-lambda-deps.sh"
LAMBDA_INPUT = ROOT / "backend" / "requirements-lambda.in"
LAMBDA_LOCK = ROOT / "backend" / "requirements-lambda.txt"


def _load_packager():
    spec = importlib.util.spec_from_file_location("package_agentcore_deps", PACKAGER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


packager = _load_packager()


def _write_distribution(tree: Path, name: str = "demo-package", version: str = "1.2.3") -> None:
    metadata = tree / f"{name.replace('-', '_')}-{version}.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n")
    (metadata.parent / "RECORD").write_text("path-dependent-record\n")


def _tree(root: Path, *, reverse: bool, timestamp: int, shebang: str) -> Path:
    root.mkdir()
    paths = [root / "demo.py", root / "data" / "value.txt"]
    for path in reversed(paths) if reverse else paths:
        path.parent.mkdir(exist_ok=True)
        path.write_text(path.name)
    _write_distribution(root)
    (root / "bin").mkdir()
    (root / "bin" / "demo").write_text(f"#!{shebang}\n")
    for path in root.rglob("*"):
        os.utime(path, (timestamp, timestamp), follow_symlinks=False)
    return root


def test_agentcore_packager_is_byte_reproducible_across_paths_order_and_mtimes(tmp_path: Path) -> None:
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("demo-package==1.2.3\n")
    left = _tree(tmp_path / "left", reverse=False, timestamp=1_700_000_000, shebang="/left/bin/python")
    right = _tree(tmp_path / "right", reverse=True, timestamp=1_800_000_000, shebang="/right/bin/python")
    left_zip = tmp_path / "left.zip"
    right_zip = tmp_path / "right.zip"

    packager.package_tree(left, constraints, left_zip)
    packager.package_tree(right, constraints, right_zip)

    assert left_zip.read_bytes() == right_zip.read_bytes()
    with zipfile.ZipFile(left_zip) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        assert names == sorted(names)
        assert "demo.py" in names
        assert any(name.endswith(".dist-info/METADATA") for name in names)
        assert not any(name.startswith("bin/") for name in names)
        assert not any(name.endswith(".dist-info/RECORD") for name in names)
        assert {item.date_time for item in infos} == {(1980, 1, 1, 0, 0, 0)}


def test_agentcore_packager_rejects_an_unlocked_transitive_dependency(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    _write_distribution(tree, name="unexpected", version="9.9.9")
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("expected==1.0.0\n")

    with pytest.raises(packager.DependencyLockError, match="unlocked=.*unexpected"):
        packager.package_tree(tree, constraints, tmp_path / "out.zip")


def test_agentcore_packager_rejects_a_mutated_locked_version(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    _write_distribution(tree)
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("demo-package==1.2.2\n")

    with pytest.raises(packager.DependencyLockError, match="installed.*1.2.3"):
        packager.package_tree(tree, constraints, tmp_path / "out.zip")


def _locked_packages() -> dict[str, str]:
    packages: dict[str, str] = {}
    for match in re.finditer(
        r"(?m)^([A-Za-z0-9_.-]+)==([^ \\\n]+) \\\n(?P<body>(?:    --hash=sha256:[0-9a-f]{64}(?: \\\n|\n))+)",
        LAMBDA_LOCK.read_text(),
    ):
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        packages[name] = match.group(2)
        assert "--hash=sha256:" in match.group("body")
    return packages


def test_lambda_lock_covers_every_direct_input_with_hashes() -> None:
    locked = _locked_packages()
    direct = {
        re.sub(r"[-_.]+", "-", Requirement(line).name).lower()
        for raw in LAMBDA_INPUT.read_text().splitlines()
        if (line := raw.split("#", 1)[0].strip())
    }
    assert len(locked) >= 30
    assert direct <= set(locked)
    assert locked["boto3"] == "1.43.109"
    assert locked["botocore"] == "1.43.109"


def test_lambda_installer_requires_hashes_and_removes_path_dependent_files() -> None:
    script = LAMBDA_INSTALLER.read_text()
    assert "--require-hashes" in script
    assert 'rm -rf "${LIB_DIR:?}/bin"' in script
    assert '-path "*.dist-info/RECORD" -delete' in script
    assert "manylinux2014_x86_64" in script
    assert "--python-version 3.12" in script


def test_agentcore_installer_uses_only_the_validated_deterministic_packager() -> None:
    script = AGENTCORE_INSTALLER.read_text()
    assert 'python3 "${PACKAGER}"' in script
    assert script.count('package_bundle "${target_dir}" "${zip_path}"') == 2
    assert "zip -r" not in script
    assert 'rm -rf "${target_dir:?}/bin"' in script
    assert '-path "*.dist-info/RECORD" -delete' in script


SDIST_EXCLUSION = '[tool.hatch.build.targets.sdist]\n# backend/lib is regenerated from the hash lock for deployment; shipping it in an\n# sdist makes package bytes depend on whether that deployment step ran first.\nexclude = ["/lib"]\n'


def _build_sdist_fixture(tmp_path: Path, *, remove_exclusion: bool) -> set[str]:
    fixture = tmp_path / ("mutated" if remove_exclusion else "protected")
    (fixture / "src" / "app").mkdir(parents=True)
    (fixture / "src" / "app" / "__init__.py").write_text("")
    (fixture / "lib").mkdir()
    (fixture / "lib" / "generated-lambda-only.txt").write_text("generated deployment dependency\n")
    (fixture / "agentcore-deps").mkdir()
    (fixture / "agentcore-deps" / "base.zip").write_bytes(b"locked runtime bundle")

    pyproject = (ROOT / "backend" / "pyproject.toml").read_text()
    assert 'requires = ["hatchling==1.32.4"]' in pyproject
    assert pyproject.count(SDIST_EXCLUSION) == 1
    if remove_exclusion:
        pyproject = pyproject.replace(f"\n{SDIST_EXCLUSION}\n", "\n", 1)
    (fixture / "pyproject.toml").write_text(pyproject)

    uv = shutil.which("uv")
    assert uv is not None, "uv is required to execute the real sdist build contract"
    output = tmp_path / ("mutated-dist" if remove_exclusion else "protected-dist")
    result = subprocess.run(
        [uv, "build", "--sdist", "--out-dir", str(output)],
        cwd=fixture,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archives = list(output.glob("*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0], "r:gz") as archive:
        return {"/".join(Path(name).parts[1:]) for name in archive.getnames()}


def test_sdist_excludes_generated_lambda_tree_but_keeps_runtime_bundles(tmp_path: Path) -> None:
    members = _build_sdist_fixture(tmp_path, remove_exclusion=False)

    assert "lib/generated-lambda-only.txt" not in members
    assert "agentcore-deps/base.zip" in members


def test_removing_sdist_exclusion_reintroduces_build_order_dependency(tmp_path: Path) -> None:
    members = _build_sdist_fixture(tmp_path, remove_exclusion=True)

    assert "lib/generated-lambda-only.txt" in members
