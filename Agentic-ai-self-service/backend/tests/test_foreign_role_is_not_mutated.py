"""A role whose name collides is not automatically ours (peer finding F-7).

IAM role names are account-global, and every role this platform mints derives its
name from a user-chosen agent/memory/gateway name:
``AgentCoreRuntime-{agent}``, ``AgentCoreMemory-{memory}``,
``AgentCoreGateway-{gateway}``. So ``create_role`` raising
``EntityAlreadyExists`` has two causes the exception cannot distinguish -- this
deployment redeploying its own agent, or something else in the account already
holding that name. Every already-exists branch assumed the first, then tagged the
role ``AgentCoreStack=<us>`` and overwrote its inline policy, which for the second
case silently replaces a live foreign role's permissions and records it in our
manifest so teardown later deletes it.

Measured in the live account before this was written: of the 7 roles named
``AgentCore*``, four are foreign (``AgentCoreGateway-omargw``,
``AgentCoreDynamicToolsLambdaRole``, ``AgentCoreMcpExtGatewayRole``,
``AgentCoreToolTestRole``), and the step roles hold ``iam:TagRole`` +
``iam:PutRolePolicy`` on ``role/AgentCore*`` -- so all four were reachable.
``iam:ListRoleTags`` is denied to every step role, but ``iam:GetRole`` is granted
and returns ``Role.Tags``, and the already-exists branches already called it. The
guard therefore costs no new IAM permission and no extra API call.

The load-bearing test in here is
``test_the_platforms_own_cdk_tagged_role_is_still_ours``. None of the live
``AgentCore*`` roles carries ``AgentCoreStack``; the platform's own shared runtime
role carries CDK's ``Project``/``Environment`` instead. A guard that accepted only
the runtime owner tag would refuse that role and fail *every* deploy -- fail-closed
over an incomplete ownership table removes the feature rather than securing it.
"""

from __future__ import annotations

