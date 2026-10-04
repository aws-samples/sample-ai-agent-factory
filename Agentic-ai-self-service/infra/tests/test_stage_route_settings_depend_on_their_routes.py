"""F-G03-007: every route the default stage's RouteSettings names must (a) exist as a synthesized
AWS::ApiGatewayV2::Route with exactly that RouteKey and (b) be a declared DependsOn of the stage.

Live failure 2026-09-25 (stack acfe2e-p0925, us-east-1): CloudFormation created the default Stage and the webhook
CfnRoute concurrently; API Gateway rejected the Stage with "Unable to find Route by key POST
/hooks/{runtime_name}/{trigger_id} within the provided RouteSettings" (404) and the whole create rolled back.
The route was never missing -- the Stage simply had no dependency edge to it (DependsOn null in the preflight
assembly). Removing the per-route throttling or asserting only that the route exists would not catch this.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

REGION, ACCOUNT = "us-east-1", "123456789012"

CONTEXTS = [
    pytest.param({}, id="defaults"),
    pytest.param({"rbac_enforce": "true"}, id="rbac_enforce"),
    pytest.param({"rbac_enforce": "false"}, id="rbac_open"),
    pytest.param(
        {
            "rbac_enforce": "true",
            "otel_endpoint": "https://otel.example",
            "otel_auth_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:x",
        },
        id="otel",
    ),
]


def _template(context: dict) -> dict:
    app = cdk.App(context=context)
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="t1",
        project_name="acfe2e",
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def _stages_and_routes(resources: dict):
    stages = {lid: r for lid, r in resources.items() if r["Type"] == "AWS::ApiGatewayV2::Stage"}
    routes = {lid: r for lid, r in resources.items() if r["Type"] == "AWS::ApiGatewayV2::Route"}
    return stages, routes


@pytest.mark.parametrize("context", CONTEXTS)
def test_every_route_settings_key_is_a_synthesized_route_key(context):
    resources = _template(context)["Resources"]
    stages, routes = _stages_and_routes(resources)
    route_keys = {r["Properties"]["RouteKey"] for r in routes.values()}
    named = [(lid, k) for lid, s in stages.items() for k in (s["Properties"].get("RouteSettings") or {})]
    assert named, "the default stage no longer names any per-route settings; this test measures nothing"
    missing = [(lid, k) for lid, k in named if k not in route_keys]
    assert not missing, f"RouteSettings keys with no matching AWS::ApiGatewayV2::Route RouteKey: {missing}"


@pytest.mark.parametrize("context", CONTEXTS)
def test_the_stage_depends_on_every_route_its_route_settings_name(context):
    resources = _template(context)["Resources"]
    stages, routes = _stages_and_routes(resources)
    by_key = {r["Properties"]["RouteKey"]: lid for lid, r in routes.items()}
    problems = []
    for lid, s in stages.items():
        depends = set(s.get("DependsOn") or [])
        for key in s["Properties"].get("RouteSettings") or {}:
            route_lid = by_key.get(key)
            if route_lid is None or route_lid not in depends:
                problems.append((lid, key, route_lid, sorted(depends)))
    assert not problems, (
        "the Stage names a route in RouteSettings without a DependsOn edge to that route's logical id; CloudFormation "
        f"then creates both concurrently and API Gateway rejects the Stage (404 Unable to find Route): {problems}"
    )
