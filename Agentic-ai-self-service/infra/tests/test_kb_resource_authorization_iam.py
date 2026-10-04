"""Synthesized IAM must make the KB customer-resource checks executable."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

REGION = "us-east-1"
ACCOUNT = "123456789012"
ACCESS_CONDITION = "aws:ResourceTag/AgentCoreFlowsAccess"


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


def _actions(statement: dict) -> set[str]:
    value = statement.get("Action") or []
    return {value} if isinstance(value, str) else set(value)


def _resources(statement: dict) -> list[object]:
    value = statement.get("Resource") or []
    return [value] if not isinstance(value, list) else value


def _role_statements(template_json: dict, role_prefix: str) -> list[dict]:
    resources = template_json["Resources"]
    role_ids = [
        logical_id
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Role" and logical_id.startswith(role_prefix)
    ]
    assert len(role_ids) == 1, role_ids
    role_id = role_ids[0]
    statements: list[dict] = []
    for policy in resources[role_id]["Properties"].get("Policies", []) or []:
        statements.extend(policy["PolicyDocument"].get("Statement", []) or [])
    for resource in resources.values():
        if resource["Type"] not in (
            "AWS::IAM::Policy",
            "AWS::IAM::ManagedPolicy",
        ):
            continue
        roles = resource["Properties"].get("Roles", []) or []
        if any(isinstance(role, dict) and role.get("Ref") == role_id for role in roles):
            statements.extend(
                resource["Properties"]["PolicyDocument"].get(
                    "Statement",
                    [],
                )
                or []
            )
    return statements


def test_arbitrary_kb_source_secret_read_requires_the_owner_opt_in(
    template_json,
):
    statements = _role_statements(template_json, "DeploymentLambdaRole")
    account_wide_reads = [
        statement
        for statement in statements
        if "secretsmanager:GetSecretValue" in _actions(statement)
        and any(isinstance(resource, str) and resource.endswith(":secret:*") for resource in _resources(statement))
    ]
    assert account_wide_reads
    for statement in account_wide_reads:
        assert statement.get("Condition", {}).get("StringEquals", {}).get(ACCESS_CONDITION) == "allow"
        assert _actions(statement) == {
            "secretsmanager:DescribeSecret",
            "secretsmanager:GetSecretValue",
        }


def test_kb_step_can_read_every_authorization_tag_it_enforces(template_json):
    statements = _role_statements(template_json, "StepKnowledgeBaseRole")
    actions = set().union(*(_actions(statement) for statement in statements))
    assert {
        "bedrock:ListTagsForResource",
        "s3:GetBucketTagging",
        "s3vectors:ListTagsForResource",
        "aoss:ListTagsForResource",
        "rds:ListTagsForResource",
        "lambda:ListTags",
        "kms:ListResourceTags",
        "secretsmanager:DescribeSecret",
    } <= actions


def test_kb_step_can_reharden_an_existing_service_role_trust(template_json):
    statements = _role_statements(template_json, "StepKnowledgeBaseRole")
    actions = set().union(*(_actions(statement) for statement in statements))
    assert "iam:UpdateAssumeRolePolicy" in actions


def test_kb_step_describes_but_never_reads_staged_secret_values(template_json):
    statements = _role_statements(template_json, "StepKnowledgeBaseRole")
    connector_actions = set().union(
        *(
            _actions(statement)
            for statement in statements
            if any("agentcore-connector/" in str(resource) for resource in _resources(statement))
        )
    )
    assert "secretsmanager:DescribeSecret" in connector_actions
    assert "secretsmanager:GetSecretValue" not in connector_actions


def test_account_wide_lambda_tag_read_does_not_include_a_write_verb(
    template_json,
):
    statements = _role_statements(template_json, "StepKnowledgeBaseRole")
    tag_reads = [
        statement
        for statement in statements
        if "lambda:ListTags" in _actions(statement)
        and any("function:*" in str(resource) for resource in _resources(statement))
    ]
    assert tag_reads
    for statement in tag_reads:
        assert _actions(statement) == {"lambda:ListTags"}
