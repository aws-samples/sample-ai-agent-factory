"""Contract tests for the managed AgentCore Gateway Web Search target."""

from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from app import deployment_handler
from app.models.deployment_models import DeployRequest
from app.services import managed_web_search as search
from botocore.session import Session
from botocore.validate import validate_parameters
from fastapi import HTTPException
from pydantic import ValidationError


def test_target_request_is_pinned_and_accepted_by_the_installed_sdk_model() -> None:
    params = search.managed_web_search_target_params("gateway-123")
    model = Session().get_service_model("bedrock-agentcore-control")

    validate_parameters(
        params,
        model.operation_model("CreateGatewayTarget").input_shape,
    )

    connector = params["targetConfiguration"]["mcp"]["connector"]
    assert connector == {
        "source": {"connectorId": "web-search", "version": "1.2.0"},
        "configurations": [{"name": "WebSearch", "parameterValues": {}}],
    }
    assert params["credentialProviderConfigurations"] == [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]


def test_managed_search_is_implied_only_by_its_template_and_explicit_tool_ids() -> None:
    assert search.managed_web_search_requested("web-search-agent", [])
    assert not search.managed_web_search_requested("strands-gateway-agent", [])
    assert search.managed_web_search_requested(None, ["web_search"])
    assert search.managed_web_search_requested(None, ["duckduckgo_search"])
    assert not search.managed_web_search_requested(None, ["weather_api"])


def test_generic_gateway_portability_control_removal_is_detected(monkeypatch) -> None:
    def assert_core_only() -> None:
        assert not search.managed_web_search_requested("strands-gateway-agent", [])

    assert_core_only()
    monkeypatch.setattr(
        search,
        "MANAGED_WEB_SEARCH_TEMPLATE_IDS",
        frozenset({"web-search-agent", "strands-gateway-agent"}),
    )
    with pytest.raises(AssertionError):
        assert_core_only()


def _deploy_request(template_id: str, **extra) -> DeployRequest:
    return DeployRequest(
        nodeId="managed-search-region",
        templateId=template_id,
        config={"name": "managed_search_agent", "model": {"modelId": "us.anthropic.claude-sonnet-5"}},
        **extra,
    )


def test_shared_request_model_keeps_omitted_region_for_export_callers() -> None:
    request = _deploy_request("web-search-agent")

    assert request.target_region is None


def test_live_deploy_uses_the_home_region_when_target_region_is_omitted(monkeypatch) -> None:
    monkeypatch.setattr(
        deployment_handler,
        "config",
        replace(deployment_handler.config, aws_region="eu-central-1"),
    )

    with pytest.raises(HTTPException) as error:
        deployment_handler._reject_unsupported_managed_web_search_region(_deploy_request("web-search-agent"))

    assert error.value.status_code == 400
    assert "Web Search is not available" in str(error.value.detail)
    assert "eu-central-1" in str(error.value.detail)


def test_generic_gateway_remains_admitted_in_a_reduced_home_region(monkeypatch) -> None:
    monkeypatch.setattr(
        deployment_handler,
        "config",
        replace(deployment_handler.config, aws_region="eu-central-1"),
    )

    deployment_handler._reject_unsupported_managed_web_search_region(_deploy_request("strands-gateway-agent"))


def test_explicit_supported_target_region_overrides_the_home_region(monkeypatch) -> None:
    monkeypatch.setattr(
        deployment_handler,
        "config",
        replace(deployment_handler.config, aws_region="eu-central-1"),
    )
    request = _deploy_request("web-search-agent", targetRegion="eu-west-1")

    deployment_handler._reject_unsupported_managed_web_search_region(request)


def test_explicit_unsupported_target_region_still_fails_model_admission() -> None:
    with pytest.raises(ValidationError, match="Web Search is not available.*eu-central-1"):
        _deploy_request("web-search-agent", targetRegion="eu-central-1")


def test_live_region_gate_precedes_deployment_state_persistence() -> None:
    source = inspect.getsource(deployment_handler.handle_deploy)

    assert source.index("_reject_unsupported_managed_web_search_region(request)") < source.index("store.create(state)")


def test_legacy_duckduckgo_id_migrates_to_managed_search_not_a_lambda_tool() -> None:
    assert search.without_managed_web_search_tools(["duckduckgo_search", "weather_api", "web_search"]) == [
        "weather_api"
    ]


@pytest.mark.parametrize("region", sorted(search.MANAGED_WEB_SEARCH_SUPPORTED_REGIONS))
def test_supported_region_produces_the_exact_service_owned_resource(region: str) -> None:
    assert search.managed_web_search_resource_arn(region) == (
        f"arn:aws:bedrock-agentcore:{region}:aws:tool/web-search.v1"
    )


def test_unsupported_region_is_rejected_before_target_creation() -> None:
    with pytest.raises(ValueError, match="not available.*eu-central-1"):
        search.require_managed_web_search_region("eu-central-1")


def test_gateway_id_is_required() -> None:
    with pytest.raises(ValueError, match="gateway_id is required"):
        search.managed_web_search_target_params("")


def test_removal_mutation_drops_the_connector_version_pin() -> None:
    source_path = Path(search.__file__)
    source = source_path.read_text(encoding="utf-8")
    pin = '                        "version": MANAGED_WEB_SEARCH_CONNECTOR_VERSION,\n'
    assert source.count(pin) == 1
    mutant = source.replace(pin, "", 1)
    namespace: dict[str, Any] = {"__name__": "managed_web_search_unpinned_mutant"}
    exec(compile(mutant, str(source_path), "exec"), namespace)

    params = namespace["managed_web_search_target_params"]("gateway-123")

    assert "version" not in params["targetConfiguration"]["mcp"]["connector"]["source"]
    assert (
        search.managed_web_search_target_params("gateway-123")["targetConfiguration"]["mcp"]["connector"]["source"][
            "version"
        ]
        == "1.2.0"
    )
