"""The runtime-creating steps can govern only AgentCore runtime log groups."""

import ast
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

REGION = "us-east-1"
ACCOUNT = "123456789012"
RUNTIME_DEPLOYER = Path(__file__).resolve().parents[2] / "backend" / "src" / "app" / "services" / "runtime_deployer.py"
RUNTIME_DEPLOYER_TREE = ast.parse(RUNTIME_DEPLOYER.read_text())


def _string_constant(name: str) -> str:
    for node in RUNTIME_DEPLOYER_TREE.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise AssertionError(f"{name} is not a literal string in runtime_deployer.py")


RUNTIME_LOG_PREFIX = _string_constant("RUNTIME_LOG_GROUP_PREFIX")
# Region "*": a same-account deploy to a non-home region creates its runtime group
# there (F-41, tests/test_same_account_non_home_region_iam.py). A home-region literal
# here would also make the TagResource negative check below pass vacuously.
RUNTIME_LOG_ARN = f"arn:aws:logs:*:{ACCOUNT}:log-group:{RUNTIME_LOG_PREFIX}*"
LOG_METHOD_TO_ACTION = {
    "create_log_group": "logs:CreateLogGroup",
    "put_retention_policy": "logs:PutRetentionPolicy",
}


@pytest.fixture(scope="module")
def template_json():
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def _statements_for_role(template: dict, role_prefix: str) -> list[dict]:
    resources = template["Resources"]
    role_ids = [
        logical_id
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Role" and logical_id.startswith(role_prefix)
    ]
    assert len(role_ids) == 1
    role_id = role_ids[0]
    statements = []
    for policy in resources[role_id]["Properties"].get("Policies", []) or []:
        statements.extend(policy["PolicyDocument"].get("Statement", []) or [])
    for resource in resources.values():
        if resource["Type"] not in {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}:
            continue
        roles = resource["Properties"].get("Roles", []) or []
        if any(isinstance(role, dict) and role.get("Ref") == role_id for role in roles):
            statements.extend(resource["Properties"]["PolicyDocument"].get("Statement", []) or [])
    return statements


def _actions(statement: dict) -> set[str]:
    actions = statement.get("Action") or []
    return {actions} if isinstance(actions, str) else set(actions)


def _resources(statement: dict) -> list[str]:
    resources = statement.get("Resource") or []
    return [resources] if isinstance(resources, str) else resources


def _backend_governance_actions() -> set[str]:
    function = next(
        node
        for node in RUNTIME_DEPLOYER_TREE.body
        if isinstance(node, ast.FunctionDef) and node.name == "govern_default_runtime_log_group"
    )
    methods = {
        call.func.attr
        for call in ast.walk(function)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "logs_client"
    }
    unknown = methods - LOG_METHOD_TO_ACTION.keys()
    assert not unknown, f"runtime log governance added CloudWatch calls with no IAM-action mapping: {sorted(unknown)}"
    return {LOG_METHOD_TO_ACTION[method] for method in methods}


@pytest.mark.parametrize(
    "role_prefix",
    ["StepRuntimeConfigureRole", "StepMcpServerRole", "StepHarnessRole"],
)
def test_runtime_creators_can_apply_retention_to_the_runtime_prefix(template_json, role_prefix):
    statements = _statements_for_role(template_json, role_prefix)
    matching = [statement for statement in statements if RUNTIME_LOG_ARN in _resources(statement)]
    granted = set().union(*(_actions(statement) for statement in matching))
    assert _backend_governance_actions() <= granted


def test_no_step_can_tag_the_account_wide_runtime_log_prefix(template_json):
    offenders = []
    for role_prefix in ("StepRuntimeConfigureRole", "StepMcpServerRole", "StepHarnessRole"):
        for statement in _statements_for_role(template_json, role_prefix):
            if "logs:TagResource" in _actions(statement) and RUNTIME_LOG_ARN in _resources(statement):
                offenders.append((role_prefix, statement))
    assert offenders == []
