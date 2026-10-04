"""Every IAM-role delete re-proves ownership from the live role tags."""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest
from app.services.resource_ownership import (
    OWNER_TAG_KEY,
    ResourceDeletionRefused,
    delete_owned_iam_role,
    owner_tag_list,
    stack_id,
)

REGION = "us-east-1"
ROLE = "AgentCoreGateway-audit"


@pytest.fixture(autouse=True)
def _stack(monkeypatch):
    monkeypatch.setenv("PROJECT_NAME", "role-delete-tests")
    # "local" keeps deployment_handler's import-time config loader off SSM.
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("APP_AWS_REGION", REGION)


def _iam(tags) -> MagicMock:
    iam = MagicMock()
    role = {"RoleName": ROLE, "Arn": f"arn:aws:iam::123456789012:role/{ROLE}"}
    if tags is not None:
        role["Tags"] = tags
    iam.get_role.return_value = {"Role": role}
    iam.list_role_policies.return_value = {"PolicyNames": ["inline"]}
    iam.list_attached_role_policies.return_value = {
        "AttachedPolicies": [{"PolicyArn": "arn:aws:iam::aws:policy/ReadOnlyAccess"}]
    }
    return iam


def _assert_no_role_mutation(iam: MagicMock) -> None:
    iam.list_role_policies.assert_not_called()
    iam.list_attached_role_policies.assert_not_called()
    iam.delete_role_policy.assert_not_called()
    iam.detach_role_policy.assert_not_called()
    iam.delete_role.assert_not_called()


@pytest.mark.parametrize(
    "tags",
    [
        None,
        [],
        [{"Key": OWNER_TAG_KEY, "Value": "another-stack-us-east-1"}],
        [
            {"Key": "Project", "Value": "role-delete-tests"},
            {"Key": "Environment", "Value": "local"},
        ],
    ],
)
def test_shared_role_delete_primitive_proves_live_ownership_before_any_mutation(tags):
    iam = _iam(tags)

    with pytest.raises(ResourceDeletionRefused):
        delete_owned_iam_role(iam, ROLE, REGION)

    iam.get_role.assert_called_once_with(RoleName=ROLE)
    _assert_no_role_mutation(iam)


def test_shared_role_delete_primitive_removes_policies_only_after_exact_proof():
    iam = _iam(owner_tag_list(REGION))

    delete_owned_iam_role(iam, ROLE, REGION)

    assert iam.method_calls[0].args == ()
    assert iam.method_calls[0].kwargs == {"RoleName": ROLE}
    iam.list_attached_role_policies.assert_called_once_with(RoleName=ROLE)
    iam.detach_role_policy.assert_called_once_with(
        RoleName=ROLE,
        PolicyArn="arn:aws:iam::aws:policy/ReadOnlyAccess",
    )
    iam.list_role_policies.assert_called_once_with(RoleName=ROLE)
    iam.delete_role_policy.assert_called_once_with(RoleName=ROLE, PolicyName="inline")
    iam.delete_role.assert_called_once_with(RoleName=ROLE)


def test_shared_role_delete_primitive_reads_and_removes_every_policy_page():
    iam = _iam(owner_tag_list(REGION))
    iam.list_attached_role_policies.side_effect = [
        {
            "AttachedPolicies": [{"PolicyArn": "arn:aws:iam::aws:policy/First"}],
            "IsTruncated": True,
            "Marker": "attached-page-2",
        },
        {
            "AttachedPolicies": [{"PolicyArn": "arn:aws:iam::aws:policy/Second"}],
            "IsTruncated": False,
        },
    ]
    iam.list_role_policies.side_effect = [
        {
            "PolicyNames": ["inline-first"],
            "IsTruncated": True,
            "Marker": "inline-page-2",
        },
        {
            "PolicyNames": ["inline-second"],
            "IsTruncated": False,
        },
    ]

    delete_owned_iam_role(iam, ROLE, REGION)

    assert iam.list_attached_role_policies.call_args_list == [
        call(RoleName=ROLE),
        call(RoleName=ROLE, Marker="attached-page-2"),
    ]
    assert iam.list_role_policies.call_args_list == [
        call(RoleName=ROLE),
        call(RoleName=ROLE, Marker="inline-page-2"),
    ]
    assert {invocation.kwargs["PolicyArn"] for invocation in iam.detach_role_policy.call_args_list} == {
        "arn:aws:iam::aws:policy/First",
        "arn:aws:iam::aws:policy/Second",
    }
    assert {invocation.kwargs["PolicyName"] for invocation in iam.delete_role_policy.call_args_list} == {
        "inline-first",
        "inline-second",
    }
    iam.delete_role.assert_called_once_with(RoleName=ROLE)


