"""StepMemoryRole must be able to send all four Memory ownership tags."""

from __future__ import annotations

import copy

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.config import governance_tag_key_globs
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import role_logical_id, statements_for_role

TAG_ACTION = "bedrock-agentcore:TagResource"
#: The operator carrying the key allowlist. ``StringLike`` since P0-B's governance tag keys are
#: admin-created at runtime and can only be bounded by namespace; the four exact keys below
#: contain no wildcard character, so they still match literally.
KEYS_OPERATOR = "ForAllValues:StringLike"
EXPECTED_KEYS = [
    "ManagedBy",
    "AgentCoreStack",
    "OwnerSubHash",
    "DeploymentId",
    *governance_tag_key_globs(),
]
EXPECTED_SHAPES = {
    "aws:RequestTag/OwnerSubHash": "?" * 32,
    "aws:RequestTag/DeploymentId": "????????-????-????-????-????????????",
}


@pytest.fixture(scope="module")
def template_json() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "MemoryOwnerTagGrantStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(
            region="us-east-1",
            account="123456789012",
        ),
    )
    return Template.from_stack(stack).to_json()


def _tag_statement(template_json: dict, role_prefix: str) -> dict:
    role = role_logical_id(template_json, role_prefix)
    matches = []
    for _source, statement in statements_for_role(template_json, role):
        actions = statement.get("Action")
        actions = [actions] if isinstance(actions, str) else actions or []
        if statement.get("Effect") == "Allow" and TAG_ACTION in actions:
            matches.append(statement)
    assert len(matches) == 1, (
        f"{role_prefix} resolves to {len(matches)} {TAG_ACTION} statements; "
        "one conditioned statement is required so CDK cannot merge a broader twin."
    )
    return matches[0]


def _assert_memory_conditions(statement: dict) -> None:
    conditions = statement.get("Condition") or {}
    assert conditions.get("StringEquals") == {
        "aws:RequestTag/ManagedBy": "agentcore-flows",
        "aws:RequestTag/AgentCoreStack": "acf-test-${aws:RequestedRegion}",
    }
    assert conditions.get(KEYS_OPERATOR, {}).get("aws:TagKeys") == EXPECTED_KEYS
    assert conditions.get("StringLike") == EXPECTED_SHAPES


def test_memory_role_admits_the_exact_four_tag_contract(template_json):
    """The old two-key set denies CreateMemory before its create action runs."""
    _assert_memory_conditions(_tag_statement(template_json, "StepMemoryRole"))


@pytest.mark.parametrize("removed", ["OwnerSubHash", "DeploymentId"])
def test_removing_either_caller_binding_key_is_caught(template_json, removed):
    """Mutation proof: neither newly allowed key is decorative."""
    mutated = copy.deepcopy(_tag_statement(template_json, "StepMemoryRole"))
    mutated["Condition"][KEYS_OPERATOR]["aws:TagKeys"].remove(removed)

    with pytest.raises(AssertionError):
        _assert_memory_conditions(mutated)


@pytest.mark.parametrize(
    "removed",
    ["aws:RequestTag/OwnerSubHash", "aws:RequestTag/DeploymentId"],
)
def test_removing_either_required_value_shape_is_caught(template_json, removed):
    """ForAllValues alone is vacuous if a required tag is omitted."""
    mutated = copy.deepcopy(_tag_statement(template_json, "StepMemoryRole"))
    del mutated["Condition"]["StringLike"][removed]

    with pytest.raises(AssertionError):
        _assert_memory_conditions(mutated)


def test_no_other_step_role_can_stamp_the_memory_binding_keys(template_json):
    """The exception is per principal, not a platform-wide widening."""
    resources = template_json["Resources"]
    offenders = []
    keyless = []
    checked = 0
    for logical_id, resource in resources.items():
        if (
            resource["Type"] != "AWS::IAM::Role"
            or not logical_id.startswith("Step")
            or logical_id.startswith("StepMemoryRole")
        ):
            continue
        for _source, statement in statements_for_role(template_json, logical_id):
            actions = statement.get("Action")
            actions = [actions] if isinstance(actions, str) else actions or []
            if TAG_ACTION not in actions:
                continue
            checked += 1
            conditions = statement.get("Condition") or {}
            # Read the allowlist under EVERY ForAllValues operator, not the one this file
            # expects. Scoped to the operator alone, this audit passed VACUOUSLY the moment the
            # allowlist moved from StringEquals to StringLike: an absent operator reads as an
            # empty key set, an empty key set intersects nothing, and "no offenders" is then a
            # statement about the test rather than about the policy. `keyless` below is what
            # makes that unfalsifiable-by-accident.
            keys = {
                key
                for operator, block in conditions.items()
                if operator.startswith("ForAllValues:")
                for key in (block or {}).get("aws:TagKeys", [])
            }
            if not keys:
                keyless.append(logical_id)
            shapes = set((conditions.get("StringLike") or {}).keys())
            if {"OwnerSubHash", "DeploymentId"} & keys or set(EXPECTED_SHAPES) & shapes:
                offenders.append(logical_id)

    assert checked >= 5, (
        f"inspected only {checked} non-memory step TagResource statements; the role-scoped audit is probably vacuous"
    )
    # An unconditioned TagResource would also read as "not an offender" here while being
    # strictly worse than one, so it fails as its own finding rather than passing quietly.
    assert keyless == [], f"these step roles grant {TAG_ACTION} with no aws:TagKeys allowlist at all: {keyless}"
    assert offenders == []
