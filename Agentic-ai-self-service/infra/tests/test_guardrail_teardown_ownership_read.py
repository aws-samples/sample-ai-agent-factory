"""Runtime teardown must read a Guardrail's ownership tags before deletion.

A real eu-west-1 customer-path run deleted its AgentCore Runtime but retained the
Guardrail: ``GetGuardrail`` and ``DeleteGuardrail`` were allowed while the
intervening ``ListTagsForResource`` failed with ``AccessDeniedException``. The
teardown is intentionally fail-closed, so all three calls must be granted to the
same DeploymentLambdaRole on the same account-scoped Guardrail pattern.
"""

from __future__ import annotations

import copy

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import role_logical_id, statements_for_role

ACCOUNT = "123456789012"
REGION = "eu-west-1"
RESOURCE = f"arn:aws:bedrock:*:{ACCOUNT}:guardrail/*"
ACTIONS = {
    "bedrock:GetGuardrail",
    "bedrock:ListTagsForResource",
    "bedrock:DeleteGuardrail",
}


@pytest.fixture(scope="module")
def statements() -> list[dict]:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "guardrail-teardown-contract",
        environment_name="test",
        project_name="guardrail-teardown",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    template = Template.from_stack(stack).to_json()
    role = role_logical_id(template, "DeploymentLambdaRole")
    return [statement for _source, statement in statements_for_role(template, role)]


def _actions(statement: dict) -> set[str]:
    value = statement.get("Action", [])
    return {value} if isinstance(value, str) else set(value)


def guardrail_teardown_contract_violations(statements: list[dict]) -> list[str]:
    matching = [statement for statement in statements if _actions(statement) & ACTIONS]
    if len(matching) != 1:
        return [f"expected one guardrail teardown statement, found {len(matching)}"]
    statement = matching[0]
    violations = []
    if statement.get("Effect") != "Allow":
        violations.append("guardrail teardown statement is not Allow")
    if _actions(statement) != ACTIONS:
        missing = sorted(ACTIONS - _actions(statement))
        extra = sorted(_actions(statement) - ACTIONS)
        violations.append(f"guardrail teardown actions differ: missing={missing}, extra={extra}")
    if statement.get("Resource") != RESOURCE:
        violations.append(f"guardrail teardown resource is {statement.get('Resource')!r}")
    if statement.get("Condition"):
        violations.append("guardrail teardown reads carry an inapplicable condition")
    return violations


def test_deployment_lambda_can_prove_and_delete_owned_guardrails(statements) -> None:
    assert guardrail_teardown_contract_violations(statements) == []


def test_removing_the_ownership_read_is_caught(statements) -> None:
    mutated = copy.deepcopy(statements)
    statement = next(item for item in mutated if _actions(item) & ACTIONS)
    statement["Action"].remove("bedrock:ListTagsForResource")
    assert guardrail_teardown_contract_violations(mutated) == [
        "guardrail teardown actions differ: missing=['bedrock:ListTagsForResource'], extra=[]"
    ]


@pytest.mark.parametrize(
    "resource",
    ["*", "arn:aws:bedrock:*:210987654321:guardrail/*"],
    ids=["account-wildcard", "foreign-account"],
)
def test_broader_or_foreign_guardrail_authority_is_caught(statements, resource) -> None:
    mutated = copy.deepcopy(statements)
    statement = next(item for item in mutated if _actions(item) & ACTIONS)
    statement["Resource"] = resource
    assert guardrail_teardown_contract_violations(mutated) == [f"guardrail teardown resource is {resource!r}"]
