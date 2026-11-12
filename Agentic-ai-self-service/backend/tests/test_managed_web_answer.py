"""Contract tests for the managed AgentCore Gateway Web Answer target.

Web Answer is the second AWS-managed connector this platform routes, beside Web Search.
The contract under test (developer guide, "Add a Connector target with Web Answer",
read 2026-11-12):

* ``connectorId`` ``web-answer`` on the connector's current default version (no pin),
  one configuration named ``WebAnswer``, ``GATEWAY_IAM_ROLE`` outbound credentials;
* the Gateway role gets ``bedrock-agentcore:InvokeWebAnswer`` on the service-owned ARN
  ``arn:<partition>:bedrock-agentcore:<region>:aws:tool/web-answer.v1`` and nothing else;
* US East (N. Virginia) only: refused before any AWS side effect at the request model
  (explicit ``targetRegion`` -> 422), at the live route (resolved home Region -> 400),
  and inside the deployer; the Region is listed in ``unsupported_features`` everywhere
  else, including the "full" release Regions that still lack it;
* the CloudFormation export emits the same connector as a NATIVE GatewayTarget (no
  version to pin, so no custom resource), the same IAM statement, a Region rule, and a
  Cedar action ``AmazonWebAnswer___WebAnswer``.

Every refusal asserts the exact message so a wrong Region or feature name in the error
text fails rather than passing on "some 4xx".
"""

from __future__ import annotations

import inspect
from dataclasses import replace

import pytest
import yaml
from app import deployment_handler
from app.models.deployment_models import DeployRequest
from app.services import cfn_template_generator as export
from app.services import gateway_deployer
from app.services import managed_web_answer as answer
from app.services import region_capabilities as capabilities
from app.services.cfn_template_generator import CfnTemplateGenerator
from botocore.session import Session
from botocore.validate import validate_parameters
from fastapi import HTTPException
from pydantic import ValidationError

MODEL_ID = "us.anthropic.claude-sonnet-5"
REGION = "us-east-1"
ACCOUNT = "111122223333"


# --------------------------------------------------------------------------- Region contract


def test_web_answer_region_set_is_exactly_us_east_1() -> None:
    assert capabilities.WEB_ANSWER_SUPPORTED_REGIONS == frozenset({"us-east-1"})
    assert capabilities.web_answer_region_supported("us-east-1") is True


@pytest.mark.parametrize(
    "region", ["us-east-2", "us-west-2", "eu-west-1", "eu-central-1", "eu-west-2", "eu-west-3", ""]
)
def test_other_regions_are_not_inferred_to_have_web_answer(region: str) -> None:
    assert capabilities.web_answer_region_supported(region) is False
    profile = capabilities.region_feature_profile(region)
    assert profile.web_answer is False
    assert "Amazon Bedrock AgentCore Web Answer" in profile.unsupported_features


def test_us_east_1_lists_no_unsupported_feature() -> None:
    assert capabilities.region_feature_profile("us-east-1").unsupported_features == ()


def test_other_full_regions_now_name_only_web_answer_as_unsupported() -> None:
    for region in ("us-east-2", "us-west-2", "eu-west-1"):
        assert capabilities.region_feature_profile(region).unsupported_features == (
            "Amazon Bedrock AgentCore Web Answer",
        )


def test_reduced_regions_list_web_answer_between_search_and_registry() -> None:
    assert capabilities.region_feature_profile("eu-west-3").unsupported_features == (
        "Amazon Bedrock AgentCore Web Search",
        "Amazon Bedrock AgentCore Web Answer",
        "AWS Agent Registry federation",
    )


def test_unavailable_message_names_region_remedy_and_the_only_alternative() -> None:
    message = capabilities.feature_unavailable_message("web_answer", "eu-west-1")
    assert message.startswith("Amazon Bedrock AgentCore Web Answer is not available in Europe (Ireland) (eu-west-1).")
    assert "Remove the Web Answer tool" in message
    alternatives = message.split("supported release Regions:", 1)[1]
    assert "US East (N. Virginia) (us-east-1)" in alternatives
    assert "eu-west-1" not in alternatives


