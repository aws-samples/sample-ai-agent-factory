"""Bug 168: shared tool Lambda resource-policy pruning.

A custom-tool Lambda is shared by name across deployments and accumulates one
``AllowAgentCoreInvoke-<role>`` statement per gateway role. When a prior
gateway's role is deleted on teardown, its statement lingers with a dangling
principal — and a policy carrying a dangling principal makes lambda:AddPermission
reject EVERY subsequent call ("The provided principal was invalid"), bricking all
future gateway deploys that reuse the Lambda. _prune_orphaned_lambda_permissions
removes statements whose principal role no longer exists in IAM.

Pure unit tests — lambda + iam clients are MagicMocks.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.services import gateway_deployer as gd
from botocore.exceptions import ClientError


def _client_error(code, msg, op="Op"):
    return ClientError({"Error": {"Code": code, "Message": msg}}, op)


ACCOUNT = "123456789012"


def _arn(role_name):
    return f"arn:aws:iam::{ACCOUNT}:role/{role_name}"


def _policy(*sids):
    return json.dumps({"Statement": [{"Sid": s, "Effect": "Allow"} for s in sids]})


def _granted(*pairs):
    """Statements as Lambda's GetPolicy returns them: one principal per invoke grant."""
    return json.dumps(
        {"Statement": [{"Sid": sid, "Effect": "Allow", "Principal": {"AWS": principal}} for sid, principal in pairs]}
    )


def _iam_with_existing(existing_roles):
    iam = MagicMock()

    def _get_role(RoleName):
        if RoleName in existing_roles:
            return {"Role": {"RoleName": RoleName, "Arn": _arn(RoleName)}}
        raise _client_error("NoSuchEntity", f"role {RoleName} not found", "GetRole")

    iam.get_role.side_effect = _get_role
    return iam


def test_prunes_statement_for_deleted_role():
    lam = MagicMock()
    lam.get_policy.return_value = {
        "Policy": _granted(
            ("AllowAgentCoreInvoke-AgentCoreGateway-gone", "AROAGONEGONEGONEGONE1"),  # role deleted
            ("AllowAgentCoreInvoke-AgentCoreGateway-live", _arn("AgentCoreGateway-live")),  # role exists
        )
    }
    iam = _iam_with_existing({"AgentCoreGateway-live"})
    with patch.object(gd, "_create_iam_client", return_value=iam):
        pruned = gd._prune_orphaned_lambda_permissions(lam, "AgentCore-CustomTool-x")
    assert pruned == 1
    lam.remove_permission.assert_called_once_with(
        FunctionName="AgentCore-CustomTool-x",
        StatementId="AllowAgentCoreInvoke-AgentCoreGateway-gone",
    )


def test_keeps_statements_for_live_roles():
    lam = MagicMock()
    lam.get_policy.return_value = {
        "Policy": _granted(("AllowAgentCoreInvoke-AgentCoreGateway-live", _arn("AgentCoreGateway-live")))
    }
    iam = _iam_with_existing({"AgentCoreGateway-live"})
    with patch.object(gd, "_create_iam_client", return_value=iam):
        pruned = gd._prune_orphaned_lambda_permissions(lam, "fn")
    assert pruned == 0
    lam.remove_permission.assert_not_called()


def test_prunes_a_statement_whose_role_was_recreated_under_the_same_name():
    """The live regression (2026-10-02, G08). The role exists again, but the statement still names
    the deleted one's unique id, so it authorizes nobody, and leaving it in place makes the caller's
    add_permission conflict on the same StatementId and read the conflict as "already permitted"."""
    lam = MagicMock()
    lam.get_policy.return_value = {
        "Policy": _granted(("AllowAgentCoreInvoke-AgentCoreGateway-support-gateway", "AROASNV5YASAZHWFURF3I"))
    }
    iam = _iam_with_existing({"AgentCoreGateway-support-gateway"})
    with patch.object(gd, "_create_iam_client", return_value=iam):
        pruned = gd._prune_orphaned_lambda_permissions(lam, "acfe2e-mxfix-gateway-tool")
    assert pruned == 1
    lam.remove_permission.assert_called_once_with(
        FunctionName="acfe2e-mxfix-gateway-tool",
        StatementId="AllowAgentCoreInvoke-AgentCoreGateway-support-gateway",
    )


