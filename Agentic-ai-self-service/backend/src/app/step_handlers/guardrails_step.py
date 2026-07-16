"""Step handler: Create or validate Amazon Bedrock Guardrails.

Supports two modes:
- "existing": validate that the specified guardrail ID exists and is READY
- "create_new": create a new guardrail with content filters, PII filters,
  denied topics, and word filters, then create a version

Requirements: 3.x (guardrails integration)
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import logging
import os
import time
import uuid
from typing import Optional

import boto3

from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services import step_clients
from app.services.deployment_state_store import DeploymentStateStore
from app.services.guardrail_builders import (
    build_contextual_grounding_config,
    build_regex_filters,
)

logger = logging.getLogger(__name__)

# Lambda default log level is WARNING — use warning for diagnostic logs
_LOG = logger.warning


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


# Content filter strength mapping
_FILTER_STRENGTHS = {"NONE", "LOW", "MEDIUM", "HIGH"}

# PII entity types supported by Bedrock Guardrails
_PII_TYPES = {
    "ADDRESS", "AGE", "AWS_ACCESS_KEY", "AWS_SECRET_KEY", "CA_HEALTH_NUMBER",
    "CA_SOCIAL_INSURANCE_NUMBER", "CREDIT_DEBIT_CARD_CVV", "CREDIT_DEBIT_CARD_EXPIRY",
    "CREDIT_DEBIT_CARD_NUMBER", "DRIVER_ID", "EMAIL", "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
    "IP_ADDRESS", "LICENSE_PLATE", "MAC_ADDRESS", "NAME", "PASSWORD", "PHONE",
    "PIN", "SSN", "URL", "UK_NATIONAL_HEALTH_SERVICE_NUMBER",
    "UK_NATIONAL_INSURANCE_NUMBER", "UK_UNIQUE_TAXPAYER_REFERENCE_NUMBER",
    "US_BANK_ACCOUNT_NUMBER", "US_BANK_ROUTING_NUMBER", "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER",
    "US_PASSPORT_NUMBER", "US_SOCIAL_SECURITY_NUMBER", "VEHICLE_IDENTIFICATION_NUMBER",
    "USERNAME",
}


def _build_content_filter_config(content_filters) -> dict:
    """Build contentPolicyConfig from a {category: strength} mapping.

    Canonical shape is a dict {violence:"HIGH", hate:"MEDIUM", ...}. Be defensive:
    a list of {type/category, inputStrength/strength} dicts (the alt shape some
    UIs emit) is normalized to the dict form rather than 500-ing with
    AttributeError on .items() (see also memory_step Bug 131).
    """
    if isinstance(content_filters, list):
        normalized = {}
        for f in content_filters:
            if isinstance(f, dict):
                cat = f.get("type") or f.get("category") or f.get("name")
                strength = f.get("inputStrength") or f.get("strength") or f.get("outputStrength") or "MEDIUM"
                if cat:
                    normalized[str(cat).lower()] = strength
        content_filters = normalized
    if not isinstance(content_filters, dict):
        return {}
    filters = []
    category_map = {
        "hate": "HATE",
        "insults": "INSULTS",
        "sexual": "SEXUAL",
        "violence": "VIOLENCE",
        "misconduct": "MISCONDUCT",
        "prompt_attack": "PROMPT_ATTACK",
    }
    for key, strength in content_filters.items():
        category = category_map.get(key.lower())
        if not category:
            continue
        strength_upper = str(strength).upper()
        if strength_upper not in _FILTER_STRENGTHS:
            strength_upper = "MEDIUM"
        # PROMPT_ATTACK is an input-only filter: Bedrock CreateGuardrail rejects
        # any non-NONE outputStrength ("PROMPT ATTACK content filter strength for
        # response must be NONE"). Mirror the CFN-export path here.
        output_strength = "NONE" if category == "PROMPT_ATTACK" else strength_upper
        filter_entry = {
            "type": category,
            "inputStrength": strength_upper,
            "outputStrength": output_strength,
        }
        filters.append(filter_entry)
    if not filters:
        return {}
    return {"filtersConfig": filters}


def _build_pii_config(pii_filters: list) -> dict:
    """Build sensitiveInformationPolicyConfig from PII filter list."""
    pii_entities = []
    for pii in pii_filters:
        pii_type = str(pii.get("type", "")).upper()
        action = str(pii.get("action", "ANONYMIZE")).upper()
        if pii_type not in _PII_TYPES:
            continue
        if action not in ("BLOCK", "ANONYMIZE"):
            action = "ANONYMIZE"
        pii_entities.append({"type": pii_type, "action": action})
    if not pii_entities:
        return {}
    return {"piiEntitiesConfig": pii_entities}


def _build_topic_config(denied_topics: list) -> dict:
    """Build topicPolicyConfig from denied topic list."""
    topics = []
    for topic in denied_topics:
        name = topic.get("name", "").strip()
        definition = topic.get("definition", "").strip()
        if not name or not definition:
            continue
        topics.append({
            "name": name,
            "definition": definition,
            "type": "DENY",
        })
    if not topics:
        return {}
    return {"topicsConfig": topics}


def _build_word_config(word_filters: list) -> dict:
    """Build wordPolicyConfig from word filter list."""
    words = [{"text": w.strip()} for w in word_filters if w.strip()]
    if not words:
        return {}
    return {"wordsConfig": words}


def _find_guardrail_id_by_name(bedrock, name: str) -> Optional[str]:
    """Return the guardrailId of a guardrail with the given name, or None.

    Used for idempotent upsert when ``create_guardrail`` raises
    ``ResourceAlreadyExistsException`` from a prior partial run.
    """
    try:
        paginator = bedrock.get_paginator("list_guardrails")
        for page in paginator.paginate():
            for gr in page.get("guardrails", []):
                if gr.get("name") == name:
                    return gr.get("id") or gr.get("guardrailId")
    except Exception:
        # Fallback: non-paginated call
        try:
            resp = bedrock.list_guardrails()
            for gr in resp.get("guardrails", []):
                if gr.get("name") == name:
                    return gr.get("id") or gr.get("guardrailId")
        except Exception:
            logger.warning("list_guardrails failed during upsert lookup", exc_info=True)
    return None


def handler(event: dict, context) -> dict:
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(deployment_id, DeploymentStepName.GUARDRAILS, DeploymentStatusEnum.IN_PROGRESS)

        guardrails_config = event.get("guardrails_config") or {}
        region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))
        mode = guardrails_config.get("mode", "existing")

        bedrock = step_clients.client(event, "bedrock")

        if mode == "existing":
            # Validate existing guardrail
            guardrail_id = guardrails_config.get("guardrailId") or guardrails_config.get("guardrail_id", "")
            guardrail_version = guardrails_config.get("guardrailVersion") or guardrails_config.get("guardrail_version", "DRAFT")
            if not guardrail_id:
                raise ValueError("guardrailId is required in existing mode")

            _LOG("Validating existing guardrail: %s (version %s)", guardrail_id, guardrail_version)
            resp = bedrock.get_guardrail(guardrailIdentifier=guardrail_id, guardrailVersion=guardrail_version)
            status = resp.get("status", "")
            if status not in ("READY", "VERSIONING"):
                raise ValueError(f"Guardrail {guardrail_id} is not ready (status: {status})")

            _LOG("Guardrail %s validated (status: %s)", guardrail_id, status)
            return {
                **event,
                "guardrails_result": {
                    "guardrail_id": guardrail_id,
                    "guardrail_version": guardrail_version,
                    "created_by_flow": False,
                },
            }

        # Create new guardrail
        _LOG("Creating new guardrail for deployment %s", deployment_id)
        name = guardrails_config.get("name", f"agentcore-guardrail-{deployment_id[:8]}")

        create_params: dict = {
            "name": name,
            "description": f"Guardrail for AgentCore Flows deployment {deployment_id[:8]}",
            "blockedInputMessaging": "Your request was blocked by a safety guardrail.",
            "blockedOutputsMessaging": "The response was blocked by a safety guardrail.",
        }

        # Content filters
        content_filters = guardrails_config.get("contentFilters") or guardrails_config.get("content_filters")
        if content_filters:
            content_policy = _build_content_filter_config(content_filters)
            if content_policy:
                create_params["contentPolicyConfig"] = content_policy

        # PII filters
        pii_filters = guardrails_config.get("piiFilters") or guardrails_config.get("pii_filters")
        if pii_filters:
            pii_policy = _build_pii_config(pii_filters)
            if pii_policy:
                create_params["sensitiveInformationPolicyConfig"] = pii_policy

        # Denied topics
        denied_topics = guardrails_config.get("deniedTopics") or guardrails_config.get("denied_topics")
        if denied_topics:
            topic_policy = _build_topic_config(denied_topics)
            if topic_policy:
                create_params["topicPolicyConfig"] = topic_policy

        # Word filters
        word_filters = guardrails_config.get("wordFilters") or guardrails_config.get("word_filters")
        if word_filters:
            word_policy = _build_word_config(word_filters)
            if word_policy:
                create_params["wordPolicyConfig"] = word_policy

        # Contextual grounding (Gap 2C). Blocks hallucinated / off-topic
        # responses by enforcing minimum grounding + relevance scores. NOTE:
        # inert unless the agent passes grounding_source + query qualifiers on
        # its ApplyGuardrail/converse call at invoke time (RAG agents).
        grounding = guardrails_config.get("contextualGrounding") or guardrails_config.get("contextual_grounding")
        if grounding:
            cg = build_contextual_grounding_config(
                grounding.get("groundingThreshold", grounding.get("grounding_threshold")),
                grounding.get("relevanceThreshold", grounding.get("relevance_threshold")),
            )
            if cg:
                create_params["contextualGroundingPolicyConfig"] = cg

        # Custom regex filters (Gap 2C). Wired into
        # sensitiveInformationPolicyConfig.regexesConfig. MUST MERGE (not
        # assign) so it coexists with any piiEntitiesConfig set above —
        # overwriting would silently drop the user's PII filters (Bug-122).
        regex_filters = guardrails_config.get("regexFilters") or guardrails_config.get("regex_filters")
        if regex_filters:
            rx = build_regex_filters(regex_filters)
            if rx:
                create_params.setdefault("sensitiveInformationPolicyConfig", {}).update(rx)

        # Fail FAST with a clear message if no actual policy was configured.
        # AWS CreateGuardrail rejects a guardrail with zero policies
        # ("Guardrail must have at least one policy") ~30s into the deploy; a
        # free-form user who drags a Guardrails node but leaves every filter
        # empty hits exactly that. Surface it here as an actionable error
        # (verified live in the free-form matrix).
        _policy_keys = (
            "contentPolicyConfig", "sensitiveInformationPolicyConfig",
            "topicPolicyConfig", "wordPolicyConfig", "contextualGroundingPolicyConfig",
        )
        if not any(k in create_params for k in _policy_keys):
            raise ValueError(
                "Guardrail has no policies configured. Add at least one of: content "
                "filters, denied topics, PII/sensitive-info filters, word filters, or "
                "contextual grounding — an empty guardrail guards nothing and AWS "
                "rejects it."
            )

        # Bug 82: idempotent upsert. A prior partial run can leave a
        # guardrail with this name behind; create_guardrail then fails with
        # ResourceAlreadyExistsException. Look up the existing guardrail by
        # name and update_guardrail in place, falling back to a UUID-suffixed
        # rename if the name lookup races.
        guardrail_id: Optional[str] = None
        try:
            resp = bedrock.create_guardrail(**create_params)
            guardrail_id = resp["guardrailId"]
            _LOG("Created guardrail: %s", guardrail_id)
        except Exception as _ce:  # noqa: BLE001
            # Bedrock has NO ResourceAlreadyExistsException — a duplicate guardrail
            # name surfaces as ConflictException / ResourceInUseException (verified
            # live: referencing the nonexistent attribute itself crashed the step).
            # Match on the error code, and re-raise anything that isn't a name clash.
            _code = getattr(getattr(_ce, "response", {}), "get", lambda *_: {})("Error", {}).get("Code", "") \
                if hasattr(_ce, "response") else ""
            if _code not in ("ConflictException", "ResourceInUseException") and \
               "already" not in str(_ce).lower() and "conflict" not in str(_ce).lower():
                raise
            existing_id = _find_guardrail_id_by_name(bedrock, name)
            if existing_id:
                _LOG("Guardrail name %s already exists (id=%s); updating in place", name, existing_id)
                # UpdateGuardrail requires `name` as a mandatory body field
                # (separate from guardrailIdentifier).
                update_params = {**create_params, "guardrailIdentifier": existing_id}
                bedrock.update_guardrail(**update_params)
                guardrail_id = existing_id
            else:
                # Race or rename collision: retry once with a UUID-suffixed name.
                fallback_name = f"{name}-{uuid.uuid4().hex[:8]}"
                _LOG("Guardrail %s exists but lookup failed; retrying with name %s", name, fallback_name)
                create_params["name"] = fallback_name
                resp = bedrock.create_guardrail(**create_params)
                guardrail_id = resp["guardrailId"]
                _LOG("Created guardrail with fallback name %s: %s", fallback_name, guardrail_id)

        # Manifest: record the flow-created guardrail immediately (before the
        # READY poll, which can be killed mid-flight) so teardown never orphans it.
        if guardrail_id:
            store.record_resource(
                deployment_id, {"type": "guardrail", "id": guardrail_id, "region": region}
            )

        # Wait for guardrail to be READY
        for attempt in range(24):  # 120s max
            time.sleep(5)
            status_resp = bedrock.get_guardrail(guardrailIdentifier=guardrail_id)
            status = status_resp.get("status", "")
            if status == "READY":
                break
            _LOG("Guardrail status: %s (attempt %d)", status, attempt + 1)
        else:
            raise TimeoutError(f"Guardrail {guardrail_id} did not reach READY within 120s")

        # Create a version
        version_resp = bedrock.create_guardrail_version(
            guardrailIdentifier=guardrail_id,
            description="Initial version created by AgentCore Flows",
        )
        guardrail_version = version_resp.get("version", "1")
        _LOG("Created guardrail version: %s", guardrail_version)

        return {
            **event,
            "guardrails_result": {
                "guardrail_id": guardrail_id,
                "guardrail_version": guardrail_version,
                "created_by_flow": True,
            },
        }

    except Exception:
        logger.exception("Guardrails step failed for deployment %s", deployment_id)
        raise
