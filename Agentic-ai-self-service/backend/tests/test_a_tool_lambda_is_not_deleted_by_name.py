"""Teardown must not delete a tool Lambda it cannot prove it created (F-7c).

F-7 closed the WRITE half: a name collision no longer lets one installation overwrite
another's tool code. This is the DELETE half, and it is the worse one, because the
mitigation that makes the write half tolerable does not exist here — an overwrite is
recorded and the code is re-derivable from the canvas, a deleted function is gone.

How it was found, which matters because no test would have found it. Tearing down three
real deployments on 2026-09-21 printed the role line

    AgentCoreDynamicToolsLambdaRole kept (NO OWNER TAG — ownership unprovable, ...)

next to the function line

    Shared tool Lambda AgentCoreDynamicTools deleted (last gateway released it)

The ROLE release was tag-gated. The FUNCTION release was not gated at all: it consulted
exactly one thing, the number of surviving ``AllowAgentCoreInvoke-*`` statements in the
function's own resource policy. Those statements are ones this platform adds, so a function
it did not create has none of them, scores zero on its first teardown, and is deleted.

**What that pairing did NOT show, established afterwards from CloudTrail and recorded here
because the first version of this docstring got it wrong.** Both lines were in fact correct
decisions about two differently-owned resources, not one inconsistency. Every
``CreateFunction``/``DeleteFunction`` event in the account's full 90-day trail window was
paginated (392 events): the *function* ``AgentCoreDynamicTools`` was created 2026-09-20 by
this platform's own gateway step role and deleted 2026-09-21 by its own deployment role, and
no function of that name existed before that. So **no foreign function was ever deleted** —
the teardown deleted its own, which is what it should do. The 2026-07-19 untagged artifact is
the *role*, and the role is the half that was already protected.

The missing check is real regardless, and is a code property rather than an incident: a
destructive call on an **account-global** name ran with no evidence of ownership, and mutant
M1 (the gate removed) is killed by these tests. The blast radius is a demonstrated *hazard*,
not a measured harm — the account holds a foreign untagged ``AgentCoreDynamicToolsLambdaRole``
created 2026-07-19 by something unrelated, which is direct evidence that another tool in this
account uses this exact name family. Had that tool also created the function, the chain would
have been: adopted on write, code replaced, our invoke grant added, then destroyed when our
last gateway went away. It did not happen. It was reachable, and nothing stood in the way.

``_authorize_tool_function_replacement``'s own docstring already promised this behaviour
-- "it stays unattributable and teardown will not delete it" -- so the intent predates the
fix and only the code was missing.

The asymmetry between the two gates is deliberate and is the design, not an inconsistency:

* WRITE, untagged -> adopt + backfill. Fail-closed over an incomplete ownership table
  removes the shared-singleton feature for every install predating the tag.
* DELETE, untagged -> keep, loudly. There is no remedy for a wrong delete, and leaking a
  function an operator can remove by hand is strictly the better failure.

The tests below therefore assert BOTH directions at every call site, because a gate tested
only by what it refuses is compatible with refusing everything (and this repo has shipped
exactly that defect before).

ARCC ``cnt_ua0cTwldOsODs8`` (a resource dies with what created it -- which presupposes
knowing what created it), ``cnt_dwzZ05hLnqhYXQ`` (least privilege on the delete verbs).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from app.services import gateway_deployer as gd
from app.services.resource_ownership import (
    CDK_ENVIRONMENT_TAG_KEY,
    CDK_PROJECT_TAG_KEY,
    OWNER_TAG_KEY,
    PRODUCT_TAG_KEY,
    stack_id,
)
from botocore.exceptions import ClientError

REGION = "us-east-1"
SHARED = "AgentCoreDynamicTools"
PER_GATEWAY = "AgentCoreKBQuery-mygw"
# Derived from the deployment id, never read back from a manifest row.
KB_TOOL = "AgentCore-KBTool-abcdef12"


@pytest.fixture(autouse=True)
def _configured_stack_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ownership decision in this file names one explicit stack."""
    monkeypatch.setenv("PROJECT_NAME", "acfe2e")
    # Keep config.py in local mode when deployment_handler is imported below.
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("AWS_REGION", REGION)


