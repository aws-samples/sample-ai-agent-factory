"""Synthesized append-only audit infrastructure contract."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import role_logical_id, statements_for_role

ACCOUNT = "123456789012"
REGION = "us-east-1"


def _template(environment: str) -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        f"AuditContract{environment.title()}",
        environment_name=environment,
        project_name="acf",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    return Template.from_stack(stack).to_json()


@pytest.fixture(scope="module")
def production_template() -> dict:
    return _template("staging")


@pytest.fixture(scope="module")
def development_template() -> dict:
    return _template("dev")


def _audit_table_id(template: dict) -> str:
    matches = [
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::DynamoDB::Table"
        and str(resource["Properties"].get("TableName", "")).endswith("-audit")
    ]
    assert len(matches) == 1
    return matches[0]


def _actions(statement: dict) -> set[str]:
    value = statement.get("Action") or []
    return {value} if isinstance(value, str) else set(value)


def _references(statement: dict, logical_id: str) -> bool:
    return logical_id in json.dumps(statement.get("Resource"))


def _lambda_environment(template: dict, function_prefix: str) -> dict:
    matches = [
        resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::Lambda::Function" and logical_id.startswith(function_prefix)
    ]
    assert len(matches) == 1
    return matches[0]["Properties"]["Environment"]["Variables"]


def test_audit_table_keeps_ttl_processor_for_legacy_rows(production_template):
    table = production_template["Resources"][_audit_table_id(production_template)]
    assert table["Properties"]["TimeToLiveSpecification"] == {
        "AttributeName": "ttl",
        "Enabled": True,
    }
    assert table["Properties"]["DeletionProtectionEnabled"] is True
    assert table["DeletionPolicy"] == "RetainExceptOnCreate"


def test_development_audit_table_retains_bounded_ttl(development_template):
    table = development_template["Resources"][_audit_table_id(development_template)]
    assert table["Properties"]["TimeToLiveSpecification"] == {
        "AttributeName": "ttl",
        "Enabled": True,
    }


@pytest.mark.parametrize(
    ("role_prefix", "expected_allows"),
    [
        ("WorkflowLambdaRole", {"dynamodb:PutItem", "dynamodb:DescribeTable"}),
        (
            "DeploymentLambdaRole",
            {"dynamodb:PutItem", "dynamodb:Query", "dynamodb:DescribeTable"},
        ),
    ],
)
def test_api_roles_can_append_but_never_mutate_audit(production_template, role_prefix, expected_allows):
    audit_table = _audit_table_id(production_template)
    role = role_logical_id(production_template, role_prefix)
    relevant = [
        statement
        for _source, statement in statements_for_role(production_template, role)
        if _references(statement, audit_table)
    ]
    assert relevant
    allowed = set().union(*(_actions(statement) for statement in relevant if statement.get("Effect") == "Allow"))
    denied = set().union(*(_actions(statement) for statement in relevant if statement.get("Effect") == "Deny"))
    assert allowed == expected_allows
    assert {
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
    } <= denied


def test_both_api_lambdas_receive_the_same_audit_table(production_template):
    audit_table = _audit_table_id(production_template)
    expected = {"Ref": audit_table}
    assert _lambda_environment(production_template, "WorkflowLambda")["AUDIT_TABLE_NAME"] == expected
    assert _lambda_environment(production_template, "DeploymentLambda")["AUDIT_TABLE_NAME"] == expected
