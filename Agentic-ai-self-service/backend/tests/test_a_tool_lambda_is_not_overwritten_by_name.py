"""A function whose NAME collides is not automatically ours either (peer finding F-7).

The role half of F-7 was fixed first (``test_foreign_role_is_not_mutated.py``). The
function half was still open, in two places, and it is the more serious of the two:

* ``create_knowledge_base_lambda`` — ``create_function`` raises
  ``ResourceConflictException``, the handler calls ``update_function_code`` **and**
  ``update_function_configuration``, replacing the whole ``Environment``.
* ``_create_or_update_lambda`` — same conflict, ``update_function_code`` under a retry
  loop, for the shared singleton tool functions (``AgentCoreDynamicTools``,
  ``AgentCoreCustomerSupportTools``) and for custom-tool functions.

Neither read a tag, and neither passed ``Tags=`` on the create — so the platform could
not prove ownership of functions *it had made*, let alone anyone else's.

Why this outranks the role finding: ARCC ``cnt_pXauQr9E6bKwke`` — "lambda:UpdateFunctionCode
updates the code run by the target Lambda function. A principal with permission to this
API can update arbitrary code to the lambdas it has access to. This provides privilege
escalation to the permissions assigned to the lambda functions." ``cnt_L4ZLZgjrCctfxl``
lists it as named privilege-escalation pattern 3: "IAM principal accesses role by
updating Lambda function code ... to execute with permissions of the attached execution
role." Overwriting a function is therefore not "breaking someone's tool", it is running
our code under an execution role we did not choose and cannot see.

Live measurements this was written against, all in account 123456789012:

* ``AgentCoreDynamicTools`` exists, ``Tags: null``. CloudTrail says
  ``CreateFunction20150331`` at 2026-09-20T12:39:25 by ``acfe2e-p0920-step-gateway``,
  then three ``UpdateFunctionCode`` calls by the same principal — so it IS ours, and
  nothing foreign has been overwritten. Stated precisely because the untagged +
  ``LastModified`` combination reads exactly like an incident and is not one; the
  finding is a latent path, and the first reviewer to check CloudTrail would have
  thrown out an overstated report.
* The role ``AgentCoreDynamicToolsLambdaRole`` (``CreateDate 2026-07-19``, untagged)
  IS foreign and is what ``_ensure_lambda_role`` adopts non-mutatingly.
* The deployed ``acfe2e-p0920-StepGatewayRole...`` grants ``CreateFunction`` /
  ``UpdateFunctionCode`` / ``UpdateFunctionConfiguration`` on
  ``function:AgentCore*`` — so every function in the account whose name starts with
  ``AgentCore`` was reachable, which is 2 today and unbounded tomorrow.

There is no residual any more (F-7d). The first version of this file kept one, tested
rather than hidden: an untagged function was ADOPTED with a warning and its tags backfilled,
on the argument that refusing would break the shared-singleton design for every install
predating the tag. Peer d3 then produced the takeover in code with this file's own fakes: an
untagged ``AgentCoreDynamicTools`` bound to an unowned role was adopted, tagged as ours, its
code replaced and our gateway granted invoke -- our code under a role nobody here chose.
So now every existing function that is not provably ours is refused BEFORE any
TagResource/UpdateFunctionCode/UpdateFunctionConfiguration/AddPermission, and what replaced
the compatibility the residual bought is the NAME: every function this stack creates is
``AgentCore-<stack token>-...`` (``naming.scoped_function_name``), so a legacy unscoped
singleton is simply never this stack's function, and a collision on the scoped name is
either ours (tagged at create) or foreign (refused). Each mutation is additionally fenced on
its own fresh, ownership-proven ``GetFunction`` via ``RevisionId``, and the shared functions
are created/updated and released under a per-function lock.
"""

from __future__ import annotations

import logging

import pytest
from app.services import gateway_deployer as gd
from app.services import resource_ownership as ro
from app.services.resource_ownership import ForeignResourceError
from botocore.exceptions import ClientError

LOGGER = "app.services.gateway_deployer"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("PROJECT_NAME", "ENVIRONMENT", "ENVIRONMENT_NAME", "APP_AWS_REGION", "AWS_REGION"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def _deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROJECT_NAME", "acfe2e")
    monkeypatch.setenv("ENVIRONMENT", "p0920")
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")


class _ResourceConflict(Exception):
    pass


def _client_error(code: str, op: str) -> ClientError:
    """A REAL botocore ClientError, because ``aws_errors.is_error`` accepts nothing else.

    A hand-rolled ``Exception`` carrying a ``.response`` attribute passes an
    ``isinstance``-free reading of the code and fails ``is_error``'s real
    ``isinstance(exc, ClientError)`` check — which would send the AccessDenied test
    down the ``raise`` branch and let it pass for the wrong reason.
    """
    return ClientError({"Error": {"Code": code, "Message": f"fake {code}"}}, op)


class _Exceptions:
    ResourceConflictException = _ResourceConflict
    InvalidParameterValueException = type("InvalidParameterValueException", (Exception,), {})
    ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})


