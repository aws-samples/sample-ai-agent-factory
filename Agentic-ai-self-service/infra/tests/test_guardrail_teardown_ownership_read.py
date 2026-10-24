"""Runtime teardown must read a Guardrail's ownership tags before deletion.

A real eu-west-1 customer-path run deleted its AgentCore Runtime but retained the
Guardrail: the explicit deletion role had ``GetGuardrail``,
``ListTagsForResource``, and ``DeleteGuardrail``, while the automatic
failed-deployment status role held only ``DeleteGuardrail``. The ownership read
therefore failed closed with exact ``AccessDeniedException``. All three calls
must be granted to both cleanup principals on the same account-scoped Guardrail
pattern; the status role's two new reads stay isolated so unrelated overflow
policies are byte-stable.
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
def template_json() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "guardrail-teardown-contract",
        environment_name="test",
        project_name="guardrail-teardown",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    return Template.from_stack(stack).to_json()


@pytest.fixture(scope="module")
def statements_by_role(template_json) -> dict[str, list[dict]]:
    return {
        prefix: [
            statement
            for _source, statement in statements_for_role(
                template_json,
                role_logical_id(template_json, prefix),
            )
        ]
        for prefix in ("DeploymentLambdaRole", "StepStatusUpdateRole")
    }


def _actions(statement: dict) -> set[str]:
    value = statement.get("Action", [])
    return {value} if isinstance(value, str) else set(value)


def guardrail_teardown_contract_violations(statements: list[dict]) -> list[str]:
    matching = [statement for statement in statements if _actions(statement) & ACTIONS]
    if not matching:
        return ["no guardrail teardown statements found"]
    violations = []
    granted: set[str] = set()
    seen: set[str] = set()
    for statement in matching:
        relevant = _actions(statement) & ACTIONS
        duplicated = sorted(relevant & seen)
        if duplicated:
            violations.append(f"duplicate guardrail teardown actions: {duplicated}")
        seen |= relevant
        granted |= relevant
        if statement.get("Effect") != "Allow":
            violations.append("guardrail teardown statement is not Allow")
        extra = sorted(_actions(statement) - ACTIONS)
        if extra:
            violations.append(f"guardrail teardown statement has extra actions: {extra}")
        if statement.get("Resource") != RESOURCE:
            violations.append(f"guardrail teardown resource is {statement.get('Resource')!r}")
        if statement.get("Condition"):
            violations.append("guardrail teardown reads carry an inapplicable condition")
    missing = sorted(ACTIONS - granted)
    if missing:
        violations.append(f"guardrail teardown actions are missing: {missing}")
    return violations


@pytest.mark.parametrize("role_prefix", ["DeploymentLambdaRole", "StepStatusUpdateRole"])
def test_every_cleanup_role_can_prove_and_delete_owned_guardrails(
    statements_by_role,
    role_prefix,
) -> None:
    assert guardrail_teardown_contract_violations(statements_by_role[role_prefix]) == []


@pytest.mark.parametrize("role_prefix", ["DeploymentLambdaRole", "StepStatusUpdateRole"])
def test_removing_the_ownership_read_is_caught(statements_by_role, role_prefix) -> None:
    mutated = copy.deepcopy(statements_by_role[role_prefix])
    for statement in mutated:
        actions = _actions(statement)
        if "bedrock:ListTagsForResource" not in actions:
            continue
        remaining = sorted(actions - {"bedrock:ListTagsForResource"})
        statement["Action"] = remaining[0] if len(remaining) == 1 else remaining
    assert guardrail_teardown_contract_violations(mutated) == [
        "guardrail teardown actions are missing: ['bedrock:ListTagsForResource']"
    ]


@pytest.mark.parametrize("role_prefix", ["DeploymentLambdaRole", "StepStatusUpdateRole"])
@pytest.mark.parametrize(
    "resource",
    ["*", "arn:aws:bedrock:*:210987654321:guardrail/*"],
    ids=["account-wildcard", "foreign-account"],
)
def test_broader_or_foreign_guardrail_authority_is_caught(
    statements_by_role,
    role_prefix,
    resource,
) -> None:
    mutated = copy.deepcopy(statements_by_role[role_prefix])
    statement = next(item for item in mutated if "bedrock:ListTagsForResource" in _actions(item))
    statement["Resource"] = resource
    assert guardrail_teardown_contract_violations(mutated) == [f"guardrail teardown resource is {resource!r}"]


def test_status_cleanup_ownership_reads_are_isolated_from_existing_policies(
    template_json,
) -> None:
    matches = [
        (logical_id, resource)
        for logical_id, resource in template_json["Resources"].items()
        if logical_id.startswith("StepStatusUpdateGuardrailOwnershipReadPolicy")
    ]
    assert len(matches) == 1
    _logical_id, policy = matches[0]
    assert policy["Type"] == "AWS::IAM::Policy"
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    assert len(statements) == 1
    assert _actions(statements[0]) == {
        "bedrock:GetGuardrail",
        "bedrock:ListTagsForResource",
    }
    assert statements[0]["Resource"] == RESOURCE
    assert policy["Properties"]["Roles"] == [{"Ref": role_logical_id(template_json, "StepStatusUpdateRole")}]
