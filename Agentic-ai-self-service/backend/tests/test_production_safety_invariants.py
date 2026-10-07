"""Production safety invariants must fail closed and resist weaker caller settings."""

from __future__ import annotations

import pytest
from app.services.production_safety import (
    PRODUCTION_GUARDRAIL_PROFILE,
    ProductionSafetyConfigurationError,
    enforce_production_guardrail_baseline,
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
    assert pii["AWS_ACCESS_KEY"] == "ANONYMIZE"
    assert pii["AWS_SECRET_KEY"] == "ANONYMIZE"
    assert pii["PASSWORD"] == "ANONYMIZE"
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
            ],
            "deniedTopics": [{"name": "custom", "definition": "custom topic"}],
        }
    )

    assert result is not None
    assert result["enabled"] is True
    assert result["contentFilters"]["hate"] == "HIGH"
    assert result["contentFilters"]["prompt_attack"] == "HIGH"
    pii = {item["type"]: item["action"] for item in result["piiFilters"]}
    assert pii["PASSWORD"] == "ANONYMIZE"
    assert pii["EMAIL"] == "BLOCK"
    assert result["deniedTopics"] == [{"name": "custom", "definition": "custom topic"}]


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