def _client_error(code: str, op: str) -> ClientError:
    """A REAL botocore ``ClientError``: ``aws_errors.is_error`` accepts nothing else, so a
    duck-typed stand-in would make every branch under test take its else path."""
    return ClientError({"Error": {"Code": code, "Message": f"fake {code}"}}, op)


def _lam(name: str, tags, *, refcount_zero: bool = True) -> MagicMock:
    """A Lambda client whose *name* carries *tags*.

    ``tags=None`` means ListTags returns no tags (the untagged case); pass a ``ClientError``
    to make the READ fail.
    """
    client = MagicMock()
    client.get_function.return_value = {
        "Configuration": {"FunctionArn": f"arn:aws:lambda:{REGION}:123456789012:function:{name}"}
    }
    if isinstance(tags, Exception):
        client.list_tags.side_effect = tags
    else:
        client.list_tags.return_value = {"Tags": tags or {}}
    sids = [] if refcount_zero else [{"Sid": "AllowAgentCoreInvoke-AgentCoreGateway-other"}]
    client.get_policy.return_value = {"Policy": json.dumps({"Statement": sids})}
    return client


def _ours() -> dict[str, str]:
    return {OWNER_TAG_KEY: stack_id(REGION), PRODUCT_TAG_KEY: "agentcore-flows"}


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_a_function_this_deployment_owns_is_cleared_for_deletion():
    """The happy path, asserted first and on purpose.

    A gate whose every test is a refusal is indistinguishable from a gate that refuses
    everything, which would silently turn teardown into a leak on every install.
    """
    assert gd._authorize_tool_function_deletion(_lam(SHARED, _ours()), SHARED, REGION) == ""


def test_a_cdk_tagged_function_is_retained_for_cloudformation_to_delete():
    """CDK tags permit update, but CloudFormation retains lifecycle authority."""
    tags = {CDK_PROJECT_TAG_KEY: "acfe2e", CDK_ENVIRONMENT_TAG_KEY: "local"}
    refusal = gd._authorize_tool_function_deletion(_lam(SHARED, tags), SHARED, REGION)
    assert "CloudFormation-owned" in refusal
    assert "do not transfer deletion authority" in refusal


def test_an_untagged_function_is_kept_not_deleted():
    """The whole finding. Untagged is where the write path ADOPTS and the delete path must
    REFUSE, because only one of those two mistakes can be undone."""
    refusal = gd._authorize_tool_function_deletion(_lam(SHARED, None), SHARED, REGION)
    assert "NO OWNER TAG" in refusal
    assert "never auto-deleted" in refusal
    # An operator has to be able to act on it, so the message says what to do.
    assert "by hand" in refusal


def test_a_function_owned_by_another_deployment_is_kept_and_both_owners_are_named():
    """Two installs of this platform in one account both want ``AgentCoreDynamicTools``.
    The refusal names the owner AND this deployment: with only one of the two, an operator
    cannot tell which side of the collision they are looking at."""
    refusal = gd._authorize_tool_function_deletion(
        _lam(SHARED, {OWNER_TAG_KEY: "other-prod-us-east-1"}), SHARED, REGION
    )
    assert "other-prod-us-east-1" in refusal
    assert stack_id(REGION) in refusal
    assert "account-global" in refusal


def test_a_product_tag_alone_is_not_ownership():
    """``ManagedBy=agentcore-flows`` names the PRODUCT. Another installation of the same
    product carries it too, so treating it as ownership would authorize deleting exactly
    the resource this gate exists to protect."""
    refusal = gd._authorize_tool_function_deletion(_lam(SHARED, {PRODUCT_TAG_KEY: "agentcore-flows"}), SHARED, REGION)
    assert "NO OWNER TAG" in refusal


