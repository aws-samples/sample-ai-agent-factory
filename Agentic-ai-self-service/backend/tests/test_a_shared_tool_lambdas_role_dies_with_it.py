"""The execution role of a shared tool Lambda must be released with the function.

``AgentCoreDynamicToolsLambdaRole`` and ``AgentCoreCustomerSupportLambdaRole`` are
created by ``_ensure_lambda_role`` and were deleted by nothing. ``create_customer_support_lambda``
returns only the *Lambda* ARN, so the role never reached a caller that could record it,
and a live account accumulated one permanent IAM role per tool Lambda ever created.

Recording it as a manifest ``iam_role`` row would be WRONG, and that is the interesting
part. The tool Lambdas are account-global singletons shared by every gateway (that is
the whole reason ``_release_shared_tool_lambda`` is reference-counted). A manifest row
orders an unconditional delete on the FIRST gateway teardown, which would pull the
execution role out from under every other live gateway still invoking the function --
the same "tear down A and B goes dead" defect the refcount was introduced to fix, moved
one resource to the left. So the role dies on the refcount-zero branch, and only there.

The second gate is ownership. These names are not proof of ownership: the live test
account holds an ``AgentCoreDynamicToolsLambdaRole`` created months earlier by something
unrelated to this platform. Deleting by name would break it. So the role is tagged at
creation and the delete refuses anything it cannot prove it created -- untagged included,
because a role predating the tag and a role belonging to somebody else are
indistinguishable, and only one of those mistakes is recoverable.

ARCC ``cnt_ua0cTwldOsODs8`` (a resource must die with what created it),
``cnt_dwzZ05hLnqhYXQ`` (least privilege: the delete verbs are scoped to AgentCore*).
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from app.services import gateway_deployer as gd
from app.services.resource_ownership import (
    OWNER_TAG_KEY,
    PRODUCT_TAG_KEY,
    ForeignResourceError,
    stack_id,
)
from botocore.exceptions import ClientError

REGION = "us-east-1"
FUNCTION = "AgentCoreCustomerSupportTools"
ROLE = "AgentCoreCustomerSupportLambdaRole"


@pytest.fixture(autouse=True)
def _configured_stack_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ownership decision in this file names one explicit stack."""
    monkeypatch.setenv("PROJECT_NAME", "acfe2e")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("AWS_REGION", REGION)


def _client_error(code: str, message: str, op: str) -> ClientError:
    """A REAL ``ClientError``.

    ``aws_errors.error_code`` returns ``""`` for anything that is not a botocore
    ``ClientError`` — deliberately, so a code quoted inside a wrapped exception's
    message cannot steer control flow. A duck-typed stand-in with a ``.response``
    attribute therefore matches nothing, and every ``is_error`` branch under test
    would silently take its else path.
    """
    return ClientError({"Error": {"Code": code, "Message": message}}, op)


def _iam_with_role(tags: list[dict] | None) -> MagicMock:
    iam = MagicMock()
    role = {"Arn": f"arn:aws:iam::123456789012:role/{ROLE}", "RoleName": ROLE}
    if tags is not None:
        role["Tags"] = tags
    iam.get_role.return_value = {"Role": role}
    iam.list_attached_role_policies.return_value = {
        "AttachedPolicies": [{"PolicyArn": "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"}]
    }
    iam.list_role_policies.return_value = {"PolicyNames": []}
    return iam


def _own_the_function(client: MagicMock) -> MagicMock:
    """Make *client* report the function as owned by THIS deployment.

    Every test in this file is about the ROLE, so the FUNCTION has to be provably ours
    or the F-7c gate declines before the role logic is ever reached. Stating it
    explicitly is also the honest fixture: a function this platform created carries
    these tags, and the reason these tests originally passed without them is that
    ``delete_function`` consulted nothing at all.
    """
    client.get_function.return_value = {
        "Configuration": {"FunctionArn": f"arn:aws:lambda:{REGION}:123456789012:function:{FUNCTION}"}
    }
    client.list_tags.return_value = {"Tags": {OWNER_TAG_KEY: stack_id(REGION), PRODUCT_TAG_KEY: "agentcore-flows"}}
    return client