class _FakeLambda:
    """Minimal Lambda client. ``existing`` means create_function conflicts."""

    exceptions = _Exceptions()

    def __init__(
        self,
        existing: bool = False,
        tags: dict[str, str] | None = None,
        tag_read_denied: bool = False,
        tag_write_denied: bool = False,
    ) -> None:
        self.existing = existing
        self.tags = dict(tags or {})
        self.tag_read_denied = tag_read_denied
        self.tag_write_denied = tag_write_denied
        self.calls: list[str] = []
        self.create_tags: dict[str, str] | None = None
        self.role_arn: str | None = None

    # -- create / update -----------------------------------------------------
    def create_function(self, **kw):
        self.calls.append("create_function")
        self.create_tags = kw.get("Tags")
        if self.existing:
            raise _ResourceConflict("Function already exist")
        self.role_arn = kw.get("Role")
        return {"FunctionArn": f"arn:aws:lambda:us-east-1:111122223333:function:{kw['FunctionName']}"}

    # -- RevisionId fence (F-7d) ----------------------------------------------
    # Every GetFunction returns the current revision; every mutation must send it back and
    # bumps it, like the real service. A stale or missing fence is PreconditionFailed.
    revision = 1
    fences: list[str | None]

    def _fence(self, kw: dict) -> None:
        if not hasattr(self, "fences"):
            self.fences = []
        sent = kw.get("RevisionId")
        self.fences.append(sent)
        if sent != str(self.revision):
            raise _client_error("PreconditionFailedException", "UpdateFunction")
        self.revision += 1

    def update_function_code(self, **kw):
        self.calls.append("update_function_code")
        self._fence(kw)
        return {}

    def update_function_configuration(self, **kw):
        self.calls.append("update_function_configuration")
        self._fence(kw)
        if "Role" in kw:
            self.role_arn = kw["Role"]
        return {}

    def get_function(self, FunctionName: str, **kw):  # noqa: N803 — boto3 casing
        self.calls.append("get_function")
        return {
            "Configuration": {
                "FunctionArn": f"arn:aws:lambda:us-east-1:111122223333:function:{FunctionName}",
                "Role": self.role_arn,
                "State": "Active",
                "LastUpdateStatus": "Successful",
                "RevisionId": str(self.revision),
            },
            "Tags": dict(self.tags),
        }

    # -- tags ----------------------------------------------------------------
    def list_tags(self, Resource: str, **kw):  # noqa: N803
        self.calls.append("list_tags")
        if self.tag_read_denied:
            raise _client_error("AccessDeniedException", "ListTags")
        return {"Tags": dict(self.tags)}

    def tag_resource(self, Resource: str, Tags: dict, **kw):  # noqa: N803
        self.calls.append("tag_resource")
        if self.tag_write_denied:
            raise _client_error("AccessDeniedException", "TagResource")
        self.tags.update(Tags)
        return {}

    # -- resource policy -----------------------------------------------------
    def get_policy(self, **kw):
        self.calls.append("get_policy")
        # A function with no resource policy yet: the benign prune path.
        raise _client_error("ResourceNotFoundException", "GetPolicy")

    def add_permission(self, **kw):
        self.calls.append("add_permission")
        return {}

    def put_function_concurrency(self, **kw):
        self.calls.append("put_function_concurrency")
        return {}


class _ConflictingUpdateLambda(_FakeLambda):
    """Existing owned function that transiently refuses update mutations."""

    def __init__(
        self,
        *,
        code_conflicts: int = 0,
        configuration_conflicts: int = 0,
    ) -> None:
        # The KB tool is per-deployment: since F-7d (peer 82) its ownership proof is the stack
        # pair PLUS the exact DeploymentId, so the fake carries the id ``_run_kb`` deploys.
        super().__init__(existing=True, tags={**_ours(), "DeploymentId": "deadbeefcafebabe"})
        self.code_conflicts = code_conflicts
        self.configuration_conflicts = configuration_conflicts

    def update_function_code(self, **kw):
        self.calls.append("update_function_code")
        if self.code_conflicts:
            self.code_conflicts -= 1
            raise _ResourceConflict(f"code still updating ({self.code_conflicts} retries remain)")
        return {}

    def update_function_configuration(self, **kw):
        self.calls.append("update_function_configuration")
        if self.configuration_conflicts:
            self.configuration_conflicts -= 1
            raise _ResourceConflict(f"configuration still updating ({self.configuration_conflicts} retries remain)")
        if "Role" in kw:
            self.role_arn = kw["Role"]
        return {}


