"""Contract tests for Gateway guardrails: Bedrock Guardrails evaluated by Policy in AgentCore.

The contract under test (developer guide, "Guardrails in policies" and "Guardrail policy
examples", Bedrock user guide "Set up permissions" for InvokeGuardrailChecks, read 2026-11-13):

* a policy node's ``guardrails.profile`` (``off`` | ``standard`` | ``strict``) adds, next to the
  platform's ``allow_permitted_tools`` permit, one ``forbid … when guardrails {…}`` per permitted
  tool and guardrail type over the tool's REQUIRED string arguments (optional ones ride along);
* those statements are Dogwood and go out as ``definition.policy.statement``; the permit keeps
  ``definition.cedar``; both are accepted by the installed SDK model;
* the Gateway role gains exactly ``bedrock:InvokeGuardrailChecks`` on ``*`` (resourceless API)
  and only when a profile asked for it;
* Region-gated and fail-closed: refused at the request model for an explicit ``targetRegion``
  (422), at the live route for the resolved home Region (400), inside the deployer before any
  client exists, and again in the policy step; Ireland, Frankfurt and Paris are release Regions
  WITHOUT it;
* ``strict`` refuses a tool it cannot screen (served-only target, no required string argument),
  at admission when that is certain and in the policy step against the live gateway;
* the CloudFormation export refuses a profile other than ``off`` with the remedy.

Every refusal asserts the exact message so a wrong Region, profile or tool name in the error
text fails rather than passing on "some 4xx".
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from app import deployment_handler
from app.models.deployment_models import DeployRequest
from app.services import gateway_deployer
from app.services import gateway_guardrails as guardrails
from app.services import policy_lifecycle as lifecycle
from app.services import region_capabilities as capabilities
from app.services.cfn_template_generator import CfnExportUnsupportedError, CfnTemplateGenerator
from app.services.gateway_deployer import GATEWAY_TOOL_SCHEMAS
from app.step_handlers import policy_step
from botocore.session import Session
from botocore.validate import validate_parameters
from fastapi import HTTPException
from pydantic import ValidationError

from tests.gateway_fakes import applying_updates

MODEL_ID = "us.anthropic.claude-sonnet-5"
REGION = "us-east-1"
ACCOUNT = "111122223333"
GATEWAY_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/strands-gateway-agent-abc1234567"
TARGET = "CustomerSupportTools"
REFUND = f"{TARGET}___process_refund"
WEATHER = f"{TARGET}___get_weather"
ORDERS = f"{TARGET}___list_orders"
WEB_SEARCH = "AmazonWebSearch___WebSearch"

PLATFORM_SCHEMAS = {f"{TARGET}___{s['name']}": s["inputSchema"] for s in GATEWAY_TOOL_SCHEMAS.values()}

STANDARD_REFUND_STATEMENT = (
    'forbid(principal, action == AgentCore::Action::"CustomerSupportTools___process_refund", '
    f'resource == AgentCore::Gateway::"{GATEWAY_ARN}") '
    'when guardrails { BedrockGuardrails::PromptAttack(["PROMPT_INJECTION", "JAILBREAK"], '
    "[context.input.order_id, context.input.reason])"
    '.maxConfidenceScore().greaterThan(decimal("0.4")) };'
)
STRICT_REFUND_CONTENT_STATEMENT = (
    'forbid(principal, action == AgentCore::Action::"CustomerSupportTools___process_refund", '
    f'resource == AgentCore::Gateway::"{GATEWAY_ARN}") '
    'when guardrails { BedrockGuardrails::ContentFilter(["HATE", "INSULTS", "MISCONDUCT", "SEXUAL", "VIOLENCE"], '
    "[context.input.order_id, context.input.reason])"
    '.maxConfidenceScore().greaterThan(decimal("0.2")) };'
)


# --------------------------------------------------------------------------- Region contract


def test_region_set_is_exactly_the_documented_seven() -> None:
    assert capabilities.GATEWAY_GUARDRAILS_SUPPORTED_REGIONS == frozenset(
        {"ap-northeast-1", "ap-southeast-2", "eu-north-1", "eu-west-2", "us-east-1", "us-east-2", "us-west-2"}
    )


@pytest.mark.parametrize("region", ["us-east-1", "us-east-2", "us-west-2", "eu-west-2"])
def test_release_regions_with_guardrails_do_not_list_them_as_unsupported(region: str) -> None:
    assert capabilities.gateway_guardrails_region_supported(region) is True
    profile = capabilities.region_feature_profile(region)
    assert profile.gateway_guardrails is True
    assert "Amazon Bedrock Guardrails in AgentCore Policy" not in profile.unsupported_features
    capabilities.require_gateway_guardrails_region(region)


@pytest.mark.parametrize("region", ["eu-west-1", "eu-central-1", "eu-west-3", "us-west-1", "ca-central-1", ""])
def test_other_regions_are_not_inferred_to_have_guardrails(region: str) -> None:
    assert capabilities.gateway_guardrails_region_supported(region) is False
    profile = capabilities.region_feature_profile(region)
    assert profile.gateway_guardrails is False
    assert "Amazon Bedrock Guardrails in AgentCore Policy" in profile.unsupported_features


def test_unsupported_feature_order_puts_guardrails_after_web_answer_and_before_registry() -> None:
    assert capabilities.region_feature_profile("eu-west-1").unsupported_features == (
        "Amazon Bedrock AgentCore Web Answer",
        "Amazon Bedrock Guardrails in AgentCore Policy",
    )
    assert capabilities.region_feature_profile("eu-west-3").unsupported_features == (
        "Amazon Bedrock AgentCore Web Search",
        "Amazon Bedrock AgentCore Web Answer",
        "Amazon Bedrock Guardrails in AgentCore Policy",
        "AWS Agent Registry federation",
    )
    assert capabilities.region_feature_profile("us-east-1").unsupported_features == ()


def test_unavailable_message_names_region_remedy_and_the_release_alternatives_only() -> None:
    message = capabilities.feature_unavailable_message("gateway_guardrails", "eu-west-1")
    assert message.startswith(
        "Amazon Bedrock Guardrails in AgentCore Policy are not available in Europe (Ireland) (eu-west-1). "
        "Set the policy node's Gateway guardrails profile to off (Cedar tool authorization still applies), "
        "or deploy it in one of these supported release Regions: "
    )
    alternatives = message.split("supported release Regions: ")[1]
    assert alternatives == (
        "Europe (London) (eu-west-2), US East (N. Virginia) (us-east-1), US East (Ohio) (us-east-2), "
        "US West (Oregon) (us-west-2)."
    )
    assert "eu-north-1" not in alternatives and "ap-" not in alternatives
    with pytest.raises(ValueError, match="Europe \\(Frankfurt\\) \\(eu-central-1\\)"):
        capabilities.require_gateway_guardrails_region("eu-central-1")


# --------------------------------------------------------------------------- profile parsing


@pytest.mark.parametrize(
    "policy_config, expected",
    [
        (None, "off"),
        ({}, "off"),
        ({"enabled": True}, "off"),
        ({"guardrails": None}, "off"),
        ({"guardrails": {}}, "off"),
        ({"guardrails": {"profile": None}}, "off"),
        ({"guardrails": {"profile": ""}}, "off"),
        ({"guardrails": {"profile": "off"}}, "off"),
        ({"guardrails": {"profile": "standard"}}, "standard"),
        ({"guardrails": {"profile": "strict"}}, "strict"),
        ({"guardrails": "standard"}, "standard"),
    ],
)
def test_profile_parsing(policy_config, expected: str) -> None:
    assert guardrails.gateway_guardrails_profile(policy_config) == expected
    assert guardrails.gateway_guardrails_requested(policy_config) is (expected != "off")


@pytest.mark.parametrize("bad", ["Standard", "STRICT", "paranoid", 1, True, ["standard"]])
def test_unknown_profile_values_are_contract_errors_not_silently_off(bad) -> None:
    with pytest.raises(guardrails.GatewayGuardrailsContractError, match="Unknown Gateway guardrails profile"):
        guardrails.gateway_guardrails_profile({"guardrails": {"profile": bad}})


def test_a_profile_on_a_disabled_engine_is_refused_not_dropped() -> None:
    guardrails.require_policy_engine_for_guardrails({"enabled": True, "guardrails": {"profile": "strict"}})
    guardrails.require_policy_engine_for_guardrails({"enabled": False})
    with pytest.raises(guardrails.GatewayGuardrailsContractError) as error:
        guardrails.require_policy_engine_for_guardrails({"enabled": False, "guardrails": {"profile": "standard"}})
    assert str(error.value) == (
        "Gateway guardrails need the policy engine: policyConfig.enabled is false while "
        "policyConfig.guardrails.profile is 'standard'. Enable the policy engine or set the profile to off."
    )


# --------------------------------------------------------------------------- schema -> data paths


def test_screenable_fields_are_top_level_string_properties_split_by_required() -> None:
    assert guardrails.screenable_input_fields(PLATFORM_SCHEMAS[REFUND]) == (("order_id", "reason"), ())
    assert guardrails.screenable_input_fields(PLATFORM_SCHEMAS[ORDERS]) == (("customer_id",), ())  # limit: integer
    assert guardrails.screenable_input_fields(PLATFORM_SCHEMAS[WEATHER]) == (("location",), ())
    schema = {
        "type": "object",
        "properties": {
            "note": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "meta": {"type": "object"},
            "count": {"type": "integer"},
            "x-ray": {"type": "string"},
            "title": {"type": "string"},
        },
        "required": ["title", "tags", "x-ray"],
    }
    assert guardrails.screenable_input_fields(schema) == (("title",), ("note",))
    assert guardrails.screenable_input_fields(None) == ((), ())
    assert guardrails.screenable_input_fields({"type": "object"}) == ((), ())


def test_every_platform_tool_has_a_required_string_argument_to_screen() -> None:
    for qualified, schema in PLATFORM_SCHEMAS.items():
        required, _optional = guardrails.screenable_input_fields(schema)
        assert required, f"{qualified} would be unscreened"


# --------------------------------------------------------------------------- statements


def test_standard_statement_matches_the_documented_mcp_tool_shape() -> None:
    statement = guardrails.guardrail_forbid_statement(
        REFUND,
        GATEWAY_ARN,
        ("order_id", "reason"),
        guardrail_type="PromptAttack",
        categories=guardrails.PROMPT_ATTACK_CATEGORIES,
        threshold=guardrails.PROMPT_ATTACK_THRESHOLD,
    )
    assert statement == STANDARD_REFUND_STATEMENT
    assert guardrails.PROMPT_ATTACK_THRESHOLD == "0.4" and guardrails.CONTENT_FILTER_THRESHOLD == "0.2"
    assert guardrails.PROMPT_ATTACK_CATEGORIES == ("PROMPT_INJECTION", "JAILBREAK")
    assert guardrails.CONTENT_FILTER_CATEGORIES == ("HATE", "INSULTS", "MISCONDUCT", "SEXUAL", "VIOLENCE")


@pytest.mark.parametrize(
    "tool, arn, fields, match",
    [
        ('T___x" || true', GATEWAY_ARN, ("a",), "Not a <Target>___<tool> action id"),
        ("process_refund", GATEWAY_ARN, ("a",), "Not a <Target>___<tool> action id"),
        (REFUND, "", ("a",), "must name the exact gateway ARN"),
        (REFUND, "arn:aws:bedrock-agentcore:us-east-1:123:gateway/x", ("a",), "must name the exact gateway ARN"),
        (REFUND, GATEWAY_ARN, (), "must be Cedar identifiers"),
        (REFUND, GATEWAY_ARN, ("ok", "not-ok"), "must be Cedar identifiers"),
    ],
)
def test_statement_builder_refuses_anything_it_cannot_embed_safely(tool, arn, fields, match) -> None:
    with pytest.raises(guardrails.GatewayGuardrailsContractError, match=match):
        guardrails.guardrail_forbid_statement(
            tool, arn, fields, guardrail_type="PromptAttack", categories=("JAILBREAK",), threshold="0.4"
        )


def test_dogwood_statements_go_out_as_definition_policy_and_plain_cedar_as_definition_cedar() -> None:
    permit = f'permit(principal is AgentCore::OAuthUser, action in [AgentCore::Action::"{REFUND}"], resource == AgentCore::Gateway::"{GATEWAY_ARN}");'
    assert lifecycle.policy_definition(permit) == {"cedar": {"statement": permit}}
    assert lifecycle.policy_definition(STANDARD_REFUND_STATEMENT) == {
        "policy": {"statement": STANDARD_REFUND_STATEMENT}
    }
    suppress = (
        f'suppressOutput(principal, action == AgentCore::Action::"{REFUND}", '
        f'resource == AgentCore::Gateway::"{GATEWAY_ARN}") '
        'when { BedrockGuardrails::SensitiveInformation(["EMAIL"], [context.output.text]).count() > 0 };'
    )
    assert lifecycle.statement_needs_policy_definition(suppress) is True
    assert lifecycle.statement_needs_policy_definition("permit(principal, action, resource) when temporal { true };")
    assert lifecycle.statement_needs_policy_definition("") is False
    model = Session().get_service_model("bedrock-agentcore-control")
    for statement in (permit, STANDARD_REFUND_STATEMENT):
        validate_parameters(
            {
                "policyEngineId": "engine-1234567890",
                "name": "gr_pa_process_ref_2b81",
                "description": "x",
                "definition": lifecycle.policy_definition(statement),
                "validationMode": "IGNORE_ALL_FINDINGS",
            },
            model.operation_model("CreatePolicy").input_shape,
        )


def test_live_statement_reads_whichever_definition_member_the_service_returns() -> None:
    assert lifecycle.live_statement({"definition": {"cedar": {"statement": "permit(a, b, c);"}}}) == "permit(a, b, c);"
    assert lifecycle.live_statement({"definition": {"policy": {"statement": STANDARD_REFUND_STATEMENT}}}) == (
        STANDARD_REFUND_STATEMENT
    )
    assert lifecycle.live_statement({"definition": {}}) == "" and lifecycle.live_statement(None) == ""


def test_reconcile_sends_a_dogwood_statement_through_the_policy_member() -> None:
    ctrl = MagicMock()
    ctrl.get_policy.side_effect = [
        {"status": "ACTIVE", "definition": {"policy": {"statement": "forbid(principal, action, resource);"}}},
        {"status": "ACTIVE", "definition": {"policy": {"statement": STANDARD_REFUND_STATEMENT}}},
    ]
    outcome = lifecycle.reconcile_policy_definition(ctrl, "eng", "pid", STANDARD_REFUND_STATEMENT, "d", attempts=1)
    assert outcome["updated"] is True
    assert ctrl.update_policy.call_args.kwargs["definition"] == {"policy": {"statement": STANDARD_REFUND_STATEMENT}}


# --------------------------------------------------------------------------- planner


def test_off_plans_nothing() -> None:
    plan = guardrails.build_gateway_guardrail_plan(
        "off", region="eu-west-1", gateway_arn="", permitted_tools=[REFUND], tool_input_schemas={}
    )
    assert plan == guardrails.GatewayGuardrailPlan.off()
    assert plan.as_result() == {"profile": "off", "policy_names": [], "screened_tools": [], "unscreened_tools": {}}


def test_standard_emits_one_prompt_attack_forbid_per_screenable_tool_and_reports_the_rest() -> None:
    plan = guardrails.build_gateway_guardrail_plan(
        "standard",
        region=REGION,
        gateway_arn=GATEWAY_ARN,
        permitted_tools=[REFUND, WEATHER, WEB_SEARCH, f"{TARGET}___opt_only"],
        tool_input_schemas={
            **PLATFORM_SCHEMAS,
            f"{TARGET}___opt_only": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    )
    assert [p["name"] for p in plan.policies] == ["gr_pa_process_ref_2b81", "gr_pa_get_weather_5127"]
    assert plan.policies[0]["statement"] == STANDARD_REFUND_STATEMENT
    assert plan.policies[0]["description"] == (
        "Gateway guardrails (standard): deny CustomerSupportTools___process_refund when Bedrock Guardrails "
        "PromptAttack scores PROMPT_INJECTION/JAILBREAK above 0.4 on order_id, reason."
    )
    assert plan.screened_tools == (REFUND, WEATHER)
    assert plan.unscreened_tools == {
        WEB_SEARCH: guardrails.UNSCREENED_NO_SCHEMA,
        f"{TARGET}___opt_only": guardrails.UNSCREENED_NO_REQUIRED_STRING,
    }
    assert all(len(p["name"]) <= 22 for p in plan.policies)


def test_optional_string_arguments_ride_along_only_once_a_required_one_anchors_the_call() -> None:
    schema = {
        "type": "object",
        "properties": {"note": {"type": "string"}, "subject": {"type": "string"}, "n": {"type": "integer"}},
        "required": ["subject"],
    }
    plan = guardrails.build_gateway_guardrail_plan(
        "standard",
        region=REGION,
        gateway_arn=GATEWAY_ARN,
        permitted_tools=["T___mail"],
        tool_input_schemas={"T___mail": schema},
    )
    assert "[context.input.subject, context.input.note])" in plan.policies[0]["statement"]
    assert "context.input.n," not in plan.policies[0]["statement"]
    assert "context.input.n]" not in plan.policies[0]["statement"]


def test_strict_adds_a_content_filter_forbid_and_refuses_an_unscreenable_tool() -> None:
    plan = guardrails.build_gateway_guardrail_plan(
        "strict", region=REGION, gateway_arn=GATEWAY_ARN, permitted_tools=[REFUND], tool_input_schemas=PLATFORM_SCHEMAS
    )
    assert [p["name"] for p in plan.policies] == ["gr_pa_process_ref_2b81", "gr_cf_process_ref_2b81"]
    assert plan.policies[1]["statement"] == STRICT_REFUND_CONTENT_STATEMENT
    with pytest.raises(guardrails.GatewayGuardrailsUnscreenedToolsError) as error:
        guardrails.build_gateway_guardrail_plan(
            "strict",
            region=REGION,
            gateway_arn=GATEWAY_ARN,
            permitted_tools=[REFUND, WEB_SEARCH],
            tool_input_schemas=PLATFORM_SCHEMAS,
        )
    assert str(error.value) == (
        "Gateway guardrails profile 'strict' requires every permitted tool to be screened, but these cannot be: "
        f"{WEB_SEARCH} ({guardrails.UNSCREENED_NO_SCHEMA}). Use profile 'standard' (screens what it can and "
        "reports the rest), remove those tools, or give each a required string argument."
    )


def test_planner_fails_closed_on_region_arn_and_profile() -> None:
    with pytest.raises(ValueError, match="not available in Europe \\(Ireland\\) \\(eu-west-1\\)"):
        guardrails.build_gateway_guardrail_plan(
            "standard",
            region="eu-west-1",
            gateway_arn=GATEWAY_ARN,
            permitted_tools=[REFUND],
            tool_input_schemas=PLATFORM_SCHEMAS,
        )
    with pytest.raises(guardrails.GatewayGuardrailsContractError, match="need the gateway ARN"):
        guardrails.build_gateway_guardrail_plan(
            "standard", region=REGION, gateway_arn="", permitted_tools=[REFUND], tool_input_schemas=PLATFORM_SCHEMAS
        )
    with pytest.raises(guardrails.GatewayGuardrailsContractError, match="Unknown Gateway guardrails profile"):
        guardrails.build_gateway_guardrail_plan(
            "lenient",
            region=REGION,
            gateway_arn=GATEWAY_ARN,
            permitted_tools=[REFUND],
            tool_input_schemas=PLATFORM_SCHEMAS,
        )


def test_policy_names_are_short_readable_and_distinct_across_targets_with_the_same_tool() -> None:
    a = guardrails.guardrail_policy_name("gr_pa", "TargetA___get_weather")
    b = guardrails.guardrail_policy_name("gr_pa", "TargetB___get_weather")
    assert a != b and a.startswith("gr_pa_get_weather_") and len(a) == 22
    assert guardrails.guardrail_policy_name("gr_cf", "T___a-very-long-tool-name-indeed") == (
        "gr_cf_a_very_long_" + guardrails.guardrail_policy_name("gr_cf", "T___a-very-long-tool-name-indeed")[-4:]
    )


# --------------------------------------------------------------------------- schema reader


def _ctrl_with_inline_target(*, extra_targets: list[dict] | None = None) -> MagicMock:
    ctrl = MagicMock()
    ctrl.list_gateway_targets.return_value = {
        "items": [{"name": TARGET, "targetId": "tgt-1"}, *(extra_targets or [])],
    }

    def _get_target(gatewayIdentifier, targetId):
        if targetId == "tgt-1":
            return {
                "targetConfiguration": {
                    "mcp": {
                        "lambda": {
                            "toolSchema": {
                                "inlinePayload": [
                                    GATEWAY_TOOL_SCHEMAS["process_refund"],
                                    GATEWAY_TOOL_SCHEMAS["weather_api"],
                                ]
                            }
                        }
                    }
                }
            }
        return {"targetConfiguration": {"mcp": {"connector": {"source": {"connectorId": "web-search"}}}}}

    ctrl.get_gateway_target.side_effect = _get_target
    return ctrl


def test_schema_reader_returns_inline_lambda_tools_only_and_raises_on_a_read_failure() -> None:
    ctrl = _ctrl_with_inline_target(extra_targets=[{"name": "AmazonWebSearch", "gatewayTargetId": "tgt-2"}])
    schemas = guardrails.read_gateway_tool_input_schemas(ctrl, "gw-1")
    assert set(schemas) == {REFUND, WEATHER}
    assert schemas[REFUND] == GATEWAY_TOOL_SCHEMAS["process_refund"]["inputSchema"]
    ctrl.get_gateway_target.side_effect = RuntimeError("AccessDenied")
    with pytest.raises(RuntimeError, match="AccessDenied"):
        guardrails.read_gateway_tool_input_schemas(ctrl, "gw-1")


# --------------------------------------------------------------------------- policy step


def _step_ctrl() -> MagicMock:
    ctrl = _ctrl_with_inline_target(extra_targets=[{"name": "AmazonWebSearch", "targetId": "tgt-2"}])
    ctrl.list_policy_engines.return_value = {"policyEngines": []}
    ctrl.create_policy_engine.return_value = {
        "policyEngineId": "eng-1",
        "policyEngineArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:policy-engine/eng-1",
    }
    ctrl.get_policy_engine.return_value = {"status": "ACTIVE"}
    counter = {"n": 0}

    def _create_policy(**kw):
        counter["n"] += 1
        return {"policyId": f"p{counter['n']}"}

    ctrl.create_policy.side_effect = _create_policy
    ctrl.get_policy.return_value = {"status": "ACTIVE"}
    ctrl.list_policies.return_value = {"policies": [{"name": "x", "status": "ACTIVE"}]}
    ctrl.get_gateway.return_value = {
        "name": "gw",
        "roleArn": "arn:role",
        "protocolType": "MCP",
        "authorizerType": "CUSTOM_JWT",
        "status": "READY",
    }
    return applying_updates(ctrl)


def _step_event(profile: str | None, *, tools=(REFUND, WEATHER, WEB_SEARCH), region: str = REGION) -> dict:
    pc = {"enabled": True, "mode": "ENFORCE", "name": "GrSpike"}
    if profile is not None:
        pc["guardrails"] = {"profile": profile}
    return {
        "deployment_id": "d1",
        "target_region": region,
        "policy_config": pc,
        "gateway_result": {
            "gateway_id": "strands-gateway-agent-abc1234567",
            "gateway_arn": GATEWAY_ARN,
            "qualified_tools": list(tools),
            "expected_tool_count": len(tools),
        },
    }


def _run_step(event: dict) -> tuple[dict, MagicMock]:
    ctrl = _step_ctrl()
    with (
        patch.object(policy_step, "_get_deployment_store", return_value=MagicMock()),
        patch.object(policy_step, "step_clients") as sc,
        patch("time.sleep"),
    ):
        sc.client.return_value = ctrl
        out = policy_step.handler(event, None)
    return out, ctrl


def _created(ctrl: MagicMock) -> dict[str, dict]:
    return {call.kwargs["name"]: call.kwargs for call in ctrl.create_policy.call_args_list}


def test_step_writes_guardrail_forbids_as_dogwood_next_to_the_cedar_permit() -> None:
    out, ctrl = _run_step(_step_event("standard"))
    created = _created(ctrl)
    assert list(created) == [
        "GrSpike_allow_permitted_tools",
        "GrSpike_gr_pa_process_ref_2b81",
        "GrSpike_gr_pa_get_weather_5127",
    ]
    assert all(len(name) <= 48 for name in created)
    assert "cedar" in created["GrSpike_allow_permitted_tools"]["definition"]
    assert created["GrSpike_gr_pa_process_ref_2b81"]["definition"] == {
        "policy": {"statement": STANDARD_REFUND_STATEMENT}
    }
    assert all(kw["validationMode"] == "IGNORE_ALL_FINDINGS" for kw in created.values())
    receipt = out["policy_result"]["gateway_guardrails"]
    assert receipt == {
        "profile": "standard",
        "policy_names": ["gr_pa_process_ref_2b81", "gr_pa_get_weather_5127"],
        "screened_tools": [REFUND, WEATHER],
        "unscreened_tools": {WEB_SEARCH: guardrails.UNSCREENED_NO_SCHEMA},
    }
    # The guardrail policies are managed children like the permit: desired, recorded, promotable.
    assert {p["name"] for p in out["policy_result"]["desired_policies"]} == set(created)
    assert {c["name"] for c in out["policy_result"]["policy_children"]} == set(created)
    assert out["policy_result"]["mode"] == "ENFORCE"


def test_step_off_is_byte_for_byte_the_previous_behaviour() -> None:
    for event in (_step_event(None), _step_event("off")):
        out, ctrl = _run_step(event)
        assert list(_created(ctrl)) == ["GrSpike_allow_permitted_tools"]
        ctrl.list_gateway_targets.assert_not_called()
        ctrl.get_gateway_target.assert_not_called()
        assert out["policy_result"]["gateway_guardrails"] == {
            "profile": "off",
            "policy_names": [],
            "screened_tools": [],
            "unscreened_tools": {},
        }


def test_step_strict_fails_closed_on_a_served_only_tool_before_creating_any_policy() -> None:
    with pytest.raises(guardrails.GatewayGuardrailsUnscreenedToolsError, match=WEB_SEARCH):
        _run_step(_step_event("strict"))


def test_step_strict_without_served_only_tools_writes_both_forbids_per_tool() -> None:
    out, ctrl = _run_step(_step_event("strict", tools=(REFUND, WEATHER)))
    assert list(_created(ctrl)) == [
        "GrSpike_allow_permitted_tools",
        "GrSpike_gr_pa_process_ref_2b81",
        "GrSpike_gr_cf_process_ref_2b81",
        "GrSpike_gr_pa_get_weather_5127",
        "GrSpike_gr_cf_get_weather_5127",
    ]
    assert out["policy_result"]["gateway_guardrails"]["unscreened_tools"] == {}


def test_step_refuses_an_unsupported_region_before_reading_the_gateway() -> None:
    ctrl = _step_ctrl()
    with (
        patch.object(policy_step, "_get_deployment_store", return_value=MagicMock()),
        patch.object(policy_step, "step_clients") as sc,
        patch("time.sleep"),
        pytest.raises(ValueError, match="Europe \\(Ireland\\) \\(eu-west-1\\)"),
    ):
        sc.client.return_value = ctrl
        policy_step.handler(_step_event("standard", region="eu-west-1"), None)
    ctrl.list_gateway_targets.assert_not_called()
    ctrl.create_policy.assert_not_called()


def test_step_refuses_guardrails_when_no_tool_action_can_be_named() -> None:
    event = _step_event("standard", tools=())
    event["policy_config"]["mode"] = "LOG_ONLY"
    ctrl = _step_ctrl()
    ctrl.list_gateway_targets.return_value = {"items": []}
    with (
        patch.object(policy_step, "_get_deployment_store", return_value=MagicMock()),
        patch.object(policy_step, "step_clients") as sc,
        patch("time.sleep"),
        pytest.raises(RuntimeError, match="no readable tools, so no guardrail policy can name an action"),
    ):
        sc.client.return_value = ctrl
        policy_step.handler(event, None)


# --------------------------------------------------------------------------- Gateway role


def _statements(policy: dict) -> dict[str, dict]:
    return {s["Sid"]: s for s in policy["Statement"]}


def test_gateway_role_grants_invoke_guardrail_checks_resourceless_only_when_requested() -> None:
    with_guardrails = gateway_deployer._build_gateway_role_policy(
        region=REGION, account_id=ACCOUNT, gateway_name="gr-gateway", gateway_guardrails=True, owner_sub="tenant:a"
    )
    assert _statements(with_guardrails)["PolicyGuardrailChecks"] == {
        "Sid": "PolicyGuardrailChecks",
        "Effect": "Allow",
        "Action": ["bedrock:InvokeGuardrailChecks"],
        "Resource": "*",
    }
    without = gateway_deployer._build_gateway_role_policy(
        region=REGION, account_id=ACCOUNT, gateway_name="gr-gateway", owner_sub="tenant:a"
    )
    assert "PolicyGuardrailChecks" not in _statements(without)
    # The only wildcard-resource statement, and the only bedrock:* action, on the role.
    wildcard = [s["Sid"] for s in with_guardrails["Statement"] if s["Resource"] == "*"]
    assert wildcard == ["PolicyGuardrailChecks"]
    actions = {a for s in with_guardrails["Statement"] for a in s["Action"]}
    assert {a for a in actions if a.startswith("bedrock:")} == {"bedrock:InvokeGuardrailChecks"}


def test_role_policy_for_arn_forwards_the_flag() -> None:
    policy = gateway_deployer._gateway_role_policy_for(
        f"arn:aws:iam::{ACCOUNT}:role/AgentCoreGateway-x",
        region=REGION,
        gateway_name="gr-gateway",
        managed_web_search=False,
        owner_sub="tenant:a",
        gateway_guardrails=True,
    )
    assert "PolicyGuardrailChecks" in _statements(policy)


def test_deployer_gates_region_before_any_client_and_threads_the_flag_to_both_role_writes() -> None:
    source = inspect.getsource(gateway_deployer.deploy_gateway)
    gate = source.index("require_gateway_guardrails_region(region)")
    assert source.index("require_web_answer_region(region)") < gate
    assert gate < source.index("Refusing to deploy gateway: unknown tool")
    assert gate < source.index("agentcore_ctrl = _create_agentcore_control_client(region)")
    assert source.count("gateway_guardrails=gateway_guardrails,") == 2


def test_deployer_refuses_an_unsupported_region_with_no_side_effect_and_only_when_flagged() -> None:
    """Behavioural twin of the source-order check: Ireland + flag -> refused before any client."""
    with (
        patch.object(gateway_deployer, "_create_agentcore_control_client") as make_client,
        patch.object(gateway_deployer, "boto3") as boto,
    ):
        refused = gateway_deployer.deploy_gateway(
            {"name": "gr-gateway"}, "eu-west-1", gateway_tools=["weather_api"], gateway_guardrails=True
        )
    assert refused["success"] is False
    assert refused["refused_before_side_effects"] is True
    assert capabilities.feature_unavailable_message("gateway_guardrails", "eu-west-1") in str(refused["error"])
    make_client.assert_not_called()
    boto.client.assert_not_called()
    # Without the flag the same call gets past the gate (and fails later on the stubbed clients
    # for an unrelated reason); the gate must not fire for a deploy that asked for nothing.
    with (
        patch.object(gateway_deployer, "_create_agentcore_control_client", side_effect=RuntimeError("past-gate")),
        patch.object(gateway_deployer, "boto3"),
    ):
        unflagged = gateway_deployer.deploy_gateway({"name": "gr-gateway"}, "eu-west-1", gateway_tools=["weather_api"])
    assert "Guardrails in AgentCore Policy" not in str(unflagged.get("error", ""))


def test_gateway_step_derives_the_flag_from_the_same_policy_config_the_policy_step_reads() -> None:
    from app.step_handlers import gateway_step

    source = inspect.getsource(gateway_step)
    assert 'gateway_guardrails_requested(event.get("policy_config") or {})' in source
    assert "gateway_guardrails=gateway_guardrails," in source


# --------------------------------------------------------------------------- admission


def _request(profile: str = "standard", **extra) -> DeployRequest:
    policy = extra.pop("policyConfig", {"enabled": True, "guardrails": {"profile": profile}})
    return DeployRequest(
        nodeId="gr",
        config={"name": "gr_agent", "model": {"modelId": MODEL_ID}},
        gatewayConfig={"name": "gr-gateway", "gateway_provider": "agentcore", "targetType": "lambda"},
        gatewayTools=extra.pop("gatewayTools", ["weather_api", "process_refund"]),
        policyConfig=policy,
        **extra,
    )


def test_request_model_refuses_an_explicit_unsupported_target_region() -> None:
    with pytest.raises(ValidationError) as error:
        _request(targetRegion="eu-west-1")
    assert capabilities.feature_unavailable_message("gateway_guardrails", "eu-west-1") in str(error.value)


def test_request_model_admits_supported_regions_and_leaves_an_omitted_region_to_the_route() -> None:
    for region in ("us-east-1", "us-east-2", "us-west-2", "eu-west-2"):
        assert _request(targetRegion=region).policy_config["guardrails"] == {"profile": "standard"}
    assert _request().target_region is None
    assert _request("off", targetRegion="eu-west-1").target_region == "eu-west-1"


def test_request_model_refuses_an_unknown_profile_and_a_disabled_engine() -> None:
    with pytest.raises(ValidationError, match="Unknown Gateway guardrails profile: 'paranoid'"):
        _request("paranoid")
    with pytest.raises(ValidationError, match="Gateway guardrails need the policy engine"):
        _request(policyConfig={"enabled": False, "guardrails": {"profile": "standard"}})


def test_request_model_refuses_strict_with_a_served_only_tool_source_at_admission() -> None:
    with pytest.raises(ValidationError) as error:
        _request("strict", gatewayTools=["weather_api", "web_search"])
    assert (
        "Gateway guardrails profile 'strict' requires every permitted tool to be screened, and this canvas has "
        "tools that cannot be: managed Web Search is a served-only connector target. Use profile 'standard' "
        "(screens what it can and reports the rest) or remove those tools."
    ) in str(error.value)
    with pytest.raises(ValidationError, match="Web Answer is a served-only connector target"):
        _request("strict", gatewayTools=["weather_api", "web_answer"], targetRegion="us-east-1")
    with pytest.raises(ValidationError, match="custom tool noop has no required string argument"):
        _request(
            "strict",
            customTools=[
                {"toolName": "noop", "lambdaCode": "def handler(e, c): return {}", "inputSchema": {"type": "object"}}
            ],
        )
    # standard tolerates the same canvas and reports instead of refusing.
    assert (
        _request("standard", gatewayTools=["weather_api", "web_search"]).policy_config["guardrails"]["profile"]
        == "standard"
    )


def test_request_model_does_not_gate_a_canvas_without_a_policy_node() -> None:
    request = DeployRequest(
        nodeId="plain", config={"name": "plain", "model": {"modelId": MODEL_ID}}, targetRegion="eu-west-1"
    )
    assert request.policy_config is None


def _home(monkeypatch, region: str) -> None:
    monkeypatch.setattr(deployment_handler, "config", replace(deployment_handler.config, aws_region=region))


@pytest.mark.parametrize("home", ["eu-west-1", "eu-central-1", "eu-west-3"])
def test_live_route_refuses_guardrails_in_a_release_region_without_them_with_400(monkeypatch, home: str) -> None:
    _home(monkeypatch, home)
    with pytest.raises(HTTPException) as error:
        deployment_handler._reject_unsupported_gateway_guardrails_region(_request())
    assert error.value.status_code == 400
    assert str(error.value.detail) == capabilities.feature_unavailable_message("gateway_guardrails", home)


def test_live_route_admits_supported_homes_an_explicit_supported_target_and_profile_off(monkeypatch) -> None:
    for home in ("us-east-1", "us-east-2", "us-west-2", "eu-west-2"):
        _home(monkeypatch, home)
        deployment_handler._reject_unsupported_gateway_guardrails_region(_request())
    _home(monkeypatch, "eu-west-1")
    deployment_handler._reject_unsupported_gateway_guardrails_region(_request(targetRegion="us-east-1"))
    deployment_handler._reject_unsupported_gateway_guardrails_region(_request("off"))


def test_live_route_gate_runs_after_the_platform_version_gate_and_before_any_write() -> None:
    source = open(deployment_handler.__file__, encoding="utf-8").read()
    gate = source.index("_reject_unsupported_gateway_guardrails_region(request)")
    assert source.index("_reject_unsupported_runtime_platform_version_region(request)") < gate
    assert gate < source.index("store = _get_state_store()\n    store.create(state)")


# --------------------------------------------------------------------------- CloudFormation export


def test_export_refuses_a_guardrails_profile_with_the_remedy_and_emits_nothing() -> None:
    with pytest.raises(CfnExportUnsupportedError) as error:
        CfnTemplateGenerator().generate(_request("standard"))
    assert str(error.value) == (
        "Gateway guardrails profile 'standard' is not supported in the CloudFormation export yet. Set "
        "policyConfig.guardrails.profile to 'off' for the export (Cedar tool authorization is still exported), "
        "or deploy live."
    )


def test_export_with_profile_off_still_exports_the_cedar_permit_and_no_guardrail_grant() -> None:
    import yaml

    bundle = CfnTemplateGenerator().generate(_request("off"))
    template = yaml.safe_load(bundle.template_yaml)
    assert "PolicyEngine" in template["Resources"]
    statements = template["Resources"]["GatewayRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert not any(item.get("Sid") == "PolicyGuardrailChecks" for item in statements)
    assert "bedrock:InvokeGuardrailChecks" not in bundle.template_yaml
