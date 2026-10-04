"""Caller Cedar actions must describe the tool plane the export actually emits.

The CloudFormation provider defaults to ``IGNORE_ALL_FINDINGS`` for documented,
live-proven reasons. That cannot mean a typo is allowed to become a green stack
whose ENFORCE policy names no real action. Lambda-backed targets are statically
knowable, so their action ids are validated exactly at export time. MCP-server
targets are intentionally different: their tool suffixes are discovered only
after the server is running, so an explicit action under that target remains
allowed.
"""

from __future__ import annotations

import pytest
import yaml
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import (
    CfnExportUnsupportedError,
    CfnTemplateGenerator,
)
from fastapi.testclient import TestClient

MODEL_ID = "us.anthropic.claude-sonnet-5"


def _statement(*actions: str, comment: str = "") -> str:
    action_list = ", ".join(f'AgentCore::Action::"{action}"' for action in actions)
    suffix = f" // {comment}" if comment else ""
    return (
        "permit(principal is AgentCore::OAuthUser, "
        f"action in [{action_list}], "
        'resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:'
        f'us-east-1:123456789012:gateway/example");{suffix}'
    )


def _request(
    statement: str,
    *,
    mcp_server: bool = False,
    naming_profile: dict | None = None,
) -> DeployRequest:
    kwargs = {
        "nodeId": "cedar-action-contract",
        "config": RuntimeConfig(
            name="Cedar Action Contract",
            model={"modelId": MODEL_ID},
        ),
        "connectedTools": ["gateway"],
        "gatewayConfig": {
            "gateway_provider": "agentcore",
            "targetType": "lambda",
        },
        "policyConfig": {
            "policies": [
                {
                    "name": "customer_policy",
                    "statement": statement,
                }
            ]
        },
    }
    if mcp_server:
        kwargs["mcpServerConfig"] = {"tools": ["get_customer"]}
    else:
        kwargs["gatewayTools"] = ["get_customer"]
    if naming_profile is not None:
        kwargs["namingProfile"] = naming_profile
    return DeployRequest(**kwargs)


def _generate(
    statement: str,
    *,
    mcp_server: bool = False,
    naming_profile: dict | None = None,
):
    return CfnTemplateGenerator().generate(
        _request(
            statement,
            mcp_server=mcp_server,
            naming_profile=naming_profile,
        )
    )


def _request_without_nameable_tools(*, runtime_discovered_mcp: bool = False):
    kwargs = {
        "nodeId": "cedar-action-contract",
        "config": RuntimeConfig(
            name="Cedar Action Contract",
            model={"modelId": MODEL_ID},
        ),
        "connectedTools": ["gateway"],
        "gatewayConfig": {
            "gateway_provider": "agentcore",
            "targetType": "lambda",
        },
        "policyConfig": {},
    }
    if runtime_discovered_mcp:
        kwargs.update(
            {
                "templateId": "mcp-server-gateway-target",
                "mcpServerConfig": {"tools": []},
                "knowledgeBaseConfig": {
                    "kbMode": "create_new",
                    "kbName": "cedar-action-contract-kb",
                    "embeddingModelId": "amazon.titan-embed-text-v2:0",
                    "vectorStoreType": "s3_vectors",
                    "dataSourceType": "s3",
                    "s3BucketUri": "s3://example-bucket/docs",
                },
            }
        )
    return DeployRequest(**kwargs)


def test_exact_lambda_backed_action_is_accepted():
    bundle = _generate(_statement("DynamicTools___get_customer"))

    assert 'AgentCore::Action::"DynamicTools___get_customer"' in bundle.template_yaml


def test_unconstrained_permit_is_an_actionable_export_refusal():
    with pytest.raises(
        CfnExportUnsupportedError,
        match="unconstrained action",
    ):
        _generate("permit(principal, action, resource);")


@pytest.mark.parametrize(
    "statement",
    [
        "// policy copied from an earlier canvas\npermit(principal, action, resource);",
        'permit(principal, action, resource); // action in [AgentCore::Action::"DynamicTools___get_customer"]',
    ],
)
def test_comments_cannot_hide_an_unconstrained_permit(statement):
    with pytest.raises(
        CfnExportUnsupportedError,
        match="unconstrained action",
    ):
        _generate(statement)


def test_cedar_whitespace_around_action_membership_is_accepted():
    statement = _statement("DynamicTools___get_customer").replace(
        "action in [",
        "action\n    in   [",
        1,
    )

    bundle = _generate(statement)

    template = yaml.safe_load(bundle.template_yaml)
    policy_statements = [
        resource["Properties"]["Statement"]
        for resource in template["Resources"].values()
        if resource.get("Type") == "Custom::AgentCorePolicy"
    ]
    assert policy_statements == [statement]