def test_a_denied_tag_read_keeps_the_function():
    """Direction matters. If an unreadable tag meant "untagged", a missing ``lambda:ListTags``
    grant would make every function look unowned -- and on THIS path unowned would mean
    deletable, so a permissions gap would become a deletion spree while every teardown
    still reported success."""
    refusal = gd._authorize_tool_function_deletion(
        _lam(SHARED, _client_error("AccessDeniedException", "ListTags")), SHARED, REGION
    )
    assert "ownership unreadable" in refusal
    assert "lambda:ListTags" in refusal  # the remedy is named


def test_an_already_absent_function_is_not_a_refusal():
    """A function that is already gone must return "" so the caller's own delete reports
    "already absent" rather than a confusing ownership refusal about a resource that does
    not exist."""
    client = MagicMock()
    client.get_function.side_effect = _client_error("ResourceNotFoundException", "GetFunction")
    assert gd._authorize_tool_function_deletion(client, SHARED, REGION) == ""


def test_the_region_argument_is_what_decides_ownership():
    """``stack_id`` embeds the region, so the same tag is ours in one region and foreign in
    another. This pins that the caller's region -- not the process's ambient default -- is
    what the gate compares against, which is the bug the ``region`` parameter exists to
    prevent."""
    client = _lam(SHARED, {OWNER_TAG_KEY: stack_id("eu-central-1")})
    assert gd._authorize_tool_function_deletion(client, SHARED, "eu-central-1") == ""
    assert "will not delete it" in gd._authorize_tool_function_deletion(client, SHARED, REGION)


# ---------------------------------------------------------------------------
# The shared-singleton release: the refcount is not an ownership proof
# ---------------------------------------------------------------------------


def test_the_refcount_alone_does_not_authorize_deleting_a_shared_lambda():
    """The exact live defect. Refcount zero, so the old code deleted; the function is
    untagged, so it must now be kept -- and the IAM role must not be touched either."""
    client = _lam(SHARED, None)
    iam = MagicMock()
    with (
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
    ):
        line = gd._release_shared_tool_lambda(client, SHARED, "AgentCoreGateway-mine", REGION)

    client.delete_function.assert_not_called()
    assert "NO OWNER TAG" in line
    # The role release is reached only from the post-delete branch, so a kept function
    # must leave its execution role alone -- otherwise the function survives without one.
    iam.delete_role.assert_not_called()


def test_this_deployments_own_shared_lambda_is_still_deleted():
    """The regression that matters in the other direction: adding the gate must not turn
    normal teardown into a permanent leak of the platform's own singletons."""
    client = _lam(SHARED, _ours())
    iam = MagicMock()
    iam.get_role.return_value = {
        "Role": {
            "RoleName": "AgentCoreDynamicToolsLambdaRole",
            "Arn": "arn:aws:iam::123456789012:role/AgentCoreDynamicToolsLambdaRole",
            "Tags": [{"Key": OWNER_TAG_KEY, "Value": stack_id(REGION)}],
        }
    }
    iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
    iam.list_role_policies.return_value = {"PolicyNames": []}
    with (
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.dict("os.environ", {"APP_AWS_REGION": REGION}, clear=False),
    ):
        line = gd._release_shared_tool_lambda(client, SHARED, "AgentCoreGateway-mine", REGION)

    client.delete_function.assert_called_once_with(FunctionName=SHARED)
    assert "deleted" in line


def test_the_release_asks_about_its_argument_region_not_the_ambient_one():
    """Written because a mutant that simply DROPPED the region on the way to the gate
    survived the rest of this file.

    Every other test happens to run with the ambient region equal to the region under
    test, so ``region`` and ``_region(None)`` agree and the argument could be deleted
    without any assertion noticing. Here they deliberately disagree: the function is
    tagged for eu-central-1 and the ambient default is us-east-1, so a release called
    FOR eu-central-1 must still recognise its own function.
    """
    client = _lam(SHARED, {OWNER_TAG_KEY: stack_id("eu-central-1")})
    iam = MagicMock()
    iam.get_role.return_value = {"Role": {"RoleName": "r", "Arn": "arn:aws:iam::123456789012:role/r", "Tags": []}}
    with (
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.dict("os.environ", {"APP_AWS_REGION": REGION}, clear=False),
    ):
        line = gd._release_shared_tool_lambda(client, SHARED, "AgentCoreGateway-mine", "eu-central-1")

    client.delete_function.assert_called_once_with(FunctionName=SHARED)
    assert "deleted" in line