def test_require_region_refuses_with_the_exact_message_and_admits_us_east_1() -> None:
    with pytest.raises(ValueError) as error:
        answer.require_web_answer_region("us-west-2")
    assert str(error.value) == capabilities.feature_unavailable_message("web_answer", "us-west-2")
    answer.require_web_answer_region("us-east-1")


# --------------------------------------------------------------------------- target request


def test_target_request_is_accepted_by_the_installed_sdk_model_and_carries_no_version_pin() -> None:
    params = answer.managed_web_answer_target_params("gateway-123")
    model = Session().get_service_model("bedrock-agentcore-control")
    validate_parameters(params, model.operation_model("CreateGatewayTarget").input_shape)
    assert params["name"] == "AmazonWebAnswer"
    connector = params["targetConfiguration"]["mcp"]["connector"]
    assert connector == {
        "source": {"connectorId": "web-answer"},
        "configurations": [{"name": "WebAnswer", "parameterValues": {}}],
    }
    assert params["credentialProviderConfigurations"] == [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]


def test_gateway_id_is_required() -> None:
    with pytest.raises(ValueError, match="gateway_id is required"):
        answer.managed_web_answer_target_params("")


def test_web_answer_is_requested_only_by_its_explicit_tool_id() -> None:
    assert answer.managed_web_answer_requested(None, ["web_answer"])
    assert answer.managed_web_answer_requested("strands-gateway-agent", ["weather_api", "web_answer"])
    assert not answer.managed_web_answer_requested("web-search-agent", [])
    assert not answer.managed_web_answer_requested(None, ["web_search"])
    assert not answer.managed_web_answer_requested(None, [{"toolId": "web_answer"}])  # ids are strings here
    assert answer.without_managed_web_answer_tools(["web_answer", "weather_api"]) == ["weather_api"]


def test_resource_arn_is_service_owned_and_region_gated() -> None:
    assert (
        answer.managed_web_answer_resource_arn("us-east-1")
        == "arn:aws:bedrock-agentcore:us-east-1:aws:tool/web-answer.v1"
    )
    with pytest.raises(ValueError):
        answer.managed_web_answer_resource_arn("eu-west-1")


# --------------------------------------------------------------------------- gateway role


def _statements(policy: dict) -> dict[str, dict]:
    return {statement["Sid"]: statement for statement in policy["Statement"]}


def test_gateway_role_grants_invoke_web_answer_on_the_exact_service_arn_only_when_requested() -> None:
    with_answer = gateway_deployer._build_gateway_role_policy(
        region=REGION, account_id=ACCOUNT, gateway_name="answer-gateway", managed_web_answer=True, owner_sub="tenant:a"
    )
    statement = _statements(with_answer)["ManagedWebAnswer"]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == ["bedrock-agentcore:InvokeWebAnswer"]
    assert statement["Resource"] == f"arn:aws:bedrock-agentcore:{REGION}:aws:tool/web-answer.v1"
    assert "ManagedWebSearch" not in _statements(with_answer)

    without = gateway_deployer._build_gateway_role_policy(
        region=REGION, account_id=ACCOUNT, gateway_name="answer-gateway", owner_sub="tenant:a"
    )
    assert "ManagedWebAnswer" not in _statements(without)
    assert "bedrock:InvokeModel" not in {
        a for s in with_answer["Statement"] for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
    }


def test_role_policy_for_arn_forwards_the_flag_and_partition() -> None:
    policy = gateway_deployer._gateway_role_policy_for(
        f"arn:aws-us-gov:iam::{ACCOUNT}:role/AgentCoreGateway-x",
        region=REGION,
        gateway_name="answer-gateway",
        managed_web_search=False,
        managed_web_answer=True,
        owner_sub="tenant:a",
    )
    assert _statements(policy)["ManagedWebAnswer"]["Resource"] == (
        f"arn:aws-us-gov:bedrock-agentcore:{REGION}:aws:tool/web-answer.v1"
    )


