"""Cross-account MCP runtimes must use a distinct, model-free execution role.

The target-account Runtime role is intentionally model-capable. Reusing it for a
standalone or hosted FastMCP server makes a protocol-only tool server able to invoke
arbitrary models, contradicting the model-free runtime contract already enforced for
home-account deployments. These tests cover registration, prepared-payload validation,
role selection, and the customer-facing onboarding document as one end-to-end contract.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from app.services.deployment_payload_validation import (
    PayloadPhase,
    ValidationContext,
    validate_deployment_payload,
)

ACCOUNT = "123456789012"
REGION = "eu-west-1"
DEPLOYMENT_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/AgentCoreFlowsDeploymentRole"
RUNTIME_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/AgentCoreFlowsRuntimeRole"
MCP_RUNTIME_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/AgentCoreFlowsMCPRuntimeRole"
HARNESS_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/AgentCoreFlowsHarnessRole"
ARTIFACT_BUCKET = "customer-agent-runtime-artifacts"

MODEL_ACTIONS = {
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
}


def _agentcore_role(arn: str) -> dict:
    return {
        "Role": {
            "Arn": arn,
            "AssumeRolePolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {
                            "Service": "bedrock-agentcore.amazonaws.com",
                        },
                        "Action": "sts:AssumeRole",
                    }
                ],
            },
        }
    }


def _prepared_payload(*, mcp_role_arn: str = MCP_RUNTIME_ROLE_ARN) -> dict:
    return {
        "deployment_id": "b3f1c2d4-1111-2222-3333-444455556666",
        "workflow_id": None,
        "node_id": "standalone-mcp",
        "config": {
            "name": "standalone-mcp",
            "model": {"modelId": "model-free-placeholder"},
            "protocol": "MCP",
        },
        "connected_tools": [],
        "template_id": "mcp-server-runtime",
        "resource_tags": {},
        "target_account_id": ACCOUNT,
        "target_region": REGION,
        "target_role_arn": DEPLOYMENT_ROLE_ARN,
        "target_runtime_role_arn": RUNTIME_ROLE_ARN,
        "target_mcp_runtime_role_arn": mcp_role_arn,
        "target_harness_role_arn": HARNESS_ROLE_ARN,
        "target_artifact_bucket": ARTIFACT_BUCKET,
        "version_id": "v1",
        "friendly_runtime_name": "standalone-mcp",
        "agentcore_runtime_name": "standalone_mcp-AbCdEf1234",
        "deployment_slot": "production",
        "parent_version_id": None,
        "owner_sub": "owner",
        "deployment_mode": "runtime",
    }


def _policy_actions(policy: dict) -> set[str]:
    actions: set[str] = set()
    for statement in policy.get("Statement", []):
        value = statement.get("Action") or []
        actions.update([value] if isinstance(value, str) else value)
    return actions


def _document() -> dict:
    repository = Path(__file__).resolve().parents[2]
    return json.loads((repository / "docs" / "cross-account-deploy-role.json").read_text())


def test_target_registry_has_a_distinct_default_mcp_runtime_role():
    from app.services import deploy_target as dt

    assert dt.DEFAULT_TARGET_MCP_RUNTIME_ROLE_NAME == "AgentCoreFlowsMCPRuntimeRole"
    assert dt.default_mcp_runtime_role_arn(ACCOUNT) == MCP_RUNTIME_ROLE_ARN
    assert MCP_RUNTIME_ROLE_ARN != dt.default_runtime_role_arn(ACCOUNT)


def test_registration_validation_proves_all_three_execution_roles():
    from app.services import deploy_target as dt

    iam = MagicMock()
    iam.get_role.side_effect = [
        _agentcore_role(RUNTIME_ROLE_ARN),
        _agentcore_role(MCP_RUNTIME_ROLE_ARN),
        _agentcore_role(HARNESS_ROLE_ARN),
    ]
    session = MagicMock()
    session.client.return_value = iam

    assert dt.validate_execution_roles(
        session,
        account_id=ACCOUNT,
        runtime_role_arn=RUNTIME_ROLE_ARN,
        mcp_runtime_role_arn=MCP_RUNTIME_ROLE_ARN,
        harness_role_arn=HARNESS_ROLE_ARN,
    ) == (
        RUNTIME_ROLE_ARN,
        MCP_RUNTIME_ROLE_ARN,
        HARNESS_ROLE_ARN,
    )
    assert [call.kwargs["RoleName"] for call in iam.get_role.call_args_list] == [
        "AgentCoreFlowsRuntimeRole",
        "AgentCoreFlowsMCPRuntimeRole",
        "AgentCoreFlowsHarnessRole",
    ]


def test_target_registry_persists_the_validated_mcp_execution_role(monkeypatch):
    from app.services import deploy_target as dt

    table = MagicMock()
    monkeypatch.setattr(dt, "_settings_table", lambda: table)

    dt.add_account(
        ACCOUNT,
        DEPLOYMENT_ROLE_ARN,
        REGION,
        runtime_role_arn=RUNTIME_ROLE_ARN,
        mcp_runtime_role_arn=MCP_RUNTIME_ROLE_ARN,
        harness_role_arn=HARNESS_ROLE_ARN,
        artifact_bucket=ARTIFACT_BUCKET,
    )

    item = table.put_item.call_args.kwargs["Item"]
    assert item["runtime_role_arn"] == RUNTIME_ROLE_ARN
    assert item["mcp_runtime_role_arn"] == MCP_RUNTIME_ROLE_ARN
    assert item["harness_role_arn"] == HARNESS_ROLE_ARN


def test_live_target_resolution_revalidates_and_returns_the_mcp_role(monkeypatch):
    from app.services import deploy_target as dt

    target = {
        "account_id": ACCOUNT,
        "role_arn": DEPLOYMENT_ROLE_ARN,
        "runtime_role_arn": RUNTIME_ROLE_ARN,
        "mcp_runtime_role_arn": MCP_RUNTIME_ROLE_ARN,
        "harness_role_arn": HARNESS_ROLE_ARN,
        "artifact_bucket": ARTIFACT_BUCKET,
        "region": REGION,
    }
    session = MagicMock()
    validate_roles = MagicMock(
        return_value=(
            RUNTIME_ROLE_ARN,
            MCP_RUNTIME_ROLE_ARN,
            HARNESS_ROLE_ARN,
        )
    )

    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "get_account", lambda _account_id: target)
    monkeypatch.setattr(dt, "resolve_region", lambda region: region)
    monkeypatch.setattr(dt, "session_for_target", lambda **_kwargs: session)
    monkeypatch.setattr(dt, "validate_execution_roles", validate_roles)
    monkeypatch.setattr(
        dt,
        "validate_artifact_bucket",
        lambda *_args, **_kwargs: ARTIFACT_BUCKET,
    )

    resolved = dt.resolve_registered_account_target(ACCOUNT)

    validate_roles.assert_called_once_with(
        session,
        account_id=ACCOUNT,
        runtime_role_arn=RUNTIME_ROLE_ARN,
        mcp_runtime_role_arn=MCP_RUNTIME_ROLE_ARN,
        harness_role_arn=HARNESS_ROLE_ARN,
    )
    assert resolved["mcp_runtime_role_arn"] == MCP_RUNTIME_ROLE_ARN


def test_admin_registration_forwards_the_mcp_role_without_reconstructing_it(
    monkeypatch,
):
    from app.routers import admin
    from app.services import deploy_target as dt

    session = MagicMock()
    add_account = MagicMock()
    validate_roles = MagicMock(
        return_value=(
            RUNTIME_ROLE_ARN,
            MCP_RUNTIME_ROLE_ARN,
            HARNESS_ROLE_ARN,
        )
    )
    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "session_for_target", lambda **_kwargs: session)
    monkeypatch.setattr(dt, "validate_execution_roles", validate_roles)
    monkeypatch.setattr(
        dt,
        "validate_artifact_bucket",
        lambda *_args, **_kwargs: ARTIFACT_BUCKET,
    )
    monkeypatch.setattr(dt, "add_account", add_account)

    body = admin.AccountTargetRequest(
        account_id=ACCOUNT,
        role_arn=DEPLOYMENT_ROLE_ARN,
        runtime_role_arn=RUNTIME_ROLE_ARN,
        mcp_runtime_role_arn=MCP_RUNTIME_ROLE_ARN,
        harness_role_arn=HARNESS_ROLE_ARN,
        artifact_bucket=ARTIFACT_BUCKET,
        region=REGION,
    )
    result = asyncio.run(admin.add_account_target(body, _caller_sub="admin"))

    validate_roles.assert_called_once_with(
        session,
        account_id=ACCOUNT,
        runtime_role_arn=RUNTIME_ROLE_ARN,
        mcp_runtime_role_arn=MCP_RUNTIME_ROLE_ARN,
        harness_role_arn=HARNESS_ROLE_ARN,
    )
    add_account.assert_called_once_with(
        ACCOUNT,
        DEPLOYMENT_ROLE_ARN,
        REGION,
        runtime_role_arn=RUNTIME_ROLE_ARN,
        mcp_runtime_role_arn=MCP_RUNTIME_ROLE_ARN,
        harness_role_arn=HARNESS_ROLE_ARN,
        artifact_bucket=ARTIFACT_BUCKET,
    )
    assert result["mcp_runtime_role_arn"] == MCP_RUNTIME_ROLE_ARN


def test_admin_catalog_backfills_the_default_mcp_role_for_legacy_rows(
    monkeypatch,
):
    from app.routers import admin
    from app.services import deploy_target as dt

    monkeypatch.setattr(dt, "targets_enabled", lambda: True)
    monkeypatch.setattr(dt, "list_regions", lambda: [REGION])
    monkeypatch.setattr(dt, "list_region_targets", lambda: [])
    monkeypatch.setattr(
        dt,
        "list_accounts",
        lambda: [
            {
                "account_id": ACCOUNT,
                "role_arn": DEPLOYMENT_ROLE_ARN,
                "runtime_role_arn": RUNTIME_ROLE_ARN,
                "harness_role_arn": HARNESS_ROLE_ARN,
                "artifact_bucket": ARTIFACT_BUCKET,
                "region": REGION,
            }
        ],
    )

    result = asyncio.run(admin.get_deploy_targets(_caller_sub="admin"))

    assert result["accounts"][0]["mcp_runtime_role_arn"] == MCP_RUNTIME_ROLE_ARN


@pytest.mark.parametrize(
    ("artifact_kind", "expected_role_arn"),
    [
        pytest.param("mcp", MCP_RUNTIME_ROLE_ARN, id="model-free-mcp"),
        pytest.param("strands", RUNTIME_ROLE_ARN, id="model-capable-agent"),
    ],
)
def test_cross_account_role_selection_matches_the_runtime_artifact(
    monkeypatch,
    artifact_kind,
    expected_role_arn,
):
    from app.step_handlers import iam_step

    monkeypatch.setattr(iam_step, "_get_deployment_store", MagicMock)
    monkeypatch.setattr(
        iam_step,
        "get_platform_observability_defaults",
        lambda: {},
    )

    result = iam_step.handler(
        {
            "deployment_id": f"dep-{artifact_kind}",
            "runtime_artifact_kind": artifact_kind,
            "target_account_id": ACCOUNT,
            "target_region": REGION,
            "target_runtime_role_arn": RUNTIME_ROLE_ARN,
            "target_mcp_runtime_role_arn": MCP_RUNTIME_ROLE_ARN,
            "config": {
                "name": f"{artifact_kind}-runtime",
                "model": {"modelId": "us.anthropic.claude-sonnet-5"},
                "modelProvider": "bedrock",
            },
        },
        None,
    )

    assert result["role_arn"] == expected_role_arn
    assert result["role_created_by_deployment"] is False


def test_prepared_payload_accepts_and_validates_the_mcp_role_arn():
    context = ValidationContext(
        home_account_id=ACCOUNT,
        home_region=REGION,
    )

    accepted = validate_deployment_payload(
        _prepared_payload(),
        phase=PayloadPhase.PREPARED,
        context=context,
    )
    assert accepted.is_valid, accepted.as_error_dicts()

    refused = validate_deployment_payload(
        _prepared_payload(mcp_role_arn=("arn:aws:iam::999999999999:role/AgentCoreFlowsMCPRuntimeRole")),
        phase=PayloadPhase.PREPARED,
        context=context,
    )
    assert "arn_wrong_account" in refused.codes()
    assert any(error.field == "$.target_mcp_runtime_role_arn" for error in refused.errors)


def test_onboarding_document_defines_a_model_free_mcp_execution_role():
    document = _document()
    serialized = json.dumps(document)
    readme = str(document.get("_README") or "")

    assert "AgentCoreFlowsMCPRuntimeRole" in serialized
    assert "<TARGET_MCP_RUNTIME_ROLE_ARN>" in serialized
    assert "pre-provision FIVE things" in readme
    assert "pre-provision FOUR things" not in readme

    mcp_permission_documents = [
        value
        for key, value in document.items()
        if "mcp" in key.lower()
        and "permission" in key.lower()
        and isinstance(value, dict)
        and isinstance(value.get("Statement"), list)
    ]
    assert len(mcp_permission_documents) == 1, (
        "the onboarding bundle must contain exactly one clearly named MCP runtime permissions document"
    )
    actions = _policy_actions(mcp_permission_documents[0])
    assert not (actions & MODEL_ACTIONS)
    assert not any(action.startswith(("bedrock:", "bedrock-agentcore:")) for action in actions), (
        "a protocol-only MCP server needs artifact and logging permissions, not "
        "model, Gateway, Memory, Browser, or other AgentCore data-plane authority"
    )
    assert {
        "s3:GetObject",
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    } <= actions

    deployment_statements = [
        statement
        for key, policy in document.items()
        if key.startswith("permissions-policy") and key.endswith(".json") and isinstance(policy, dict)
        for statement in policy.get("Statement", [])
    ]
    for action in ("iam:GetRole", "iam:PassRole"):
        resources = {
            resource
            for statement in deployment_statements
            if action
            in ([statement.get("Action")] if isinstance(statement.get("Action"), str) else statement.get("Action", []))
            for resource in (
                [statement.get("Resource")]
                if isinstance(statement.get("Resource"), str)
                else statement.get("Resource", [])
            )
        }
        assert ("arn:aws:iam::<TARGET_ACCOUNT_ID>:role/AgentCoreFlowsMCPRuntimeRole") in resources
        assert "<TARGET_MCP_RUNTIME_ROLE_ARN>" in resources
