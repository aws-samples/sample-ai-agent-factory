"""The synthesized deployment workflow must not treat CreateMemory as readiness."""

from __future__ import annotations

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
        "MemoryReadinessStack",
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
    fragments = []
    intrinsic_tokens: dict[str, str] = {}
    for part in parts:
        if isinstance(part, str):
            fragments.append(part)
            continue
        canonical = json.dumps(part, sort_keys=True, separators=(",", ":"))
        token = intrinsic_tokens.setdefault(
            canonical,
            f"__INTRINSIC_{len(intrinsic_tokens)}__",
        )
        fragments.append(token)
    return json.loads(separator.join(fragments))


def test_memory_create_cannot_bypass_the_readiness_gate(definition):
    states = definition["States"]

    assert states["CreateMemory"]["Type"] == "Task"
    assert states["CreateMemory"]["Next"] == "IsMemoryReady?"
    assert states["CreateMemory"]["Next"] != "SkipMemory"

    choice = states["IsMemoryReady?"]
    assert choice["Type"] == "Choice"
    assert choice["Default"] == "WaitForMemoryReady"
    assert len(choice["Choices"]) == 1
    assert choice["Choices"][0]["Next"] == "SkipMemory"
    condition = choice["Choices"][0]
    raw_condition = json.dumps(condition, sort_keys=True)
    assert "$.memory_result.ready" in raw_condition
    assert "BooleanEquals" in raw_condition
    assert "IsPresent" in raw_condition


def test_memory_wait_and_check_form_a_read_only_poll_loop(definition):
    states = definition["States"]
    wait = states["WaitForMemoryReady"]
    check = states["CheckMemoryReady"]
    create = states["CreateMemory"]

    assert wait == {
        "Type": "Wait",
        "Seconds": 10,
        "Next": "CheckMemoryReady",
    }
    assert check["Type"] == "Task"
    assert check["Next"] == "IsMemoryReady?"
    assert check["Resource"] == create["Resource"], (
        "CreateMemory and CheckMemoryReady must invoke the same handler; the "
        "persisted memory_result is what selects its side-effect-free check path"
    )


def test_memory_create_and_check_fail_through_the_normal_cleanup_path(
    definition,
):
    states = definition["States"]
    for state_name in ("CreateMemory", "CheckMemoryReady"):
        state = states[state_name]
        catches = state.get("Catch") or []
        assert catches, f"{state_name} has no failure cleanup edge"
        assert any(catch.get("Next") == "StatusUpdateFailure" for catch in catches), catches