def _ours(region: str = "us-east-1") -> dict[str, str]:
    return ro.owner_tags(region)


# ---------------------------------------------------------------------------
# The authorizer itself
# ---------------------------------------------------------------------------


def test_our_own_tagged_function_is_owned(_deployment) -> None:
    fn = _FakeLambda(existing=True, tags=_ours())
    assert gd._authorize_tool_function_replacement(fn, "AgentCoreDynamicTools") == "owned"
    assert "tag_resource" not in fn.calls, "an owned function must not be re-tagged"


def test_a_function_tagged_by_another_deployment_is_refused(_deployment) -> None:
    """The realistic case, and the one that must fail closed.

    Two installs of this platform in one account both want ``AgentCoreDynamicTools``.
    Before this guard the second one replaced the first one's code and the first one's
    gateway then served whatever the second one generated.
    """
    fn = _FakeLambda(
        existing=True,
        tags={"ManagedBy": "agentcore-flows", "AgentCoreStack": "othertenant-prod-us-east-1"},
    )
    with pytest.raises(ForeignResourceError) as exc:
        gd._authorize_tool_function_replacement(fn, "AgentCoreDynamicTools")
    msg = str(exc.value)
    assert "othertenant-prod-us-east-1" in msg, "the message must name the owner, or it is unactionable"
    assert "acfe2e-p0920-us-east-1" in msg
    assert "update_function_code" not in fn.calls
    assert "tag_resource" not in fn.calls, "refusing must not stamp our identity on it either"


def test_a_cdk_tagged_function_from_another_project_is_refused(_deployment) -> None:
    """Proof 2 of ownership (CDK ``Project``/``Environment``) must also work in reverse."""
    fn = _FakeLambda(existing=True, tags={"Project": "someoneelse", "Environment": "prod"})
    with pytest.raises(ForeignResourceError):
        gd._authorize_tool_function_replacement(fn, "AgentCore-KBTool-deadbeef")


def test_an_untagged_function_is_refused_and_never_tagged(_deployment, caplog) -> None:
    """The reversed residual (F-7d). An untagged function is nobody's to touch.

    It used to be adopted with a warning and its tags backfilled. Peer d3 showed that path
    accepting a foreign function+role pair. Now: refuse, before any write, and say what
    the caller can do about it. ``fn.tags`` stays empty -- the refusal must not stamp our
    identity onto a function we just declined to claim.
    """
    fn = _FakeLambda(existing=True, tags={})
    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(ForeignResourceError) as exc:
        gd._authorize_tool_function_replacement(fn, "AgentCoreDynamicTools")
    msg = str(exc.value)
    assert "no ownership tag" in msg and "acfe2e-p0920-us-east-1" in msg and "redeploy" in msg
    assert fn.tags == {}, "refusing must not backfill"
    assert "tag_resource" not in fn.calls and "update_function_code" not in fn.calls
    assert not any("Adopting" in r.message for r in caplog.records), "no adoption path exists any more"


def test_an_unrelated_tag_does_not_pass_for_ownership(_deployment) -> None:
    """``ManagedBy`` alone proves nothing: it is a product marker, not an owner.

    No AgentCoreStack and no Project -> untagged -> refused (not adopted, as it once was).
    """
    fn = _FakeLambda(existing=True, tags={"ManagedBy": "agentcore-flows", "team": "platform"})
    with pytest.raises(ForeignResourceError, match="no ownership tag"):
        gd._authorize_tool_function_replacement(fn, "AgentCoreDynamicTools")
    assert "tag_resource" not in fn.calls


def test_no_tag_write_is_ever_attempted_on_an_existing_function(_deployment) -> None:
    """The backfill is gone, so a denied TagResource cannot even be reached on this path.

    A ``tag_write_denied`` fake would have surfaced the old "loud but not fatal" branch;
    now the authorizer refuses before it gets there, and the deny is never exercised.
    """
    fn = _FakeLambda(existing=True, tags={}, tag_write_denied=True)
    with pytest.raises(ForeignResourceError):
        gd._authorize_tool_function_replacement(fn, "AgentCoreDynamicTools")
    assert "tag_resource" not in fn.calls


