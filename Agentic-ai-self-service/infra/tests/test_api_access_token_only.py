"""Every Cognito-protected HTTP API route accepts access tokens, never ID tokens.

API Gateway's JWT authorizer accepts a Cognito ID token when only issuer/audience
are configured. Requiring the Cognito access-token scope on each authenticated
route rejects ID tokens before Lambda because they carry no ``scope`` claim.
"""

import copy

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

ACCESS_TOKEN_SCOPE = "aws.cognito.signin.user.admin"
UNAUTHENTICATED_ROUTE_KEYS = {
    "GET /health",
    "POST /hooks/{runtime_name}/{trigger_id}",
}


def _synth() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    return Template.from_stack(stack).to_json()


@pytest.fixture(scope="module")
def template() -> dict:
    return _synth()


def _routes(template: dict) -> list[dict]:
    return [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::ApiGatewayV2::Route"
    ]


def access_token_contract_violations(template: dict) -> list[str]:
    violations = []
    for route in _routes(template):
        route_key = route["RouteKey"]
        if route_key in UNAUTHENTICATED_ROUTE_KEYS:
            if route.get("AuthorizationScopes"):
                violations.append(f"{route_key}: unauthenticated route has scopes")
            continue
        if route.get("AuthorizationType") != "JWT" or not route.get("AuthorizerId"):
            violations.append(f"{route_key}: missing JWT authorizer")
        if route.get("AuthorizationScopes") != [ACCESS_TOKEN_SCOPE]:
            violations.append(f"{route_key}: scopes={route.get('AuthorizationScopes')!r}")
    return sorted(violations)


def _route(template: dict, route_key: str) -> dict:
    return next(route for route in _routes(template) if route["RouteKey"] == route_key)


def test_every_authenticated_route_requires_the_cognito_access_token_scope(template):
    routes = _routes(template)
    assert len(routes) > 50
    assert {route["RouteKey"] for route in routes} >= UNAUTHENTICATED_ROUTE_KEYS
    assert access_token_contract_violations(template) == []


def test_mutation_removing_the_scope_is_caught(template):
    mutated = copy.deepcopy(template)
    _route(mutated, "GET /api/workflows").pop("AuthorizationScopes")
    assert access_token_contract_violations(mutated) == ["GET /api/workflows: scopes=None"]


def test_mutation_replacing_the_scope_is_caught(template):
    mutated = copy.deepcopy(template)
    _route(mutated, "POST /api/deploy")["AuthorizationScopes"] = ["openid"]
    assert access_token_contract_violations(mutated) == ["POST /api/deploy: scopes=['openid']"]


@pytest.mark.parametrize("route_key", sorted(UNAUTHENTICATED_ROUTE_KEYS))
def test_mutation_adding_scopes_to_an_unauthenticated_route_is_caught(template, route_key):
    mutated = copy.deepcopy(template)
    _route(mutated, route_key)["AuthorizationScopes"] = [ACCESS_TOKEN_SCOPE]
    assert access_token_contract_violations(mutated) == [f"{route_key}: unauthenticated route has scopes"]
