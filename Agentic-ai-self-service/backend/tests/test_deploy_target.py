"""Phase 7: multi-region/account deployment targets (opt-in).

Verifies the OFF-BY-DEFAULT gate, region allowlist enforcement, and that
same-account resolution returns the default session unchanged. moto-backed
settings table (reuses the tag-policy table shape).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest

moto = pytest.importorskip("moto")
from app.services import deploy_target as dt  # noqa: E402
from moto import mock_aws  # noqa: E402


def _create_table() -> None:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="TagPolicy",
        KeySchema=[
            {"AttributeName": "org_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TAG_POLICY_TABLE_NAME", "TagPolicy")
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
    # Ensure the env override isn't accidentally on.
    monkeypatch.delenv("DEPLOY_TARGETS_ENABLED", raising=False)


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        _create_table()
        yield


# -- feature gate: OFF by default --------------------------------------------


def test_disabled_by_default(aws):
    assert dt.targets_enabled() is False


def test_enable_then_disabled_flag(aws):
    dt.set_targets_enabled(True)
    assert dt.targets_enabled() is True
    dt.set_targets_enabled(False)
    assert dt.targets_enabled() is False


def test_env_override_enables(aws, monkeypatch):
    monkeypatch.setenv("DEPLOY_TARGETS_ENABLED", "true")
    assert dt.targets_enabled() is True


# -- region resolution -------------------------------------------------------


def test_resolve_region_home_when_none(aws):
    assert dt.resolve_region(None) == "us-east-1"


def test_resolve_region_same_as_home_ok(aws):
    assert dt.resolve_region("us-east-1") == "us-east-1"


def test_resolve_other_region_blocked_when_disabled(aws):
    with pytest.raises(dt.TargetError, match="disabled"):
        dt.resolve_region("us-west-2")


def test_resolve_other_region_blocked_when_not_allowlisted(aws):
    dt.set_targets_enabled(True)
    with pytest.raises(dt.TargetError, match="allowlist"):
        dt.resolve_region("us-west-2")


def test_resolve_other_region_ok_when_allowlisted(aws):
    dt.set_targets_enabled(True)
    dt.add_region("us-west-2")
    assert dt.resolve_region("us-west-2") == "us-west-2"


# -- session resolution ------------------------------------------------------


def test_home_account_returns_default_session(aws):
    # No account_id → default session (unchanged path), even when disabled.
    sess = dt.session_for_target(account_id=None, region=None)
    assert isinstance(sess, boto3.Session)


def test_cross_account_blocked_when_disabled(aws):
    with pytest.raises(dt.TargetError, match="disabled"):
        dt.session_for_target(account_id="123456789012", region="us-east-1")


def test_cross_account_unregistered_rejected(aws):
    dt.set_targets_enabled(True)
    with pytest.raises(dt.TargetError, match="not a registered"):
        dt.session_for_target(account_id="123456789012", region="us-east-1")


def test_account_registry_roundtrip(aws):
    dt.set_targets_enabled(True)
    dt.add_account("123456789012", "arn:aws:iam::123456789012:role/AgentCoreFlowsDeploymentRole", "us-east-1")
    got = dt.get_account("123456789012")
    assert got["role_arn"].endswith("AgentCoreFlowsDeploymentRole")
    assert got["runtime_role_arn"].endswith("AgentCoreFlowsRuntimeRole")
    assert got["harness_role_arn"].endswith("AgentCoreFlowsHarnessRole")
    assert got["artifact_bucket"] == ("agentcore-flows-artifacts-123456789012-us-east-1")
    assert len(dt.list_accounts()) == 1


def test_target_execution_role_rejects_another_accounts_arn():
    with pytest.raises(dt.TargetError, match="not target account"):
        dt.target_execution_role_arn(
            "123456789012",
            role_arn="arn:aws:iam::999999999999:role/AgentCoreFlowsRuntimeRole",
            default_role_name=dt.DEFAULT_TARGET_RUNTIME_ROLE_NAME,
        )


def test_target_deployment_role_rejects_an_arbitrary_role_name():
    with pytest.raises(dt.TargetError, match="must be named exactly"):
        dt.target_deployment_role_arn(
            "123456789012",
            "arn:aws:iam::123456789012:role/CustomerDeploymentRole",
        )


def test_target_deployment_role_rejects_an_iam_path():
    with pytest.raises(dt.TargetError, match="must not use an IAM path"):
        dt.target_deployment_role_arn(
            "123456789012",
            ("arn:aws:iam::123456789012:role/platform/AgentCoreFlowsDeploymentRole"),
        )


def _agentcore_role(arn: str, *, trusts_agentcore: bool = True) -> dict:
    service = "bedrock-agentcore.amazonaws.com" if trusts_agentcore else "lambda.amazonaws.com"
    return {
        "Role": {
            "Arn": arn,
            "AssumeRolePolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": service},
                        "Action": "sts:AssumeRole",
                    }
                ],
            },
        }
    }


def test_validate_execution_roles_proves_both_roles_and_trust():
    account_id = "123456789012"
    runtime_arn = dt.default_runtime_role_arn(account_id)
    mcp_runtime_arn = dt.default_mcp_runtime_role_arn(account_id)
    harness_arn = dt.default_harness_role_arn(account_id)
    iam = MagicMock()
    iam.get_role.side_effect = [
        _agentcore_role(runtime_arn),
        _agentcore_role(mcp_runtime_arn),
        _agentcore_role(harness_arn),
    ]
    session = MagicMock()
    session.client.return_value = iam

    assert dt.validate_execution_roles(session, account_id=account_id) == (
        runtime_arn,
        mcp_runtime_arn,
        harness_arn,
    )
    assert [call.kwargs["RoleName"] for call in iam.get_role.call_args_list] == [
        dt.DEFAULT_TARGET_RUNTIME_ROLE_NAME,
        dt.DEFAULT_TARGET_MCP_RUNTIME_ROLE_NAME,
        dt.DEFAULT_TARGET_HARNESS_ROLE_NAME,
    ]


def test_validate_execution_roles_rejects_a_harness_role_with_wrong_trust():
    account_id = "123456789012"
    iam = MagicMock()
    iam.get_role.side_effect = [
        _agentcore_role(dt.default_runtime_role_arn(account_id)),
        _agentcore_role(dt.default_mcp_runtime_role_arn(account_id)),
        _agentcore_role(
            dt.default_harness_role_arn(account_id),
            trusts_agentcore=False,
        ),
    ]
    session = MagicMock()
    session.client.return_value = iam

    with pytest.raises(dt.TargetError, match="harness execution role.*does not trust"):
        dt.validate_execution_roles(session, account_id=account_id)


def test_validate_artifact_bucket_pins_owner_and_accepts_us_east_1_legacy_location():
    account_id = "123456789012"
    bucket = "customer-agent-runtime-artifacts"
    s3 = MagicMock()
    s3.get_bucket_location.return_value = {"LocationConstraint": None}
    session = MagicMock()
    session.client.return_value = s3

    assert (
        dt.validate_artifact_bucket(
            session,
            account_id=account_id,
            region="us-east-1",
            artifact_bucket=bucket,
        )
        == bucket
    )
    session.client.assert_called_once_with("s3", region_name="us-east-1")
    s3.head_bucket.assert_called_once_with(
        Bucket=bucket,
        ExpectedBucketOwner=account_id,
    )
    s3.get_bucket_location.assert_called_once_with(
        Bucket=bucket,
        ExpectedBucketOwner=account_id,
    )


def test_validate_artifact_bucket_rejects_a_bucket_in_another_region():
    s3 = MagicMock()
    s3.get_bucket_location.return_value = {"LocationConstraint": "eu-west-1"}
    session = MagicMock()
    session.client.return_value = s3

    with pytest.raises(dt.TargetError, match="eu-west-1.*not registered.*us-east-1"):
        dt.validate_artifact_bucket(
            session,
            account_id="123456789012",
            region="us-east-1",
        )


def test_validate_artifact_bucket_rejects_an_unreadable_or_foreign_bucket():
    s3 = MagicMock()
    s3.head_bucket.side_effect = RuntimeError("403")
    session = MagicMock()
    session.client.return_value = s3

    with pytest.raises(dt.TargetError, match="Cannot access target artifact bucket"):
        dt.validate_artifact_bucket(
            session,
            account_id="123456789012",
            region="us-east-1",
        )
    s3.get_bucket_location.assert_not_called()


@pytest.mark.parametrize(
    "bucket",
    [
        "UPPERCASE-is-invalid",
        "two..dots",
        "192.168.0.1",
        "ab",
    ],
)
def test_target_artifact_bucket_name_rejects_invalid_bucket_names(bucket):
    with pytest.raises(dt.TargetError, match="Invalid S3 artifact bucket name"):
        dt.target_artifact_bucket_name(
            "123456789012",
            "us-east-1",
            artifact_bucket=bucket,
        )


def test_session_refuses_to_assume_a_role_from_another_account(aws, monkeypatch):
    dt.set_targets_enabled(True)
    assume = MagicMock()
    monkeypatch.setattr(boto3, "client", assume)

    with pytest.raises(dt.TargetError, match="not target account"):
        dt.session_for_target(
            account_id="123456789012",
            region="us-east-1",
            role_arn="arn:aws:iam::999999999999:role/AgentCoreFlowsDeploymentRole",
        )

    assume.assert_not_called()


def test_admin_registration_does_not_persist_a_target_that_fails_validation(monkeypatch):
    from app.routers import admin

    add_account = MagicMock()
    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "session_for_target", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(
        dt,
        "validate_execution_roles",
        MagicMock(side_effect=dt.TargetError("missing harness role")),
    )
    monkeypatch.setattr(dt, "add_account", add_account)

    body = admin.AccountTargetRequest(
        account_id="123456789012",
        role_arn="arn:aws:iam::123456789012:role/AgentCoreFlowsDeploymentRole",
        region="us-east-1",
    )
    with pytest.raises(admin.HTTPException, match="missing harness role"):
        asyncio.run(admin.add_account_target(body, _caller_sub="admin"))

    add_account.assert_not_called()


def test_admin_registration_does_not_persist_a_target_with_an_invalid_bucket(monkeypatch):
    from app.routers import admin

    add_account = MagicMock()
    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "session_for_target", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(
        dt,
        "validate_execution_roles",
        lambda *_args, **_kwargs: (
            dt.default_runtime_role_arn("123456789012"),
            dt.default_mcp_runtime_role_arn("123456789012"),
            dt.default_harness_role_arn("123456789012"),
        ),
    )
    monkeypatch.setattr(
        dt,
        "validate_artifact_bucket",
        MagicMock(side_effect=dt.TargetError("bucket is in another region")),
    )
    monkeypatch.setattr(dt, "add_account", add_account)

    body = admin.AccountTargetRequest(
        account_id="123456789012",
        role_arn="arn:aws:iam::123456789012:role/AgentCoreFlowsDeploymentRole",
        region="us-east-1",
    )
    with pytest.raises(admin.HTTPException, match="bucket is in another region"):
        asyncio.run(admin.add_account_target(body, _caller_sub="admin"))

    add_account.assert_not_called()


def test_admin_registration_persists_only_validated_prerequisites(monkeypatch):
    from app.routers import admin

    account_id = "123456789012"
    runtime_arn = dt.default_runtime_role_arn(account_id)
    mcp_runtime_arn = dt.default_mcp_runtime_role_arn(account_id)
    harness_arn = dt.default_harness_role_arn(account_id)
    artifact_bucket = "customer-agent-runtime-artifacts"
    add_account = MagicMock()
    session = MagicMock()
    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "session_for_target", lambda **_kwargs: session)
    monkeypatch.setattr(
        dt,
        "validate_execution_roles",
        lambda *_args, **_kwargs: (runtime_arn, mcp_runtime_arn, harness_arn),
    )
    validate_artifact_bucket = MagicMock(return_value=artifact_bucket)
    monkeypatch.setattr(
        dt,
        "validate_artifact_bucket",
        validate_artifact_bucket,
    )
    monkeypatch.setattr(dt, "add_account", add_account)

    body = admin.AccountTargetRequest(
        account_id=account_id,
        role_arn=f"arn:aws:iam::{account_id}:role/AgentCoreFlowsDeploymentRole",
        artifact_bucket=artifact_bucket,
        region="us-east-1",
    )
    result = asyncio.run(admin.add_account_target(body, _caller_sub="admin"))

    add_account.assert_called_once_with(
        account_id,
        body.role_arn,
        body.region,
        runtime_role_arn=runtime_arn,
        mcp_runtime_role_arn=mcp_runtime_arn,
        harness_role_arn=harness_arn,
        artifact_bucket=artifact_bucket,
    )
    validate_artifact_bucket.assert_called_once_with(
        session,
        account_id=account_id,
        region="us-east-1",
        artifact_bucket=artifact_bucket,
    )
    assert result["runtime_role_arn"] == runtime_arn
    assert result["mcp_runtime_role_arn"] == mcp_runtime_arn
    assert result["harness_role_arn"] == harness_arn
    assert result["artifact_bucket"] == artifact_bucket


def test_live_resolution_uses_the_accounts_registered_region_and_revalidates(
    aws,
    monkeypatch,
):
    account_id = "123456789012"
    region = "eu-west-1"
    dt.set_targets_enabled(True)
    dt.add_region(region)
    dt.add_account(
        account_id,
        f"arn:aws:iam::{account_id}:role/AgentCoreFlowsDeploymentRole",
        region,
    )
    session = MagicMock()
    session_for_target = MagicMock(return_value=session)
    validate = MagicMock(
        return_value=(
            dt.default_runtime_role_arn(account_id),
            dt.default_mcp_runtime_role_arn(account_id),
            dt.default_harness_role_arn(account_id),
        )
    )
    monkeypatch.setattr(dt, "session_for_target", session_for_target)
    monkeypatch.setattr(dt, "validate_execution_roles", validate)
    artifact_bucket = dt.default_artifact_bucket_name(account_id, region)
    validate_bucket = MagicMock(return_value=artifact_bucket)
    monkeypatch.setattr(dt, "validate_artifact_bucket", validate_bucket)

    resolved = dt.resolve_registered_account_target(account_id)

    assert resolved["region"] == region
    assert resolved["artifact_bucket"] == artifact_bucket
    session_for_target.assert_called_once_with(
        account_id=account_id,
        region=region,
        role_arn=f"arn:aws:iam::{account_id}:role/AgentCoreFlowsDeploymentRole",
    )
    validate.assert_called_once_with(
        session,
        account_id=account_id,
        runtime_role_arn=dt.default_runtime_role_arn(account_id),
        mcp_runtime_role_arn=dt.default_mcp_runtime_role_arn(account_id),
        harness_role_arn=dt.default_harness_role_arn(account_id),
    )
    validate_bucket.assert_called_once_with(
        session,
        account_id=account_id,
        region=region,
        artifact_bucket=artifact_bucket,
    )


def test_live_resolution_rejects_a_region_not_registered_for_the_account(
    aws,
    monkeypatch,
):
    account_id = "123456789012"
    dt.set_targets_enabled(True)
    dt.add_region("eu-west-1")
    dt.add_region("us-west-2")
    dt.add_account(
        account_id,
        f"arn:aws:iam::{account_id}:role/AgentCoreFlowsDeploymentRole",
        "eu-west-1",
    )
    session_for_target = MagicMock()
    monkeypatch.setattr(dt, "session_for_target", session_for_target)

    with pytest.raises(dt.TargetError, match="registered for region 'eu-west-1'"):
        dt.resolve_registered_account_target(account_id, "us-west-2")

    session_for_target.assert_not_called()


def test_cross_account_admission_rejects_per_agent_role_isolation(monkeypatch):
    from app import deployment_handler

    monkeypatch.delenv("TAG_POLICY_TABLE_NAME", raising=False)
    request = SimpleNamespace(
        identity_config=SimpleNamespace(mode="per_agent"),
        connected_tools=[],
    )

    with pytest.raises(deployment_handler.HTTPException, match="per-agent"):
        deployment_handler._reject_unsupported_cross_account_features(request)


def test_cross_account_admission_rejects_hitl(monkeypatch):
    from app import deployment_handler

    monkeypatch.delenv("TAG_POLICY_TABLE_NAME", raising=False)
    request = SimpleNamespace(identity_config=None, connected_tools=["hitl"])

    with pytest.raises(deployment_handler.HTTPException, match="HITL"):
        deployment_handler._reject_unsupported_cross_account_features(request)


def test_cross_account_admission_rejects_enabled_org_approval_policies(monkeypatch):
    from app import deployment_handler
    from app.services import approval_policy_store

    class _Store:
        def __init__(self, *_args):
            pass

        def list(self, _org):
            return [
                SimpleNamespace(
                    enabled=True,
                    tool_match=["delete_*"],
                )
            ]

    monkeypatch.setenv("TAG_POLICY_TABLE_NAME", "TagPolicy")
    monkeypatch.setattr(approval_policy_store, "ApprovalPolicyStore", _Store)
    request = SimpleNamespace(identity_config=None, connected_tools=[])

    with pytest.raises(deployment_handler.HTTPException, match="approval policies"):
        deployment_handler._reject_unsupported_cross_account_features(request)


def test_cross_account_admission_fails_closed_when_policy_state_is_unknown(
    monkeypatch,
):
    from app import deployment_handler
    from app.services import approval_policy_store

    class _Store:
        def __init__(self, *_args):
            pass

        def list(self, _org):
            raise RuntimeError("DynamoDB unavailable")

    monkeypatch.setenv("TAG_POLICY_TABLE_NAME", "TagPolicy")
    monkeypatch.setattr(approval_policy_store, "ApprovalPolicyStore", _Store)
    request = SimpleNamespace(identity_config=None, connected_tools=[])

    with pytest.raises(deployment_handler.HTTPException) as exc:
        deployment_handler._reject_unsupported_cross_account_features(request)
    assert exc.value.status_code == 503


def test_cross_account_admission_allows_supported_shared_role_path(monkeypatch):
    from app import deployment_handler

    monkeypatch.delenv("TAG_POLICY_TABLE_NAME", raising=False)
    request = SimpleNamespace(
        identity_config=SimpleNamespace(mode="shared"),
        connected_tools=["gateway", "memory"],
    )

    deployment_handler._reject_unsupported_cross_account_features(request)
