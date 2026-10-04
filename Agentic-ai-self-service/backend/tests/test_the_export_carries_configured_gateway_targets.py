"""A gateway's configured targets reach the CloudFormation export: Lambda ones as targets, others refused.

The platform deploys every entry of ``gatewayConfig.targets`` (gateway_deployer._deploy_config_targets).
The export never read the list. A canvas whose gateway served a Lambda by ARN exported a stack whose
gateway lacked that target, with a 200; a Lambda-only gateway exported with no target at all and a
log warning. Measured 2026-10-02 by generating the export of such a request and searching the
template for the ARN. The live matrix exported two gallery canvases this way (strands-gateway-agent
and customer-support-assistant, whose gateway the UI requires to serve something, so refusing would
have left them unexportable from the UI). Lambda targets are now exported the way the platform deploys
them; an OpenAPI spec or a Smithy model, which the export has no emitter for, is refused by name.
"""

from __future__ import annotations

import pytest
import yaml
from app.models.deployment_models import DeployRequest
from app.services.cfn_template_generator import CfnExportUnsupportedError, CfnTemplateGenerator
from app.services.gateway_deployer import _default_lambda_tool_schema

ARN = "arn:aws:lambda:us-east-1:111122223333:function:customer-order-tool"
ARN_B = "arn:aws:lambda:us-east-1:111122223333:function:second-tool:live"
SPEC = "https://example.invalid/openapi.json"


def _request(gateway_config: dict, **extra) -> DeployRequest:
    return DeployRequest.model_validate(
        {
            "nodeId": "gateway-targets",
            "config": {
                "name": "gateway_targets",
                "model": {"modelId": "us.anthropic.claude-sonnet-5"},
                "systemPrompt": "You are helpful.",
            },
            "connectedTools": ["gateway"],
            "gatewayTools": ["get_order"],
            "gatewayConfig": {"name": "orders", **gateway_config},
            **extra,
        }
    )


def _template(gateway_config: dict, **extra) -> dict:
    bundle = CfnTemplateGenerator().generate(_request(gateway_config, **extra))
    return yaml.load(bundle.template_yaml, Loader=yaml.BaseLoader)


def _targets(template: dict) -> dict:
    return {
        lid: r["Properties"]
        for lid, r in template["Resources"].items()
        if r["Type"] == "AWS::BedrockAgentCore::GatewayTarget" and lid.startswith("ConfiguredLambdaTarget")
    }


def _gateway_statements(template: dict) -> list[dict]:
    statements = []
    for policy in template["Resources"]["GatewayRole"]["Properties"]["Policies"]:
        if "PolicyDocument" not in policy:  # an Fn::If-wrapped optional policy
            branch = (policy.get("Fn::If") or [None, {}])[1]
            policy = branch if isinstance(branch, dict) else {}
        statements += (policy.get("PolicyDocument") or {}).get("Statement", [])
    return statements


def test_a_configured_lambda_target_is_exported_as_the_platform_deploys_it():
    template = _template({"targets": [{"type": "lambda", "functionArn": ARN}]})

    assert template["Parameters"]["ConfiguredLambdaTarget0Arn"]["Default"] == ARN
    props = _targets(template)["ConfiguredLambdaTarget0"]
    assert props["Name"] == "cfgtgt-lambda-0", "the platform's target name, so action ids match"
    lam = props["TargetConfiguration"]["Mcp"]["Lambda"]
    assert lam["LambdaArn"] == {"Ref": "ConfiguredLambdaTarget0Arn"}
    expected = _default_lambda_tool_schema(ARN)["inlinePayload"][0]
    assert [t["Name"] for t in lam["ToolSchema"]["InlinePayload"]] == [expected["name"]]
    assert lam["ToolSchema"]["InlinePayload"][0]["Description"] == expected["description"]
    assert props["CredentialProviderConfigurations"] == [{"CredentialProviderType": "GATEWAY_IAM_ROLE"}]


def test_the_gateway_role_may_invoke_exactly_the_configured_function():
    template = _template({"targets": [{"type": "lambda", "functionArn": ARN}]})

    grants = [s for s in _gateway_statements(template) if s.get("Sid") == "ConfiguredLambdaTargetInvoke"]
    assert len(grants) == 1
    assert grants[0]["Action"] == ["lambda:InvokeFunction"]
    assert grants[0]["Resource"] == [{"Ref": "ConfiguredLambdaTarget0Arn"}]
    permissions = [r for r in template["Resources"].values() if r["Type"] == "AWS::Lambda::Permission"]
    assert all(ARN not in str(p) and "ConfiguredLambdaTarget0Arn" not in str(p) for p in permissions), (
        "the stack must not write into the resource policy of a function it does not own"
    )


