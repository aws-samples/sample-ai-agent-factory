"""The gateway step can bound its tool functions' log groups, and only theirs.

``gateway_deployer.govern_tool_function_log_group`` creates or adopts ``/aws/lambda/<function>``
with the platform's retention before each tool function's gateway target exists
(backend/tests/test_tool_lambda_log_groups_are_bounded.py). It is fail-closed, so without
this grant every gateway deploy that makes a tool function fails. The actions are therefore
derived from the backend function's own calls rather than restated, and the reach is pinned
to exactly the function grant's, under ``/aws/lambda/``.
"""

from __future__ import annotations

import ast
import fnmatch
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform import tool_lambda_names
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import role_logical_id, statements_by_role, statements_for_role

ACCOUNT = "123456789012"
PROJECT = "acf"
ENVIRONMENT = "test"
GATEWAY_DEPLOYER = Path(__file__).resolve().parents[2] / "backend" / "src" / "app" / "services" / "gateway_deployer.py"
LOG_METHOD_TO_ACTION = {
    "create_log_group": "logs:CreateLogGroup",
    "put_retention_policy": "logs:PutRetentionPolicy",
}


@pytest.fixture(scope="module")
def template_json() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "ToolLogGroups",
        environment_name=ENVIRONMENT,
        project_name=PROJECT,
        env=cdk.Environment(region="us-east-1", account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def _list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _backend_actions() -> set[str]:
    tree = ast.parse(GATEWAY_DEPLOYER.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "govern_tool_function_log_group"
    )
    methods = {
        call.func.attr
        for call in ast.walk(function)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "logs_client"
    }
    assert methods, "govern_tool_function_log_group makes no logs_client call; re-derive this contract"
    unknown = methods - LOG_METHOD_TO_ACTION.keys()
    assert not unknown, f"tool log governance added CloudWatch calls with no IAM-action mapping: {sorted(unknown)}"
    return {LOG_METHOD_TO_ACTION[method] for method in methods}


def _expected_patterns() -> set[str]:
    return {
        f"arn:aws:logs:{region}:{ACCOUNT}:log-group:/aws/lambda/"
        f"{tool_lambda_names.function_name_prefix(PROJECT, ENVIRONMENT, region)}*"
        for region in tool_lambda_names.SUPPORTED_TARGET_REGIONS
    }


def _tool_group_arns() -> list[str]:
    """A concrete tool function's log group in every supported region, in both ARN forms."""
    arns = []
    for region in tool_lambda_names.SUPPORTED_TARGET_REGIONS:
        name = f"/aws/lambda/{tool_lambda_names.function_name_prefix(PROJECT, ENVIRONMENT, region)}DynamicTools"
        arn = f"arn:aws:logs:{region}:{ACCOUNT}:log-group:{name}"
        arns += [arn, f"{arn}:*"]
    return arns


def _gateway_statements(template_json: dict) -> list[dict]:
    role = role_logical_id(template_json, "StepGatewayRole")
    return [
        statement
        for _source, statement in statements_for_role(template_json, role)
        if statement.get("Effect") == "Allow"
    ]


def test_the_gateway_step_can_govern_every_tool_function_log_group(template_json):
    needed = _backend_actions()
    statements = _gateway_statements(template_json)
    for pattern in sorted(_expected_patterns()):
        granted: set[str] = set()
        for statement in statements:
            if pattern in _list(statement.get("Resource")):
                granted |= set(_list(statement.get("Action")))
        assert needed <= granted, f"{pattern}: the gateway step cannot {sorted(needed - granted)}"


def test_the_reach_is_exactly_the_function_grant_under_aws_lambda(template_json):
    """No broader than the function grant. A statement on ``/aws/lambda/*`` would let the step
    shorten the retention of every function's logs in the account, the platform's own included."""
    statements = _gateway_statements(template_json)
    function_patterns = {
        resource
        for statement in statements
        if "lambda:CreateFunction" in _list(statement.get("Action"))
        for resource in _list(statement.get("Resource"))
    }
    assert function_patterns, "no lambda:CreateFunction grant found on the gateway step role"
    mirrored = {
        pattern.replace("arn:aws:lambda:", "arn:aws:logs:", 1).replace(":function:", ":log-group:/aws/lambda/", 1)
        for pattern in function_patterns
    }
    retention = {
        resource
        for statement in statements
        if "logs:PutRetentionPolicy" in _list(statement.get("Action"))
        for resource in _list(statement.get("Resource"))
    }
    assert retention == mirrored == _expected_patterns()


def test_no_role_may_delete_or_tag_a_tool_functions_log_group(template_json):
    """Teardown leaves the groups to expire and the manifest is the ownership authority, so no
    principal needs either verb on them. Template-wide on purpose: an over-reach rule narrowed
    to one role would let the next offending grant appear on another."""
    offenders = []
    for role, statements in statements_by_role(template_json).items():
        for _source, statement in statements:
            if statement.get("Effect") != "Allow":
                continue
            risky = set(_list(statement.get("Action"))) & {"logs:DeleteLogGroup", "logs:TagResource", "logs:*", "*"}
            if not risky:
                continue
            for resource in _list(statement.get("Resource")):
                if not isinstance(resource, str):
                    continue
                if any(fnmatch.fnmatchcase(arn, resource) for arn in _tool_group_arns()):
                    offenders.append((role, sorted(risky), resource))
    assert offenders == []
