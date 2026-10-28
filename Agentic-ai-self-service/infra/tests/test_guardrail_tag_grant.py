"""Bedrock guardrail creation must carry a bounded tag-on-create grant.

A real eu-west-1 deployment reached ``CreateGuardrail`` with the create action
allowed, then failed atomically because the handler passes ``tags=`` and
``StepGuardrailsRole`` lacked the separately authorized
``bedrock:TagResource`` action.  The generated guardrail id does not exist when
IAM evaluates the create, so ``guardrail/*`` is unavoidable; request-tag
conditions are the safety boundary.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.config import governance_tag_key_globs
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import statements_for_role

ACCOUNT = "123456789012"
PROJECT = "guardrailprobe"
ENVIRONMENT = "t1008"
REGION = "us-east-1"
ACTION = "bedrock:TagResource"
EXPECTED_RESOURCE = f"arn:aws:bedrock:*:{ACCOUNT}:guardrail/*"
EXPECTED_OWNER = f"{PROJECT}-{ENVIRONMENT}-${{aws:RequestedRegion}}"
EXPECTED_KEYS = ["ManagedBy", "AgentCoreStack", *governance_tag_key_globs()]
REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def statements() -> list[dict]:
    app = cdk.App()
    stack = PlatformStack(
        app,
        f"{PROJECT}-{ENVIRONMENT}",
        environment_name=ENVIRONMENT,
        project_name=PROJECT,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    template = Template.from_stack(stack).to_json()
    role_ids = [
        logical_id
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("StepGuardrailsRole") and resource["Type"] == "AWS::IAM::Role"
    ]
    assert len(role_ids) == 1, role_ids
    return [statement for _source, statement in statements_for_role(template, role_ids[0])]


def _actions(statement: dict) -> list[str]:
    value = statement.get("Action", [])
    return [value] if isinstance(value, str) else value


def _tag_statements(statements: list[dict]) -> list[dict]:
    return [statement for statement in statements if ACTION in _actions(statement)]


def guardrail_tag_contract_violations(statements: list[dict]) -> list[str]:
    tagged = _tag_statements(statements)
    if len(tagged) != 1:
        return [f"expected exactly one {ACTION} statement, found {len(tagged)}"]
    statement = tagged[0]
    violations = []
    if statement.get("Effect") != "Allow":
        violations.append("tag statement is not Allow")
    if _actions(statement) != [ACTION]:
        violations.append(f"tag statement actions are {_actions(statement)!r}")
    if statement.get("Resource") != EXPECTED_RESOURCE:
        violations.append(f"tag statement resource is {statement.get('Resource')!r}")
    condition = statement.get("Condition") or {}
    equals = condition.get("StringEquals") or {}
    if equals.get("aws:RequestTag/ManagedBy") != "agentcore-flows":
        violations.append("ManagedBy request tag is not pinned")
    if equals.get("aws:RequestTag/AgentCoreStack") != EXPECTED_OWNER:
        violations.append("AgentCoreStack request tag is not pinned to the requested region")
    if condition.get("ForAllValues:StringLike", {}).get("aws:TagKeys") != EXPECTED_KEYS:
        violations.append("tag-key allowlist differs from ownership keys plus governance namespaces")
    if "ForAllValues:StringEquals" in condition or "StringEqualsIfExists" in condition:
        violations.append("tag conditions contain a fail-open or namespace-breaking operator")
    return violations


def test_guardrail_create_call_really_sends_governed_tags() -> None:
    source = (REPOSITORY / "backend/src/app/step_handlers/guardrails_step.py").read_text(encoding="utf-8")
    assert 'create_params["tags"] = governed_lower_tag_list(' in source
    assert "bedrock.create_guardrail(**create_params)" in source


def test_step_guardrails_role_allows_only_the_owned_guardrail_tag_set(statements) -> None:
    assert guardrail_tag_contract_violations(statements) == []


def test_mutation_removing_guardrail_tag_permission_is_caught(statements) -> None:
    mutated = [statement for statement in copy.deepcopy(statements) if ACTION not in _actions(statement)]
    assert guardrail_tag_contract_violations(mutated) == [f"expected exactly one {ACTION} statement, found 0"]


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("Resource", "*", "tag statement resource is '*'"),
        ("ManagedBy", "*", "ManagedBy request tag is not pinned"),
        ("AgentCoreStack", "foreign-*", "AgentCoreStack request tag is not pinned to the requested region"),
        ("TagKeys", ["*"], "tag-key allowlist differs from ownership keys plus governance namespaces"),
    ],
)
def test_adversarial_broadening_of_guardrail_tag_permission_is_caught(
    statements,
    field,
    value,
    expected,
) -> None:
    mutated = copy.deepcopy(statements)
    statement = _tag_statements(mutated)[0]
    if field == "Resource":
        statement["Resource"] = value
    elif field == "TagKeys":
        statement["Condition"]["ForAllValues:StringLike"]["aws:TagKeys"] = value
    else:
        statement["Condition"]["StringEquals"][f"aws:RequestTag/{field}"] = value
    assert expected in guardrail_tag_contract_violations(mutated)


def test_cross_account_role_has_the_same_bounded_guardrail_tag_contract() -> None:
    document = json.loads((REPOSITORY / "docs/cross-account-deploy-role.json").read_text(encoding="utf-8"))
    policy = document["permissions-policy-bedrock-and-storage.json"]
    tagged = [statement for statement in policy["Statement"] if ACTION in _actions(statement)]
    assert len(tagged) == 1
    statement = tagged[0]
    assert statement["Sid"] == "BedrockGuardrailOwnershipTagging"
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == [ACTION]
    assert statement["Resource"] == "arn:aws:bedrock:<REGION>:<TARGET_ACCOUNT_ID>:guardrail/*"
    assert statement["Condition"] == {
        "StringEquals": {
            "aws:RequestTag/ManagedBy": "agentcore-flows",
            "aws:RequestTag/AgentCoreStack": "<PLATFORM_PROJECT>-<PLATFORM_ENV>-<REGION>",
        },
        "ForAllValues:StringLike": {"aws:TagKeys": ["ManagedBy", "AgentCoreStack", "platform:*", "org:*"]},
    }
    broad = next(item for item in policy["Statement"] if item["Sid"] == "BedrockModelsKnowledgeBasesAndGuardrails")
    assert ACTION not in broad["Action"], "tag authority must retain its request-tag conditions"
