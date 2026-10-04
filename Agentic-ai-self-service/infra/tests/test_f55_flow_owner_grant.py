"""F-55: the deployment Lambda can prove flow ownership, and can do nothing else to flows.

``deployment_handler._reject_unowned_flow`` makes exactly one call against the flows table: a
``GetItem`` by partition key. Two failure modes, one per direction:

* no grant -> the owner check can only fail closed with 503, so EVERY deploy that names a flow
  is refused. Measured live before this change: ``simulate-principal-policy`` returned
  ``implicitDeny`` for ``dynamodb:GetItem`` on ``acfe2e-p0920-flows``.
* ``grant_read_data`` -> Query, Scan and BatchGetItem on the table AND its indexes, none of which
  this Lambda uses; a Scan of every tenant's flows is not a permission an owner check needs
  (ARCC cnt_SFJJhkOueCPRkd, least privilege).

So this pins the exact action and the exact resource, not "some read access".
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack


@pytest.fixture(scope="module")
def resources() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "FlowOwnerGrantStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    return Template.from_stack(stack).to_json()["Resources"]


def _logical_id(resources: dict, type_: str, predicate) -> str:
    matches = [k for k, v in resources.items() if v["Type"] == type_ and predicate(v["Properties"])]
    assert len(matches) == 1, f"expected one {type_}, found {matches}"
    return matches[0]


def _mentions(value, logical_id: str) -> bool:
    return logical_id in repr(value)


def _flows(resources: dict) -> str:
    return _logical_id(resources, "AWS::DynamoDB::Table", lambda p: p.get("TableName") == "acf-test-flows")


def _deployment_role(resources: dict) -> str:
    function = _logical_id(resources, "AWS::Lambda::Function", lambda p: p.get("FunctionName") == "acf-test-deployment")
    return resources[function]["Properties"]["Role"]["Fn::GetAtt"][0]


def _statements_touching(resources: dict, role: str, table: str) -> list[dict]:
    found = []
    for resource in resources.values():
        if resource["Type"] != "AWS::IAM::Policy":
            continue
        props = resource["Properties"]
        if not any(_mentions(r, role) for r in props.get("Roles", [])):
            continue
        for statement in props["PolicyDocument"]["Statement"]:
            if _mentions(statement.get("Resource"), table):
                found.append(statement)
    return found


def test_the_deployment_role_may_get_one_flow_item_and_nothing_more(resources):
    table = _flows(resources)
    statements = _statements_touching(resources, _deployment_role(resources), table)

    assert statements == [
        {
            "Action": "dynamodb:GetItem",
            "Effect": "Allow",
            "Resource": {"Fn::GetAtt": [table, "Arn"]},
        }
    ], statements


def test_the_deployment_lambda_is_told_which_table_holds_flows(resources):
    table = _flows(resources)
    function = _logical_id(resources, "AWS::Lambda::Function", lambda p: p.get("FunctionName") == "acf-test-deployment")
    env = resources[function]["Properties"]["Environment"]["Variables"]

    assert env.get("DYNAMODB_FLOWS_TABLE_NAME") == {"Ref": table}, env.get("DYNAMODB_FLOWS_TABLE_NAME")
