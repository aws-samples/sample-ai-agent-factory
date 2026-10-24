#!/usr/bin/env python3
"""Validate and package an AgentCore dependency tree reproducibly."""

from __future__ import annotations

import argparse
import email
import hashlib
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path

_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_NAME_NORMALIZER = re.compile(r"[-_.]+")


class DependencyLockError(RuntimeError):
    """An installed dependency tree does not match its complete lock."""


def normalize_name(name: str) -> str:
    return _NAME_NORMALIZER.sub("-", name).lower()


def load_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, separator, version = line.partition("==")
        if separator != "==" or not name or not version or "=" in name or "=" in version:
            raise DependencyLockError(f"non-exact dependency lock entry: {raw!r}")
        normalized = normalize_name(name)
        if normalized in pins:
            raise DependencyLockError(f"duplicate dependency lock entry: {normalized}")
        pins[normalized] = version
    if not pins:
        raise DependencyLockError("dependency lock is empty")
    return pins


def installed_distributions(source: Path) -> dict[str, str]:
    installed: dict[str, str] = {}
    for metadata_path in sorted(source.glob("*.dist-info/METADATA")):
        message = email.message_from_bytes(metadata_path.read_bytes())
        name = message.get("Name")
        version = message.get("Version")
        if not name or not version:
            raise DependencyLockError(f"incomplete package metadata: {metadata_path}")
        normalized = normalize_name(name)
        previous = installed.setdefault(normalized, version)
        if previous != version:
            raise DependencyLockError(f"multiple installed versions for {normalized}: {previous}, {version}")
    if not installed:
        raise DependencyLockError(f"no installed distributions found in {source}")
    return installed


def verify_tree(source: Path, constraints: Path) -> dict[str, str]:
    pins = load_pins(constraints)
    installed = installed_distributions(source)
    unlocked = sorted(name for name in installed if name not in pins)
    wrong = {
        name: {"locked": pins[name], "installed": version}
        for name, version in sorted(installed.items())
        if name in pins and pins[name] != version
    }
    if unlocked or wrong:
        raise DependencyLockError(f"dependency tree is not fully locked: unlocked={unlocked}, wrong={wrong}")
    return installed


def _include(path: Path, source: Path) -> bool:
    relative = path.relative_to(source)
    if "__pycache__" in relative.parts or path.suffix == ".pyc":
        return False
    if relative.parts and relative.parts[0] == "bin":
        return False
    if path.name == "RECORD" and path.parent.name.endswith(".dist-info"):
        return False
    return True


def source_files(source: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        if path.is_symlink():
            raise DependencyLockError(f"symlinks are not allowed in dependency bundles: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise DependencyLockError(f"unsupported dependency bundle entry: {path}")
        if _include(path, source):
            files.append(path)
    if not files:
        raise DependencyLockError(f"dependency tree has no packageable files: {source}")
    return files


def write_deterministic_zip(source: Path, output: Path) -> tuple[int, str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    files = source_files(source)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in files:
                relative = path.relative_to(source).as_posix()
                mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                info = zipfile.ZipInfo(relative, date_time=_FIXED_ZIP_TIME)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return len(files), hashlib.sha256(output.read_bytes()).hexdigest()


def package_tree(source: Path, constraints: Path, output: Path) -> tuple[int, int, str]:
    source = Path(os.path.abspath(source))
    constraints = Path(os.path.abspath(constraints))
    output = Path(os.path.abspath(output))
    if source.is_symlink() or not source.is_dir():
        raise DependencyLockError(f"dependency source must be a real directory: {source}")
    if constraints.is_symlink() or not constraints.is_file():
        raise DependencyLockError(f"dependency lock must be a real file: {constraints}")
    installed = verify_tree(source, constraints)
    file_count, archive_digest = write_deterministic_zip(source, output)
    return len(installed), file_count, archive_digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    distribution_count, file_count, archive_digest = package_tree(args.source, args.constraints, args.output)
    print(f"Packaged {distribution_count} locked distributions / {file_count} files; sha256={archive_digest}")


if __name__ == "__main__":
    main()
