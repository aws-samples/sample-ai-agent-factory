"""The Observability node's frontend spelling must survive model validation."""

from app.models.deployment_models import ObservabilityConfig, RuntimeConfig


def test_frontend_enable_otel_false_maps_to_the_effective_enabled_flag() -> None:
    config = ObservabilityConfig.model_validate({"enableOtel": False})

    assert config.enabled is False
    assert config.model_dump()["enabled"] is False
    assert config.model_dump(by_alias=True)["enableOtel"] is False


def test_internal_enabled_spelling_remains_backward_compatible() -> None:
    assert ObservabilityConfig.model_validate({"enabled": False}).enabled is False


def test_nested_runtime_observability_does_not_turn_false_back_on() -> None:
    runtime = RuntimeConfig.model_validate(
        {
            "name": "observability_alias_probe",
            "model": {"modelId": "us.anthropic.claude-sonnet-5"},
            "observability": {"enableOtel": False},
        }
    )

    assert runtime.observability is not None
    assert runtime.observability.enabled is False
