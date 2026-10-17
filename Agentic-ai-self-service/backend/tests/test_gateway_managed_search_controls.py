"""Least-privilege and synchronization controls for managed Gateway search."""

from fnmatch import fnmatchcase
from unittest.mock import MagicMock, call

import pytest
from app.services import gateway_deployer

REGION = "eu-west-1"
ACCOUNT = "111122223333"
GATEWAY_NAME = "search-gateway"
OWNER = "tenant:alpha"


def _statements(policy: dict) -> dict[str, dict]:
    return {statement["Sid"]: statement for statement in policy["Statement"]}


def _actions(policy: dict) -> set[str]:
    values: set[str] = set()
    for statement in policy["Statement"]:
        action = statement["Action"]
        values.update([action] if isinstance(action, str) else action)
    return values


def _resource_matches(statement: dict, resource: str) -> bool:
    configured = statement["Resource"]
    patterns = [configured] if isinstance(configured, str) else configured
    return any(fnmatchcase(resource, pattern) for pattern in patterns)


def _assert_managed_role_contract(policy: dict) -> None:
    statements = _statements(policy)
    assert set(statements) == {
        "GatewayInvocation",
        "PolicyAuthorization",
        "WorkloadIdentityToken",
        "OutboundCredentialTokens",
        "CredentialSecretRead",
        "InvokeGatewayToolLambdas",
        "ManagedWebSearch",
    }
    assert _actions(policy) == {
        "bedrock-agentcore:InvokeGateway",
        "bedrock-agentcore:GetPolicyEngine",
        "bedrock-agentcore:AuthorizeAction",
        "bedrock-agentcore:PartiallyAuthorizeActions",
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetResourceOauth2Token",
        "bedrock-agentcore:GetResourceApiKey",
        "secretsmanager:GetSecretValue",
        "lambda:InvokeFunction",
        "bedrock-agentcore:InvokeWebSearch",
    }
    assert all(not action.endswith("*") for action in _actions(policy))
    assert statements["GatewayInvocation"]["Resource"] == (
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{GATEWAY_NAME}-*"
    )
    assert statements["PolicyAuthorization"]["Resource"] == [
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:policy-engine/*",
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{GATEWAY_NAME}-*",
    ]
    assert statements["WorkloadIdentityToken"]["Resource"] == [
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:workload-identity-directory/default",
        (
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:workload-identity-directory/default/"
            f"workload-identity/{GATEWAY_NAME}-*"
        ),
    ]
    assert statements["CredentialSecretRead"]["Resource"] == (
        f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/tenant-alpha/*"
    )
    assert statements["InvokeGatewayToolLambdas"]["Resource"] == (
        f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:AgentCore*"
    )
    assert statements["ManagedWebSearch"] == {
        "Sid": "ManagedWebSearch",
        "Effect": "Allow",
        "Action": ["bedrock-agentcore:InvokeWebSearch"],
        "Resource": f"arn:aws:bedrock-agentcore:{REGION}:aws:tool/web-search.v1",
    }


def test_gateway_role_has_only_exact_runtime_actions_and_managed_search_resource():
    policy = gateway_deployer._build_gateway_role_policy(
        region=REGION,
        account_id=ACCOUNT,
        gateway_name=GATEWAY_NAME,
        managed_web_search=True,
        owner_sub=OWNER,
    )

    _assert_managed_role_contract(policy)

    search = _statements(policy)["ManagedWebSearch"]
    assert _resource_matches(search, f"arn:aws:bedrock-agentcore:{REGION}:aws:tool/web-search.v1")
    assert not _resource_matches(search, "arn:aws:bedrock-agentcore:us-east-1:aws:tool/web-search.v1")
    assert not _resource_matches(search, f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:tool/web-search.v1")
    assert not _resource_matches(search, f"arn:aws:bedrock-agentcore:{REGION}:aws:tool/web-search.v2")


def test_non_search_gateway_has_no_invoke_web_search_grant():
    policy = gateway_deployer._build_gateway_role_policy(
        region=REGION,
        account_id=ACCOUNT,
        gateway_name=GATEWAY_NAME,
        managed_web_search=False,
        owner_sub=OWNER,
    )

    assert "ManagedWebSearch" not in _statements(policy)
    assert "bedrock-agentcore:InvokeWebSearch" not in _actions(policy)
    assert "bedrock-agentcore:*" not in _actions(policy)
    assert not any(action.startswith("iam:") or action.startswith("bedrock:") for action in _actions(policy))
    assert {
        "secretsmanager:CreateSecret",
        "secretsmanager:PutSecretValue",
        "secretsmanager:DeleteSecret",
    }.isdisjoint(_actions(policy))


def test_role_arn_wrapper_rejects_missing_account_instead_of_widening_policy():
    with pytest.raises(ValueError, match="invalid Gateway role ARN"):
        gateway_deployer._gateway_role_policy_for(
            "not-an-arn",
            region=REGION,
            gateway_name=GATEWAY_NAME,
            managed_web_search=True,
            owner_sub=OWNER,
        )


@pytest.mark.parametrize("mutation", ["search", "workload", "oauth", "gateway"])
def test_role_permission_removal_mutations_break_the_contract(mutation: str):
    policy = gateway_deployer._build_gateway_role_policy(
        region=REGION,
        account_id=ACCOUNT,
        gateway_name=GATEWAY_NAME,
        managed_web_search=True,
        owner_sub=OWNER,
    )
    statements = _statements(policy)
    if mutation == "search":
        policy["Statement"].remove(statements["ManagedWebSearch"])
    elif mutation == "workload":
        statements["WorkloadIdentityToken"]["Action"].remove("bedrock-agentcore:GetWorkloadAccessToken")
    elif mutation == "oauth":
        statements["OutboundCredentialTokens"]["Action"].remove("bedrock-agentcore:GetResourceOauth2Token")
    else:
        statements["GatewayInvocation"]["Action"].remove("bedrock-agentcore:InvokeGateway")

    with pytest.raises(AssertionError):
        _assert_managed_role_contract(policy)


def _target(name: str, target_id: str, family: str, *, synced=None) -> dict:
    mcp: dict = {family: {}}
    if family == "lambda":
        mcp[family] = {"toolSchema": {"inlinePayload": [{"name": f"{name}_tool"}]}}
    detail = {
        "name": name,
        "targetId": target_id,
        "status": "READY",
        "targetConfiguration": {"mcp": mcp},
    }
    if synced is not None:
        detail["lastSynchronizedAt"] = synced
    return detail


def test_resolver_synchronizes_only_supported_crawled_target_ids(monkeypatch):
    client = MagicMock()
    summaries = {
        "items": [
            {"name": "Inline", "targetId": "lambda-id"},
            {"name": "Search", "targetId": "search-id"},
            {"name": "OpenApi", "targetId": "openapi-id"},
        ]
    }
    client.list_gateway_targets.side_effect = [summaries, summaries]
    pre = {
        "lambda-id": _target("Inline", "lambda-id", "lambda"),
        "search-id": _target("Search", "search-id", "connector", synced="old-search"),
        "openapi-id": _target("OpenApi", "openapi-id", "openApiSchema", synced="old-openapi"),
    }
    post = {
        "lambda-id": _target("Inline", "lambda-id", "lambda"),
        "search-id": _target("Search", "search-id", "connector", synced="new-search"),
        "openapi-id": _target("OpenApi", "openapi-id", "openApiSchema", synced="new-openapi"),
    }
    reads = [
        pre["lambda-id"],
        pre["search-id"],
        pre["openapi-id"],
        post["lambda-id"],
        post["search-id"],
        post["openapi-id"],
    ]
    client.get_gateway_target.side_effect = reads
    monkeypatch.setattr(gateway_deployer.time, "sleep", lambda _seconds: None)

    actions, expected = gateway_deployer._resolve_gateway_tool_actions(client, "gateway-id", timeout=1)

    client.synchronize_gateway_targets.assert_called_once_with(
        gatewayIdentifier="gateway-id",
        targetIdList=["openapi-id"],
    )
    assert actions == ["Inline___Inline_tool"]
    assert expected == 1
    assert client.get_gateway_target.call_args_list == [
        call(gatewayIdentifier="gateway-id", targetId="lambda-id"),
        call(gatewayIdentifier="gateway-id", targetId="search-id"),
        call(gatewayIdentifier="gateway-id", targetId="openapi-id"),
        call(gatewayIdentifier="gateway-id", targetId="lambda-id"),
        call(gatewayIdentifier="gateway-id", targetId="search-id"),
        call(gatewayIdentifier="gateway-id", targetId="openapi-id"),
    ]
