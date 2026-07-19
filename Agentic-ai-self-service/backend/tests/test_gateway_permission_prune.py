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


def _policy(*sids):
    return json.dumps({"Statement": [{"Sid": s, "Effect": "Allow"} for s in sids]})


def _iam_with_existing(existing_roles):
    iam = MagicMock()

    def _get_role(RoleName):
        if RoleName in existing_roles:
            return {"Role": {"RoleName": RoleName}}
        raise _client_error("NoSuchEntity", f"role {RoleName} not found", "GetRole")

    iam.get_role.side_effect = _get_role
    return iam


def test_prunes_statement_for_deleted_role():
    lam = MagicMock()
    lam.get_policy.return_value = {
        "Policy": _policy(
            "AllowAgentCoreInvoke-AgentCoreGateway-gone",  # role deleted
            "AllowAgentCoreInvoke-AgentCoreGateway-live",  # role exists
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
    lam.get_policy.return_value = {"Policy": _policy("AllowAgentCoreInvoke-AgentCoreGateway-live")}
    iam = _iam_with_existing({"AgentCoreGateway-live"})
    with patch.object(gd, "_create_iam_client", return_value=iam):
        pruned = gd._prune_orphaned_lambda_permissions(lam, "fn")
    assert pruned == 0
    lam.remove_permission.assert_not_called()


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
