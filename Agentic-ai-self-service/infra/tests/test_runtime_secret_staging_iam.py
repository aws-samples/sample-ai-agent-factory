"""Synthesized IAM and routing contract for provider/OTEL credential staging."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

REGION = "us-east-1"
ACCOUNT = "123456789012"


@pytest.fixture(scope="module")
def template_json():
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="agentcore-workflow",
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def _statements_for_role(template_json: dict, prefix: str) -> list[dict]:
    resources = template_json["Resources"]
    role_ids = [
        logical_id
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Role" and logical_id.startswith(prefix)
    ]
    assert len(role_ids) == 1
    role_id = role_ids[0]
    statements: list[dict] = []
    for policy in resources[role_id]["Properties"].get("Policies", []) or []:
        statements.extend(policy["PolicyDocument"].get("Statement", []) or [])
    for resource in resources.values():
        if resource["Type"] not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            continue
        if any(
            isinstance(role, dict) and role.get("Ref") == role_id
            for role in resource["Properties"].get("Roles", []) or []
        ):
            statements.extend(resource["Properties"]["PolicyDocument"].get("Statement", []) or [])
    return statements


def _actions(statement: dict) -> set[str]:
    value = statement.get("Action") or []
    return {value} if isinstance(value, str) else set(value)


def _resources(statement: dict) -> list[str]:
    value = statement.get("Resource") or []
    values = [value] if isinstance(value, str) else value
    return [item for item in values if isinstance(item, str)]


def test_provider_storage_route_is_explicitly_exposed(template_json):
    route_keys = {
        resource["Properties"].get("RouteKey")
        for resource in template_json["Resources"].values()
        if resource["Type"] == "AWS::ApiGatewayV2::Route"
    }
    assert "POST /api/provider-credentials" in route_keys


def test_workflow_role_can_create_but_not_read_provider_keys(template_json):
    statements = _statements_for_role(template_json, "WorkflowLambdaRole")
    provider_statements = [
        statement
        for statement in statements
        if any("secret:agentcore-provider/" in arn for arn in _resources(statement))
    ]
    assert provider_statements
    granted = set().union(*(_actions(statement) for statement in provider_statements))
    assert {"secretsmanager:CreateSecret", "secretsmanager:TagResource"} <= granted
    assert (
        not {
            "secretsmanager:GetSecretValue",
            "secretsmanager:PutSecretValue",
            "secretsmanager:DeleteSecret",
        }
        & granted
    )


def test_deployment_role_reads_sources_but_cannot_mutate_or_delete_them(template_json):
    statements = _statements_for_role(template_json, "DeploymentLambdaRole")
    source_statements = [
        statement
        for statement in statements
        if any("secret:agentcore-provider/" in arn or "secret:agentcore-otel/" in arn for arn in _resources(statement))
    ]
    assert source_statements
    granted = set().union(*(_actions(statement) for statement in source_statements))
    assert granted == {
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
    }


def test_deployment_role_can_manage_target_region_connector_copies(template_json):
    statements = _statements_for_role(template_json, "DeploymentLambdaRole")
    connector_statements = [
        statement
        for statement in statements
        if any("secret:agentcore-connector/" in arn for arn in _resources(statement))
    ]
    granted = set().union(*(_actions(statement) for statement in connector_statements))
    assert {
        "secretsmanager:CreateSecret",
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DeleteSecret",
    } <= granted
    assert any(
        arn == f"arn:aws:secretsmanager:*:{ACCOUNT}:secret:agentcore-connector/*"
        for statement in connector_statements
        for arn in _resources(statement)
    )


def test_runtime_configure_step_has_no_source_secret_read(template_json):
    statements = _statements_for_role(template_json, "StepRuntimeConfigureRole")
    secret_resources = [
        arn
        for statement in statements
        if "secretsmanager:GetSecretValue" in _actions(statement)
        for arn in _resources(statement)
    ]
    assert not any("secret:agentcore-provider/" in arn for arn in secret_resources)
    assert not any("secret:agentcore-otel/" in arn for arn in secret_resources)