def test_a_denied_tag_read_refuses_rather_than_reading_as_untagged(_deployment) -> None:
    """The direction of this failure is the whole point.

    If a missing ``lambda:ListTags`` grant returned an empty tag map, every function
    would look untagged, the different-deployment refusal above would be UNREACHABLE,
    and every deploy would still go green. That is a security control that deletes
    itself when a grant is wrong — so an AccessDenied on the READ refuses, and names
    the permission.
    """
    fn = _FakeLambda(existing=True, tags=_ours(), tag_read_denied=True)
    with pytest.raises(ForeignResourceError) as exc:
        gd._authorize_tool_function_replacement(fn, "AgentCoreDynamicTools")
    assert "lambda:ListTags" in str(exc.value)
    assert "update_function_code" not in fn.calls


# ---------------------------------------------------------------------------
# The two call sites
# ---------------------------------------------------------------------------


def test_a_created_tool_function_is_tagged_at_creation(_deployment) -> None:
    """Tag on CREATE, or the conflict branch has nothing to read next time.

    This is the half that makes the guard work at all: the live
    ``AgentCoreDynamicTools`` was created by this platform and is untagged, so before
    this change the platform could not prove ownership of its own function.
    """
    fn = _FakeLambda(existing=False)
    arn = gd._create_or_update_lambda(fn, "AgentCoreDynamicTools", "arn:aws:iam::1:role/r", b"zip", "desc")
    assert arn.endswith(":function:AgentCoreDynamicTools")
    assert fn.create_tags == _ours(), f"create_function must carry the owner tags, got {fn.create_tags}"


def test_the_shared_singleton_update_path_checks_ownership_first(_deployment) -> None:
    fn = _FakeLambda(existing=True, tags={"AgentCoreStack": "othertenant-prod-us-east-1"})
    with pytest.raises(ForeignResourceError):
        gd._create_or_update_lambda(fn, "AgentCoreDynamicTools", "arn:aws:iam::1:role/r", b"zip", "desc")
    assert "update_function_code" not in fn.calls, "the refusal must precede the overwrite"


def test_the_shared_singleton_update_path_still_works_for_our_own_function(_deployment) -> None:
    """The happy path, asserted explicitly.

    A guard tested only by what it rejects is compatible with rejecting everything.
    """
    fn = _FakeLambda(existing=True, tags=_ours())
    arn = gd._create_or_update_lambda(fn, "AgentCoreDynamicTools", "arn:aws:iam::1:role/r", b"zip", "desc")
    assert arn.endswith(":function:AgentCoreDynamicTools")
    assert "update_function_code" in fn.calls


def test_the_kb_tool_function_update_path_checks_ownership_first(_deployment, monkeypatch: pytest.MonkeyPatch) -> None:
    """The KB tool's name is ``AgentCore-KBTool-<first 8 hex of the deployment id>``.

    A conflict is either a redeploy of the same deployment (owner tag present, allowed)
    or a different deployment sharing those 8 characters (refused). The live account
    already holds a log group for an ``AgentCore-KBTool-64b57b4c`` from March, so the
    name space is demonstrably not empty.
    """
    iam = _FakeIamForKb(role_exists=False)
    fn = _FakeLambda(existing=True, tags={"AgentCoreStack": "othertenant-prod-us-east-1"})
    monkeypatch.setattr(gd, "_create_iam_client", lambda *a, **k: iam)
    monkeypatch.setattr(gd, "_create_lambda_client", lambda *a, **k: fn)
    monkeypatch.setattr(gd.time, "sleep", lambda *_: None)

    with pytest.raises(ForeignResourceError):
        gd.create_knowledge_base_lambda(
            region="us-east-1",
            gateway_role_arn="arn:aws:iam::111122223333:role/AgentCoreGateway-g",
            kb_id="KB123",
            foundation_model_arn="arn:aws:bedrock:::foundation-model/x",
            deployment_id="deadbeefcafebabe",
        )
    assert "update_function_code" not in fn.calls
    assert "update_function_configuration" not in fn.calls, (
        "the configuration overwrite replaces the whole Environment, so it matters as much as the code overwrite"
    )


# ---------------------------------------------------------------------------
# The third site: a policy attached to an ADOPTED role
# ---------------------------------------------------------------------------


