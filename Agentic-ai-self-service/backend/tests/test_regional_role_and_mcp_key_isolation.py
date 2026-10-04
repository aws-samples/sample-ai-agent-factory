"""Regional IAM names and MCP artifacts must not collide across deployments.

AgentCore resources are regional, but IAM role names are account-global.  A
same-account deployment of the same canvas into two regions may therefore reuse
the AgentCore resource names while its execution-role names must still differ.
The home-region spelling stays unchanged for compatibility with existing stacks.

The generated MCP bundle has a similar identity requirement.  Its S3 prefix must
stay stable for one primary runtime (to preserve AgentCore's IAM/S3 cache) while
two primary runtimes that happen to use the same MCP server name must not replace
each other's code.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from app.services import naming

HOME_REGION = "us-east-1"
OTHER_REGION = "eu-west-1"


def _regional_role_name(
    base_name: str,
    region: str,
    *,
    home_region: str = HOME_REGION,
) -> str:
    helper = getattr(naming, "regional_iam_role_name", None)
    assert callable(helper), "services.naming must expose regional_iam_role_name"
    return helper(base_name, region, home_region=home_region)


def _mcp_code_key(primary_runtime_name: str, mcp_name: str) -> str:
    helper = getattr(naming, "scoped_mcp_code_s3_key", None)
    assert callable(helper), "services.naming must expose scoped_mcp_code_s3_key"
    return helper(primary_runtime_name, mcp_name)


def test_home_region_keeps_the_existing_role_name():
    assert _regional_role_name("AgentCoreGateway-orders", HOME_REGION) == "AgentCoreGateway-orders"


def test_non_home_region_gets_an_account_global_discriminator():
    assert (
        _regional_role_name(
            "AgentCoreGateway-orders",
            OTHER_REGION,
        )
        == "AgentCoreGateway-orders-eu-west-1"
    )


def test_ambient_home_region_prefers_the_platform_region(monkeypatch):
    monkeypatch.setenv("APP_AWS_REGION", HOME_REGION)
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    helper = getattr(naming, "regional_iam_role_name", None)
    assert callable(helper)
    assert helper("AgentCoreMemory-orders", HOME_REGION) == "AgentCoreMemory-orders"
    assert helper("AgentCoreMemory-orders", OTHER_REGION).endswith("-eu-west-1")


def test_long_regional_role_names_are_valid_deterministic_and_collision_safe():
    common = "AgentCoreRuntime-" + ("customer_support_" * 5)
    first = _regional_role_name(common + "alpha", OTHER_REGION)
    second = _regional_role_name(common + "bravo", OTHER_REGION)

    assert first != second
    assert first == _regional_role_name(common + "alpha", OTHER_REGION)
    assert first.endswith("-eu-west-1")
    assert second.endswith("-eu-west-1")
    assert len(first) <= 64
    assert len(second) <= 64
    assert re.fullmatch(r"[A-Za-z0-9+=,.@_-]+", first)
    assert re.fullmatch(r"[A-Za-z0-9+=,.@_-]+", second)


def test_mcp_code_key_is_stable_for_one_primary_runtime():
    # Preserve the existing runtime-name sanitizer's case semantics. Changing
    # case here would move an established agent to a new S3 prefix and recreate
    # the IAM/S3-prefix propagation race this stable key is designed to avoid.
    expected = "deployments/by-name/Orders_Agent/mcp/Shared_Tools/mcp-server-code.zip"
    assert _mcp_code_key("Orders Agent", "Shared Tools") == expected
    assert _mcp_code_key("Orders Agent", "Shared Tools") == expected


def test_two_primary_runtimes_cannot_replace_the_same_named_mcp_bundle():
    first = _mcp_code_key("orders-agent", "shared-tools")
    second = _mcp_code_key("support-agent", "shared-tools")

    assert first != second
    assert first.endswith("/mcp/shared_tools/mcp-server-code.zip")
    assert second.endswith("/mcp/shared_tools/mcp-server-code.zip")


def test_per_agent_runtime_role_uses_the_same_regional_helper():
    from app.services.per_agent_identity import build_per_agent_role_name

    assert build_per_agent_role_name(
        "orders_agent",
        region=OTHER_REGION,
        home_region=HOME_REGION,
    ) == _regional_role_name(
        "AgentCoreRuntime-orders_agent",
        OTHER_REGION,
    )


def test_gateway_reports_the_exact_regional_role_iam_acknowledged(monkeypatch):
    from app.services import gateway_deployer as gd

    monkeypatch.setenv("APP_AWS_REGION", HOME_REGION)
    expected = _regional_role_name("AgentCoreGateway-orders", OTHER_REGION)
    iam = MagicMock()
    iam.exceptions.EntityAlreadyExistsException = type(
        "EntityAlreadyExistsException",
        (Exception,),
        {},
    )
    iam.create_role.return_value = {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{expected}"}}
    control = MagicMock()
    control.list_gateways.return_value = {"items": []}  # no same-name gateway: the create path
    control.create_gateway.side_effect = RuntimeError("stop after role creation")

    with (
        patch.object(gd, "_create_agentcore_control_client", return_value=control),
        patch.object(gd, "_create_cognito_client", return_value=MagicMock()),
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd.boto3, "client", return_value=MagicMock()),
        patch.object(gd.time, "sleep"),
        patch.object(
            gd,
            "_create_external_oauth_config",
            return_value={
                "authorizer_config": {"customJWTAuthorizer": {}},
                "client_info": {"provider": "custom"},
            },
        ),
        patch.object(gd, "cleanup_gateway_resources", return_value=[]),
    ):
        result = gd.deploy_gateway(
            {"name": "orders"},
            OTHER_REGION,
            identity_config={
                "provider": "custom",
                "client_id": "client",
                "discovery_url": "https://idp.example/.well-known/openid-configuration",
            },
        )

    assert iam.create_role.call_args.kwargs["RoleName"] == expected
    assert result["gateway_role_name"] == expected


def test_shared_lambda_manifest_keeps_the_exact_gateway_role():
    from app.step_handlers.gateway_step import _gateway_manifest_resources

    exact_role = _regional_role_name("AgentCoreGateway-orders", OTHER_REGION)
    rows = _gateway_manifest_resources(
        OTHER_REGION,
        {
            "success": True,
            "gateway_name": "orders",
            "gateway_role_name": exact_role,
            "gateway_role_created_by_deployment": True,
            "lambda_function_name": "AgentCoreDynamicTools",
        },
    )

    lambda_row = next(row for row in rows if row["type"] == "lambda")
    assert lambda_row["gateway_role"] == exact_role


@pytest.mark.parametrize(
    ("creator_name", "kind"),
    [("create_dynamic_gateway_lambda", "DynamicTools"), ("create_customer_support_lambda", "CustomerSupportTools")],
)
def test_shared_tool_lambda_role_is_stack_and_region_scoped_on_create(monkeypatch, creator_name, kind):
    """F-7d replaced the regional suffix with a stack token that CONTAINS the region: the
    function and its role both carry it, so two regions of one stack -- or two stacks --
    never share a name, and the infra grants exactly this prefix."""
    from app.services import gateway_deployer as gd

    monkeypatch.setenv("APP_AWS_REGION", HOME_REGION)
    monkeypatch.setenv("PROJECT_NAME", "acfe2e")
    monkeypatch.setenv("ENVIRONMENT", "p0920")
    requested_roles: list[str] = []
    iam = MagicMock()
    lambda_client = MagicMock()

    def _ensure_role(_iam, role_name, _description, *, outcome, **_kwargs):
        requested_roles.append(role_name)
        outcome["created"] = True
        outcome["owned"] = True
        return f"arn:aws:iam::123456789012:role/{role_name}"

    expected_function = gd.shared_tool_function_name(kind, OTHER_REGION)
    create_or_update = MagicMock(return_value=f"arn:aws:lambda:{OTHER_REGION}:123:function:{expected_function}")
    monkeypatch.setattr(gd, "_create_iam_client", lambda: iam)
    monkeypatch.setattr(gd, "_create_lambda_client", lambda _region: lambda_client)
    monkeypatch.setattr(gd, "_ensure_lambda_role", _ensure_role)
    monkeypatch.setattr(gd, "_create_or_update_lambda", create_or_update)

    getattr(gd, creator_name)(
        OTHER_REGION,
        "arn:aws:iam::123456789012:role/AgentCoreGateway-orders-eu-west-1",
    )

    expected_role = gd.shared_tool_role_name(kind, OTHER_REGION)
    assert requested_roles == [expected_role]
    assert create_or_update.call_args.args[1] == expected_function
    assert create_or_update.call_args.args[2].endswith(f"/{expected_role}")
    assert expected_role != gd.shared_tool_role_name(kind, HOME_REGION), "another region is another role"
    assert expected_function.startswith("AgentCore-") and expected_function.endswith(f"-{kind}")


def test_shared_tool_lambda_cleanup_uses_the_same_regional_role(monkeypatch):
    from app.services import gateway_deployer as gd
    from app.services.resource_ownership import owner_tag_list

    monkeypatch.setenv("APP_AWS_REGION", HOME_REGION)
    expected_role = _regional_role_name(
        "AgentCoreCustomerSupportLambdaRole",
        OTHER_REGION,
    )
    iam = MagicMock()
    iam.get_role.return_value = {
        "Role": {
            "Arn": f"arn:aws:iam::123456789012:role/{expected_role}",
            "Tags": owner_tag_list(OTHER_REGION),
        }
    }
    delete_role = MagicMock()
    monkeypatch.setattr(gd, "_create_iam_client", lambda: iam)
    monkeypatch.setattr(gd, "delete_owned_iam_role", delete_role)

    line = gd._release_shared_tool_lambda_role(
        "AgentCoreCustomerSupportTools",
        OTHER_REGION,
    )

    iam.get_role.assert_called_once_with(RoleName=expected_role)
    delete_role.assert_called_once_with(iam, expected_role, OTHER_REGION)
    assert expected_role in line


def test_existing_non_home_shared_lambda_moves_off_the_legacy_home_role(monkeypatch):
    from app.services import gateway_deployer as gd
    from app.services.resource_ownership import owner_tags

    monkeypatch.setenv("APP_AWS_REGION", HOME_REGION)
    function_name = "AgentCoreDynamicTools"
    expected_role = _regional_role_name(
        "AgentCoreDynamicToolsLambdaRole",
        OTHER_REGION,
    )
    expected_role_arn = f"arn:aws:iam::123456789012:role/{expected_role}"
    legacy_role_arn = "arn:aws:iam::123456789012:role/AgentCoreDynamicToolsLambdaRole"

    conflict = type("ResourceConflictException", (Exception,), {})
    lambda_client = MagicMock()
    lambda_client.exceptions.ResourceConflictException = conflict
    lambda_client.exceptions.InvalidParameterValueException = type(
        "InvalidParameterValueException",
        (Exception,),
        {},
    )
    lambda_client.create_function.side_effect = conflict("already exists")
    legacy_function = {
        "Configuration": {
            "FunctionArn": (f"arn:aws:lambda:{OTHER_REGION}:123456789012:function:{function_name}"),
            "Role": legacy_role_arn,
            "State": "Active",
            "LastUpdateStatus": "Successful",
            "RevisionId": "1",  # F-7d: every update is fenced on the revision it read
        }
    }
    migrated_function = {
        "Configuration": {
            **legacy_function["Configuration"],
            "Role": expected_role_arn,
        }
    }
    lambda_client.get_function.side_effect = lambda **_kwargs: (
        migrated_function if lambda_client.update_function_configuration.call_count else legacy_function
    )
    lambda_client.list_tags.return_value = {"Tags": owner_tags(OTHER_REGION)}
    monkeypatch.setattr(gd, "_wait_lambda_updatable", lambda *_args, **_kwargs: None)

    gd._create_or_update_lambda(
        lambda_client,
        function_name,
        expected_role_arn,
        b"zip",
        "dynamic tools",
        region=OTHER_REGION,
    )

    lambda_client.update_function_configuration.assert_called_once_with(
        FunctionName=function_name,
        Role=expected_role_arn,
        RevisionId="1",
    )
    lambda_client.update_function_code.assert_called_once_with(
        FunctionName=function_name,
        ZipFile=b"zip",
        RevisionId="1",
    )


def test_shared_lambda_code_update_conflicts_cannot_fall_through_as_success(monkeypatch):
    from app.services import gateway_deployer as gd
    from app.services.resource_ownership import owner_tags

    function_name = "AgentCoreDynamicTools"
    role_arn = "arn:aws:iam::123456789012:role/" + _regional_role_name("AgentCoreDynamicToolsLambdaRole", OTHER_REGION)
    conflict = type("ResourceConflictException", (Exception,), {})
    lambda_client = MagicMock()
    lambda_client.exceptions.ResourceConflictException = conflict
    lambda_client.exceptions.InvalidParameterValueException = type(
        "InvalidParameterValueException",
        (Exception,),
        {},
    )
    lambda_client.create_function.side_effect = conflict("already exists")
    lambda_client.get_function.return_value = {
        "Configuration": {
            "FunctionArn": (f"arn:aws:lambda:{OTHER_REGION}:123456789012:function:{function_name}"),
            "Role": role_arn,
            "State": "Active",
            "LastUpdateStatus": "Successful",
            "RevisionId": "1",  # F-7d: every update is fenced on the revision it read
        }
    }
    lambda_client.list_tags.return_value = {"Tags": owner_tags(OTHER_REGION)}
    lambda_client.update_function_code.side_effect = [conflict(f"still updating {attempt}") for attempt in range(8)]
    monkeypatch.setattr(gd, "_wait_lambda_updatable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gd.time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(conflict, match="still updating 7"):
        gd._create_or_update_lambda(
            lambda_client,
            function_name,
            role_arn,
            b"zip",
            "dynamic tools",
            region=OTHER_REGION,
        )

    assert lambda_client.update_function_code.call_count == 8


def test_shared_lambda_role_migration_retries_conflicts_before_code_update(monkeypatch):
    from app.services import gateway_deployer as gd
    from app.services.resource_ownership import owner_tags

    function_name = "AgentCoreDynamicTools"
    role_arn = "arn:aws:iam::123456789012:role/" + _regional_role_name("AgentCoreDynamicToolsLambdaRole", OTHER_REGION)
    legacy_role_arn = "arn:aws:iam::123456789012:role/AgentCoreDynamicToolsLambdaRole"
    conflict = type("ResourceConflictException", (Exception,), {})
    lambda_client = MagicMock()
    lambda_client.exceptions.ResourceConflictException = conflict
    lambda_client.exceptions.InvalidParameterValueException = type(
        "InvalidParameterValueException",
        (Exception,),
        {},
    )
    lambda_client.create_function.side_effect = conflict("already exists")
    legacy_function = {
        "Configuration": {
            "FunctionArn": (f"arn:aws:lambda:{OTHER_REGION}:123456789012:function:{function_name}"),
            "Role": legacy_role_arn,
            "State": "Active",
            "LastUpdateStatus": "Successful",
            "RevisionId": "1",  # F-7d: every update is fenced on the revision it read
        }
    }
    migrated_function = {
        "Configuration": {
            **legacy_function["Configuration"],
            "Role": role_arn,
        }
    }
    migration_state = {"complete": False}

    def _get_function(**_kwargs):
        return migrated_function if migration_state["complete"] else legacy_function

    update_attempts = 0

    def _update_configuration(**_kwargs):
        nonlocal update_attempts
        update_attempts += 1
        if update_attempts == 1:
            raise conflict("configuration still updating")
        migration_state["complete"] = True
        return {}

    lambda_client.get_function.side_effect = _get_function
    lambda_client.list_tags.return_value = {"Tags": owner_tags(OTHER_REGION)}
    lambda_client.update_function_configuration.side_effect = _update_configuration
    monkeypatch.setattr(gd, "_wait_lambda_updatable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gd.time, "sleep", lambda *_args, **_kwargs: None)

    gd._create_or_update_lambda(
        lambda_client,
        function_name,
        role_arn,
        b"zip",
        "dynamic tools",
        region=OTHER_REGION,
    )

    assert lambda_client.update_function_configuration.call_count == 2
    lambda_client.update_function_code.assert_called_once_with(
        FunctionName=function_name,
        ZipFile=b"zip",
        RevisionId="1",
    )


def test_shared_lambda_role_migration_conflict_exhaustion_is_not_success(monkeypatch):
    from app.services import gateway_deployer as gd
    from app.services.resource_ownership import owner_tags

    function_name = "AgentCoreDynamicTools"
    role_arn = "arn:aws:iam::123456789012:role/" + _regional_role_name("AgentCoreDynamicToolsLambdaRole", OTHER_REGION)
    legacy_role_arn = "arn:aws:iam::123456789012:role/AgentCoreDynamicToolsLambdaRole"
    conflict = type("ResourceConflictException", (Exception,), {})
    lambda_client = MagicMock()
    lambda_client.exceptions.ResourceConflictException = conflict
    lambda_client.exceptions.InvalidParameterValueException = type(
        "InvalidParameterValueException",
        (Exception,),
        {},
    )
    lambda_client.create_function.side_effect = conflict("already exists")
    lambda_client.get_function.return_value = {
        "Configuration": {
            "FunctionArn": (f"arn:aws:lambda:{OTHER_REGION}:123456789012:function:{function_name}"),
            "Role": legacy_role_arn,
            "State": "Active",
            "LastUpdateStatus": "Successful",
            "RevisionId": "1",  # F-7d: every update is fenced on the revision it read
        }
    }
    lambda_client.list_tags.return_value = {"Tags": owner_tags(OTHER_REGION)}
    lambda_client.update_function_configuration.side_effect = [
        conflict(f"still updating {attempt}") for attempt in range(8)
    ]
    monkeypatch.setattr(gd, "_wait_lambda_updatable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gd.time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(conflict, match="still updating 7"):
        gd._create_or_update_lambda(
            lambda_client,
            function_name,
            role_arn,
            b"zip",
            "dynamic tools",
            region=OTHER_REGION,
        )

    assert lambda_client.update_function_configuration.call_count == 8
    lambda_client.update_function_code.assert_not_called()


def test_lambda_wait_refuses_failed_updates():
    from app.services import gateway_deployer as gd

    lambda_client = MagicMock()
    lambda_client.get_function.return_value = {
        "Configuration": {
            "FunctionArn": "arn:aws:lambda:eu-west-1:123456789012:function:AgentCoreDynamicTools",
            "State": "Active",
            "LastUpdateStatus": "Failed",
            "LastUpdateStatusReason": "The execution role cannot be assumed.",
        }
    }

    with pytest.raises(RuntimeError, match="execution role cannot be assumed"):
        gd._wait_lambda_updatable(
            lambda_client,
            "AgentCoreDynamicTools",
            require_success=True,
        )


def test_lambda_wait_allows_a_new_attempt_after_an_older_update_failed():
    from app.services import gateway_deployer as gd

    configuration = {
        "FunctionArn": "arn:aws:lambda:eu-west-1:123456789012:function:AgentCoreDynamicTools",
        "State": "Active",
        "LastUpdateStatus": "Failed",
        "LastUpdateStatusReason": "A previous package could not be loaded.",
    }
    lambda_client = MagicMock()
    lambda_client.get_function.return_value = {"Configuration": configuration}

    assert gd._wait_lambda_updatable(lambda_client, "AgentCoreDynamicTools") == configuration


def test_lambda_wait_timeout_is_not_reported_as_ready():
    from app.services import gateway_deployer as gd

    with pytest.raises(TimeoutError, match="AgentCoreDynamicTools"):
        gd._wait_lambda_updatable(MagicMock(), "AgentCoreDynamicTools", timeout=0)


def test_shared_lambda_create_path_refuses_an_async_lambda_failure():
    from app.services import gateway_deployer as gd

    lambda_client = MagicMock()
    lambda_client.create_function.return_value = {
        "FunctionArn": "arn:aws:lambda:eu-west-1:123456789012:function:AgentCoreDynamicTools"
    }
    lambda_client.get_function.return_value = {
        "Configuration": {
            "FunctionArn": "arn:aws:lambda:eu-west-1:123456789012:function:AgentCoreDynamicTools",
            "State": "Active",
            "LastUpdateStatus": "Failed",
            "LastUpdateStatusReason": "The deployment package could not be loaded.",
        }
    }

    with pytest.raises(RuntimeError, match="deployment package could not be loaded"):
        gd._create_or_update_lambda(
            lambda_client,
            "AgentCoreDynamicTools",
            "arn:aws:iam::123456789012:role/AgentCoreDynamicToolsLambdaRole",
            b"zip",
            "dynamic tools",
            region=OTHER_REGION,
        )


def test_harness_role_factory_receives_the_regional_name(monkeypatch):
    from app.services import harness_deployer

    monkeypatch.delenv("SHARED_HARNESS_ROLE_ARN", raising=False)
    create_role = MagicMock(
        return_value=(
            "arn:aws:iam::123456789012:role/" + _regional_role_name("AgentCoreHarness-orders", OTHER_REGION),
            True,
        )
    )
    monkeypatch.setattr(harness_deployer, "create_harness_iam_role", create_role)

    harness_deployer.get_shared_or_new_harness_role(
        MagicMock(),
        "orders",
        region=OTHER_REGION,
        return_provenance=True,
    )

    assert create_role.call_args.args[1] == _regional_role_name(
        "AgentCoreHarness-orders",
        OTHER_REGION,
    )


def test_mcp_step_scopes_both_role_and_bundle_to_the_primary_runtime(monkeypatch):
    from app.step_handlers import mcp_server_step

    monkeypatch.setenv("APP_AWS_REGION", HOME_REGION)
    monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", "platform-artifacts")

    store = MagicMock()
    upload = MagicMock()
    role_calls: list[str] = []
    upload_client = MagicMock()
    dependency_client = MagicMock()
    dependency_client.get_object.return_value = {"Body": MagicMock(read=lambda: b"deps")}
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    cognito = MagicMock()
    cognito.create_user_pool.side_effect = RuntimeError("stop after artifact and role resolution")
    clients = {
        "s3": upload_client,
        "sts": sts,
        "iam": MagicMock(),
        "cognito-idp": cognito,
    }

    monkeypatch.setattr(mcp_server_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(
        mcp_server_step.step_clients,
        "client",
        lambda _event, service, **_kwargs: clients[service],
    )
    monkeypatch.setattr(
        mcp_server_step.boto3,
        "client",
        lambda service, **_kwargs: dependency_client if service == "s3" else pytest.fail(service),
    )
    monkeypatch.setattr(
        mcp_server_step,
        "generate_mcp_server_code",
        lambda **_kwargs: "print('mcp')",
    )
    monkeypatch.setattr(mcp_server_step, "upload_code_to_s3", upload)

    def _create_role(_iam, role_name, *_args, **_kwargs):
        role_calls.append(role_name)
        return f"arn:aws:iam::123456789012:role/{role_name}", True

    monkeypatch.setattr(
        mcp_server_step,
        "create_runtime_iam_role",
        _create_role,
    )

    event = {
        "deployment_id": "dep-orders",
        "target_region": OTHER_REGION,
        "target_artifact_bucket": "regional-artifacts",
        "agentcore_runtime_name": "orders_agent",
        "mcp_server_config": {"name": "shared_tools"},
        "gateway_config": {"name": "orders"},
    }
    with pytest.raises(
        RuntimeError,
        match="stop after artifact and role resolution",
    ):
        mcp_server_step.handler(event, None)

    expected_key = _mcp_code_key("orders_agent", "shared_tools")
    assert upload.call_args.args[1] == "regional-artifacts"
    assert upload.call_args.args[2] == expected_key
    artifact_row = next(
        call.args[1] for call in store.record_resource.call_args_list if call.args[1].get("type") == "s3_object"
    )
    assert artifact_row["id"] == f"s3://regional-artifacts/{expected_key}"
    assert artifact_row["region"] == OTHER_REGION
    assert role_calls == [_regional_role_name("AgentCoreMCP-shared_tools", OTHER_REGION)]


def test_supplemental_memory_cleanup_uses_the_recorded_role_name():
    from app.step_handlers.status_update_step import (
        _supplemental_failure_resources,
    )

    exact_role = _regional_role_name("AgentCoreMemory-orders", OTHER_REGION)
    rows = _supplemental_failure_resources(
        {},
        {
            "memory_result": {
                "memory_id": "memory-123",
                "memory_name": "orders",
                "memory_role_name": exact_role,
                "memory_role_created_by_deployment": True,
            }
        },
        OTHER_REGION,
    )

    role_row = next(row for row in rows if row["type"] == "iam_role")
    assert role_row["name"] == exact_role
    assert role_row["created_by_deployment"] is True


def test_evaluation_role_is_recorded_before_policy_attachment_can_fail(monkeypatch):
    from app.step_handlers import evaluation_step

    store = MagicMock()
    iam = MagicMock()
    iam.exceptions.EntityAlreadyExistsException = type(
        "EntityAlreadyExistsException",
        (Exception,),
        {},
    )
    expected_role = _regional_role_name(
        "AgentCoreEval-orders_runtime",
        OTHER_REGION,
    )
    iam.create_role.return_value = {
        "Role": {
            "Arn": f"arn:aws:iam::123456789012:role/{expected_role}",
        }
    }
    iam.put_role_policy.side_effect = RuntimeError("policy attachment denied")
    control = MagicMock()

    def _client(_event, service, **_kwargs):
        return {
            "bedrock-agentcore-control": control,
            "iam": iam,
        }[service]

    monkeypatch.setattr(evaluation_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(evaluation_step.step_clients, "client", _client)

    with pytest.raises(RuntimeError, match="policy attachment denied"):
        evaluation_step.handler(
            {
                "deployment_id": "dep-eval-role-failure",
                "target_region": OTHER_REGION,
                "runtime_id": "orders_runtime",
                "runtime_arn": ("arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/orders_runtime"),
                "evaluation_config": {"enabled": True},
            },
            None,
        )

    role_rows = [
        call.args[1] for call in store.record_resource.call_args_list if call.args[1].get("type") == "iam_role"
    ]
    assert role_rows == [
        {
            "type": "iam_role",
            "name": expected_role,
            "region": OTHER_REGION,
            "created_by_deployment": True,
        }
    ]
    control.create_online_evaluation_config.assert_not_called()


def test_memory_role_is_recorded_before_policy_attachment_can_fail(monkeypatch):
    from app.step_handlers import memory_step

    store = MagicMock()
    iam = MagicMock()
    iam.exceptions.EntityAlreadyExistsException = type(
        "EntityAlreadyExistsException",
        (Exception,),
        {},
    )
    expected_role = _regional_role_name(
        "AgentCoreMemory-orders",
        OTHER_REGION,
    )
    iam.create_role.return_value = {
        "Role": {
            "Arn": f"arn:aws:iam::123456789012:role/{expected_role}",
        }
    }
    iam.put_role_policy.side_effect = RuntimeError("policy attachment denied")
    control = MagicMock()
    control.list_memories.return_value = {"memories": []}

    def _client(_event, service, **_kwargs):
        return {
            "bedrock-agentcore-control": control,
            "iam": iam,
        }[service]

    monkeypatch.setattr(memory_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(memory_step.step_clients, "client", _client)
    monkeypatch.setattr(memory_step.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="policy attachment denied"):
        memory_step.handler(
            {
                "deployment_id": "dep-memory-role-failure",
                "owner_sub": "owner-memory-role-failure",
                "target_region": OTHER_REGION,
                "memory_config": {"name": "orders"},
            },
            None,
        )

    role_rows = [
        call.args[1] for call in store.record_resource.call_args_list if call.args[1].get("type") == "iam_role"
    ]
    assert role_rows == [
        {
            "type": "iam_role",
            "name": expected_role,
            "region": OTHER_REGION,
            "created_by_deployment": True,
        }
    ]
    control.create_memory.assert_not_called()
