"""F-61 IAM contract for runtime-name observability target routing.

The evaluation, dashboard, trace, and cost routes run in the deployment API
Lambda. Same-account regional requests use that role in the target region;
cross-account requests assume the documented fixed-name deployment role. A
resolver-only test can stay green while either principal lacks one of the API
calls, so pin both effective policy surfaces here.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import role_logical_id, statements_for_role

ACCOUNT = "123456789012"
TARGET_ROLE_ARN = "arn:aws:iam::*:role/AgentCoreFlowsDeploymentRole"
TARGET_QUERY_ACTIONS = {
    "bedrock-agentcore:GetOnlineEvaluationConfig",
    "bedrock-agentcore:ListOnlineEvaluationConfigs",
    "cloudwatch:GetDashboard",
    "logs:DescribeLogGroups",
    "logs:GetQueryResults",
    "logs:StartQuery",
    "logs:StopQuery",
}


def _listify(value) -> list:
    return value if isinstance(value, list) else [value]


def _allowed_actions(statements: list[dict]) -> set[str]:
    return {
        action
        for statement in statements
        if statement.get("Effect") == "Allow"
        for action in _listify(statement.get("Action") or [])
    }


@pytest.fixture(scope="module")
def template() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "observability-target-test",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(account=ACCOUNT, region="us-east-1"),
    )
    return Template.from_stack(stack).to_json()


def _deployment_role_statements(template: dict) -> list[dict]:
    role_id = role_logical_id(template, "DeploymentLambdaRole")
    return [statement for _, statement in statements_for_role(template, role_id)]


def _deployment_function(template: dict) -> dict:
    role_id = role_logical_id(template, "DeploymentLambdaRole")
    matches = [
        resource
        for resource in template["Resources"].values()
        if resource.get("Type") == "AWS::Lambda::Function"
        and resource.get("Properties", {}).get("Role") == {"Fn::GetAtt": [role_id, "Arn"]}
    ]
    assert len(matches) == 1, (
        "runtime-name observability account binding requires one DeploymentLambda "
        f"using {role_id}; found {len(matches)}"
    )
    return matches[0]


def _documented_target_statements() -> list[dict]:
    repository = Path(__file__).resolve().parents[2]
    document = json.loads((repository / "docs" / "cross-account-deploy-role.json").read_text())
    return [
        statement
        for name, policy in document.items()
        if name.startswith("permissions-policy") and name.endswith(".json")
        for statement in policy["Statement"]
    ]


def test_deployment_api_role_can_query_a_same_account_regional_runtime(template):
    actions = _allowed_actions(_deployment_role_statements(template))
    assert TARGET_QUERY_ACTIONS <= actions, (
        "runtime-name observability routes run on DeploymentLambdaRole; "
        f"its effective policies are missing {sorted(TARGET_QUERY_ACTIONS - actions)}"
    )


def test_deployment_api_receives_a_trusted_home_account_identifier(template):
    environment = _deployment_function(template).get("Properties", {}).get("Environment", {}).get("Variables", {})
    state_machine_arn = environment.get("STATE_MACHINE_ARN")
    assert isinstance(state_machine_arn, dict)
    state_machine_id = state_machine_arn.get("Ref")
    assert state_machine_id
    assert template["Resources"][state_machine_id]["Type"] == "AWS::StepFunctions::StateMachine"


def test_deployment_api_role_can_assume_only_the_fixed_target_role(template):
    assume = [
        statement
        for statement in _deployment_role_statements(template)
        if "sts:AssumeRole" in _listify(statement.get("Action") or [])
    ]
    assert len(assume) == 1
    assert assume[0].get("Effect") == "Allow"
    assert _listify(assume[0].get("Resource")) == [TARGET_ROLE_ARN]


def test_documented_cross_account_role_can_query_runtime_observability():
    statements = _documented_target_statements()
    actions = _allowed_actions(statements)
    assert TARGET_QUERY_ACTIONS <= actions, (
        "the API Lambda can assume the target role, but that role cannot execute "
        f"the routed calls: {sorted(TARGET_QUERY_ACTIONS - actions)}"
    )


def test_cross_account_logs_query_lifecycle_is_not_scoped_to_log_group_arns():
    """Query-result and stop operations address query ids, not log-group ARNs."""
    statements = _documented_target_statements()
    for action in ("logs:GetQueryResults", "logs:StopQuery"):
        grants = [
            statement
            for statement in statements
            if statement.get("Effect") == "Allow" and action in _listify(statement.get("Action") or [])
        ]
        assert grants, f"no target-role statement grants {action}"
        assert any("*" in _listify(statement.get("Resource")) for statement in grants), (
            f"{action} must be usable for a service-generated query id"
        )