class _FakeIamForKb:
    """IAM client for create_knowledge_base_lambda."""

    class exceptions:  # noqa: N801 — boto3 casing
        EntityAlreadyExistsException = type("EntityAlreadyExistsException", (Exception,), {})
        NoSuchEntityException = type("NoSuchEntityException", (Exception,), {})

    def __init__(self, role_exists: bool, role_tags: list[dict[str, str]] | None = None) -> None:
        self.role_exists = role_exists
        self.role_tags = role_tags
        self.calls: list[str] = []

    def create_role(self, **kw):
        self.calls.append("create_role")
        if self.role_exists:
            raise self.exceptions.EntityAlreadyExistsException("exists")
        return {"Role": {"Arn": f"arn:aws:iam::111122223333:role/{kw['RoleName']}"}}

    def get_role(self, RoleName: str, **kw):  # noqa: N803
        self.calls.append("get_role")
        return {"Role": {"Arn": f"arn:aws:iam::111122223333:role/{RoleName}", "Tags": self.role_tags}}

    def attach_role_policy(self, **kw):
        self.calls.append("attach_role_policy")
        return {}

    def put_role_policy(self, **kw):
        self.calls.append("put_role_policy")
        return {}


def _run_kb(monkeypatch: pytest.MonkeyPatch, iam: _FakeIamForKb, fn: _FakeLambda) -> None:
    monkeypatch.setattr(gd, "_create_iam_client", lambda *a, **k: iam)
    monkeypatch.setattr(gd, "_create_lambda_client", lambda *a, **k: fn)
    monkeypatch.setattr(gd.time, "sleep", lambda *_: None)
    gd.create_knowledge_base_lambda(
        region="us-east-1",
        gateway_role_arn="arn:aws:iam::111122223333:role/AgentCoreGateway-g",
        kb_id="KB123",
        foundation_model_arn="arn:aws:bedrock:::foundation-model/x",
        deployment_id="deadbeefcafebabe",
    )


def test_kb_redeploy_retries_both_lambda_mutations(_deployment, monkeypatch: pytest.MonkeyPatch) -> None:
    iam = _FakeIamForKb(role_exists=False)
    function = _ConflictingUpdateLambda(
        code_conflicts=1,
        configuration_conflicts=1,
    )

    _run_kb(monkeypatch, iam, function)

    assert function.calls.count("update_function_code") == 2
    assert function.calls.count("update_function_configuration") == 2


