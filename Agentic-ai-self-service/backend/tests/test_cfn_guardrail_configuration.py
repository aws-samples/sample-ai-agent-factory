"""Configured Guardrails are immutable, scoped, and enforced in CFN exports."""

from __future__ import annotations

import ast
import copy

import pytest
import yaml
from app.models.deployment_models import DeployRequest
from app.services.cfn_template_generator import CfnExportUnsupportedError, CfnTemplateGenerator
from app.services.export_guardrails import GuardrailExportError, prepare_guardrail_export
from app.services.guardrail_builders import (
    build_content_filter_config,
    build_pii_config,
    build_regex_filters,
    build_topic_config,
    build_word_config,
)
from app.services.production_safety import (
    apply_production_guardrail_profile,
    enforce_production_guardrail_baseline,
)
from botocore.session import Session
from botocore.validate import validate_parameters


def _configuration() -> dict:
    return {
        "componentType": "guardrails",
        "name": "Production Guardrails",
        "enabled": True,
        "mode": "create_new",
        "contentFilters": {"hate": "MEDIUM", "prompt_attack": "HIGH"},
        "piiFilters": [
            {"type": "SSN", "action": "ANONYMIZE"},
            {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCK"},
            {"type": "EMAIL", "action": "ANONYMIZE"},
        ],
        "deniedTopics": [{"name": "Credentials", "definition": "Requests to disclose authentication secrets"}],
        "wordFilters": ["restricted-topic"],
        "regexFilters": [{"name": "InternalCode", "pattern": "INTERNAL-[0-9]{4}", "action": "BLOCK"}],
    }


def _request(configuration: dict | None = None, **overrides) -> DeployRequest:
    body = {
        "nodeId": "guardrail-export",
        "config": {
            "name": "guardrail_export",
            "model": {"provider": "bedrock", "modelId": "us.anthropic.claude-sonnet-5"},
            "systemPrompt": "Produce a brief with checkable sources.",
        },
        "guardrailsConfig": _configuration() if configuration is None else configuration,
        **overrides,
    }
    return DeployRequest.model_validate(body)


def _template(configuration: dict | None = None, **overrides) -> tuple[dict, str]:
    bundle = CfnTemplateGenerator().generate(_request(configuration, **overrides))
    return yaml.safe_load(bundle.template_yaml), bundle.agent_code


def _pascal(value):
    if isinstance(value, dict):
        return {key[0].upper() + key[1:]: _pascal(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_pascal(item) for item in value]
    return value


def test_created_policy_uses_shared_live_builders_without_strengthening_the_draft():
    supplied = _configuration()
    before = copy.deepcopy(supplied)
    plan = prepare_guardrail_export(supplied)
    assert supplied == before
    assert plan.properties["ContentPolicyConfig"] == _pascal(build_content_filter_config(supplied["contentFilters"]))
    assert plan.properties["SensitiveInformationPolicyConfig"] == _pascal(
        {**build_pii_config(supplied["piiFilters"]), **build_regex_filters(supplied["regexFilters"])}
    )
    assert plan.properties["TopicPolicyConfig"] == _pascal(build_topic_config(supplied["deniedTopics"]))
    assert plan.properties["WordPolicyConfig"] == _pascal(build_word_config(supplied["wordFilters"]))
    assert len(plan.properties["ContentPolicyConfig"]["FiltersConfig"]) == 2
    assert plan.properties["ContentPolicyConfig"]["FiltersConfig"][0]["InputStrength"] == "MEDIUM"

    # Pin the exact service request shape without contacting AWS.
    create_request = {
        "name": "guardrail_export",
        "blockedInputMessaging": "Blocked",
        "blockedOutputsMessaging": "Blocked",
        "contentPolicyConfig": build_content_filter_config(supplied["contentFilters"]),
        "sensitiveInformationPolicyConfig": {
            **build_pii_config(supplied["piiFilters"]),
            **build_regex_filters(supplied["regexFilters"]),
        },
        "topicPolicyConfig": build_topic_config(supplied["deniedTopics"]),
        "wordPolicyConfig": build_word_config(supplied["wordFilters"]),
    }
    validate_parameters(
        create_request, Session().get_service_model("bedrock").operation_model("CreateGuardrail").input_shape
    )


@pytest.mark.parametrize("configuration", [None, {}])
def test_legacy_connected_marker_retains_its_documented_high_content_baseline(configuration):
    plan = prepare_guardrail_export(configuration)
    filters = plan.properties["ContentPolicyConfig"]["FiltersConfig"]
    assert len(filters) == 6
    assert all(item["InputStrength"] == "HIGH" for item in filters)
    assert next(item for item in filters if item["Type"] == "PROMPT_ATTACK")["OutputStrength"] == "NONE"
    assert "SensitiveInformationPolicyConfig" not in plan.properties


@pytest.mark.parametrize("profile", [None, {"prefix": "ecb"}])
def test_canvas_display_name_is_normalized_overridable_and_retained_with_both_naming_profiles(profile):
    overrides = {"namingProfile": profile} if profile is not None else {}
    template, _source = _template(**overrides)
    name = template["Parameters"]["GuardrailName"]
    assert name["Default"] == "Production_Guardrails"
    assert name["MaxLength"] == 50
    assert name["AllowedPattern"] == "[0-9a-zA-Z_-]+"
    guardrail = template["Resources"]["BedrockGuardrail"]["Properties"]
    assert guardrail["Name"] == {"Ref": "GuardrailName"}
    assert "Production Guardrails" in guardrail["Description"]


def test_valid_explicit_physical_name_is_not_changed():
    supplied = _configuration()
    supplied["name"] = "launch_intelligence_guardrails"
    assert prepare_guardrail_export(supplied).parameters["GuardrailName"]["Default"] == supplied["name"]


@pytest.mark.parametrize("profile", [None, "production-v1"])
def test_hydrated_camel_case_prompt_attack_preserves_exactly_one_input_only_filter(profile):
    supplied = _configuration()
    supplied["contentFilters"]["promptAttack"] = supplied["contentFilters"].pop("prompt_attack")
    if profile:
        supplied["baselineProfile"] = profile
    plan = prepare_guardrail_export(supplied)
    prompt_attack = [
        item for item in plan.properties["ContentPolicyConfig"]["FiltersConfig"] if item["Type"] == "PROMPT_ATTACK"
    ]
    assert prompt_attack == [{"Type": "PROMPT_ATTACK", "InputStrength": "HIGH", "OutputStrength": "NONE"}]
    assert build_content_filter_config({"promptAttack": "HIGH"}) == {
        "filtersConfig": [{"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"}]
    }
    if profile:
        effective = apply_production_guardrail_profile(supplied)
        assert "promptAttack" not in effective["contentFilters"]
        assert effective["contentFilters"]["prompt_attack"] == "HIGH"


def test_policy_change_replaces_immutable_version_and_runtime_is_pinned():
    supplied = _configuration()
    first, source = _template(supplied)
    supplied["contentFilters"]["hate"] = "HIGH"
    second, _ = _template(supplied)
    version = first["Resources"]["BedrockGuardrailVersion"]
    assert version["Type"] == "AWS::Bedrock::GuardrailVersion"
    assert version["DependsOn"] == ["BedrockGuardrail"]
    assert version["Properties"]["GuardrailIdentifier"] == {"Fn::GetAtt": ["BedrockGuardrail", "GuardrailId"]}
    assert (
        version["Properties"]["Description"]
        != second["Resources"]["BedrockGuardrailVersion"]["Properties"]["Description"]
    )
    env = first["Resources"]["AgentCoreRuntime"]["Properties"]["EnvironmentVariables"]
    assert env["GUARDRAIL_ID"] == {"Fn::GetAtt": ["BedrockGuardrail", "GuardrailId"]}
    assert env["GUARDRAIL_VERSION"] == {"Fn::GetAtt": ["BedrockGuardrailVersion", "Version"]}
    assert env["GUARDRAIL_VERSION"] != "DRAFT"

    # The downloaded source, not just its infrastructure, guards both model and
    # tool boundaries. Behavioral failure modes are exercised by the existing
    # test_guardrail_model_wiring/tool_boundary suites.
    ast.parse(source)
    assert "**_GUARDRAIL_KWARGS" in source
    assert '"guardrail_trace": "disabled"' in source
    assert '"guardrail_redact_output": True' in source
    assert "_GUARDRAIL_HOOKS" in source
    assert "apply_guardrail(" in source


@pytest.mark.parametrize("identifier", ["abc123", "arn:aws:bedrock:us-east-1:123456789012:guardrail/abc123"])
def test_existing_numbered_policy_is_parameterized_not_recreated_and_has_exact_iam(identifier):
    supplied = {
        "componentType": "guardrails",
        "mode": "existing",
        "guardrailId": identifier,
        "guardrailVersion": "7",
    }
    template, source = _template(supplied)
    resources = template["Resources"]
    assert "BedrockGuardrail" not in resources
    assert "BedrockGuardrailVersion" not in resources
    assert template["Parameters"]["ExistingGuardrailIdentifier"]["Default"] == identifier
    assert template["Parameters"]["ExistingGuardrailVersion"]["Default"] == "7"
    assert template["Parameters"]["ExistingGuardrailVersion"]["AllowedPattern"] == "[1-9][0-9]{0,7}"
    env = resources["AgentCoreRuntime"]["Properties"]["EnvironmentVariables"]
    assert env["GUARDRAIL_ID"] == {"Ref": "ExistingGuardrailIdentifier"}
    assert env["GUARDRAIL_VERSION"] == {"Ref": "ExistingGuardrailVersion"}
    plan = prepare_guardrail_export(supplied)
    statements = resources["RuntimeExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    allow = next(item for item in statements if item.get("Sid") == "GuardrailsAccess")
    assert allow["Action"] == ["bedrock:ApplyGuardrail", "bedrock:GetGuardrail"]
    assert allow["Resource"] == plan.guardrail_arn
    assert allow["Resource"] != "*"
    deny = next(item for item in statements if item.get("Sid") == "RequireExportedGuardrailVersion")
    assert deny["Action"] == ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    assert deny["Condition"]["StringNotEquals"]["bedrock:GuardrailIdentifier"] == {
        "Fn::Join": [":", [plan.guardrail_arn, {"Ref": "ExistingGuardrailVersion"}]]
    }
    ast.parse(source)


def test_created_policy_iam_is_scoped_to_the_resource_and_exact_version():
    template, _source = _template()
    statements = template["Resources"]["RuntimeExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    allow = next(item for item in statements if item.get("Sid") == "GuardrailsAccess")
    assert allow["Resource"] == {"Fn::GetAtt": ["BedrockGuardrail", "GuardrailArn"]}
    deny = next(item for item in statements if item.get("Sid") == "RequireExportedGuardrailVersion")
    assert deny["Condition"]["StringNotEquals"]["bedrock:GuardrailIdentifier"] == {
        "Fn::Join": [
            ":",
            [
                {"Fn::GetAtt": ["BedrockGuardrail", "GuardrailArn"]},
                {"Fn::GetAtt": ["BedrockGuardrailVersion", "Version"]},
            ],
        ]
    }


def test_explicit_production_profile_uses_the_same_pure_live_floor_without_environment_dependence(monkeypatch):
    supplied = _configuration()
    supplied["baselineProfile"] = "production-v1"
    before = copy.deepcopy(supplied)
    effective = apply_production_guardrail_profile(supplied)
    monkeypatch.setenv("ENVIRONMENT", "dev")
    dev_plan = prepare_guardrail_export(supplied)
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert enforce_production_guardrail_baseline(supplied) == effective
    assert prepare_guardrail_export(supplied) == dev_plan
    assert supplied == before
    assert dev_plan.properties["ContentPolicyConfig"] == _pascal(
        build_content_filter_config(effective["contentFilters"])
    )
    pii = {
        item["Type"]: item["Action"]
        for item in dev_plan.properties["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]
    }
    assert len(pii) == 8
    assert pii["US_SOCIAL_SECURITY_NUMBER"] == "BLOCK"
    assert pii["EMAIL"] == "ANONYMIZE"
    assert pii["PASSWORD"] == "ANONYMIZE"  # pragma: allowlist secret


def test_explicit_managed_profile_floors_a_disabled_flag_and_alternative_content_shape():
    supplied = {
        "mode": "create_new",
        "enabled": False,
        "baselineProfile": "production-v1",
        "contentFilters": [{"type": "promptAttack", "inputStrength": "LOW", "outputStrength": "NONE"}],
    }
    plan = prepare_guardrail_export(supplied)
    filters = plan.properties["ContentPolicyConfig"]["FiltersConfig"]
    assert len(filters) == 6
    assert all(item["InputStrength"] == "HIGH" for item in filters)
    assert len(plan.properties["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]) == 8


@pytest.mark.parametrize(
    "grounding", [{}, {"enabled": False}, {"groundingThreshold": None, "relevanceThreshold": None}]
)
def test_explicitly_inert_contextual_grounding_does_not_block_real_policy_export(grounding):
    supplied = _configuration()
    supplied["contextualGrounding"] = grounding
    assert "ContextualGroundingPolicyConfig" not in prepare_guardrail_export(supplied).properties


@pytest.mark.parametrize(
    "patch",
    [
        {"unrecognizedSafetyControl": True},
        {"mode": []},
        {"componentType": "browser"},
        {"enabled": "true"},
        {"enabled": False},
        {"name": {}},
        {"contentFilters": {"unknown": "HIGH"}},
        {"contentFilters": {"hate": "INVALID"}},
        {"contentFilters": [{"type": "hate", "inputStrength": "HIGH", "outputStrength": "LOW"}]},
        {"contentFilters": [{"type": "hate", "category": "violence", "strength": "HIGH"}]},
        {"contentFilters": [{"type": "hate", "inputStrength": "HIGH", "strength": "LOW"}]},
        {"contentFilters": [{"type": "hate", "strength": "HIGH"}, {"type": "HATE", "strength": "LOW"}]},
        {"contentFilters": {"prompt_attack": "HIGH", "promptAttack": "LOW"}},
        {"piiFilters": [{"type": "UNSUPPORTED", "action": "BLOCK"}]},
        {"piiFilters": {}},
        {"piiFilters": [{"type": "EMAIL", "action": "ALLOW"}]},
        {"piiFilters": [{"type": "EMAIL", "action": "BLOCK", "outputAction": "NONE"}]},
        {"deniedTopics": [{"name": "Invalid/@name", "definition": "Blocked topic"}]},
        {"deniedTopics": [{"name": "Topic", "definition": ""}]},
        {"wordFilters": [42]},
        {"wordFilters": {}},
        {"regexFilters": [{"name": "Bad", "pattern": "[", "action": "BLOCK"}]},
        {"regexFilters": [{"name": "Bad", "pattern": "a{9999999999999999999}", "action": "BLOCK"}]},
        {"regexFilters": [{"name": "Bad", "pattern": "x", "inputAction": "NONE"}]},
        {"regexFilters": [{"name": "Bad", "pattern": "x", "description": False}]},
        {"contextualGrounding": {"enabled": True}},
        {"contextualGrounding": 0},
        {"contextualGrounding": []},
        {"contextualGrounding": {"groundingThreshold": 0}},
        {"contextualGrounding": {"groundingThreshold": 0.7, "relevanceThreshold": 0.7}},
        {"baselineProfile": "unknown-profile"},
        {"baselineProfile": []},
        {"guardrailId": "abc123"},
        {"guardrailVersion": "7"},
        {"content_filters": {"hate": "LOW"}},
    ],
)
def test_invalid_or_unsupported_controls_are_refused_without_a_partial_bundle(patch):
    supplied = {**_configuration(), **patch}
    with pytest.raises(GuardrailExportError, match="cannot faithfully export guardrailsConfig"):
        prepare_guardrail_export(supplied)
    with pytest.raises(CfnExportUnsupportedError, match="cannot faithfully export guardrailsConfig"):
        CfnTemplateGenerator().generate(_request(supplied))


def test_explicit_empty_creation_never_falls_back_to_an_unrequested_policy():
    with pytest.raises(GuardrailExportError, match="at least one actual policy"):
        prepare_guardrail_export({"mode": "create_new"})


@pytest.mark.parametrize("version", ["DRAFT", "0", "123456789", 7, "", None])
def test_existing_guardrail_requires_an_actual_supported_immutable_version(version):
    with pytest.raises(GuardrailExportError, match="immutable numbered"):
        prepare_guardrail_export({"mode": "existing", "guardrailId": "abc123", "guardrailVersion": version})


def test_existing_guardrail_cannot_claim_an_unattested_managed_profile():
    with pytest.raises(GuardrailExportError, match="cannot be attested"):
        prepare_guardrail_export(
            {"mode": "existing", "guardrailId": "abc123", "guardrailVersion": "7", "baselineProfile": "production-v1"}
        )
