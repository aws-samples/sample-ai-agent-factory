"""Least-privilege and synchronization controls for managed Gateway search."""

import json
from fnmatch import fnmatchcase
from io import BytesIO
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
    elif family == "openApiSchema":
        mcp[family] = {
            "inlinePayload": json.dumps(
                {
                    "openapi": "3.0.1",
                    "paths": {"/thing": {"get": {"operationId": "getThing"}}},
                }
            )
        }
    elif family == "smithyModel":
        mcp[family] = {
            "inlinePayload": json.dumps(
                {
                    "smithy": "2.0",
                    "shapes": {
                        "example#Service": {
                            "type": "service",
                            "operations": [{"target": "example#PutThing"}],
                        },
                        "example#PutThing": {"type": "operation"},
                    },
                }
            )
        }
    detail = {
        "name": name,
        "targetId": target_id,
        "status": "READY",
        "targetConfiguration": {"mcp": mcp},
    }
    if synced is not None:
        detail["lastSynchronizedAt"] = synced
    return detail


def test_resolver_synchronizes_only_dynamic_mcp_target_ids(monkeypatch):
    client = MagicMock()
    summaries = {
        "items": [
            {"name": "Inline", "targetId": "lambda-id"},
            {"name": "Search", "targetId": "search-id"},
            {"name": "OpenApi", "targetId": "openapi-id"},
            {"name": "Smithy", "targetId": "smithy-id"},
            {"name": "ExternalMcp", "targetId": "mcp-id"},
        ]
    }
    client.list_gateway_targets.side_effect = [summaries, summaries]
    pre = {
        "lambda-id": _target("Inline", "lambda-id", "lambda"),
        "search-id": _target("Search", "search-id", "connector", synced="old-search"),
        "openapi-id": _target("OpenApi", "openapi-id", "openApiSchema", synced="old-openapi"),
        "smithy-id": _target("Smithy", "smithy-id", "smithyModel", synced="old-smithy"),
        "mcp-id": _target("ExternalMcp", "mcp-id", "mcpServer", synced="old-mcp"),
    }
    post = {
        **pre,
        "mcp-id": _target("ExternalMcp", "mcp-id", "mcpServer", synced="new-mcp"),
    }
    client.get_gateway_target.side_effect = [
        pre["lambda-id"],
        pre["search-id"],
        pre["openapi-id"],
        pre["smithy-id"],
        pre["mcp-id"],
        post["lambda-id"],
        post["search-id"],
        post["openapi-id"],
        post["smithy-id"],
        post["mcp-id"],
    ]
    monkeypatch.setattr(gateway_deployer.time, "sleep", lambda _seconds: None)

    actions, expected = gateway_deployer._resolve_gateway_tool_actions(client, "gateway-id", timeout=1)

    client.synchronize_gateway_targets.assert_called_once_with(
        gatewayIdentifier="gateway-id",
        targetIdList=["mcp-id"],
    )
    assert actions == ["Inline___Inline_tool", "OpenApi___getThing", "Smithy___PutThing"]
    assert expected == 3
    assert client.get_gateway_target.call_args_list == [
        call(gatewayIdentifier="gateway-id", targetId="lambda-id"),
        call(gatewayIdentifier="gateway-id", targetId="search-id"),
        call(gatewayIdentifier="gateway-id", targetId="openapi-id"),
        call(gatewayIdentifier="gateway-id", targetId="smithy-id"),
        call(gatewayIdentifier="gateway-id", targetId="mcp-id"),
        call(gatewayIdentifier="gateway-id", targetId="lambda-id"),
        call(gatewayIdentifier="gateway-id", targetId="search-id"),
        call(gatewayIdentifier="gateway-id", targetId="openapi-id"),
        call(gatewayIdentifier="gateway-id", targetId="smithy-id"),
        call(gatewayIdentifier="gateway-id", targetId="mcp-id"),
    ]