def test_deployer_gates_region_and_strips_the_id_before_the_unknown_tool_preflight() -> None:
    source = inspect.getsource(gateway_deployer.deploy_gateway)
    gate = source.index("require_web_answer_region(region)")
    strip = source.index("gateway_tools = without_managed_web_answer_tools(gateway_tools)")
    preflight = source.index("Refusing to deploy gateway: unknown tool")
    client = source.index("agentcore_ctrl = _create_agentcore_control_client(region)")
    assert gate < strip < preflight < client
    target = source.index("MANAGED_WEB_ANSWER_TARGET_NAME,\n                web_answer_params,")
    assert target > source.index("MANAGED_WEB_SEARCH_TARGET_NAME,\n                web_search_params,")
    assert source.count("managed_web_answer=managed_web_answer,") == 2


# --------------------------------------------------------------------------- admission


def _request(**extra) -> DeployRequest:
    return DeployRequest(
        nodeId="web-answer",
        config={"name": "web_answer_agent", "model": {"modelId": MODEL_ID}},
        gatewayConfig={"name": "answer-gateway", "gateway_provider": "agentcore", "targetType": "lambda"},
        gatewayTools=["web_answer"],
        **extra,
    )


def test_request_model_refuses_an_explicit_unsupported_target_region() -> None:
    with pytest.raises(ValidationError) as error:
        _request(targetRegion="eu-west-1")
    assert "Amazon Bedrock AgentCore Web Answer is not available in Europe (Ireland) (eu-west-1)" in str(error.value)


def test_request_model_admits_us_east_1_and_leaves_an_omitted_region_to_the_route() -> None:
    assert _request(targetRegion="us-east-1").gateway_tools == ["web_answer"]
    assert _request().target_region is None


def test_request_model_does_not_gate_a_canvas_without_the_tool() -> None:
    request = DeployRequest(
        nodeId="plain", config={"name": "plain", "model": {"modelId": MODEL_ID}}, targetRegion="eu-west-1"
    )
    assert request.gateway_tools is None


def _home(monkeypatch, region: str) -> None:
    monkeypatch.setattr(deployment_handler, "config", replace(deployment_handler.config, aws_region=region))


@pytest.mark.parametrize("home", ["us-east-2", "us-west-2", "eu-west-1", "eu-central-1"])
def test_live_route_refuses_web_answer_outside_us_east_1_with_400(monkeypatch, home: str) -> None:
    _home(monkeypatch, home)
    with pytest.raises(HTTPException) as error:
        deployment_handler._reject_unsupported_web_answer_region(_request())
    assert error.value.status_code == 400
    assert str(error.value.detail) == capabilities.feature_unavailable_message("web_answer", home)


def test_live_route_admits_us_east_1_and_an_explicit_us_east_1_target(monkeypatch) -> None:
    _home(monkeypatch, "us-east-1")
    deployment_handler._reject_unsupported_web_answer_region(_request())
    _home(monkeypatch, "eu-west-1")
    deployment_handler._reject_unsupported_web_answer_region(_request(targetRegion="us-east-1"))


def test_live_route_gate_runs_after_web_search_and_before_any_write() -> None:
    source = open(deployment_handler.__file__, encoding="utf-8").read()
    gate = source.index("_reject_unsupported_web_answer_region(request)")
    assert source.index("_reject_unsupported_managed_web_search_region(request)") < gate
    assert gate < source.index("store = _get_state_store()\n    store.create(state)")


# --------------------------------------------------------------------------- CloudFormation export


def _export_template(**extra) -> dict:
    bundle = CfnTemplateGenerator().generate(_request(**extra))
    return yaml.safe_load(bundle.template_yaml)