def test_shared_role_delete_does_not_mutate_on_a_repeated_policy_marker():
    iam = _iam(owner_tag_list(REGION))
    iam.list_attached_role_policies.side_effect = [
        {
            "AttachedPolicies": [{"PolicyArn": "arn:aws:iam::aws:policy/First"}],
            "IsTruncated": True,
            "Marker": "same",
        },
        {
            "AttachedPolicies": [{"PolicyArn": "arn:aws:iam::aws:policy/Second"}],
            "IsTruncated": True,
            "Marker": "same",
        },
    ]

    with pytest.raises(RuntimeError, match="repeated pagination token"):
        delete_owned_iam_role(iam, ROLE, REGION)

    iam.list_role_policies.assert_not_called()
    iam.detach_role_policy.assert_not_called()
    iam.delete_role_policy.assert_not_called()
    iam.delete_role.assert_not_called()


@pytest.mark.parametrize(
    "tags",
    [
        None,
        [],
        [{"Key": OWNER_TAG_KEY, "Value": "another-stack-us-east-1"}],
        [
            {"Key": "Project", "Value": "role-delete-tests"},
            {"Key": "Environment", "Value": "local"},
        ],
    ],
)
def test_user_delete_manifest_refuses_a_role_without_exact_runtime_ownership(monkeypatch, tags):
    from app import deployment_handler
    from app.services import step_clients

    iam = _iam(tags)
    monkeypatch.setattr(step_clients, "client", lambda event, service, **kwargs: iam)

    with pytest.raises(ResourceDeletionRefused):
        deployment_handler._delete_managed_resource(
            {"type": "iam_role", "name": ROLE, "region": REGION},
            REGION,
        )

    iam.get_role.assert_called_once_with(RoleName=ROLE)
    _assert_no_role_mutation(iam)


def test_user_delete_manifest_deletes_an_exactly_owned_role(monkeypatch):
    from app import deployment_handler
    from app.services import step_clients

    iam = _iam(owner_tag_list(REGION))
    monkeypatch.setattr(step_clients, "client", lambda event, service, **kwargs: iam)

    line = deployment_handler._delete_managed_resource(
        {"type": "iam_role", "name": ROLE, "region": REGION},
        REGION,
    )

    iam.get_role.assert_called_once_with(RoleName=ROLE)
    iam.delete_role_policy.assert_called_once_with(RoleName=ROLE, PolicyName="inline")
    iam.detach_role_policy.assert_called_once()
    iam.delete_role.assert_called_once_with(RoleName=ROLE)
    assert "deleted" in line


def test_failure_cleanup_refuses_a_foreign_role_before_any_mutation(monkeypatch):
    from app.services import step_clients
    from app.step_handlers import status_update_step

    iam = _iam([{"Key": OWNER_TAG_KEY, "Value": "another-stack-us-east-1"}])
    monkeypatch.setattr(step_clients, "client", lambda event, service, **kwargs: iam)

    with pytest.raises(ResourceDeletionRefused):
        status_update_step._cleanup_resource(
            {"type": "iam_role", "name": ROLE, "region": REGION},
            REGION,
            {},
        )

    iam.get_role.assert_called_once_with(RoleName=ROLE)
    _assert_no_role_mutation(iam)


def test_gateway_cleanup_never_derives_a_role_from_the_requested_gateway_name(monkeypatch):
    from app.services import gateway_deployer

    iam = _iam(owner_tag_list(REGION))
    monkeypatch.setattr(gateway_deployer, "_create_iam_client", lambda: iam)
    monkeypatch.setattr(gateway_deployer, "_create_lambda_client", lambda region: MagicMock())

    gateway_deployer.cleanup_gateway_resources(
        "partial",
        REGION,
        {"gateway_name": "audit"},
    )

    iam.get_role.assert_not_called()
    _assert_no_role_mutation(iam)