def test_each_target_keeps_its_index_and_the_tools_canvas_nodes_still_export():
    template = _template(
        {
            "targets": [
                {"type": "lambda", "functionArn": ARN},
                {"type": "mcp_server", "serverId": "aws-knowledge"},
                {"type": "lambda", "functionArn": ARN_B},
            ]
        }
    )

    targets = _targets(template)
    assert sorted(targets) == ["ConfiguredLambdaTarget0", "ConfiguredLambdaTarget2"]
    assert targets["ConfiguredLambdaTarget2"]["Name"] == "cfgtgt-lambda-2"
    assert template["Parameters"]["ConfiguredLambdaTarget2Arn"]["Default"] == ARN_B
    assert "get_order" in str(template), "the gateway's own canvas tools are exported beside the configured ones"


def test_the_default_policy_permits_the_configured_tool():
    template = _template({"targets": [{"type": "lambda", "functionArn": ARN}]}, policyConfig={"enabled": True})

    tool = _default_lambda_tool_schema(ARN)["inlinePayload"][0]["name"]
    policies = [r for r in template["Resources"].values() if r["Type"] == "Custom::AgentCorePolicy"]
    assert policies, "a policy node with a gateway must emit its Cedar policy"
    assert f"cfgtgt-lambda-0___{tool}" in str(policies), "under ENFORCE an unnamed tool is denied"


def test_an_inline_tool_schema_is_exported_and_a_staged_one_refused():
    inline = {
        "inlinePayload": [
            {
                "name": "lookup_order",
                "description": "Look an order up.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
            }
        ]
    }
    template = _template({"targets": [{"type": "lambda", "functionArn": ARN, "toolSchema": inline}]})
    payload = _targets(template)["ConfiguredLambdaTarget0"]["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]
    assert payload["InlinePayload"][0]["Name"] == "lookup_order"
    assert payload["InlinePayload"][0]["InputSchema"]["Required"] == ["order_id"]

    with pytest.raises(CfnExportUnsupportedError) as raised:
        _template({"targets": [{"type": "lambda", "functionArn": ARN, "toolSchema": {"s3": {"uri": "s3://b/k"}}}]})
    assert "lambda target #0" in str(raised.value)


@pytest.mark.parametrize(
    ("target", "named"),
    [
        ({"type": "openapi", "specUrl": SPEC, "specContent": ""}, SPEC),
        ({"type": "smithy", "modelName": "dynamodb"}, "dynamodb"),
        ({"type": "lambda", "functionArn": "not-an-arn"}, "not-an-arn"),
    ],
    ids=["openapi", "smithy", "malformed-arn"],
)
def test_a_target_the_export_cannot_express_is_refused_by_name(target, named):
    with pytest.raises(CfnExportUnsupportedError) as raised:
        _template({"targets": [{"type": "lambda", "functionArn": ARN}, target]})

    message = str(raised.value)
    assert "configured targets" in message and "#1" in message
    assert named in message, f"the caller cannot tell which target to remove: {message}"


def test_the_row_a_new_gateway_starts_with_is_not_a_target():
    template = _template(
        {
            "targets": [{"type": "lambda", "functionArn": ""}],
            "targetType": "lambda",
            "targetConfig": {"type": "lambda", "functionArn": ""},
        }
    )

    assert _targets(template) == {}
    assert not [p for p in template["Parameters"] if p.startswith("ConfiguredLambdaTarget")]


def test_the_ui_copy_of_the_first_target_is_not_deployed_twice():
    """The UI also sends ``targetConfig``, a copy of targets[0]. The platform deploys ``targets`` only."""
    template = _template(
        {
            "targets": [{"type": "lambda", "functionArn": ARN}],
            "targetType": "lambda",
            "targetConfig": {"type": "lambda", "functionArn": ARN},
        }
    )

    assert sorted(_targets(template)) == ["ConfiguredLambdaTarget0"]


def test_a_litellm_gateway_deploys_no_agentcore_target():
    """The platform's LiteLLM path deploys no AgentCore target, so the export neither emits nor refuses one."""
    template = _template(
        {
            "gatewayProvider": "litellm",
            "litellmBaseUrl": "https://litellm.example.invalid",
            "litellmServers": ["github"],
            "litellmApiKeySecretArn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:litellm-key-AbCdEf",
            "targets": [{"type": "lambda", "functionArn": ARN}, {"type": "openapi", "specUrl": SPEC}],
        }
    )

    assert _targets(template) == {}
    assert "AWS::BedrockAgentCore::Gateway" not in str(template)


def test_the_naming_pass_renames_the_target_and_keeps_its_grant():
    """The naming pass rewrites the LambdaInvoke statement's resources to the stack's own functions;
    the configured target's grant is its own statement so it survives."""
    template = _template(
        {"targets": [{"type": "lambda", "functionArn": ARN}]},
        namingProfile={"prefix": "ecb", "resourceNames": {"gatewayTarget": "{prefix}-{deployment}-{component}"}},
    )

    props = _targets(template)["ConfiguredLambdaTarget0"]
    assert props["Name"] != "cfgtgt-lambda-0", "the profile renames every gateway target"
    grants = [s for s in _gateway_statements(template) if s.get("Sid") == "ConfiguredLambdaTargetInvoke"]
    assert grants and grants[0]["Resource"] == [{"Ref": "ConfiguredLambdaTarget0Arn"}]