@pytest.mark.parametrize(
    ("runtime_discovered_mcp", "message"),
    [
        (False, "no gateway tool whose actions can be named"),
        (True, "discover their tools at runtime"),
    ],
)
def test_policy_without_statically_nameable_actions_is_an_actionable_refusal(
    runtime_discovered_mcp,
    message,
):
    with pytest.raises(
        CfnExportUnsupportedError,
        match=message,
    ):
        CfnTemplateGenerator().generate(
            _request_without_nameable_tools(
                runtime_discovered_mcp=runtime_discovered_mcp,
            )
        )


@pytest.mark.parametrize(
    "invalid_action",
    [
        "DynamicTools___get_order",
        "WrongTarget___get_customer",
        "WrongTarget___does_not_exist",
    ],
)
def test_lambda_backed_policy_refuses_an_action_the_template_does_not_serve(
    invalid_action,
):
    with pytest.raises(
        CfnExportUnsupportedError,
        match=invalid_action,
    ) as exc:
        _generate(
            _statement(
                "DynamicTools___get_customer",
                invalid_action,
            )
        )

    message = str(exc.value)
    assert "DynamicTools___get_customer" in message
    assert "not served" in message.lower() or "not emitted" in message.lower()


def test_runtime_discovered_mcp_target_can_name_a_tool_not_known_at_export_time():
    bundle = _generate(
        _statement("MCPServerRuntime___customer_lookup"),
        mcp_server=True,
    )

    assert 'AgentCore::Action::"MCPServerRuntime___customer_lookup"' in (bundle.template_yaml)


@pytest.mark.parametrize(
    "invalid_action",
    [
        "MCPServerRuntime",
        "MCPServerRuntime___",
    ],
)
def test_runtime_discovered_mcp_action_still_requires_a_tool_suffix(invalid_action):
    with pytest.raises(
        CfnExportUnsupportedError,
        match=invalid_action,
    ):
        _generate(
            _statement(invalid_action),
            mcp_server=True,
        )


def test_unqualified_action_reference_cannot_bypass_served_tool_validation():
    statement = _statement("WrongTarget___get_customer").replace(
        "AgentCore::Action::",
        "Action::",
    )

    with pytest.raises(
        CfnExportUnsupportedError,
        match="WrongTarget___get_customer",
    ):
        _generate(statement)


def test_action_syntax_inside_a_comment_is_not_validated_or_rewritten():
    comment = 'historical example AgentCore::Action::"WrongTarget___not_a_tool"'
    bundle = _generate(
        _statement(
            "DynamicTools___get_customer",
            comment=comment,
        )
    )

    assert comment in bundle.template_yaml


def test_naming_profile_does_not_rewrite_action_syntax_inside_a_comment():
    comment = 'example AgentCore::Action::"DynamicTools___get_customer"'
    bundle = _generate(
        _statement(
            "DynamicTools___get_customer",
            comment=comment,
        ),
        naming_profile={"prefix": "ecb"},
    )

    assert comment in bundle.template_yaml


def test_historical_action_syntax_inside_a_comment_is_not_migrated():
    comment = 'historical example AgentCore::Action::"CustomerSupportTools___get_customer"'
    bundle = _generate(
        _statement(
            "DynamicTools___get_customer",
            comment=comment,
        )
    )

    assert comment in bundle.template_yaml


def test_explicit_forbid_refuses_an_action_the_template_does_not_serve():
    statement = _statement("WrongTarget___get_customer").replace(
        "permit(",
        "forbid(",
        1,
    )

    with pytest.raises(
        CfnExportUnsupportedError,
        match="WrongTarget___get_customer",
    ):
        _generate(statement)


def test_broad_forbid_without_an_explicit_action_stays_allowed():
    statement = "forbid(principal, action, resource);"

    bundle = _generate(statement)

    assert statement in bundle.template_yaml


def test_api_returns_the_actionable_refusal_instead_of_an_opaque_500():
    import app.deployment_handler as deployment_handler

    request = _request(
        _statement(
            "DynamicTools___get_customer",
            "WrongTarget___does_not_exist",
        )
    )
    response = TestClient(
        deployment_handler.deployment_app,
        raise_server_exceptions=False,
    ).post(
        "/api/generate-cfn-template",
        json=request.model_dump(mode="json", by_alias=True),
    )

    assert response.status_code == 400, response.text[:500]
    detail = str(response.json().get("detail", ""))
    assert "WrongTarget___does_not_exist" in detail
    assert "Internal server error" not in detail
