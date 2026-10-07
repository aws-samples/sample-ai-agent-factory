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
    "bedrock-agentcore:StartBatchEvaluation",
    "bedrock-agentcore:GetBatchEvaluation",
    "cloudwatch:GetDashboard",
    "logs:DescribeLogGroups",
    "logs:GetLogEvents",
    "logs:FilterLogEvents",
    "logs:GetQueryResults",
    "logs:StartQuery",
    "logs:StopQuery",
    "logs:CreateLogGroup",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
    "logs:PutRetentionPolicy",
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


def test_batch_evaluation_actions_are_scoped_to_batch_resources(template):
    statements = _deployment_role_statements(template)
    matches = [
        statement
        for statement in statements
        if set(_listify(statement.get("Action") or []))
        == {"bedrock-agentcore:StartBatchEvaluation", "bedrock-agentcore:GetBatchEvaluation"}
    ]
    assert len(matches) == 1
    assert _listify(matches[0]["Resource"]) == [f"arn:aws:bedrock-agentcore:*:{ACCOUNT}:batch-evaluate/*"]


def test_documented_target_role_scopes_batch_evaluation_to_the_target_account():
    matches = [
        statement for statement in _documented_target_statements() if statement.get("Sid") == "AgentCoreBatchEvaluation"
    ]
    assert len(matches) == 1
    assert set(matches[0]["Action"]) == {
        "bedrock-agentcore:StartBatchEvaluation",
        "bedrock-agentcore:GetBatchEvaluation",
    }
    assert matches[0]["Resource"] == ("arn:aws:bedrock-agentcore:<REGION>:<TARGET_ACCOUNT_ID>:batch-evaluate/*")


def test_deployment_api_receives_an_explicit_dev_gate_default(template):
    environment = _deployment_function(template)["Properties"]["Environment"]["Variables"]
    assert environment["PRODUCTION_EVALUATION_GATE"] == "false"


def test_production_evaluation_gate_context_is_strict():
    from stacks.platform.lambdas import _production_evaluation_gate_value

    app = cdk.App(context={"production_evaluation_gate": "false"})
    stack = cdk.Stack(app, "ExplicitGate")
    assert _production_evaluation_gate_value(stack, "prod") == "false"
    assert _production_evaluation_gate_value(cdk.Stack(cdk.App(), "ProdGate"), "prod") == "true"
    assert _production_evaluation_gate_value(cdk.Stack(cdk.App(), "DevGate"), "dev") == "false"
    with pytest.raises(ValueError, match="must be true or false"):
        _production_evaluation_gate_value(
            cdk.Stack(cdk.App(context={"production_evaluation_gate": "typo"}), "BadGate"),
            "prod",
        )


def test_production_evidence_key_is_rotating_retained_and_bound_to_the_api(template):
    keys = [
        (logical_id, resource)
        for logical_id, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::KMS::Key"
        and resource.get("Properties", {}).get("Description")
        == "Encrypts AgentCore batch-evaluation production promotion evidence"
    ]
    assert len(keys) == 1
    key_id, key = keys[0]
    assert key["Properties"]["EnableKeyRotation"] is True
    assert key["Properties"]["PendingWindowInDays"] == 30
    assert key["DeletionPolicy"] == "Retain"
    environment = _deployment_function(template)["Properties"]["Environment"]["Variables"]
    assert environment["EVALUATION_EVIDENCE_KMS_KEY_ARN"] == {"Fn::GetAtt": [key_id, "Arn"]}


def test_evidence_key_policy_scopes_agentcore_to_batch_evaluations(template):
    key = next(
        resource
        for resource in template["Resources"].values()
        if resource.get("Type") == "AWS::KMS::Key"
        and resource.get("Properties", {}).get("Description")
        == "Encrypts AgentCore batch-evaluation production promotion evidence"
    )
    service = next(
        statement
        for statement in key["Properties"]["KeyPolicy"]["Statement"]
        if statement.get("Principal") == {"Service": "bedrock-agentcore.amazonaws.com"}
    )
    assert set(_listify(service["Action"])) == {"kms:GenerateDataKey", "kms:Decrypt"}
    assert service["Condition"]["StringEquals"] == {"aws:SourceAccount": ACCOUNT}
    expected_batch_arn = {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                f":bedrock-agentcore:us-east-1:{ACCOUNT}:batch-evaluate/*",
            ],
        ]
    }
    assert service["Condition"]["ArnLike"] == {"aws:SourceArn": expected_batch_arn}


def test_deployment_role_kms_crypto_is_bound_by_batch_evaluation_context(template):
    statements = _deployment_role_statements(template)
    crypto = next(
        statement
        for statement in statements
        if set(_listify(statement.get("Action") or [])) == {"kms:GenerateDataKey", "kms:Decrypt"}
    )
    expected_batch_arn = {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                f":bedrock-agentcore:us-east-1:{ACCOUNT}:batch-evaluate/*",
            ],
        ]
    }
    assert crypto["Condition"]["StringLike"] == {
        "kms:EncryptionContext:aws:bedrock-agentcore:batchEvaluationArn": expected_batch_arn
    }
