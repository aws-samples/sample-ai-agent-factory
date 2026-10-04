"""The deployment Lambda invokes MCP runtimes WITH ``runtimeUserId``; that is a distinct action.

Measured live on 2026-09-28: the MCP server runtime was READY, its DEFAULT endpoint READY, an IAM
simulation of the deployment Lambda's role ALLOWED ``bedrock-agentcore:InvokeAgentRuntime`` on both
the runtime and the runtime-endpoint ARN, an MCP initialize with plain credentials succeeded -- and
every product discovery still failed ``AccessDeniedException``. The product's MCP client forwards the
caller's identity as ``runtimeUserId``, and AgentCore authorises that form of the call as
``bedrock-agentcore:InvokeAgentRuntimeForUser`` (present in the service reference with the same
``runtime`` and ``runtime-endpoint`` resources). A grant that names only the plain invoke lets the
runtime deploy green and then makes every tool discovery a 503.

The pin is on the synthesized template, not the source: the statement that grants the plain invoke
to the deployment Lambda's role must grant the for-user form in the same statement (same resources,
same conditions), and no other role acquires it by accident.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

PLAIN = "bedrock-agentcore:InvokeAgentRuntime"
FOR_USER = "bedrock-agentcore:InvokeAgentRuntimeForUser"


@pytest.fixture(scope="module")
def statements() -> list[tuple[str, dict]]:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "InvokeForUserGrantStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    tpl = Template.from_stack(stack).to_json()
    out: list[tuple[str, dict]] = []
    for lid, res in tpl["Resources"].items():
        if res["Type"] not in {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}:
            continue
        for st in res["Properties"]["PolicyDocument"]["Statement"]:
            out.append((lid, st))
    return out


def _actions(st: dict) -> list[str]:
    a = st.get("Action")
    return [a] if isinstance(a, str) else (a or [])


def test_the_deployment_lambda_statement_grants_both_invoke_forms_together(statements):
    deployment_plain = [
        (lid, st)
        for lid, st in statements
        if lid.startswith("DeploymentLambdaRole") and st.get("Effect") == "Allow" and PLAIN in _actions(st)
    ]
    assert deployment_plain, "the deployment Lambda's plain invoke grant was not found; the walk is broken"
    for lid, st in deployment_plain:
        assert FOR_USER in _actions(st), (
            f"{lid}: InvokeAgentRuntime is granted without InvokeAgentRuntimeForUser; the product MCP client "
            "sends runtimeUserId and every discovery will be AccessDenied"
        )


def test_the_for_user_form_never_outruns_the_plain_one(statements):
    """Same statement means same resources and conditions: the for-user grant can never be wider."""
    for lid, st in statements:
        if FOR_USER in _actions(st):
            assert PLAIN in _actions(st), f"{lid}: InvokeAgentRuntimeForUser granted without the plain invoke"


def test_only_roles_that_invoke_for_a_user_receive_the_action(statements):
    """The stream Lambda's invoke-only statement does not forward a user id; it stays plain."""
    holders = {lid.split("Overflow")[0].split("DefaultPolicy")[0] for lid, st in statements if FOR_USER in _actions(st)}
    assert holders == {"DeploymentLambdaRole"}, holders
