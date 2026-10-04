"""Provenance for evidence records: commit SHA, manifest SHA, harness version.

Evidence is only useful if it can be tied back to the exact code and account
topology that produced it. The commit SHA is read from the environment first
(CI supplies it) and otherwise from the git directory *as files* — no
subprocess, no network, and it works inside a linked worktree where ``.git`` is
a file rather than a directory.

If the SHA cannot be determined, this module returns ``None`` rather than a
placeholder. Evidence validation then rejects the record, which forces CI to
supply it explicitly instead of shipping an untraceable bundle.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
import os

HARNESS_VERSION = "1.0.0"
EVIDENCE_SCHEMA_VERSION = "1.0"

ENV_COMMIT_SHA = "AGENTICAI_EVIDENCE_COMMIT_SHA"
_FALLBACK_COMMIT_ENV = (
    ENV_COMMIT_SHA,
    "GIT_COMMIT",
    "GITHUB_SHA",
    "CODEBUILD_RESOLVED_SOURCE_VERSION",
    "CI_COMMIT_SHA",
)

_SHA1_RE = re.compile(r"\A[0-9a-f]{40}\Z")


def is_commit_sha(value: str | None) -> bool:
    return bool(value) and bool(_SHA1_RE.match(str(value).strip().lower()))


def _resolve_git_dir(start: Path) -> Path | None:
    """Find the git directory for ``start``, following worktree indirection."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        git_path = candidate / ".git"
        if git_path.is_dir():
            return git_path
        if git_path.is_file():
            try:
                content = git_path.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if content.startswith("gitdir:"):
                target = Path(content.split(":", 1)[1].strip())
                if not target.is_absolute():
                    target = (candidate / target).resolve()
                if target.is_dir():
                    return target
            return None
    return None


def _read_head_sha(git_dir: Path) -> str | None:
    head_file = git_dir / "HEAD"
    try:
        head = head_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if is_commit_sha(head):
        return head.lower()
    if not head.startswith("ref:"):
        return None
    ref = head.split(":", 1)[1].strip()

    ref_file = git_dir / ref
    try:
        value = ref_file.read_text(encoding="utf-8").strip()
        if is_commit_sha(value):
            return value.lower()
    except OSError:
        pass

    # A linked worktree's refs live in the common dir.
    common_dir_file = git_dir / "commondir"
    common_dir: Path | None = None
    try:
        common = common_dir_file.read_text(encoding="utf-8").strip()
        candidate = Path(common)
        common_dir = candidate if candidate.is_absolute() else (git_dir / candidate)
        common_dir = common_dir.resolve()
    except OSError:
        common_dir = None

    for base in [path for path in (common_dir,) if path is not None]:
        try:
            value = (base / ref).read_text(encoding="utf-8").strip()
            if is_commit_sha(value):
                return value.lower()
        except OSError:
            pass

    for base in [path for path in (git_dir, common_dir) if path is not None]:
        packed = base / "packed-refs"
        try:
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref and is_commit_sha(parts[0]):
                    return parts[0].lower()
        except OSError:
            continue
    return None


def commit_sha(
    repo_root: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Best-effort commit SHA, or ``None`` if it cannot be determined."""
    environ: Mapping[str, str] = os.environ if env is None else env
    for name in _FALLBACK_COMMIT_ENV:
        value = (environ.get(name) or "").strip().lower()
        if is_commit_sha(value):
            return value
    start = Path(repo_root) if repo_root is not None else Path(__file__).parent
    git_dir = _resolve_git_dir(start)
    if git_dir is None:
        return None
    return _read_head_sha(git_dir)


@dataclass(frozen=True)
class Provenance:
    """Everything an evidence record needs to be traceable."""

    commit_sha: str | None
    manifest_sha: str
    catalog_sha: str
    harness_version: str = HARNESS_VERSION
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, str | None]:
        return {
            "commitSha": self.commit_sha,
            "manifestSha": self.manifest_sha,
            "catalogSha": self.catalog_sha,
            "harnessVersion": self.harness_version,
            "schemaVersion": self.schema_version,
        }


__all__ = [
    "ENV_COMMIT_SHA",
    "EVIDENCE_SCHEMA_VERSION",
    "HARNESS_VERSION",
    "Provenance",
    "commit_sha",
    "is_commit_sha",
]