def _lambda_at_refcount_zero() -> MagicMock:
    """A shared Lambda whose last per-gateway invoke grant has just been removed."""
    client = MagicMock()
    client.get_policy.return_value = {"Policy": json.dumps({"Statement": []})}
    return _own_the_function(client)


def _lambda_with_another_gateway() -> MagicMock:
    client = MagicMock()
    client.get_policy.return_value = {
        "Policy": json.dumps({"Statement": [{"Sid": "AllowAgentCoreInvoke-AgentCoreGateway-other"}]})
    }
    return _own_the_function(client)


def test_the_role_is_deleted_when_the_last_gateway_releases_the_function():
    lambda_client = _lambda_at_refcount_zero()
    iam = _iam_with_role([{"Key": OWNER_TAG_KEY, "Value": stack_id(REGION)}])

    with (
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.dict("os.environ", {"APP_AWS_REGION": REGION}, clear=False),
    ):
        line = gd._release_shared_tool_lambda(lambda_client, FUNCTION, "AgentCoreGateway-mine")

    lambda_client.delete_function.assert_called_once_with(FunctionName=FUNCTION)
    iam.delete_role.assert_called_once_with(RoleName=ROLE)
    # The attached managed policy must come off first or DeleteRole fails with
    # DeleteConflict, which the caller would log as a delete error and move on from.
    iam.detach_role_policy.assert_called_once()
    assert "deleted" in line and ROLE in line


def test_the_role_survives_while_another_gateway_still_uses_the_function():
    """The regression this ordering exists to prevent: the role must not be deleted
    while a live gateway is still invoking the function it belongs to."""
    lambda_client = _lambda_with_another_gateway()
    iam = _iam_with_role([{"Key": OWNER_TAG_KEY, "Value": stack_id(REGION)}])

    with (
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.dict("os.environ", {"APP_AWS_REGION": REGION}, clear=False),
    ):
        line = gd._release_shared_tool_lambda(lambda_client, FUNCTION, "AgentCoreGateway-mine")

    lambda_client.delete_function.assert_not_called()
    iam.delete_role.assert_not_called()
    assert "kept" in line


@pytest.mark.parametrize(
    ("tags", "expect", "why"),
    [
        (None, "NO OWNER TAG", "untagged — indistinguishable from a legacy or foreign role"),
        ([], "NO OWNER TAG", "empty tag list"),
        (
            [{"Key": PRODUCT_TAG_KEY, "Value": "agentcore-flows"}],
            "NO OWNER TAG",
            "product tag only names the PRODUCT, not the stack",
        ),
        (
            [{"Key": OWNER_TAG_KEY, "Value": "someone-else-prod-us-east-1"}],
            "owned by another stack",
            "another stack's role",
        ),
    ],
)
def test_a_role_this_stack_cannot_prove_it_created_is_left_alone(tags, expect, why):
    """A live account really does hold an identically-named foreign role.

    The two ``kept`` wordings are asserted separately on purpose. A role with no owner
    tag can NEVER be released by this code (see the next test), while a role tagged to
    another stack is the gate doing its job — one is a permanent no-op and the other is a
    correct refusal, and a single message for both makes the no-op unreadable in a log.
    """
    lambda_client = _lambda_at_refcount_zero()
    iam = _iam_with_role(tags)

    with (
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.dict("os.environ", {"APP_AWS_REGION": REGION}, clear=False),
    ):
        line = gd._release_shared_tool_lambda(lambda_client, FUNCTION, "AgentCoreGateway-mine")

    # The FUNCTION is still deleted — this stack created that, and the refcount proves
    # nothing needs it. Only the role is spared.
    lambda_client.delete_function.assert_called_once()
    iam.delete_role.assert_not_called(), why
    assert expect in line, f"{why}: got {line!r}"
    if expect == "NO OWNER TAG":
        # The remedy has to be in the line, because the only fix is an operator action.
        assert "delete it by hand" in line