def test_kb_redeploy_cannot_report_success_after_exhausting_code_updates(
    _deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    iam = _FakeIamForKb(role_exists=False)
    function = _ConflictingUpdateLambda(code_conflicts=8)

    with pytest.raises(_ResourceConflict, match="0 retries remain"):
        _run_kb(monkeypatch, iam, function)

    assert function.calls.count("update_function_code") == 8
    assert "update_function_configuration" not in function.calls


def test_kb_create_path_refuses_an_async_lambda_failure(
    _deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    iam = _FakeIamForKb(role_exists=False)
    function = _FakeLambda(existing=False)

    def _failed_function(FunctionName: str, **_kwargs):  # noqa: N803
        function.calls.append("get_function")
        return {
            "Configuration": {
                "FunctionArn": f"arn:aws:lambda:us-east-1:111122223333:function:{FunctionName}",
                "Role": function.role_arn,
                "State": "Active",
                "LastUpdateStatus": "Failed",
                "LastUpdateStatusReason": "The deployment package could not be loaded.",
            }
        }

    function.get_function = _failed_function

    with pytest.raises(RuntimeError, match="deployment package could not be loaded"):
        _run_kb(monkeypatch, iam, function)


def test_kb_lambda_is_not_created_under_an_adopted_foreign_role(_deployment, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sharpest half of F-7 in this module, and it was one line from the fix.

    ``_ensure_lambda_role``'s already-exists branch is adoptive AND deliberately
    non-mutating — no ``tag_role``, no ``put_role_policy`` — precisely so an
    identically-named foreign role is never modified. ``create_knowledge_base_lambda``
    then called ``put_role_policy`` on it anyway, one line later, granting
    ``bedrock:Retrieve``/``RetrieveAndGenerate``/``InvokeModel`` on ``Resource: "*"``.
    Granting permissions to somebody else's principal is worse than overwriting code:
    the code we push at least runs under a role we can see.
    """
    iam = _FakeIamForKb(role_exists=True, role_tags=None)
    function = _FakeLambda(existing=False)
    with pytest.raises(ro.ForeignResourceError, match="carries no ownership tag"):
        _run_kb(monkeypatch, iam, function)
    assert "put_role_policy" not in iam.calls
    assert "create_function" not in function.calls


def test_bedrock_policy_is_attached_to_a_role_we_just_created(_deployment, monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path: the common case is a brand-new role and it must still be granted."""
    iam = _FakeIamForKb(role_exists=False)
    _run_kb(monkeypatch, iam, _FakeLambda(existing=False))
    assert "create_role" in iam.calls
    assert "put_role_policy" in iam.calls


def test_bedrock_policy_is_attached_to_an_existing_role_that_is_provably_ours(
    _deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A redeploy of the same agent: the role exists and its tags prove it is ours -- the
    stack pair AND this deployment's id (F-7d: the KB role is bound per deployment)."""
    iam = _FakeIamForKb(
        role_exists=True, role_tags=ro.owner_tag_list("us-east-1", extra={"DeploymentId": "deadbeefcafebabe"})
    )
    _run_kb(monkeypatch, iam, _FakeLambda(existing=False))
    assert "put_role_policy" in iam.calls


def test_the_outcome_out_param_is_reset_per_call(_deployment) -> None:
    """A stale ``created``/``owned`` from a previous call would re-authorize a mutation."""
    iam = _FakeIamForKb(role_exists=False)
    outcome: dict[str, bool] = {"created": True, "owned": True}
    iam.role_exists = True
    iam.role_tags = None
    # An unowned existing role is refused now (F-7d: no allow_unowned_reuse), but the
    # out-param must still have been reset BEFORE the refusal, or a caller that catches
    # and inspects it would read the previous call's proof.
    with pytest.raises(ForeignResourceError):
        gd._ensure_lambda_role(iam, "AgentCoreKBToolRole-deadbeef", "d", outcome=outcome, region="us-east-1")
    assert outcome == {"created": False, "owned": False}


# ---------------------------------------------------------------------------
# F-7d: the foreign pair, the fence, and the order of operations
# ---------------------------------------------------------------------------


def _shared_deploy(monkeypatch: pytest.MonkeyPatch, iam: _FakeIamForKb, fn: _FakeLambda) -> str:
    monkeypatch.setattr(gd, "_create_iam_client", lambda *a, **k: iam)
    monkeypatch.setattr(gd, "_create_lambda_client", lambda *a, **k: fn)
    monkeypatch.setattr(gd.time, "sleep", lambda *_: None)
    return gd.create_dynamic_gateway_lambda("us-east-1", "arn:aws:iam::111122223333:role/AgentCoreGateway-g")


def test_the_shared_function_name_is_scoped_to_this_stack(_deployment) -> None:
    from app.services.naming import scoped_function_name, scoped_role_name

    assert gd.shared_tool_function_name("DynamicTools", "us-east-1") == scoped_function_name(
        "DynamicTools", "acfe2e-p0920-us-east-1"
    )
    assert gd.shared_tool_role_name("DynamicTools", "us-east-1") == scoped_role_name(
        "DynamicTools", "acfe2e-p0920-us-east-1"
    )
    # The legacy account-global literals are recognized for teardown of old rows only.
    assert gd.is_shared_tool_function("AgentCoreDynamicTools")
    assert gd.is_shared_tool_function(gd.shared_tool_function_name("DynamicTools", "us-east-1"))
    assert not gd.is_shared_tool_function("AgentCore-KBTool-deadbeef")
    # Another region of the same stack is a different name: no cross-region collision.
    assert gd.shared_tool_function_name("DynamicTools", "eu-west-1") != gd.shared_tool_function_name(
        "DynamicTools", "us-east-1"
    )


def test_a_foreign_untagged_function_and_unowned_role_pair_gets_zero_mutations(
    _deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Peer d3's takeover, reproduced and refused.

    The role exists untagged, the function exists untagged and bound to that role. The
    old code adopted the function, tagged it, replaced its code and granted invoke. Now
    the ROLE is refused first (no unowned reuse), so the function is never even read.
    """
    iam = _FakeIamForKb(role_exists=True)
    iam.role_tags = None
    fn = _FakeLambda(existing=True, tags={})
    fn.role_arn = "arn:aws:iam::111122223333:role/AgentCoreDynamicToolsLambdaRole"
    with pytest.raises(ForeignResourceError):
        _shared_deploy(monkeypatch, iam, fn)
    assert fn.calls == [], f"the foreign pair must see no Lambda call at all, saw {fn.calls}"
    assert "tag_role" not in iam.calls and "put_role_policy" not in iam.calls


def test_an_owned_role_with_an_untagged_function_still_refuses_before_any_write(
    _deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Our role, somebody's (or nobody's) function under our scoped name: refused at the
    function, and the refusal precedes TagResource / UpdateFunctionCode / AddPermission."""
    iam = _FakeIamForKb(role_exists=True)
    iam.role_tags = ro.owner_tag_list("us-east-1")
    fn = _FakeLambda(existing=True, tags={})
    with pytest.raises(ForeignResourceError, match="no ownership tag"):
        _shared_deploy(monkeypatch, iam, fn)
    forbidden = {"tag_resource", "update_function_code", "update_function_configuration", "add_permission"}
    assert not forbidden & set(fn.calls), fn.calls


def test_our_own_shared_function_is_updated_under_a_fresh_fence_each_time(
    _deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path, and the order that makes it safe: for EACH mutation a GetFunction, an
    ownership read on that GetFunction, then the mutation carrying that read's RevisionId."""
    iam = _FakeIamForKb(role_exists=True)
    iam.role_tags = ro.owner_tag_list("us-east-1")
    fn = _FakeLambda(existing=True, tags=_ours())
    fn.role_arn = "arn:aws:iam::111122223333:role/other"  # forces the role move + the code update
    arn = _shared_deploy(monkeypatch, iam, fn)
    assert arn.endswith(":function:" + gd.shared_tool_function_name("DynamicTools", "us-east-1"))
    assert fn.fences == ["1", "2"], f"each mutation must carry the revision of its own fresh read, got {fn.fences}"
    calls = fn.calls
    first_mutation = min(calls.index("update_function_configuration"), calls.index("update_function_code"))
    assert "list_tags" in calls[:first_mutation], "ownership must be read before the first mutation"
    # Between the two mutations: a fresh get_function + list_tags, not a reused proof.
    between = calls[calls.index("update_function_configuration") : calls.index("update_function_code")]
    assert "get_function" in between and "list_tags" in between, between
    assert "add_permission" in calls and calls.index("add_permission") > calls.index("update_function_code")


def test_a_stale_fence_is_raised_never_retried_as_success(_deployment, monkeypatch: pytest.MonkeyPatch) -> None:
    """Somebody changed the function between our read and our write: PreconditionFailed
    propagates. Retrying it as if it were a transient conflict would be the race."""

    class _Raced(_FakeLambda):
        def get_function(self, FunctionName: str, **kw):  # noqa: N803
            resp = super().get_function(FunctionName, **kw)
            self.revision += 1  # another writer moves the function after every read
            return resp

    iam = _FakeIamForKb(role_exists=True)
    iam.role_tags = ro.owner_tag_list("us-east-1")
    fn = _Raced(existing=True, tags=_ours())
    # Already on the right role, so the only mutation is the code update.
    fn.role_arn = f"arn:aws:iam::111122223333:role/{gd.shared_tool_role_name('DynamicTools', 'us-east-1')}"
    with pytest.raises(ClientError) as exc:
        _shared_deploy(monkeypatch, iam, fn)
    assert exc.value.response["Error"]["Code"] == "PreconditionFailedException"
    assert fn.calls.count("update_function_code") == 1, "a fence failure is not retried"
    assert "add_permission" not in fn.calls


def test_the_shared_deploy_holds_the_functions_lock(
    _deployment, monkeypatch: pytest.MonkeyPatch, gateway_lock_table
) -> None:
    """Deploy and teardown of a shared function serialize on one lock row (F-7d)."""
    from app.services.gateway_mutation_lock import lambda_lock_key

    iam = _FakeIamForKb(role_exists=True)
    iam.role_tags = ro.owner_tag_list("us-east-1")
    fn = _FakeLambda(existing=True, tags=_ours())
    name = gd.shared_tool_function_name("DynamicTools", "us-east-1")
    seen: list[str] = []
    original_put = gateway_lock_table.put_item

    def _spy_put(**kw):
        seen.append(kw["Item"]["claim_key"])
        return original_put(**kw)

    monkeypatch.setattr(gateway_lock_table, "put_item", _spy_put)
    _shared_deploy(monkeypatch, iam, fn)
    assert lambda_lock_key("us-east-1", name) in seen
    assert gateway_lock_table.items.get(lambda_lock_key("us-east-1", name)) is None, "the lock must be released"


# ---------------------------------------------------------------------------
# F-7d: same-stack collisions between per-deployment / per-scope functions (peers 82, 5a)
# ---------------------------------------------------------------------------


def test_two_deployments_sharing_eight_hex_no_longer_share_a_kb_function_name(_deployment) -> None:
    """The old suffix was ``deployment_id[:8]``; these two ids collide under it."""
    from app.services.naming import deployment_scope_suffix

    a, b = "deadbeef-0000-4000-8000-000000000001", "deadbeef-ffff-4fff-8fff-ffffffffffff"
    assert a[:8] == b[:8]
    assert deployment_scope_suffix(a) != deployment_scope_suffix(b)
    assert len(deployment_scope_suffix(a)) == 12


def test_a_kb_function_of_another_deployment_of_this_stack_is_refused_not_overwritten(
    _deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when two deployments DO land on one name (the digest is injected to force it),
    the exact DeploymentId binding refuses the second deploy before any mutation."""
    from app.services import naming

    monkeypatch.setattr(naming, "deployment_scope_suffix", lambda _id: "collidedcoll")
    monkeypatch.setattr(gd, "deployment_scope_suffix", lambda _id: "collidedcoll")
    first_id, second_id = "deadbeef-0000-4000-8000-000000000001", "deadbeef-ffff-4fff-8fff-ffffffffffff"
    iam = _FakeIamForKb(role_exists=True)
    # The role AND the function the FIRST deployment created: our stack's tags, its
    # DeploymentId. The role is checked first and refuses the collision on its own.
    iam.role_tags = ro.owner_tag_list("us-east-1", extra={"DeploymentId": first_id})
    fn = _FakeLambda(existing=True, tags={**_ours(), "DeploymentId": first_id})
    monkeypatch.setattr(gd, "_create_iam_client", lambda *a, **k: iam)
    monkeypatch.setattr(gd, "_create_lambda_client", lambda *a, **k: fn)
    monkeypatch.setattr(gd.time, "sleep", lambda *_: None)
    with pytest.raises(ForeignResourceError, match="DeploymentId=deadbeef-0000"):
        gd.create_knowledge_base_lambda(
            region="us-east-1",
            gateway_role_arn="arn:aws:iam::111122223333:role/AgentCoreGateway-g",
            kb_id="KB999",
            foundation_model_arn="arn:aws:bedrock:::foundation-model/x",
            deployment_id=second_id,
        )
    forbidden = {"update_function_code", "update_function_configuration", "tag_resource", "add_permission"}
    assert not forbidden & set(fn.calls), fn.calls


def test_a_kb_redeploy_grants_the_current_gateway_role_on_the_update_branch(
    _deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Peer 82: the invoke grant used to live only inside the create branch, so a redeploy
    onto an existing KB function after the gateway role was recreated left the target
    pointing at a function the new role could not invoke."""
    iam = _FakeIamForKb(role_exists=False)
    fn = _ConflictingUpdateLambda()
    _run_kb(monkeypatch, iam, fn)
    assert "add_permission" in fn.calls
    assert fn.calls.index("add_permission") > fn.calls.index("update_function_configuration")


def test_two_custom_tool_scopes_no_longer_share_a_name_and_a_collision_is_refused(
    _deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Peer 5a: the scope digest was 8 hex (32 bits). Now 12 hex in the name AND the full
    digest bound as ToolScope on the role and the function; a forced name collision with a
    different scope is refused at the ROLE, before PassRole or any function call."""
    fn_a, role_a, _, binding_a = gd._custom_tool_resource_names("tool", "owner-a", "gw-1", "us-east-1")
    fn_b, role_b, _, binding_b = gd._custom_tool_resource_names("tool", "owner-b", "gw-1", "us-east-1")
    assert fn_a != fn_b and role_a != role_b and binding_a != binding_b
    assert len(fn_a) <= 64 and len(role_a) <= 64
    # Force the collision: the role that exists carries OUR stack tags but scope A's binding.
    iam = _FakeIamForKb(role_exists=True)
    iam.role_tags = ro.owner_tag_list("us-east-1", extra={"ToolScope": binding_a})
    with pytest.raises(ForeignResourceError, match="ToolScope="):
        gd._ensure_lambda_role(iam, role_a, "d", region="us-east-1", extra_tags={"ToolScope": binding_b})
    assert "tag_role" not in iam.calls and "put_role_policy" not in iam.calls
    # And the same on the function: our stack's function under scope A, scope B asks for it.
    fn = _FakeLambda(existing=True, tags={**_ours(), "ToolScope": binding_a})
    with pytest.raises(ForeignResourceError, match="ToolScope="):
        gd._create_or_update_lambda(
            fn,
            fn_a,
            f"arn:aws:iam::1:role/{role_a}",
            b"zip",
            "d",
            region="us-east-1",
            extra_tags={"ToolScope": binding_b},
        )
    assert not {"update_function_code", "update_function_configuration", "tag_resource"} & set(fn.calls)
    # The matching scope is the happy path, and the create writes the binding as a tag.
    fresh = _FakeLambda(existing=False)
    gd._create_or_update_lambda(
        fresh,
        fn_a,
        f"arn:aws:iam::1:role/{role_a}",
        b"zip",
        "d",
        region="us-east-1",
        extra_tags={"ToolScope": binding_a},
    )
    assert fresh.create_tags["ToolScope"] == binding_a