def test_static_openapi_missing_operation_id_fails_before_dynamic_sync(monkeypatch):
    client = MagicMock()
    client.list_gateway_targets.return_value = {
        "items": [
            {"name": "OpenApi", "targetId": "openapi-id"},
            {"name": "ExternalMcp", "targetId": "mcp-id"},
        ]
    }
    invalid = _target("OpenApi", "openapi-id", "openApiSchema")
    invalid["targetConfiguration"]["mcp"]["openApiSchema"]["inlinePayload"] = json.dumps(
        {"openapi": "3.0.1", "paths": {"/thing": {"get": {"responses": {}}}}}
    )
    client.get_gateway_target.return_value = invalid

    with pytest.raises(RuntimeError, match="1 operation.*without operationId"):
        gateway_deployer._resolve_gateway_tool_actions(client, "gateway-id", timeout=1)

    client.synchronize_gateway_targets.assert_not_called()


def test_static_action_derivation_removal_mutation_fails_closed(monkeypatch):
    client = MagicMock()
    client.list_gateway_targets.return_value = {"items": [{"name": "OpenApi", "targetId": "openapi-id"}]}
    client.get_gateway_target.return_value = _target("OpenApi", "openapi-id", "openApiSchema")
    monkeypatch.setattr(gateway_deployer, "_static_schema_tool_names", lambda *_args, **_kwargs: [])

    with pytest.raises(RuntimeError, match="static target .* exposes no named tools"):
        gateway_deployer._resolve_gateway_tool_actions(client, "gateway-id", timeout=1)

    client.synchronize_gateway_targets.assert_not_called()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"openapi": "3.0.1", "paths": {}},
            "exposes no named tools",
        ),
        (
            {
                "openapi": "3.0.1",
                "paths": {
                    "/one": {"get": {"operationId": "same"}},
                    "/two": {"post": {"operationId": "same"}},
                },
            },
            "duplicate tool names",
        ),
        (
            {
                "openapi": "3.0.1",
                "paths": {"/one": {"get": {"operationId": "not/valid"}}},
            },
            "invalid tool name",
        ),
    ],
    ids=["empty", "duplicate", "invalid-name"],
)
def test_static_openapi_action_manifest_rejects_unsafe_shapes(payload, message):
    detail = {"targetConfiguration": {"mcp": {"openApiSchema": {"inlinePayload": json.dumps(payload)}}}}

    with pytest.raises(RuntimeError, match=message):
        gateway_deployer._static_schema_tool_names(detail, "OpenApi")


def test_staged_openapi_action_manifest_is_owner_pinned(monkeypatch):
    payload = json.dumps(
        {
            "openapi": "3.0.1",
            "paths": {"/whoami": {"get": {"operationId": "whoami"}}},
        }
    ).encode()
    s3 = MagicMock()
    s3.get_object.return_value = {
        "ContentLength": len(payload),
        "Body": BytesIO(payload),
    }
    monkeypatch.setattr(gateway_deployer, "_aws_client", lambda service, **_kwargs: s3)
    detail = {
        "targetConfiguration": {
            "mcp": {
                "openApiSchema": {
                    "s3": {
                        "uri": "s3://agentcore-flows-artifacts-111122223333-eu-west-1/connector-specs/spec.json",
                        "bucketOwnerAccountId": "111122223333",
                    }
                }
            }
        }
    }

    assert gateway_deployer._static_schema_tool_names(
        detail,
        "OpenApi",
        region="eu-west-1",
    ) == ["whoami"]
    s3.get_object.assert_called_once_with(
        Bucket="agentcore-flows-artifacts-111122223333-eu-west-1",
        Key="connector-specs/spec.json",
        ExpectedBucketOwner="111122223333",
    )


def test_static_schema_s3_without_an_owner_is_rejected_before_read(monkeypatch):
    s3 = MagicMock()
    monkeypatch.setattr(gateway_deployer, "_aws_client", lambda service, **_kwargs: s3)
    detail = {
        "targetConfiguration": {
            "mcp": {"openApiSchema": {"s3": {"uri": "s3://agentcore-flows-artifacts-111122223333-eu-west-1/spec.json"}}}
        }
    }

    with pytest.raises(RuntimeError, match="owner is absent or invalid"):
        gateway_deployer._static_schema_tool_names(detail, "OpenApi", region="eu-west-1")

    s3.get_object.assert_not_called()