def test_the_ownership_gate_runs_before_any_policy_mutation():
    """Ordering, reversed on purpose (F-7d, peer 2c). The first version ran the gate AFTER
    the refcount, to spare the common path a ListTags call. But RemovePermission and the
    orphan prune are mutations of the function's resource policy, and
    ``is_shared_tool_function`` matches ANY stack's scoped name by shape -- so a manifest
    row naming somebody else's function could edit their policy before ownership was ever
    asked. Now an unowned function is not touched at all, whatever its refcount."""
    client = _lam(SHARED, None, refcount_zero=False)
    with (
        patch.object(gd, "_create_iam_client", return_value=MagicMock()),
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0) as prune,
    ):
        line = gd._release_shared_tool_lambda(client, SHARED, "AgentCoreGateway-mine", REGION)

    client.list_tags.assert_called_once()
    client.remove_permission.assert_not_called()
    client.get_policy.assert_not_called()
    prune.assert_not_called()
    client.delete_function.assert_not_called()
    assert "NO OWNER TAG" in line or "kept" in line


def test_an_owned_function_with_another_gateways_grant_is_kept_after_the_read():
    """The common path still ends in "kept", and the ownership read precedes the one
    mutation it does make (removing OUR grant)."""
    client = _lam(SHARED, _ours(), refcount_zero=False)
    with (
        patch.object(gd, "_create_iam_client", return_value=MagicMock()),
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
    ):
        line = gd._release_shared_tool_lambda(client, SHARED, "AgentCoreGateway-mine", REGION)

    order = [c[0] for c in client.mock_calls]
    assert order.index("list_tags") < order.index("remove_permission")
    client.delete_function.assert_not_called()
    assert "other gateway grant(s) remain" in line


# ---------------------------------------------------------------------------
# Every other delete site in the teardown path
# ---------------------------------------------------------------------------


def _cleanup(client, fn_name, *, custom=()):
    ctrl = MagicMock()
    ctrl.get_gateway.side_effect = [
        {"gatewayArn": (f"arn:aws:bedrock-agentcore:{REGION}:123456789012:gateway/gw-1")},
        _client_error("ResourceNotFoundException", "GetGateway"),
    ]
    ctrl.list_tags_for_resource.return_value = {"tags": {OWNER_TAG_KEY: stack_id(REGION)}}
    ctrl.list_gateway_targets.return_value = {"items": []}
    cfg = {
        "gateway_id": "gw-1",
        "gateway_name": "mygw",
        "lambda_function_name": fn_name,
        "custom_tool_lambdas": list(custom),
    }
    with (
        patch.object(gd, "_create_agentcore_control_client", return_value=ctrl),
        patch.object(gd, "_create_lambda_client", return_value=client),
        patch.object(gd, "time", MagicMock()),
    ):
        return gd.cleanup_gateway_resources("rt-x", REGION, cfg)


def test_a_per_gateway_tool_lambda_is_not_deleted_when_untagged():
    """The non-shared branch had no gate either. Its name comes from a manifest row, and a
    manifest row records what this deployment intended to create -- never a capability over
    whatever holds that name at teardown time (F-8's shape, on F-7's resource)."""
    client = _lam(PER_GATEWAY, None)
    log = _cleanup(client, PER_GATEWAY)
    client.delete_function.assert_not_called()
    assert any("NO OWNER TAG" in line for line in log), log


def test_a_per_gateway_tool_lambda_of_ours_is_deleted():
    client = _lam(PER_GATEWAY, _ours())
    log = _cleanup(client, PER_GATEWAY)
    client.delete_function.assert_called_once_with(FunctionName=PER_GATEWAY)
    assert any("deleted" in line for line in log), log


