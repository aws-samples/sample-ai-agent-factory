"""Pure, fail-closed Guardrail configuration for downloadable CloudFormation.

Policy shapes come from the same builders as the live Guardrails step. A
numbered GuardrailVersion is required; creating or updating the draft policy
alone must never leave an exported Runtime using a mutable DRAFT.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.services.guardrail_builders import (
    BEDROCK_PII_TYPES,
    build_content_filter_config,
    build_pii_config,
    build_regex_filters,
    build_topic_config,
    build_word_config,
    normalize_content_filter_type,
    normalize_pii_type,
)
from app.services.naming import sanitize_agentcore_name
from app.services.production_safety import (
    PRODUCTION_GUARDRAIL_PROFILE,
    apply_production_guardrail_profile,
)


class GuardrailExportError(ValueError):
    """The requested policy cannot be faithfully enforced by this export."""


@dataclass(frozen=True)
class GuardrailExportPlan:
    mode: str
    properties: dict[str, Any]
    guardrail_id: Any
    guardrail_version: Any
    guardrail_arn: Any
    parameters: dict[str, Any]
    conditions: dict[str, Any]
    policy_digest: str | None = None


_POLICY_FIELDS = {
    "contentFilters": "content_filters",
    "piiFilters": "pii_filters",
    "deniedTopics": "denied_topics",
    "wordFilters": "word_filters",
    "regexFilters": "regex_filters",
    "contextualGrounding": "contextual_grounding",
}
_ALLOWED_FIELDS = {
    "name",
    "enabled",
    "mode",
    "guardrailId",
    "guardrail_id",
    "guardrailVersion",
    "guardrail_version",
    "baselineProfile",
    "baseline_profile",
    "componentType",
    "component_type",
    *_POLICY_FIELDS,
    *_POLICY_FIELDS.values(),
}
_CONTENT_TYPES = {"hate", "insults", "sexual", "violence", "misconduct", "prompt_attack"}
_STRENGTHS = {"NONE", "LOW", "MEDIUM", "HIGH"}
_IDENTIFIER_PATTERN = r"(?:[a-z0-9]+|arn:aws(?:-[a-z0-9-]+)?:bedrock:[a-z0-9-]+:[0-9]{12}:guardrail/[a-z0-9]+)"


def _refuse(message: str) -> None:
    raise GuardrailExportError(
        f"CloudFormation cannot faithfully export guardrailsConfig: {message}; no policy was silently omitted."
    )


def _field(config: dict, camel: str, snake: str) -> Any:
    first, second = config.get(camel), config.get(snake)
    if first not in (None, "", [], {}) and second not in (None, "", [], {}) and first != second:
        _refuse(f"{camel} and {snake} disagree")
    if first not in (None, "", [], {}):
        return first
    # Preserve malformed empty values for the typed validators rather than
    # dropping an empty object supplied where a policy array was required.
    return second if second is not None else first


def _list(value: Any, name: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        _refuse(f"{name} must be a list")
    return value


def _entry(entry: Any, allowed: set[str], name: str) -> dict:
    if not isinstance(entry, dict) or set(entry) - allowed:
        _refuse(f"{name} contains unsupported fields or is not an object")
    return entry


def _text(value: Any, maximum: int, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        _refuse(f"{name} requires a nonempty string of at most {maximum} characters")
    return value.strip()


def _validate_content(value: Any) -> None:
    if value is None:
        return
    if isinstance(value, dict):
        entries = value.items()
    elif isinstance(value, list):
        entries = []
        for item in value:
            item = _entry(
                item, {"type", "category", "name", "inputStrength", "outputStrength", "strength"}, "contentFilters"
            )
            declared_categories = [item[key] for key in ("type", "category", "name") if key in item]
            if any(
                not isinstance(category, str) or normalize_content_filter_type(category) not in _CONTENT_TYPES
                for category in declared_categories
            ):
                _refuse("contentFilters names an unsupported category")
            if len({normalize_content_filter_type(category) for category in declared_categories}) > 1:
                _refuse("contentFilters category aliases disagree")
            for key in ("inputStrength", "outputStrength", "strength"):
                if key in item and (not isinstance(item[key], str) or item[key].upper() not in _STRENGTHS):
                    _refuse("contentFilters contains an invalid strength")
            if (
                "inputStrength" in item
                and "strength" in item
                and item["inputStrength"].upper() != item["strength"].upper()
            ):
                _refuse("contentFilters strength aliases disagree")
            category = item.get("type") or item.get("category") or item.get("name")
            strength = item.get("inputStrength") or item.get("strength") or item.get("outputStrength") or "MEDIUM"
            expected_output = (
                "NONE" if normalize_content_filter_type(category) == "prompt_attack" else str(strength).upper()
            )
            if item.get("outputStrength") and str(item["outputStrength"]).upper() != expected_output:
                _refuse("asymmetric content-filter strengths are not represented by the live builder")
            entries.append((category, strength))
    else:
        _refuse("contentFilters must be an object or list")
    categories = set()
    for category, strength in entries:
        canonical_category = normalize_content_filter_type(category)
        if not isinstance(category, str) or canonical_category not in _CONTENT_TYPES:
            _refuse("contentFilters names an unsupported category")
        if not isinstance(strength, str) or strength.upper() not in _STRENGTHS:
            _refuse("contentFilters contains an invalid strength")
        if canonical_category in categories:
            _refuse("contentFilters repeats the same category")
        categories.add(canonical_category)


def _validate_grounding(value: Any) -> None:
    if value is None or value == {} or value is False:
        return
    value = _entry(
        value,
        {"enabled", "groundingThreshold", "grounding_threshold", "relevanceThreshold", "relevance_threshold"},
        "contextualGrounding",
    )
    if "enabled" in value and not isinstance(value["enabled"], bool):
        _refuse("contextualGrounding.enabled must be a boolean")
    if value.get("enabled") is False:
        return
    thresholds = [
        _field(value, "groundingThreshold", "grounding_threshold"),
        _field(value, "relevanceThreshold", "relevance_threshold"),
    ]
    if value.get("enabled") is True or any(threshold is not None for threshold in thresholds):
        _refuse(
            "contextual grounding requires grounding_source/query runtime qualifiers, "
            "which this generated agent does not provide"
        )


def prepare_guardrail_export(config: dict | None) -> GuardrailExportPlan:
    """Build policy/bindings without AWS calls or importing a Lambda handler.

    ``None``/``{}`` are the old connected-node-only contract and retain their
    documented HIGH content-filter baseline. An explicit create_new configuration is never
    replaced with that baseline when it has no usable policy.
    """
    if config is None or config == {}:
        config = {"mode": "create_new", "contentFilters": dict.fromkeys(sorted(_CONTENT_TYPES), "HIGH")}
    if not isinstance(config, dict) or set(config) - _ALLOWED_FIELDS:
        _refuse("guardrailsConfig contains unsupported fields or is not an object")
    if "enabled" in config and not isinstance(config["enabled"], bool):
        _refuse("enabled must be a boolean")
    if config.get("name") is not None and not isinstance(config["name"], str):
        _refuse("name must be a string")
    for field in ("componentType", "component_type"):
        if field in config and config[field] is not None and config[field] != "guardrails":
            _refuse("componentType must describe a guardrails node")
    mode = config.get("mode", "existing")
    if not isinstance(mode, str) or mode not in {"existing", "create_new"}:
        _refuse("mode must be existing or create_new")
    baseline_profile = _field(config, "baselineProfile", "baseline_profile")
    for field in ("baselineProfile", "baseline_profile"):
        if field in config and config[field] is not None and not isinstance(config[field], str):
            _refuse("baselineProfile must be a string")
    if baseline_profile and baseline_profile != PRODUCTION_GUARDRAIL_PROFILE:
        _refuse("baselineProfile names an unsupported managed policy")
    if config.get("enabled") is False and not baseline_profile:
        _refuse("a connected disabled Guardrail cannot be exported as an enforced policy")
    if mode == "existing":
        if baseline_profile:
            _refuse("an existing Guardrail cannot be attested against the declared managed baseline")
        identifier = _field(config, "guardrailId", "guardrail_id")
        version = _field(config, "guardrailVersion", "guardrail_version")
        if not isinstance(identifier, str) or not re.fullmatch(_IDENTIFIER_PATTERN, identifier):
            _refuse("existing mode requires a valid guardrail ID or ARN")
        if not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]{0,7}", version):
            _refuse("existing mode requires an immutable numbered guardrailVersion, not DRAFT")
        identifier_ref = {"Ref": "ExistingGuardrailIdentifier"}
        version_ref = {"Ref": "ExistingGuardrailVersion"}
        arn = {
            "Fn::If": [
                "ExistingGuardrailIsArn",
                identifier_ref,
                {
                    "Fn::Sub": "arn:${AWS::Partition}:bedrock:${AWS::Region}:${AWS::AccountId}:guardrail/${ExistingGuardrailIdentifier}"
                },
            ]
        }
        return GuardrailExportPlan(
            mode=mode,
            properties={},
            guardrail_id=identifier_ref,
            guardrail_version=version_ref,
            guardrail_arn=arn,
            parameters={
                "ExistingGuardrailIdentifier": {
                    "Type": "String",
                    "Default": identifier,
                    "AllowedPattern": _IDENTIFIER_PATTERN,
                    "Description": "Existing Guardrail ID/ARN. The stack does not create, change or delete this policy.",
                },
                "ExistingGuardrailVersion": {
                    "Type": "String",
                    "Default": version,
                    "AllowedPattern": "[1-9][0-9]{0,7}",
                    "Description": "Immutable numbered version of the supplied existing Guardrail; DRAFT is refused.",
                },
            },
            conditions={
                "ExistingGuardrailIsArn": {
                    "Fn::Equals": [
                        {"Fn::Select": [0, {"Fn::Split": [":", identifier_ref]}]},
                        "arn",
                    ]
                }
            },
        )

    # These identifiers do not configure create_new in the live step. Refuse
    # substantive stale references rather than suggesting that they were used.
    if _field(config, "guardrailId", "guardrail_id"):
        _refuse("create_new must not carry an existing guardrailId")
    if _field(config, "guardrailVersion", "guardrail_version"):
        _refuse("create_new must not carry a stale existing guardrailVersion")
    content = _field(config, "contentFilters", "content_filters")
    _validate_content(content)
    pii = _list(_field(config, "piiFilters", "pii_filters"), "piiFilters")
    for item in pii:
        item = _entry(item, {"type", "action"}, "piiFilters")
        if normalize_pii_type(item.get("type")) not in BEDROCK_PII_TYPES:
            _refuse("piiFilters names an unsupported PII entity")
        if str(item.get("action", "ANONYMIZE")).upper() not in {"BLOCK", "ANONYMIZE"}:
            _refuse("piiFilters contains an invalid action")
    topics = _list(_field(config, "deniedTopics", "denied_topics"), "deniedTopics")
    for item in topics:
        item = _entry(item, {"name", "definition"}, "deniedTopics")
        name = _text(item.get("name"), 100, "deniedTopics.name")
        if not re.fullmatch(r"[0-9a-zA-Z_ !?.-]+", name):
            _refuse("deniedTopics.name contains characters Bedrock does not accept")
        _text(item.get("definition"), 1000, "deniedTopics.definition")
    words = _list(_field(config, "wordFilters", "word_filters"), "wordFilters")
    for word in words:
        _text(word, 100, "wordFilters")
    regexes = _list(_field(config, "regexFilters", "regex_filters"), "regexFilters")
    for item in regexes:
        item = _entry(item, {"name", "pattern", "action", "description"}, "regexFilters")
        _text(item.get("name"), 100, "regexFilters.name")
        pattern = _text(item.get("pattern"), 500, "regexFilters.pattern")
        try:
            re.compile(pattern)
        except (re.error, OverflowError):
            _refuse("regexFilters contains an invalid regex")
        if str(item.get("action", "ANONYMIZE")).upper() not in {"BLOCK", "ANONYMIZE"}:
            _refuse("regexFilters contains an invalid action")
        if "description" in item:
            if not isinstance(item["description"], str):
                _refuse("regexFilters.description must be a string")
            if item["description"].strip():
                _text(item["description"], 1000, "regexFilters.description")
    _validate_grounding(_field(config, "contextualGrounding", "contextual_grounding"))

    if baseline_profile:
        # Validate the raw inputs above before flooring them, so a malformed
        # requested policy cannot disappear behind a managed default.
        # The live baseline accepts an object; normalize the builder's supported
        # alternative list shape and camel/snake aliases into that same input.
        normalized_content = {
            item["type"].lower(): item["inputStrength"]
            for item in build_content_filter_config(content).get("filtersConfig", [])
        }
        config = apply_production_guardrail_profile({**config, "contentFilters": normalized_content, "piiFilters": pii})
        content = config["contentFilters"]
        pii = config["piiFilters"]

    # Conversion only changes schema casing, not policy strengths/actions.
    policies = {
        "ContentPolicyConfig": build_content_filter_config(content),
        "SensitiveInformationPolicyConfig": {**build_pii_config(pii), **build_regex_filters(regexes)},
        "TopicPolicyConfig": build_topic_config(topics),
        "WordPolicyConfig": build_word_config(words),
    }

    def pascal(value):
        if isinstance(value, dict):
            return {key[0].upper() + key[1:]: pascal(item) for key, item in value.items()}
        if isinstance(value, list):
            return [pascal(item) for item in value]
        return value

    properties = {key: pascal(value) for key, value in policies.items() if value}
    if not properties:
        _refuse("create_new requires at least one actual policy")
    # A version is replaced when its policy changes, not just when the mutable
    # Guardrail draft is updated. Display/physical names are not policy.
    digest = hashlib.sha256(json.dumps(properties, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    parameters = {}
    if config.get("name"):
        display_name = _text(config["name"], 150, "name")
        physical_name = (
            display_name
            if re.fullmatch(r"[0-9a-zA-Z_-]{1,50}", display_name)
            else sanitize_agentcore_name(display_name, style="underscore", max_len=50, fallback="guardrail")
        )
        parameters["GuardrailName"] = {
            "Type": "String",
            "Default": physical_name,
            "MinLength": 1,
            "MaxLength": 50,
            "AllowedPattern": "[0-9a-zA-Z_-]+",
            "Description": "Guardrail physical name, normalized from its canvas label. Override to avoid collisions.",
        }
        properties["Name"] = {"Ref": "GuardrailName"}
        properties["Description"] = f"Guardrail for AgentCore Flows: {display_name}"
    return GuardrailExportPlan(
        mode=mode,
        properties=properties,
        guardrail_id={"Fn::GetAtt": ["BedrockGuardrail", "GuardrailId"]},
        guardrail_version={"Fn::GetAtt": ["BedrockGuardrailVersion", "Version"]},
        guardrail_arn={"Fn::GetAtt": ["BedrockGuardrail", "GuardrailArn"]},
        parameters=parameters,
        conditions={},
        policy_digest=digest,
    )