def test_reusing_an_existing_role_deliberately_does_not_tag_it():
    """Pins the limitation, because the alternative is worse than the limitation.

    ``Tags=owner_tag_list()`` is on ``create_role`` only. In any account that ever ran the
    pre-tag code the singleton already exists untagged, ``create_role`` never runs again,
    and so ``_release_shared_tool_lambda_role`` can never fire for it — measured live in
    166827918465: ``AgentCoreDynamicToolsLambdaRole`` (2026-07-19) and
    ``AgentCoreCustomerSupportLambdaRole`` (2026-09-20), both ``Tags: null``.

    Tagging here would fix that and is NOT wanted: ``AgentCoreDynamicToolsLambdaRole`` is
    in use right now by a live Lambda this platform did not create, so tagging on reuse
    would claim precisely the foreign role the gate exists to protect, and then delete it
    at the next refcount zero. Adopting an untagged singleton is an ownership claim an
    operator makes knowingly. If this assertion ever fails, that decision was reversed —
    check that the reversal is deliberate rather than convenient.
    """
    iam = MagicMock()
    iam.exceptions.EntityAlreadyExistsException = type("EntityAlreadyExistsException", (Exception,), {})
    iam.create_role.side_effect = iam.exceptions.EntityAlreadyExistsException()
    iam.get_role.return_value = {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{ROLE}"}}

    # F-7d: an untagged existing role is REFUSED (there is no unowned-reuse path any more),
    # and it is still not tagged -- refusing must not claim it either.
    with (
        patch.object(gd.time, "sleep"),
        patch.dict("os.environ", {"APP_AWS_REGION": REGION}, clear=False),
        pytest.raises(ForeignResourceError),
    ):
        gd._ensure_lambda_role(
            iam,
            ROLE,
            "Role for AgentCore Customer Support Lambda",
            region=REGION,
        )

    iam.tag_role.assert_not_called()
    # And the consequence is what the release path reports, end to end: an OWNED function
    # whose (legacy) role is untagged is deleted, and the role is kept, saying why.
    lambda_client = _lambda_at_refcount_zero()
    with (
        patch.object(gd, "_create_iam_client", return_value=_iam_with_role(None)),
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.dict("os.environ", {"APP_AWS_REGION": REGION}, clear=False),
    ):
        line = gd._release_shared_tool_lambda(lambda_client, FUNCTION, "AgentCoreGateway-mine")
    assert "NO OWNER TAG" in line


def test_an_already_absent_role_is_not_an_error():
    lambda_client = _lambda_at_refcount_zero()
    iam = MagicMock()
    iam.get_role.side_effect = _client_error("NoSuchEntity", "cannot be found", "GetRole")

    with (
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        # Needed, and the failure it prevents is worth naming: without it the ambient
        # default region (us-west-2 on a developer machine) goes into stack_id, the
        # fixture's us-east-1 owner tag reads as another deployment's, and the F-7c gate
        # correctly refuses the delete -- so this test would fail on the role assertion
        # for a reason that has nothing to do with roles.
        patch.dict("os.environ", {"APP_AWS_REGION": REGION}, clear=False),
    ):
        line = gd._release_shared_tool_lambda(lambda_client, FUNCTION, "AgentCoreGateway-mine")

    assert "already absent" in line
    iam.delete_role.assert_not_called()


def test_a_function_that_was_already_gone_does_not_delete_a_role():
    """``delete_function`` raising ResourceNotFound means some other teardown removed
    the function. Its refcount evidence is not ours, so don't act on the role."""
    lambda_client = _lambda_at_refcount_zero()
    lambda_client.delete_function.side_effect = _client_error(
        "ResourceNotFoundException", "Function not found", "DeleteFunction"
    )
    iam = _iam_with_role([{"Key": OWNER_TAG_KEY, "Value": stack_id(REGION)}])

    with (
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd, "_prune_orphaned_lambda_permissions", return_value=0),
        patch.dict("os.environ", {"APP_AWS_REGION": REGION}, clear=False),
    ):
        line = gd._release_shared_tool_lambda(lambda_client, FUNCTION, "AgentCoreGateway-mine")

    iam.delete_role.assert_not_called()
    assert "already absent" in line


def test_an_unmapped_function_name_touches_no_role():
    """A non-shared per-gateway Lambda has no entry, and must not fall through to
    deleting some other function's role."""
    with patch.object(gd, "_create_iam_client") as mk:
        assert gd._release_shared_tool_lambda_role("SomeOtherFunction") == ""
    mk.assert_not_called()