def test_a_foreign_custom_tool_lambda_is_kept_and_the_loop_continues():
    """Two entries, the first foreign: the refusal must not abort the loop, or one foreign
    name would strand every remaining function behind it."""
    foreign, mine = "AgentCore-CustomTool-foreign-x", "AgentCore-CustomTool-mine-x"
    client = MagicMock()
    client.get_function.side_effect = lambda FunctionName: {
        "Configuration": {"FunctionArn": f"arn:aws:lambda:{REGION}:123456789012:function:{FunctionName}"}
    }
    client.list_tags.side_effect = lambda Resource: {
        "Tags": {} if foreign in Resource else _ours(),
    }
    log = _cleanup(client, "", custom=(foreign, mine))
    assert [c.kwargs["FunctionName"] for c in client.delete_function.call_args_list] == [mine]
    assert any(foreign in line and "NO OWNER TAG" in line for line in log), log
    # A refusal is a DECISION, not a failure, and the difference is load-bearing: the
    # caller scans this log for " error:" lines and turns any of them into
    # ``cleanup_failures``, which makes the whole DELETE report success=False. Declining
    # to delete somebody else's function must not fail the user's teardown. (A mutant
    # that raised instead of continuing survived until this assertion existed, because
    # the loop's own except swallowed it into exactly such an error line.) Match the
    # loop's OWN error prefix rather than a bare " error", because this fixture does not
    # stub the IAM client and its unrelated "Gateway IAM role cleanup error: Unable to
    # locate credentials" line would make the assertion fail for a reason that has
    # nothing to do with the gate.
    assert not any(line.startswith("Custom tool Lambda delete error") for line in log), log


def test_the_manifest_teardown_path_refuses_a_foreign_lambda():
    import app.deployment_handler as dh

    client = _lam(PER_GATEWAY, None)
    with (
        patch("app.services.step_clients.client", return_value=client),
        patch.object(dh, "_release_shared_tool_lambda") as rel,
    ):
        msg = dh._delete_managed_resource(
            {"type": "lambda", "name": PER_GATEWAY, "region": REGION},
            REGION,
        )
    rel.assert_not_called()
    client.delete_function.assert_not_called()
    assert "NO OWNER TAG" in msg


def test_the_failure_path_teardown_refuses_a_foreign_lambda():
    """The failure path runs after a PARTIAL deploy, which makes it the most likely place
    to hold a name the deploy never actually created."""
    from app.step_handlers import status_update_step as sus

    client = _lam(PER_GATEWAY, None)
    with (
        patch("app.services.step_clients.client", return_value=client),
        patch.object(sus, "_release_shared_tool_lambda") as rel,
    ):
        with pytest.raises(sus._ResourceRetained, match="NO OWNER TAG"):
            sus._cleanup_resource(
                {"type": "lambda", "name": PER_GATEWAY, "region": REGION},
                REGION,
                {},
            )
    rel.assert_not_called()
    client.delete_function.assert_not_called()


