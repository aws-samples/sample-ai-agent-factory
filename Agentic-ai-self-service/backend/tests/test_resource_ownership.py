"""Ownership tagging: which deployment is allowed to delete a resource.

Why these tests exist: the platform creates resources whose names are
account-global and carry no deployment identity — Cognito pools
``AgentCore-{gateway name}``, secrets under ``agentcore-connector/`` and
``agentcore-otel/``, IAM roles ``AgentCoreMemory-*`` and ``AgentCoreRuntime-*``.
``scripts/cleanup.sh`` swept those namespaces by name prefix, so a customer with
two deployments in one account (dev + prod, or two teams — routine, because
customers deploy and delete this often) destroyed the other deployment's
resources on teardown, including secrets holding raw customer credentials.

The identity string is duplicated in bash (``cleanup.sh``'s ``STACK_OWNER_ID``),
so the format is a contract, not an implementation detail. The bash side is
exercised for real against AWS by ``scripts/verify-cleanup-ownership.sh``.
"""

from __future__ import annotations

import pytest
from app.services import resource_ownership as ro


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("PROJECT_NAME", "ENVIRONMENT", "ENVIRONMENT_NAME", "APP_AWS_REGION", "AWS_REGION"):
        monkeypatch.delenv(var, raising=False)


def test_stack_id_is_project_env_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact format cleanup.sh recomputes as ${PROJECT_NAME}-${ENVIRONMENT_NAME}-${AWS_REGION}.

    Changing this string without changing cleanup.sh makes teardown skip every
    resource this stack owns, which fails safe but silently leaks.
    """
    monkeypatch.setenv("PROJECT_NAME", "acme-platform")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert ro.stack_id("eu-central-1") == "acme-platform-prod-eu-central-1"


def test_stack_id_defaults_match_the_shell_defaults() -> None:
    """cleanup.sh defaults to agentcore-workflow / dev / us-east-1; so must this.

    A mismatch here means a default-configured deployment tags its resources with
    one identity and then refuses to delete them under another.
    """
    assert ro.stack_id() == "agentcore-workflow-dev-us-east-1"


def test_region_is_part_of_the_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same {project}-{env} in two regions must not claim each other's resources.

    IAM roles are not regional at all, and config.py deliberately supports the
    same environment in a second region.
    """
    monkeypatch.setenv("PROJECT_NAME", "p")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert ro.stack_id("us-east-1") != ro.stack_id("eu-central-1")


def test_explicit_region_beats_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers that know the target region must win over the Lambda's own region."""
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    assert ro.stack_id("ap-southeast-2").endswith("-ap-southeast-2")


def test_environment_name_is_accepted_as_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API Lambdas set ENVIRONMENT; the shell config uses ENVIRONMENT_NAME."""
    monkeypatch.setenv("ENVIRONMENT_NAME", "staging")
    assert "-staging-" in ro.stack_id("us-east-1")


def test_owner_tags_always_include_both_keys() -> None:
    tags = ro.owner_tags("us-east-1")
    assert tags["ManagedBy"] == "agentcore-flows"
    assert tags["AgentCoreStack"] == ro.stack_id("us-east-1")


def test_governance_tags_are_merged_but_cannot_hijack_ownership() -> None:
    """A caller-supplied owner tag must not be able to reassign ownership.

    Governance tags come from user-controlled canvas metadata. If one could set
    AgentCoreStack, a tenant could mark its resources as belonging to another
    deployment and have that deployment's teardown delete them.
    """
    tags = ro.owner_tags(
        "us-east-1",
        extra={"cost-center": "cc-42", "AgentCoreStack": "victim-stack", "ManagedBy": "attacker"},
    )
    assert tags["cost-center"] == "cc-42"
    assert tags["AgentCoreStack"] == ro.stack_id("us-east-1")
    assert tags["ManagedBy"] == "agentcore-flows"


def test_owner_tag_list_is_the_iam_and_secretsmanager_shape() -> None:
    as_list = ro.owner_tag_list("us-east-1")
    assert {"Key": "AgentCoreStack", "Value": ro.stack_id("us-east-1")} in as_list
    assert all(set(item) == {"Key", "Value"} for item in as_list)


def test_ownership_accepts_both_tag_shapes() -> None:
    """SecretsManager/IAM return [{Key,Value}]; Cognito returns a plain map."""
    sid = ro.stack_id("us-east-1")
    assert ro.is_owned_by_this_stack([{"Key": "AgentCoreStack", "Value": sid}], "us-east-1")
    assert ro.is_owned_by_this_stack({"AgentCoreStack": sid}, "us-east-1")


@pytest.mark.parametrize(
    "tags",
    [
        None,
        {},
        [],
        {"ManagedBy": "agentcore-flows"},
        [{"Key": "ManagedBy", "Value": "agentcore-flows"}],
        {"AgentCoreStack": "some-other-stack-us-east-1"},
        {"AgentCoreStack": ""},
        [{"not": "a tag"}],
    ],
)
def test_ownership_fails_closed(tags) -> None:
    """Anything short of this stack's own tag is foreign.

    Untagged is deliberately foreign: a resource created before the tag existed
    and a resource belonging to someone else are indistinguishable, and only one
    of those two mistakes is recoverable. Note ManagedBy alone is NOT enough — it
    names the product, so it is present on every deployment's resources.
    """
    assert ro.is_owned_by_this_stack(tags, "us-east-1") is False


def test_ownership_is_region_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Frankfurt-owned role must not be claimed by a us-east-1 teardown."""
    monkeypatch.setenv("PROJECT_NAME", "agentcore-workflow")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    frankfurt = {"AgentCoreStack": ro.stack_id("eu-central-1")}
    assert ro.is_owned_by_this_stack(frankfurt, "eu-central-1") is True
    assert ro.is_owned_by_this_stack(frankfurt, "us-east-1") is False