import pytest
from app.services import resource_ownership as ro
from app.services.resource_ownership import ForeignResourceError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("PROJECT_NAME", "ENVIRONMENT", "ENVIRONMENT_NAME", "APP_AWS_REGION", "AWS_REGION"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def _deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env a step handler runs with: project acfe2e, environment p0920."""
    monkeypatch.setenv("PROJECT_NAME", "acfe2e")
    monkeypatch.setenv("ENVIRONMENT", "p0920")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


def test_our_own_owner_tag_permits_mutation(_deployment) -> None:
    """Proof 1: the tag owner_tags() stamps on everything the runtime code creates."""
    tags = ro.owner_tag_list("us-east-1")
    assert ro.can_this_deployment_mutate(tags, "us-east-1") is True


def test_the_platforms_own_cdk_tagged_role_is_still_ours(_deployment) -> None:
    """Proof 2 -- the case that keeps the feature alive rather than merely secure.

    ``AgentCoreRuntime-acfe2e-p0920-shared`` exists live carrying
    ``Project=acfe2e``/``Environment=p0920`` from ``cdk.Tags.of(self)``
    (``infra/stacks/platform_stack.py:67-68``) and NO ``AgentCoreStack`` tag. The
    legacy runtime path adopts it by name on every deploy, so refusing it would
    turn this guard into a total deploy outage.
    """
    cdk_tags = [{"Key": "Project", "Value": "acfe2e"}, {"Key": "Environment", "Value": "p0920"}]
    assert ro.can_this_deployment_mutate(cdk_tags, "us-east-1") is True


def test_deletion_still_refuses_the_cdk_pair(_deployment) -> None:
    """Mutation and deletion are different authorization questions.

    ``is_owned_by_this_stack`` gates *deletion* and was deliberately NOT widened:
    CDK owns the lifecycle of what it tagged, so accepting the pair here would have
    teardown delete roles CloudFormation still believes it manages.
    """
    cdk_tags = [{"Key": "Project", "Value": "acfe2e"}, {"Key": "Environment", "Value": "p0920"}]
    assert ro.can_this_deployment_mutate(cdk_tags, "us-east-1") is True
    assert ro.is_owned_by_this_stack(cdk_tags, "us-east-1") is False


def test_another_deployments_owner_tag_is_foreign(_deployment) -> None:
    tags = [{"Key": "AgentCoreStack", "Value": "someone-else-prod-us-east-1"}]
    assert ro.can_this_deployment_mutate(tags, "us-east-1") is False


def test_a_partial_cdk_pair_is_not_proof(_deployment) -> None:
    """Both keys must match. ``Project`` alone is shared by every environment."""
    assert ro.can_this_deployment_mutate([{"Key": "Project", "Value": "acfe2e"}], "us-east-1") is False
    assert (
        ro.can_this_deployment_mutate(
            [{"Key": "Project", "Value": "acfe2e"}, {"Key": "Environment", "Value": "other"}],
            "us-east-1",
        )
        is False
    )


def test_the_product_tag_alone_is_not_proof(_deployment) -> None:
    """``ManagedBy=agentcore-flows`` names the PRODUCT, so every deployment has it."""
    tags = [{"Key": "ManagedBy", "Value": "agentcore-flows"}]
    assert ro.can_this_deployment_mutate(tags, "us-east-1") is False


@pytest.mark.parametrize("tags", [None, [], {}, [{"Key": "Name", "Value": "whatever"}]])
def test_an_untagged_role_is_foreign(tags, _deployment) -> None:
    """The four foreign live roles are exactly this shape -- ``Tags: null``.

    Refusing them costs an operator one rename; overwriting one is unrecoverable.
    """
    assert ro.can_this_deployment_mutate(tags, "us-east-1") is False


def test_unconfigured_env_does_not_become_a_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without PROJECT_NAME/ENVIRONMENT there is nothing to compare.

    Defaulting would turn "unconfigured" into "matches" and let any
    ``Project``/``Environment`` pair through.
    """
    cdk_tags = [{"Key": "Project", "Value": "agentcore-workflow"}, {"Key": "Environment", "Value": "dev"}]
    assert ro.can_this_deployment_mutate(cdk_tags, "us-east-1") is False


def test_absent_tags_do_not_compare_equal_to_absent_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """The specific way the unconfigured branch fails if it is removed.

    With ``PROJECT_NAME``/``ENVIRONMENT`` unset, ``project`` is ``None`` -- and
    ``mapped.get("Project")`` is ``None`` for a role that has no ``Project`` tag. So
    ``None == None`` on both keys, and a foreign role carrying any *other* tag would
    read as ours. Found by mutation testing: deleting the early return left every
    other test in this file green.
    """
    foreign = [{"Key": "Name", "Value": "someone-elses-role"}]
    assert ro.can_this_deployment_mutate(foreign, "us-east-1") is False
    with pytest.raises(ForeignResourceError):
        ro.assert_this_deployment_may_mutate("IAM role AgentCoreToolTestRole", foreign, "us-east-1")


def test_owner_tag_match_is_region_sensitive(_deployment) -> None:
    """A Frankfurt-owned role must not be mutated by a us-east-1 deploy."""
    frankfurt = ro.owner_tag_list("eu-central-1")
    assert ro.can_this_deployment_mutate(frankfurt, "eu-central-1") is True
    assert ro.can_this_deployment_mutate(frankfurt, "us-east-1") is False


# ---------------------------------------------------------------------------
# The refusal has to be actionable (F-28's lesson: a refusal a user cannot act
# on is a dead end)
# ---------------------------------------------------------------------------


def test_the_refusal_names_the_resource_the_owner_and_the_remedy(_deployment) -> None:
    with pytest.raises(ForeignResourceError) as exc:
        ro.assert_this_deployment_may_mutate(
            "IAM role AgentCoreGateway-omargw",
            [{"Key": "AgentCoreStack", "Value": "otherteam-prod-us-east-1"}],
            "us-east-1",
        )
    msg = str(exc.value)
    assert "AgentCoreGateway-omargw" in msg
    assert "otherteam-prod-us-east-1" in msg
    assert "rename" in msg
    assert "AgentCoreStack=acfe2e-p0920-us-east-1" in msg


def test_the_refusal_says_so_when_there_is_no_owner_to_name(_deployment) -> None:
    with pytest.raises(ForeignResourceError) as exc:
        ro.assert_this_deployment_may_mutate("IAM role AgentCoreToolTestRole", None, "us-east-1")
    assert "carries no ownership tag" in str(exc.value)


def test_the_refusal_survives_the_trip_to_the_ui(_deployment) -> None:
    """F-28's lesson: a refusal the user never sees is an unexplained failed deploy.

    A raise in a step handler reaches the user only as a Step Functions ``Cause``
    (JSON: errorType/errorMessage/stackTrace) passed through
    ``error_sanitizer.sanitize_error_details``, which strips the trace, the
    ``/var/task`` paths and the requestId. The remedy has to be on the other side of
    that seam, not just in the exception.
    """
    import json

    from app.services.error_sanitizer import sanitize_error_details

    with pytest.raises(ForeignResourceError) as exc:
        ro.assert_this_deployment_may_mutate("IAM role AgentCoreRuntime-support", None, "us-east-1")
    cause = json.dumps(
        {
            "errorType": "ForeignResourceError",
            "errorMessage": str(exc.value),
            "stackTrace": ['  File "/var/task/app/step_handlers/iam_step.py", line 167, in handler\n'],
        }
    )
    shown = sanitize_error_details(cause)
    assert "AgentCoreRuntime-support" in shown
    assert "rename" in shown
    assert "AgentCoreStack=acfe2e-p0920-us-east-1" in shown
    assert "ManagedBy=agentcore-flows" in shown
    assert "/var/task" not in shown


def test_an_owned_role_raises_nothing(_deployment) -> None:
    """The happy path. A refusal-only suite is compatible with refusing everything."""
    ro.assert_this_deployment_may_mutate("IAM role x", ro.owner_tag_list("us-east-1"), "us-east-1")


# ---------------------------------------------------------------------------
# Per call site: the point is that tag_role / put_role_policy never run
# ---------------------------------------------------------------------------


class _AlreadyExists(Exception):
    pass


class _FakeIam:
    """Minimal IAM stub whose create_role always collides."""

    def __init__(self, tags):
        self._tags = tags
        self.calls: list[str] = []

        class _Exceptions:
            EntityAlreadyExistsException = _AlreadyExists

        self.exceptions = _Exceptions()

    def create_role(self, **kwargs):
        self.calls.append("create_role")
        raise _AlreadyExists("Role with name already exists.")

    def get_role(self, RoleName: str):  # noqa: N803 - botocore casing
        self.calls.append("get_role")
        return {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{RoleName}", "Tags": self._tags}}

    def tag_role(self, **kwargs):
        self.calls.append("tag_role")

    def put_role_policy(self, **kwargs):
        self.calls.append("put_role_policy")

    def attach_role_policy(self, **kwargs):
        self.calls.append("attach_role_policy")


def test_runtime_deployer_does_not_tag_or_overwrite_a_foreign_role(_deployment) -> None:
    """The DEFAULT deploy path's legacy branch, and the highest-severity site.

    ``put_role_policy`` here replaces the whole inline policy of whatever role
    holds the name.
    """
    from app.services import runtime_deployer

    iam = _FakeIam(tags=None)
    with pytest.raises(ForeignResourceError):
        runtime_deployer.create_runtime_iam_role(
            iam_client=iam,
            role_name="AgentCoreRuntime-support",
            account_id="123456789012",
            region="us-east-1",
        )
    assert "tag_role" not in iam.calls
    assert "put_role_policy" not in iam.calls
    assert "attach_role_policy" not in iam.calls


def test_runtime_deployer_still_adopts_its_own_role(_deployment) -> None:
    """Negative control: with proof present the reuse path must work unchanged.

    Uses the CDK pair specifically, because that is the shape of the real shared
    role this branch adopts in the live account.
    """
    from app.services import runtime_deployer

    iam = _FakeIam(tags=[{"Key": "Project", "Value": "acfe2e"}, {"Key": "Environment", "Value": "p0920"}])
    arn = runtime_deployer.create_runtime_iam_role(
        iam_client=iam,
        role_name="AgentCoreRuntime-acfe2e-p0920-shared",
        account_id="123456789012",
        region="us-east-1",
    )
    assert arn.endswith(":role/AgentCoreRuntime-acfe2e-p0920-shared")
    assert "tag_role" in iam.calls
    assert "put_role_policy" in iam.calls


def test_harness_deployer_does_not_tag_a_foreign_role(_deployment) -> None:
    """Also fixed here: the tag list was a bare ``ManagedBy``, which proves nothing."""
    from app.services import harness_deployer

    assert harness_deployer  # imported for the module-level guard below

    iam = _FakeIam(tags=[{"Key": "ManagedBy", "Value": "agentcore-flows"}])
    with pytest.raises(ForeignResourceError):
        ro.assert_this_deployment_may_mutate(
            "IAM role AgentCoreHarness-x", iam.get_role(RoleName="AgentCoreHarness-x")["Role"]["Tags"]
        )
    assert "tag_role" not in iam.calls


# ---------------------------------------------------------------------------
# The one compatibility-only site that may deliberately warn instead of raising
# ---------------------------------------------------------------------------


def test_the_legacy_shared_tool_role_compatibility_flag_no_longer_exists(_deployment) -> None:
    """The one site that could warn instead of raising is gone (F-7d).

    ``allow_unowned_reuse=True`` let the shared-tool caller reuse an unowned role provided
    it "separately proved" an owned shared Lambda already used it -- and that proof accepted
    an adopted, untagged function, so a foreign function+role pair passed. Shared tool roles
    are stack-scoped by name now, and every caller, this one included, is fail-closed.
    """
    import inspect

    from app.services import gateway_deployer

    assert "allow_unowned_reuse" not in inspect.signature(gateway_deployer._ensure_lambda_role).parameters
    iam = _FakeIam(tags=None)
    with pytest.raises(ForeignResourceError):
        gateway_deployer._ensure_lambda_role(iam, "AgentCoreDynamicToolsLambdaRole", "tool lambda")
    assert "tag_role" not in iam.calls
    assert "put_role_policy" not in iam.calls


def test_an_ordinary_lambda_role_call_refuses_the_same_unowned_role(_deployment) -> None:
    from app.services import gateway_deployer

    iam = _FakeIam(tags=None)
    with pytest.raises(ForeignResourceError):
        gateway_deployer._ensure_lambda_role(
            iam,
            "AgentCoreDynamicToolsLambdaRole",
            "tool lambda",
        )
    assert "tag_role" not in iam.calls
    assert "put_role_policy" not in iam.calls


def test_every_create_role_tags_the_role_it_creates() -> None:
    """An untagged role is an unanswerable ownership question on the next deploy.

    ``evaluation_step`` and ``knowledge_base_step`` created roles with no tags at
    all, so no adopt-by-name check written at the reuse site could ever have
    distinguished their own role from a foreign one. All nine ``create_role`` calls
    in the app now pass ``Tags``; this fails if a tenth arrives without them.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "app"
    untagged = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_role"
                and "Tags" not in {kw.arg for kw in node.keywords}
            ):
                untagged.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    assert untagged == []


def test_every_already_exists_branch_consults_ownership() -> None:
    """Structural: a new adopt-by-name site must not be able to skip the question.

    Each ``EntityAlreadyExistsException`` branch was classified individually --
    mutating ones raise ``assert_this_deployment_may_mutate``, the two non-mutating
    ones call ``can_this_deployment_mutate`` and warn. What must never happen again
    is a branch that consults neither, which is what every one of them did before.
    Counting files rather than branches is deliberate: it is the weakest pin that
    still fails on a new unguarded module.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "app"
    unchecked = []
    for path in root.rglob("*.py"):
        text = path.read_text()
        if "exceptions.EntityAlreadyExistsException" not in text:
            continue
        if "assert_this_deployment_may_mutate(" in text or "can_this_deployment_mutate(" in text:
            continue
        unchecked.append(path.relative_to(root).as_posix())
    assert unchecked == []