def test_every_delete_function_call_in_the_teardown_path_is_ownership_gated():
    """The universal version, because a gate is only as good as the call site someone adds
    next month.

    Five separate sites reached ``delete_function`` on a tool Lambda and the fix had to find
    all five by reading; a per-site test would pin the five that exist and say nothing about
    the sixth. So this walks the AST of every teardown module and requires each enclosing
    function that calls ``delete_function`` to also call the authorizer.

    The one exemption is stated rather than pattern-matched, so adding another exemption is
    an edit a reviewer sees: ``tool_tester._cleanup_temp_lambda`` deletes the sandbox
    function this same invocation created moments earlier under a name containing a fresh
    random suffix, and gating it would add a ListTags round trip plus a new "leak the
    sandbox" failure mode to every single tool test.
    """
    import ast
    import pathlib

    modules = [
        "src/app/services/gateway_deployer.py",
        "src/app/deployment_handler.py",
        "src/app/step_handlers/status_update_step.py",
    ]
    exempt = {"_cleanup_temp_lambda"}
    root = pathlib.Path(__file__).resolve().parents[1]
    ungated: list[str] = []

    for rel in modules:
        tree = ast.parse((root / rel).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or node.name in exempt:
                continue
            calls = {
                n.func.attr for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            } | {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            if "delete_function" not in calls:
                continue
            if not ({"_authorize_tool_function_deletion", "_release_shared_tool_lambda"} & calls):
                ungated.append(f"{rel}::{node.name}")

    assert not ungated, f"delete_function with no ownership gate: {ungated}"


# ---------------------------------------------------------------------------
# The create side has to agree with the delete side
# ---------------------------------------------------------------------------


def test_the_create_stamps_the_same_region_the_delete_asks_about():
    """The gate compares a tag against ``stack_id(region)``, so the create must stamp
    ``owner_tags(region)`` with the SAME region. It stamped ``owner_tags()`` -- the
    deploying Lambda's own AWS_REGION -- which is identical for a same-region deploy and
    wrong for a cross-region one, where the deployment Lambda in us-east-1 would tag a
    function it just created in eu-central-1 as ``…-us-east-1``. Harmless while nothing
    read the tag on delete; a self-inflicted leak the moment something did."""
    client = MagicMock()
    client.create_function.return_value = {"FunctionArn": f"arn:aws:lambda:eu-central-1:1:function:{SHARED}"}
    client.get_function.return_value = {"Configuration": {"State": "Active"}}
    gd._create_or_update_lambda(client, SHARED, "arn:role", b"z", "d", region="eu-central-1")

    tags = client.create_function.call_args.kwargs["Tags"]
    assert tags[OWNER_TAG_KEY] == stack_id("eu-central-1")
    # And the round trip closes: what the create wrote, the delete accepts.
    assert gd._authorize_tool_function_deletion(_lam(SHARED, tags), SHARED, "eu-central-1") == ""


def test_the_kb_tool_create_stamps_the_region_its_delete_will_ask_about():
    """The same round trip for the KB tool function, which has its own ``create_function``
    call in ``create_knowledge_base_lambda`` rather than going through
    ``_create_or_update_lambda``.

    It matters more here than anywhere else: the KB function is torn down by a name this
    code DERIVES (``AgentCore-KBTool-<first 8 hex of the deployment id>``) rather than one
    read back from a manifest row, so the owner tag is the only evidence the delete gate
    has. A mutant that stamped ``owner_tags()`` here survived the whole suite until this
    test existed -- the sibling assertion above covers only the shared-singleton create.
    """
    lam = MagicMock()
    lam.create_function.return_value = {"FunctionArn": f"arn:aws:lambda:eu-central-1:1:function:{KB_TOOL}"}
    lam.get_function.return_value = {"Configuration": {"State": "Active", "FunctionArn": "arn:fn"}}

    def created_role(*_args, outcome=None, **_kwargs):
        outcome.update({"created": True, "owned": True})
        return "arn:role"

    with (
        patch.object(gd, "_create_iam_client", return_value=MagicMock()),
        patch.object(gd, "_create_lambda_client", return_value=lam),
        patch.object(gd, "_ensure_lambda_role", side_effect=created_role),
    ):
        gd.create_knowledge_base_lambda("eu-central-1", "", "kb-1", "arn:model", "abcdef1234567890")

    tags = lam.create_function.call_args.kwargs["Tags"]
    assert tags[OWNER_TAG_KEY] == stack_id("eu-central-1")
    assert gd._authorize_tool_function_deletion(_lam(KB_TOOL, tags), KB_TOOL, "eu-central-1") == ""


@pytest.mark.parametrize("fn", ["create_dynamic_gateway_lambda", "create_customer_support_lambda"])
def test_both_shared_singleton_entry_points_thread_the_region(fn):
    """A gate is only as good as the worst call site, so this is universal over the public
    entry points rather than a spot check on one."""

    def owned_role(*_args, outcome=None, **_kwargs):
        outcome.update({"created": False, "owned": True})
        return "arn:role"

    with (
        patch.object(gd, "_create_iam_client", return_value=MagicMock()),
        patch.object(gd, "_create_lambda_client", return_value=MagicMock()),
        patch.object(gd, "_ensure_lambda_role", side_effect=owned_role),
        patch.object(gd, "_create_or_update_lambda", return_value="arn:fn") as mk,
    ):
        getattr(gd, fn)("eu-central-1", "arn:gwrole")
    assert mk.call_args.kwargs.get("region") == "eu-central-1"
