"""The stack says what it was synthesized from -- and says ``unknown`` rather than guessing.

``SourceCommit`` / ``SourceProjectTree`` / ``SourceDirty`` are claims the release scenarios
cross-check against the candidate's sealed assembly; they must be exact in a git checkout,
``true`` the moment the project or the repository CI workflow has uncommitted changes, and
``unknown`` throughout (never a mix) when the source is not a git checkout at all.
"""

from __future__ import annotations

import pathlib
import subprocess

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from source_identity import ROOT_WORKFLOW_RELATIVE, UNKNOWN, SourceIdentity, derive_source_identity
from stacks.platform_stack import PlatformStack


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-P", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo_with_project(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    repo = tmp_path / "repo"
    project = repo / "Agentic-ai-self-service"
    (project / "infra").mkdir(parents=True)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ROOT_WORKFLOW_RELATIVE).write_text("name: ci\n")
    (project / "infra" / "app.py").write_text("print('app')\n")
    (repo / "README.md").write_text("# fixture\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo, project


def test_a_clean_checkout_reports_exact_commit_and_project_tree(repo_with_project):
    repo, project = repo_with_project
    identity = derive_source_identity(project)
    assert identity.known
    assert identity.commit == _git(repo, "rev-parse", "HEAD")
    assert identity.project_tree == _git(repo, "rev-parse", "HEAD:Agentic-ai-self-service")
    assert identity.dirty == "false"


def test_uncommitted_project_or_workflow_changes_mark_the_synth_dirty(repo_with_project):
    repo, project = repo_with_project
    (project / "infra" / "app.py").write_text("print('changed')\n")
    assert derive_source_identity(project).dirty == "true"
    _git(repo, "checkout", "--", "Agentic-ai-self-service")
    assert derive_source_identity(project).dirty == "false"
    (repo / ROOT_WORKFLOW_RELATIVE).write_text("name: ci-changed\n")
    assert derive_source_identity(project).dirty == "true", "the repository CI workflow is part of the identity"


def test_changes_outside_the_project_and_workflow_do_not_mark_it_dirty(repo_with_project):
    repo, project = repo_with_project
    (repo / "README.md").write_text("# unrelated edit\n")
    assert derive_source_identity(project).dirty == "false"


def test_a_source_archive_without_git_is_unknown_throughout(tmp_path: pathlib.Path):
    project = tmp_path / "archive" / "Agentic-ai-self-service"
    (project / "infra").mkdir(parents=True)
    identity = derive_source_identity(project)
    assert identity == SourceIdentity(UNKNOWN, UNKNOWN, UNKNOWN)
    assert not identity.known


def _synth(**kwargs) -> assertions.Template:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "acf-production",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
        environment_name="production",
        project_name="acf",
        **kwargs,
    )
    return assertions.Template.from_stack(stack)


def test_the_template_emits_the_three_source_outputs_with_the_supplied_identity():
    identity = SourceIdentity("a" * 40, "b" * 40, "false")
    template = _synth(source_identity=identity)
    template.has_output("SourceCommit", {"Value": "a" * 40})
    template.has_output("SourceProjectTree", {"Value": "b" * 40})
    template.has_output("SourceDirty", {"Value": "false"})


def test_without_an_explicit_identity_the_stack_derives_it_from_this_checkout():
    expected = derive_source_identity(pathlib.Path(__file__).resolve().parents[2])
    outputs = _synth().to_json()["Outputs"]
    assert outputs["SourceCommit"]["Value"] == expected.commit
    assert outputs["SourceProjectTree"]["Value"] == expected.project_tree
    assert outputs["SourceDirty"]["Value"] == expected.dirty
