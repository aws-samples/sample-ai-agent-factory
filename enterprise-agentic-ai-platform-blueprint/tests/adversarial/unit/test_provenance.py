"""Provenance: commit SHA discovery without a subprocess or a network call.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.provenance import (
    ENV_COMMIT_SHA,
    HARNESS_VERSION,
    Provenance,
    commit_sha,
    is_commit_sha,
)

pytestmark = pytest.mark.adversarial

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"


def test_env_commit_sha_wins():
    assert commit_sha(env={ENV_COMMIT_SHA: SHA}) == SHA


@pytest.mark.parametrize(
    "name", ["GIT_COMMIT", "GITHUB_SHA", "CODEBUILD_RESOLVED_SOURCE_VERSION"]
)
def test_ci_commit_env_vars_are_honoured(name):
    assert commit_sha(env={name: SHA}) == SHA


def test_invalid_env_value_is_ignored(tmp_path: Path):
    assert commit_sha(repo_root=tmp_path, env={ENV_COMMIT_SHA: "HEAD"}) is None


def test_detached_head_is_read_from_the_git_directory(tmp_path: Path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text(SHA + "\n", encoding="utf-8")
    assert commit_sha(repo_root=tmp_path, env={}) == SHA


def test_symbolic_head_is_followed_to_the_ref_file(tmp_path: Path):
    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(SHA + "\n", encoding="utf-8")
    assert commit_sha(repo_root=tmp_path, env={}) == SHA


def test_packed_refs_are_consulted(tmp_path: Path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted\n{SHA} refs/heads/main\n",
        encoding="utf-8",
    )
    assert commit_sha(repo_root=tmp_path, env={}) == SHA


def test_linked_worktree_git_file_is_followed(tmp_path: Path):
    """This repository is checked out as a linked worktree."""
    common = tmp_path / "main-checkout" / ".git"
    (common / "refs" / "heads").mkdir(parents=True)
    (common / "refs" / "heads" / "feature").write_text(SHA + "\n", encoding="utf-8")

    worktree_git = common / "worktrees" / "wt1"
    worktree_git.mkdir(parents=True)
    (worktree_git / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")

    worktree = tmp_path / "wt1"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {worktree_git}\n", encoding="utf-8")

    assert commit_sha(repo_root=worktree, env={}) == SHA


def test_missing_git_directory_returns_none(tmp_path: Path):
    assert commit_sha(repo_root=tmp_path, env={}) is None


def test_unresolvable_ref_returns_none(tmp_path: Path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/ghost\n", encoding="utf-8")
    assert commit_sha(repo_root=tmp_path, env={}) is None


@pytest.mark.parametrize("value", [SHA, OTHER_SHA, SHA.upper()])
def test_is_commit_sha_accepts_sha1(value):
    assert is_commit_sha(value)


@pytest.mark.parametrize("value", [None, "", "HEAD", "abc", "g" * 40, SHA + "0"])
def test_is_commit_sha_rejects_everything_else(value):
    assert not is_commit_sha(value)


def test_provenance_serializes_all_hashes():
    payload = Provenance(
        commit_sha=SHA, manifest_sha="a" * 64, catalog_sha="b" * 64
    ).to_dict()
    assert payload["commitSha"] == SHA
    assert payload["manifestSha"] == "a" * 64
    assert payload["catalogSha"] == "b" * 64
    assert payload["harnessVersion"] == HARNESS_VERSION