def test_gateway_cleanup_rechecks_the_confirmed_role_and_protects_a_replacement(monkeypatch):
    from app.services import gateway_deployer

    iam = _iam([{"Key": OWNER_TAG_KEY, "Value": "replacement-owner-us-east-1"}])
    monkeypatch.setattr(gateway_deployer, "_create_iam_client", lambda: iam)
    monkeypatch.setattr(gateway_deployer, "_create_lambda_client", lambda region: MagicMock())

    log = gateway_deployer.cleanup_gateway_resources(
        "partial",
        REGION,
        {
            "gateway_name": "audit",
            "gateway_role_name": ROLE,
        },
    )

    iam.get_role.assert_called_once_with(RoleName=ROLE)
    _assert_no_role_mutation(iam)
    assert any("ownership could not be proven" in line for line in log)


def test_gateway_abort_retains_an_adopted_gateway_and_its_attached_graph(monkeypatch):
    """An adopted gateway is one dependency graph, not an independently safe id.

    Keeping the gateway while deleting its current app client, targets, connector
    credentials, or tool Lambda would leave the pre-existing gateway live but
    unusable. The abort path must therefore make no service mutation for the graph.
    """
    from app.services import gateway_deployer

    control_factory = MagicMock()
    cognito_factory = MagicMock()
    lambda_factory = MagicMock()
    secrets_factory = MagicMock()
    iam_factory = MagicMock()
    monkeypatch.setattr(
        gateway_deployer,
        "_create_agentcore_control_client",
        control_factory,
    )
    monkeypatch.setattr(gateway_deployer, "_create_cognito_client", cognito_factory)
    monkeypatch.setattr(gateway_deployer, "_create_lambda_client", lambda_factory)
    monkeypatch.setattr(gateway_deployer, "_create_secrets_client", secrets_factory)
    monkeypatch.setattr(gateway_deployer, "_create_iam_client", iam_factory)

    log = gateway_deployer.cleanup_gateway_resources(
        "failed-redeploy",
        REGION,
        {
            "gateway_id": "gw-existing",
            "gateway_created_by_deployment": False,
            "gateway_role_name": ROLE,
            "gateway_role_created_by_deployment": False,
            "client_info": {
                "provider": "cognito",
                "user_pool_id": "us-east-1_SHARED",
                "client_id": "new-client",
            },
            "lambda_function_name": "AgentCoreDynamicTools",
            "custom_tool_lambdas": ["custom-tool"],
            "custom_tool_roles": ["custom-role"],
            "connector_credential_providers": ["API_KEY:provider"],
            "connector_secret_arns": ["arn:aws:secretsmanager:us-east-1:1:secret:s"],
            "connector_spec_s3_uris": ["s3://bucket/spec.json"],
        },
        deployment_id="failed-redeploy",
    )

    control_factory.assert_not_called()
    cognito_factory.assert_not_called()
    lambda_factory.assert_not_called()
    secrets_factory.assert_not_called()
    iam_factory.assert_not_called()
    assert any("Gateway gw-existing left in place" in line for line in log)
    assert any("dependency graph left in place" in line for line in log)


def test_gateway_abort_can_reclaim_only_a_new_unused_role_beside_an_adopted_gateway(
    monkeypatch,
):
    """CreateRole happens before CreateGateway discovers a name conflict.

    If the existing gateway uses another role, the newly created role is not part
    of the adopted graph and may be reclaimed after exact live-tag verification.
    """
    from app.services import gateway_deployer

    iam = _iam(owner_tag_list(REGION))
    control_factory = MagicMock()
    monkeypatch.setattr(
        gateway_deployer,
        "_create_agentcore_control_client",
        control_factory,
    )
    monkeypatch.setattr(gateway_deployer, "_create_iam_client", lambda: iam)

    log = gateway_deployer.cleanup_gateway_resources(
        "failed-redeploy",
        REGION,
        {
            "gateway_id": "gw-existing",
            "gateway_created_by_deployment": False,
            "gateway_role_name": ROLE,
            "gateway_role_created_by_deployment": True,
        },
    )

    control_factory.assert_not_called()
    iam.get_role.assert_called_once_with(RoleName=ROLE)
    iam.delete_role.assert_called_once_with(RoleName=ROLE)
    assert any(f"Gateway IAM role {ROLE} deleted" in line for line in log)


def test_exact_owner_value_is_the_one_the_delete_gate_requires():
    assert {tag["Key"]: tag["Value"] for tag in owner_tag_list(REGION)}[OWNER_TAG_KEY] == stack_id(REGION)
