"""F-55: the synthesized workflow must consume validation before creating anything."""

from __future__ import annotations

import copy
import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack


@pytest.fixture(scope="module")
def definition() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "DeploymentInputGateStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(
            region="us-east-1",
            account="123456789012",
        ),
    )
    resources = Template.from_stack(stack).to_json()["Resources"]
    machines = [resource for resource in resources.values() if resource["Type"] == "AWS::StepFunctions::StateMachine"]
    assert len(machines) == 1
    rendered = machines[0]["Properties"]["DefinitionString"]
    if isinstance(rendered, str):
        return json.loads(rendered)

    separator, parts = rendered["Fn::Join"]
    fragments: list[str] = []
    intrinsic_tokens: dict[str, str] = {}
    for part in parts:
        if isinstance(part, str):
            fragments.append(part)
            continue
        canonical = json.dumps(part, sort_keys=True, separators=(",", ":"))
        fragments.append(
            intrinsic_tokens.setdefault(
                canonical,
                f"__INTRINSIC_{len(intrinsic_tokens)}__",
            )
        )
    return json.loads(separator.join(fragments))


def _assert_validity_choice(choice: dict) -> None:
    assert choice["Type"] == "Choice"
    assert choice["Default"] == "DeploymentInputInvalid"
    assert len(choice["Choices"]) == 1

    admitted = choice["Choices"][0]
    assert admitted["Next"] == "HasGuardrails?"
    assert admitted["And"] == [
        {"Variable": "$.is_valid", "IsPresent": True},
        {"Variable": "$.is_valid", "IsBoolean": True},
        {"Variable": "$.is_valid", "BooleanEquals": True},
    ]


def test_validation_is_the_start_state_and_its_verdict_is_consumed(definition):
    states = definition["States"]

    assert definition["StartAt"] == "ValidateWorkflow"
    assert states["ValidateWorkflow"]["Type"] == "Task"
    assert states["ValidateWorkflow"]["Next"] == "IsDeploymentInputValid?"
    _assert_validity_choice(states["IsDeploymentInputValid?"])


@pytest.mark.parametrize(
    "condition_key",
    ["IsPresent", "IsBoolean", "BooleanEquals"],
)
def test_removing_any_fail_closed_condition_is_caught(
    definition,
    condition_key,
):
    """Mutation proof for missing, wrong-type, and explicit-false verdicts."""
    mutated = copy.deepcopy(definition["States"]["IsDeploymentInputValid?"])
    mutated["Choices"][0]["And"] = [
        condition for condition in mutated["Choices"][0]["And"] if condition_key not in condition
    ]

    with pytest.raises(AssertionError):
        _assert_validity_choice(mutated)


def test_invalid_verdict_records_why_and_reaches_only_failure_states(definition):
    states = definition["States"]

    invalid = states["DeploymentInputInvalid"]
    assert invalid["Type"] == "Pass"
    assert invalid["ResultPath"] == "$.error_info"
    assert invalid["Parameters"]["Error"] == "DeploymentInputInvalid"
    assert "No resource-creating step ran" in invalid["Parameters"]["Cause"]
    assert invalid["Next"] == "NoResourcesCreatedOnInvalidInput"

    marker = states["NoResourcesCreatedOnInvalidInput"]
    assert marker == {
        "Type": "Pass",
        "Parameters": {
            "proven": True,
            "reason": ("rejected at ValidateWorkflow, before any resource-creating task"),
        },
        "ResultPath": "$.no_resources_created",
        "Next": "StatusUpdateFailure",
    }
    assert states["StatusUpdateFailure"]["Next"] == "DeploymentFailed"
    assert states["DeploymentFailed"]["Type"] == "Fail"


def test_thrown_validation_errors_get_the_same_no_resource_marker(definition):
    catches = definition["States"]["ValidateWorkflow"].get("Catch") or []

    assert catches == [
        {
            "ErrorEquals": ["States.ALL"],
            "ResultPath": "$.error_info",
            "Next": "NoResourcesCreatedOnInvalidInput",
        }
    ]


def test_invalid_branch_has_no_edge_to_a_resource_creating_step(definition):
    """Walk the complete invalid chain rather than checking isolated labels."""
    states = definition["States"]
    current = "DeploymentInputInvalid"
    visited: list[str] = []
    while current:
        assert current not in visited, f"invalid branch loops at {current}"
        visited.append(current)
        state = states[current]
        current = state.get("Next")

    assert visited == [
        "DeploymentInputInvalid",
        "NoResourcesCreatedOnInvalidInput",
        "StatusUpdateFailure",
        "DeploymentFailed",
    ]