def test_a_statement_for_another_principal_under_our_sid_is_pruned():
    """The Sid namespace is the platform's; a grant in it that names anything but the role's current
    ARN (another account's role of the same name, a statement with no principal) authorizes nothing
    this platform deployed."""
    lam = MagicMock()
    lam.get_policy.return_value = {
        "Policy": _granted(
            ("AllowAgentCoreInvoke-AgentCoreGateway-live", "arn:aws:iam::999999999999:role/AgentCoreGateway-live")
        )
    }
    iam = _iam_with_existing({"AgentCoreGateway-live"})
    with patch.object(gd, "_create_iam_client", return_value=iam):
        assert gd._prune_orphaned_lambda_permissions(lam, "fn") == 1


def test_the_users_lambda_target_is_granted_after_a_role_is_recreated():
    """End to end through _grant_gateway_invoke_on_lambda: the stale same-Sid statement is removed
    first, so add_permission lands for the new role instead of conflicting."""
    policy = {"stmts": [("AllowAgentCoreInvoke-AgentCoreGateway-support-gateway", "AROASNV5YASAZHWFURF3I")]}
    lam = MagicMock()
    lam.exceptions.ResourceConflictException = type("ResourceConflictException", (Exception,), {})
    lam.exceptions.InvalidParameterValueException = type("InvalidParameterValueException", (Exception,), {})
    lam.get_policy.side_effect = lambda **_kw: {"Policy": _granted(*policy["stmts"])}

    def _remove(FunctionName, StatementId):  # noqa: N803 -- boto3 keywords
        policy["stmts"] = [s for s in policy["stmts"] if s[0] != StatementId]

    def _add(FunctionName, StatementId, Action, Principal):  # noqa: N803 -- boto3 keywords
        if any(s[0] == StatementId for s in policy["stmts"]):
            raise lam.exceptions.ResourceConflictException("The statement id provided already exists")
        policy["stmts"].append((StatementId, Principal))

    lam.remove_permission.side_effect = _remove
    lam.add_permission.side_effect = _add
    role_arn = _arn("AgentCoreGateway-support-gateway")
    iam = _iam_with_existing({"AgentCoreGateway-support-gateway"})
    fixture = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:acfe2e-mxfix-gateway-tool"
    with (
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd, "_create_lambda_client", return_value=lam),
    ):
        gd._grant_gateway_invoke_on_lambda("us-east-1", fixture, role_arn)
    assert policy["stmts"] == [("AllowAgentCoreInvoke-AgentCoreGateway-support-gateway", role_arn)]


def test_ignores_non_managed_statements():
    """Only AllowAgentCoreInvoke-* statements are touched; others are left alone."""
    lam = MagicMock()
    lam.get_policy.return_value = {"Policy": _policy("SomeOtherGrant", "AllowAgentCoreInvoke-")}
    iam = _iam_with_existing(set())
    with patch.object(gd, "_create_iam_client", return_value=iam):
        pruned = gd._prune_orphaned_lambda_permissions(lam, "fn")
    assert pruned == 0
    lam.remove_permission.assert_not_called()


def test_no_policy_is_safe():
    lam = MagicMock()
    lam.get_policy.side_effect = Exception("ResourceNotFoundException: no policy")
    with patch.object(gd, "_create_iam_client", return_value=MagicMock()):
        assert gd._prune_orphaned_lambda_permissions(lam, "fn") == 0


def test_unknown_iam_error_does_not_prune():
    """A non-NoSuchEntity IAM error must NOT remove a possibly-valid grant."""
    lam = MagicMock()
    lam.get_policy.return_value = {"Policy": _policy("AllowAgentCoreInvoke-AgentCoreGateway-x")}
    iam = MagicMock()
    iam.get_role.side_effect = Exception("Throttling: rate exceeded")
    with patch.object(gd, "_create_iam_client", return_value=iam):
        pruned = gd._prune_orphaned_lambda_permissions(lam, "fn")
    assert pruned == 0
    lam.remove_permission.assert_not_called()