def test_export_emits_a_native_connector_target_with_the_live_shape() -> None:
    template = _export_template()
    target = template["Resources"]["WebAnswerTarget"]
    assert target["Type"] == "AWS::BedrockAgentCore::GatewayTarget"
    assert target["Properties"] == {
        "GatewayIdentifier": {"Fn::GetAtt": ["AgentCoreGateway", "GatewayIdentifier"]},
        "Name": "AmazonWebAnswer",
        "Description": answer.MANAGED_WEB_ANSWER_DESCRIPTION,
        "TargetConfiguration": {
            "Mcp": {
                "Connector": {
                    "Source": {"ConnectorId": "web-answer"},
                    "Configurations": [{"Name": "WebAnswer", "ParameterValues": {}}],
                }
            }
        },
        "CredentialProviderConfigurations": [{"CredentialProviderType": "GATEWAY_IAM_ROLE"}],
    }
    # No custom resource and no version: parity with the live request.
    assert "ManagedWebSearchTarget" not in template["Resources"]
    assert "Version" not in str(target["Properties"]["TargetConfiguration"])


def test_export_gateway_role_gets_the_same_iam_statement_as_the_live_deployer() -> None:
    template = _export_template()
    statements = template["Resources"]["GatewayRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    statement = next(item for item in statements if item.get("Sid") == "ManagedWebAnswer")
    assert statement == {
        "Sid": "ManagedWebAnswer",
        "Effect": "Allow",
        "Action": ["bedrock-agentcore:InvokeWebAnswer"],
        "Resource": {"Fn::Sub": "arn:aws:bedrock-agentcore:${AWS::Region}:aws:tool/web-answer.v1"},
    }
    assert not any(item.get("Sid") == "ManagedWebSearch" for item in statements)


def test_export_asserts_the_region_rule_and_derives_the_cedar_action() -> None:
    template = _export_template()
    rule = template["Rules"]["WebAnswerRegion"]["Assertions"][0]
    assert rule["Assert"]["Fn::Contains"] == [["us-east-1"], {"Ref": "AWS::Region"}]
    assert rule["AssertDescription"] == "AgentCore Web Answer is available only in us-east-1."
    actions, unknowable = CfnTemplateGenerator._gateway_tool_actions(template)
    assert "AmazonWebAnswer___WebAnswer" in actions
    assert "AmazonWebAnswer" not in unknowable


def test_export_does_not_turn_the_id_into_a_lambda_tool() -> None:
    template = _export_template()
    for resource in template["Resources"].values():
        if resource["Type"] != "AWS::BedrockAgentCore::GatewayTarget":
            continue
        schemas = resource["Properties"].get("TargetConfiguration", {}).get("Mcp", {}).get("Lambda", {})
        assert "web_answer" not in str(schemas)


def test_export_without_the_tool_emits_none_of_it() -> None:
    bundle = CfnTemplateGenerator().generate(
        DeployRequest(
            nodeId="web-answer",
            config={"name": "web_answer_agent", "model": {"modelId": MODEL_ID}},
            gatewayConfig={"name": "answer-gateway", "gateway_provider": "agentcore", "targetType": "lambda"},
            gatewayTools=["weather_api"],
        )
    )
    template = yaml.safe_load(bundle.template_yaml)
    assert "WebAnswerTarget" not in template["Resources"]
    assert "WebAnswerRegion" not in template.get("Rules", {})
    statements = template["Resources"]["GatewayRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert not any(item.get("Sid") == "ManagedWebAnswer" for item in statements)


def test_export_region_gate_matches_the_live_route() -> None:
    with pytest.raises(ValidationError):
        _request(targetRegion="us-west-2")


def test_export_source_keeps_both_managed_ids_out_of_lambda_schemas() -> None:
    source = inspect.getsource(export.CfnTemplateGenerator._resolve_gateway_tools)
    assert "MANAGED_WEB_ANSWER_TOOL_IDS" in source and "MANAGED_WEB_SEARCH_TOOL_IDS" in source
