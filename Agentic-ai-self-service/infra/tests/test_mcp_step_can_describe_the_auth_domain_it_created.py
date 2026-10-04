"""The MCP step waits for the auth domain it created to be ACTIVE; that wait is a describe call.

Measured live on 2026-09-28 (matrix run 14, deployment 8a23c209): the step created the Cognito pool
and hosted domain, then polled ``DescribeUserPoolDomain`` for nine minutes -- every attempt
``AccessDeniedException`` -- and failed the deployment with "did not become ACTIVE and resolvable
before the deployment deadline (last Cognito status unknown)". No step role granted the action.
The service reference lists ``cognito-idp:DescribeUserPoolDomain`` with NO resource type, so the
grant must be ``Resource: "*"`` (as ``CreateUserPool`` already is) and no condition key applies.

Pinned on the synthesized template: the gateway and MCP-server step roles hold the action on "*",
and no other step role acquires it.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

DESCRIBE_DOMAIN = "cognito-idp:DescribeUserPoolDomain"
STEP_ROLES_WITH_DOMAINS = ("StepGatewayRole", "StepMcpServerRole")


@pytest.fixture(scope="module")
def template_json() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "DescribeDomainGrantStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    return Template.from_stack(stack).to_json()


def _actions(st: dict) -> set[str]:
    a = st.get("Action") or []
    return {a} if isinstance(a, str) else set(a)


def _role_statements(template_json: dict, role_prefix: str) -> list[dict]:
    resources = template_json["Resources"]
    role_ids = [lid for lid, r in resources.items() if r["Type"] == "AWS::IAM::Role" and lid.startswith(role_prefix)]
    assert len(role_ids) == 1, role_ids
    role_id = role_ids[0]
    out: list[dict] = []
    for policy in resources[role_id]["Properties"].get("Policies", []) or []:
        out.extend(policy["PolicyDocument"].get("Statement", []) or [])
    for r in resources.values():
        if r["Type"] not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            continue
        roles = r["Properties"].get("Roles", []) or []
        if any(isinstance(x, dict) and x.get("Ref") == role_id for x in roles):
            out.extend(r["Properties"]["PolicyDocument"].get("Statement", []) or [])
    return out


@pytest.mark.parametrize("role_prefix", STEP_ROLES_WITH_DOMAINS)
def test_the_step_that_creates_an_auth_domain_may_describe_it(template_json, role_prefix):
    grants = [
        st
        for st in _role_statements(template_json, role_prefix)
        if st.get("Effect") == "Allow" and DESCRIBE_DOMAIN in _actions(st)
    ]
    assert grants, f"{role_prefix} cannot describe the auth domain it creates"
    for st in grants:
        resource = st.get("Resource")
        assert resource == "*" or resource == ["*"], (
            "DescribeUserPoolDomain supports no resource type; a narrower Resource silently denies it"
        )


def test_no_other_step_role_acquires_the_describe_grant(template_json):
    resources = template_json["Resources"]
    other_step_roles = [
        lid
        for lid, r in resources.items()
        if r["Type"] == "AWS::IAM::Role"
        and lid.startswith("Step")
        and lid.endswith(("Role", "Role" + lid.split("Role")[-1]))
        and not lid.startswith(STEP_ROLES_WITH_DOMAINS)
    ]
    assert other_step_roles, "no other step roles found; the walk is broken"
    for lid in other_step_roles:
        prefix = lid.split("Role")[0] + "Role"
        for st in _role_statements(template_json, prefix):
            assert DESCRIBE_DOMAIN not in _actions(st), f"{lid} acquired {DESCRIBE_DOMAIN} by accident"