def test_static_openapi_yaml_operation_ids_are_supported():
    detail = {
        "targetConfiguration": {
            "mcp": {
                "openApiSchema": {
                    "inlinePayload": """openapi: 3.0.1
paths:
  /whoami:
    get:
      operationId: whoami
"""
                }
            }
        }
    }

    assert gateway_deployer._static_schema_tool_names(detail, "OpenApi") == ["whoami"]


@pytest.mark.parametrize(
    "payload",
    [
        '{"openapi":"3.0.1","paths":{},"paths":{}}',
        "openapi: 3.0.1\npaths: {}\npaths: {}\n",
    ],
    ids=["json", "yaml"],
)
def test_static_schema_duplicate_mapping_keys_are_rejected(payload):
    detail = {"targetConfiguration": {"mcp": {"openApiSchema": {"inlinePayload": payload}}}}

    with pytest.raises(RuntimeError, match="duplicate key"):
        gateway_deployer._static_schema_tool_names(detail, "OpenApi")


def test_staged_schema_owner_falls_back_to_the_scoped_sts_identity(monkeypatch):
    payload = b'{"openapi":"3.0.1","paths":{"/x":{"get":{"operationId":"getX"}}}}'
    s3 = MagicMock()
    s3.get_object.return_value = {"ContentLength": len(payload), "Body": BytesIO(payload)}
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": ACCOUNT}
    monkeypatch.setattr(
        gateway_deployer,
        "_aws_client",
        lambda service, **_kwargs: sts if service == "sts" else s3,
    )
    detail = {
        "targetConfiguration": {
            "mcp": {"openApiSchema": {"s3": {"uri": f"s3://agentcore-flows-artifacts-{ACCOUNT}-eu-west-1/spec.json"}}}
        }
    }

    assert gateway_deployer._static_schema_tool_names(detail, "OpenApi", region=REGION) == ["getX"]
    sts.get_caller_identity.assert_called_once_with()
    s3.get_object.assert_called_once_with(
        Bucket=f"agentcore-flows-artifacts-{ACCOUNT}-eu-west-1",
        Key="spec.json",
        ExpectedBucketOwner=ACCOUNT,
    )


def test_staged_schema_outside_the_bound_artifact_bucket_is_rejected(monkeypatch):
    session = MagicMock()
    detail = {
        "targetConfiguration": {
            "mcp": {
                "openApiSchema": {
                    "s3": {
                        "uri": "s3://foreign-bucket/spec.json",
                        "bucketOwnerAccountId": ACCOUNT,
                    }
                }
            }
        }
    }

    with gateway_deployer.gateway_aws_session(
        session,
        artifact_bucket=f"agentcore-flows-artifacts-{ACCOUNT}-eu-west-1",
        expected_bucket_owner=ACCOUNT,
    ):
        with pytest.raises(RuntimeError, match="bucket differs"):
            gateway_deployer._static_schema_tool_names(detail, "OpenApi", region=REGION)

    session.client.assert_not_called()


def test_staged_schema_over_ten_mib_is_rejected_before_body_read(monkeypatch):
    body = MagicMock()
    s3 = MagicMock()
    s3.get_object.return_value = {
        "ContentLength": gateway_deployer._STATIC_SCHEMA_MAX_BYTES + 1,
        "Body": body,
    }
    monkeypatch.setattr(gateway_deployer, "_aws_client", lambda service, **_kwargs: s3)
    detail = {
        "targetConfiguration": {
            "mcp": {
                "openApiSchema": {
                    "s3": {
                        "uri": f"s3://agentcore-flows-artifacts-{ACCOUNT}-eu-west-1/spec.json",
                        "bucketOwnerAccountId": ACCOUNT,
                    }
                }
            }
        }
    }

    with pytest.raises(RuntimeError, match="exceeds the 10 MiB read limit"):
        gateway_deployer._static_schema_tool_names(detail, "OpenApi", region=REGION)

    body.read.assert_not_called()
