"""Infrastructure contract for atomic pre-invocation budget reservations."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.lambdas import _budget_reservation_value
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import role_logical_id, statements_for_role

ACCOUNT = "123456789012"
REGION = "us-east-1"


@pytest.fixture(scope="module")
def template() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "BudgetReservationContract",
        environment_name="staging",
        project_name="acf",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    return Template.from_stack(stack).to_json()


def _budget_table_id(template: dict) -> str:
    matches = [
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::DynamoDB::Table"
        and str(resource["Properties"].get("TableName", "")).endswith("-budget")
    ]
    assert len(matches) == 1
    return matches[0]


def _actions(statement: dict) -> set[str]:
    value = statement.get("Action") or []
    return {value} if isinstance(value, str) else set(value)


def test_budget_table_expires_only_rows_that_carry_ttl(template):
    table = template["Resources"][_budget_table_id(template)]
    assert table["Properties"]["TimeToLiveSpecification"] == {
        "AttributeName": "ttl",
        "Enabled": True,
    }


def test_budget_table_indexes_live_reservations_by_expiry(template):
    table = template["Resources"][_budget_table_id(template)]
    indexes = table["Properties"]["GlobalSecondaryIndexes"]
    assert indexes == [
        {
            "IndexName": "reservation-status-expires-index",
            "KeySchema": [
                {"AttributeName": "reservation_status", "KeyType": "HASH"},
                {"AttributeName": "expires_epoch", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }
    ]


def test_deployment_role_can_transact_only_against_budget_table(template):
    budget_table = _budget_table_id(template)
    role = role_logical_id(template, "DeploymentLambdaRole")
    matching = [
        statement
        for _source, statement in statements_for_role(template, role)
        if "dynamodb:TransactWriteItems" in _actions(statement)
    ]
    assert len(matching) == 1
    assert budget_table in json.dumps(matching[0]["Resource"])


def test_deployment_lambda_receives_fixed_reservation_unit(template):
    functions = [
        resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::Lambda::Function" and logical_id.startswith("DeploymentLambda")
    ]
    assert len(functions) == 1
    assert functions[0]["Properties"]["Environment"]["Variables"]["BUDGET_RESERVATION_USD"] == "1"


@pytest.mark.parametrize("role_prefix", ["DeploymentLambdaRole", "StreamLambdaRole"])
def test_budget_query_grants_include_reservation_index(template, role_prefix):
    budget_table = _budget_table_id(template)
    role = role_logical_id(template, role_prefix)
    matching = [
        statement
        for _source, statement in statements_for_role(template, role)
        if "dynamodb:Query" in _actions(statement)
    ]
    rendered_resources = json.dumps([statement.get("Resource") for statement in matching])
    assert budget_table in rendered_resources
    assert "/index/*" in rendered_resources


@pytest.mark.parametrize("invalid", ["0", "-1", "nan", "1001", "not-money"])
def test_budget_reservation_context_is_strict(invalid):
    app = cdk.App(context={"budget_reservation_usd": invalid})
    with pytest.raises(ValueError, match="budget_reservation_usd"):
        _budget_reservation_value(cdk.Stack(app, "InvalidReservation"))


def test_budget_reservation_context_accepts_bounded_decimal():
    app = cdk.App(context={"budget_reservation_usd": "0.25"})
    assert _budget_reservation_value(cdk.Stack(app, "ValidReservation")) == "0.25"
