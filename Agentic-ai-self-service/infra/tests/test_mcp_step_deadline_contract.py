"""F-44 infrastructure half: Step Functions must outlive the MCP Lambda.

The MCP step creates and mutates several resources before it reaches its
readiness probes.  If Step Functions times out at the same instant as Lambda,
its retry can start a second non-idempotent invocation while the first one is
still running.  A positive outer margin is therefore part of the deadline
contract, not merely a larger number inside the handler.
"""

from __future__ import annotations

import aws_cdk as cdk
from stacks.platform_stack import PlatformStack

from tests.test_a_step_task_outlives_its_lambda import (
    _definition_and_tokens,
    _lambda_logical_id,
)


def _mcp_timeout_pair() -> tuple[int, int]:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "McpDeadlineStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(
            region="us-east-1",
            account="123456789012",
        ),
    )
    resources = app.synth().get_stack_by_name(stack.stack_name).template["Resources"]
    definition, tokens = _definition_and_tokens(resources)

    state = definition["States"]["DeployMCPServer"]
    task_timeout = state.get("TimeoutSeconds")
    assert isinstance(task_timeout, int), (
        "DeployMCPServer has no concrete TimeoutSeconds; the outer deadline cannot be compared with the Lambda's"
    )

    candidates = [
        state.get("Resource"),
        (state.get("Parameters") or {}).get("FunctionName"),
    ]
    logical_id = None
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        for token, intrinsic in tokens.items():
            if token in candidate:
                logical_id = _lambda_logical_id(intrinsic)
                break
        if logical_id:
            break

    assert logical_id, "could not resolve the Lambda invoked by DeployMCPServer"
    target = resources[logical_id]
    assert target["Type"] == "AWS::Lambda::Function"
    lambda_timeout = target["Properties"].get("Timeout")
    assert isinstance(lambda_timeout, int)
    return task_timeout, lambda_timeout


def test_mcp_task_has_a_positive_margin_beyond_the_lambda_timeout():
    task_timeout, lambda_timeout = _mcp_timeout_pair()
    assert task_timeout > lambda_timeout, (
        f"DeployMCPServer task timeout is {task_timeout}s and its Lambda timeout "
        f"is {lambda_timeout}s. Equality lets Step Functions raise States.Timeout "
        "and retry while the original resource-creating invocation is still alive."
    )
