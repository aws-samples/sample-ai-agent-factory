"""Source identity baked into the stack at synth time.

The deployed platform used to carry no record of what it was built from: a live
scenario could only *infer* the deployed commit by hashing every Lambda's code
against a sealed assembly. These three values let a stack say so itself --
``SourceCommit``, ``SourceProjectTree`` (the git tree id of the project
directory, which is what the release evidence recorder binds a run to) and
``SourceDirty`` (whether the synthesizing working tree had uncommitted changes
under the project or the repository CI workflow).

They are *claims*, not proof: the release scenarios still compare the live
template digest and every Lambda ``CodeSha256`` against the candidate's sealed
assembly, and refuse a stack that says ``SourceDirty=true``. Outside a git
checkout (a customer who downloaded the source as an archive) every value is
``unknown``; a synth must never fail for want of git.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

UNKNOWN = "unknown"
ROOT_WORKFLOW_RELATIVE = ".github/workflows/agentic-ai-self-service-ci.yml"
_SAFE_GIT_OBJECT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class SourceIdentity:
    commit: str
    project_tree: str
    dirty: str  # "true" / "false" / "unknown" -- CloudFormation outputs are strings

    @property
    def known(self) -> bool:
        return UNKNOWN not in (self.commit, self.project_tree, self.dirty)


def _git(repo: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-P", *args],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def derive_source_identity(project_root: Path) -> SourceIdentity:
    """Return the identity of ``project_root`` as git sees it, or ``unknown`` throughout.

    ``project_root`` is the sample's directory (the one holding ``infra/``), which may be a
    subdirectory of the repository. Partial knowledge is reported as ``unknown`` for every
    field rather than mixing a real commit with an unknown tree: a reader must be able to
    trust that a known value was derived alongside the others.
    """
    project_root = project_root.resolve()
    top = _git(project_root, "rev-parse", "--show-toplevel")
    if not top:
        return SourceIdentity(UNKNOWN, UNKNOWN, UNKNOWN)
    repo = Path(top).resolve()
    try:
        project_rel = project_root.relative_to(repo).as_posix()
    except ValueError:
        return SourceIdentity(UNKNOWN, UNKNOWN, UNKNOWN)
    commit = _git(repo, "rev-parse", "HEAD")
    tree_spec = f"{commit}:{project_rel}" if project_rel not in ("", ".") else f"{commit}^{{tree}}"
    project_tree = _git(repo, "rev-parse", tree_spec) if commit else None
    status = (
        _git(
            repo, "status", "--porcelain=v1", "--untracked-files=all", "--", project_rel or ".", ROOT_WORKFLOW_RELATIVE
        )
        if commit
        else None
    )
    if (
        not commit
        or not project_tree
        or status is None
        or not _SAFE_GIT_OBJECT.fullmatch(commit)
        or not _SAFE_GIT_OBJECT.fullmatch(project_tree)
    ):
        return SourceIdentity(UNKNOWN, UNKNOWN, UNKNOWN)
    return SourceIdentity(commit, project_tree, "true" if status else "false")
