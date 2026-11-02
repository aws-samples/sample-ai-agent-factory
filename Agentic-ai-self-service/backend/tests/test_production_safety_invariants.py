"""Production safety invariants must fail closed and resist weaker caller settings."""

from __future__ import annotations

import pytest
from app.services.guardrail_builders import BEDROCK_PII_TYPES
from app.services.production_safety import (
    PRODUCTION_GUARDRAIL_PROFILE,
    PRODUCTION_POLICY_PROFILE,
    ProductionSafetyConfigurationError,
    enforce_production_execution_mode,
    enforce_production_guardrail_baseline,
    enforce_production_policy_baseline,
)
from app.services.promotion_evidence import (
    PromotionPolicyConfigurationError,
    production_evaluation_gate_required,
)


def test_development_environment_preserves_optional_guardrails(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert enforce_production_guardrail_baseline(None) is None
    supplied = {"mode": "existing", "guardrailId": "gr-dev"}
    assert enforce_production_guardrail_baseline(supplied) == supplied


def test_managed_harness_is_development_only_until_guardrail_hooks_can_retract_output(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    enforce_production_execution_mode("harness")

    monkeypatch.setenv("ENVIRONMENT", "prod")
    enforce_production_execution_mode("runtime")
    with pytest.raises(ProductionSafetyConfigurationError, match="Harness hooks"):
        enforce_production_execution_mode("harness")


def test_production_injects_complete_managed_baseline(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    result = enforce_production_guardrail_baseline(None)

    assert result is not None
    assert result["enabled"] is True
    assert result["mode"] == "create_new"
    assert result["baselineProfile"] == PRODUCTION_GUARDRAIL_PROFILE
    assert result["contentFilters"] == {
        "hate": "HIGH",
        "insults": "HIGH",
        "sexual": "HIGH",
        "violence": "HIGH",
        "misconduct": "HIGH",
        "prompt_attack": "HIGH",
    }
    pii = {item["type"]: item["action"] for item in result["piiFilters"]}
    assert set(pii) <= BEDROCK_PII_TYPES
    assert "SSN" not in pii
    assert pii["AWS_ACCESS_KEY"] == "ANONYMIZE"
    assert pii["AWS_SECRET_KEY"] == "ANONYMIZE"  # pragma: allowlist secret
    assert pii["PASSWORD"] == "ANONYMIZE"  # pragma: allowlist secret
    assert pii["US_SOCIAL_SECURITY_NUMBER"] == "ANONYMIZE"


def test_production_caller_cannot_weaken_baseline(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    result = enforce_production_guardrail_baseline(
        {
            "enabled": False,
            "mode": "create_new",
            "contentFilters": {"hate": "NONE", "prompt_attack": "LOW"},
            "piiFilters": [
                {"type": "PASSWORD", "action": "ANONYMIZE"},
                {"type": "EMAIL", "action": "BLOCK"},
                {"type": "SSN", "action": "BLOCK"},
            ],
            "deniedTopics": [{"name": "custom", "definition": "custom topic"}],
        }
    )

    assert result is not None
    assert result["enabled"] is True
    assert result["contentFilters"]["hate"] == "HIGH"
    assert result["contentFilters"]["prompt_attack"] == "HIGH"
    pii = {item["type"]: item["action"] for item in result["piiFilters"]}
    assert pii["PASSWORD"] == "ANONYMIZE"  # pragma: allowlist secret
    assert pii["EMAIL"] == "BLOCK"
    assert pii["US_SOCIAL_SECURITY_NUMBER"] == "BLOCK"
    assert "SSN" not in pii
    assert result["deniedTopics"] == [{"name": "custom", "definition": "custom topic"}]


def test_mutation_reintroducing_legacy_ssn_fails_service_enum_oracle(monkeypatch):
    from app.services import production_safety

    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setattr(
        production_safety,
        "normalize_pii_type",
        lambda value: str(value or "").strip().upper(),
    )
    mutated = production_safety.enforce_production_guardrail_baseline(
        {"piiFilters": [{"type": "SSN", "action": "BLOCK"}]}
    )
    invalid = [item["type"] for item in mutated["piiFilters"] if item["type"] not in BEDROCK_PII_TYPES]
    assert invalid == ["SSN"]


def test_production_rejects_unattested_existing_guardrail(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    with pytest.raises(ProductionSafetyConfigurationError, match="cannot be attested"):
        enforce_production_guardrail_baseline({"mode": "existing", "guardrailId": "gr-unverified"})


def test_production_evaluation_gate_cannot_be_disabled(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("PRODUCTION_EVALUATION_GATE", "false")
    with pytest.raises(PromotionPolicyConfigurationError, match="cannot be disabled"):
        production_evaluation_gate_required()


def test_development_evaluation_gate_can_be_explicitly_enabled_or_disabled(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("PRODUCTION_EVALUATION_GATE", "true")
    assert production_evaluation_gate_required() is True
    monkeypatch.setenv("PRODUCTION_EVALUATION_GATE", "false")
    assert production_evaluation_gate_required() is False


def test_development_preserves_optional_policy_mode(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    supplied = {"enabled": False, "mode": "LOG_ONLY", "on_enforce_failure": "log_only"}
    assert (
        enforce_production_policy_baseline(
            supplied,
            tool_gateway_required=True,
            gateway_provider="agentcore",
        )
        == supplied
    )


def test_production_without_tool_gateway_does_not_inject_cedar(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert (
        enforce_production_policy_baseline(
            None,
            tool_gateway_required=False,
            gateway_provider="agentcore",
        )
        is None
    )


def test_production_tool_gateway_injects_fail_closed_cedar(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    result = enforce_production_policy_baseline(
        None,
        tool_gateway_required=True,
        gateway_provider="agentcore",
    )
    assert result == {
        "enabled": True,
        "mode": "ENFORCE",
        "on_enforce_failure": "fail_closed",
        "baselineProfile": PRODUCTION_POLICY_PROFILE,
    }


def test_production_caller_cannot_select_log_only_or_disable_policy(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    result = enforce_production_policy_baseline(
        {
            "enabled": False,
            "mode": "LOG_ONLY",
            "on_enforce_failure": "log_only",
            "rules": [{"effect": "forbid", "action": "delete"}],
        },
        tool_gateway_required=True,
        gateway_provider="agentcore",
    )
    assert result is not None
    assert result["enabled"] is True
    assert result["mode"] == "ENFORCE"
    assert result["on_enforce_failure"] == "fail_closed"
    assert result["rules"] == [{"effect": "forbid", "action": "delete"}]


def test_production_tool_gateway_rejects_provider_without_cedar(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    with pytest.raises(ProductionSafetyConfigurationError, match="require AgentCore Gateway"):
        enforce_production_policy_baseline(
            None,
            tool_gateway_required=True,
            gateway_provider="litellm",
        )