def test_every_shared_tool_lambda_has_its_role_mapped():
    """A shared tool Lambda whose name resolves to no role silently reintroduces the
    leak — the release would just return an empty string. Both name families: this
    stack's scoped names (F-7d) and the legacy unscoped literals old manifests carry."""
    for kind in gd._SHARED_TOOL_KINDS:
        fn = gd.shared_tool_function_name(kind, REGION)
        assert gd.is_shared_tool_function(fn, REGION)
        role = gd._shared_tool_lambda_role_name(fn, REGION)
        assert role == gd.shared_tool_role_name(kind, REGION) and role.endswith(f"{kind}Role"), (kind, fn, role)
        # The role is derived from the token IN THE NAME, so a caller passing the wrong
        # region (or none) still pairs the function with its own role.
        assert gd._shared_tool_lambda_role_name(fn, None) == role
    for legacy_fn, (_kind, legacy_role) in gd._LEGACY_SHARED_TOOL_LAMBDAS.items():
        assert gd.is_shared_tool_function(legacy_fn)
        assert gd._shared_tool_lambda_role_name(legacy_fn, REGION) == legacy_role
    assert gd._shared_tool_lambda_role_name("AgentCore-KBTool-deadbeef", REGION) == ""


def test_the_role_is_tagged_at_creation():
    """Without the tag at creation the ownership gate above can never open, so the
    release would correctly-but-uselessly refuse forever."""
    iam = MagicMock()
    iam.create_role.return_value = {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{ROLE}"}}

    with patch.object(gd.time, "sleep"), patch.dict("os.environ", {"APP_AWS_REGION": REGION}, clear=False):
        gd._ensure_lambda_role(iam, ROLE, "Role for AgentCore Customer Support Lambda")

    tags = iam.create_role.call_args.kwargs["Tags"]
    assert {"Key": OWNER_TAG_KEY, "Value": stack_id(REGION)} in tags
    assert any(t["Key"] == PRODUCT_TAG_KEY for t in tags)


def test_role_creation_uses_the_explicit_target_region():
    iam = MagicMock()
    iam.create_role.return_value = {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{ROLE}"}}

    with (
        patch.object(gd.time, "sleep"),
        patch.dict(
            "os.environ",
            {"APP_AWS_REGION": "us-east-1"},
            clear=False,
        ),
    ):
        gd._ensure_lambda_role(
            iam,
            ROLE,
            "Role for AgentCore Customer Support Lambda",
            region="eu-central-1",
        )

    tags = iam.create_role.call_args.kwargs["Tags"]
    assert {"Key": OWNER_TAG_KEY, "Value": stack_id("eu-central-1")} in tags


def test_the_legacy_role_binding_compatibility_path_is_gone():
    """``_assert_legacy_shared_role_binding`` accepted an unowned role whenever the shared
    function bound to it was "owned" -- and "owned" included ADOPTED-untagged, so a
    foreign function+role pair passed (peer d3, F-7d). Deleted, not narrowed: the shared
    roles are stack-scoped by name now, so there is no legacy binding left to honour."""
    assert not hasattr(gd, "_assert_legacy_shared_role_binding")
    import inspect

    assert "allow_unowned_reuse" not in inspect.signature(gd._ensure_lambda_role).parameters


def test_an_unowned_existing_role_is_refused_for_a_shared_lambda():
    """The create path for a shared function under a role that exists without our tags:
    refused before CreateFunction / PassRole, whatever function may be bound to it."""
    iam = MagicMock()
    iam.exceptions.EntityAlreadyExistsException = type("EntityAlreadyExistsException", (Exception,), {})
    iam.create_role.side_effect = iam.exceptions.EntityAlreadyExistsException()
    iam.get_role.return_value = {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{ROLE}", "Tags": []}}
    lambda_client = MagicMock()
    with (
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd, "_create_lambda_client", return_value=lambda_client),
        patch.object(gd.time, "sleep"),
        pytest.raises(ForeignResourceError),
    ):
        gd.create_customer_support_lambda(REGION, "arn:aws:iam::123456789012:role/AgentCoreGateway-g")
    lambda_client.create_function.assert_not_called()
    lambda_client.update_function_code.assert_not_called()
    iam.tag_role.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
