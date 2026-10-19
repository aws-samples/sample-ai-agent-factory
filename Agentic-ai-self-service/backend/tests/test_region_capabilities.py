"""Region capability contract and unsupported-feature fail-closed behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.services import aws_agent_registry as registry
from app.services import region_capabilities as capabilities

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGION_MATRIX = PROJECT_ROOT / "release" / "emea-agentcore-regions.json"


def test_release_regions_and_feature_flags_match_the_reviewed_matrix() -> None:
    matrix = json.loads(REGION_MATRIX.read_text(encoding="utf-8"))
    entries = {entry["region"]: entry for entry in matrix["regions"]}
    regions = set(entries)

    assert {
        region for region, entry in entries.items() if entry["production_release"]
    } == capabilities.SUPPORTED_EMEA_SINGLE_REGION_RELEASES
    assert {
        region for region, entry in entries.items() if entry["web_search"]
    } == capabilities.MANAGED_WEB_SEARCH_SUPPORTED_REGIONS & regions
    assert {
        region for region, entry in entries.items() if entry["agent_registry"]
    } == capabilities.AWS_AGENT_REGISTRY_SUPPORTED_REGIONS & regions


def test_reduced_regions_keep_core_support_but_disable_unavailable_features() -> None:
    for region in ("eu-central-1", "eu-west-2", "eu-west-3"):
        profile = capabilities.region_feature_profile(region)
        assert profile.release_supported is True
        assert profile.managed_web_search is False
        assert profile.aws_agent_registry is False
        assert profile.unsupported_features == (
            "Amazon Bedrock AgentCore Web Search",
            "AWS Agent Registry federation",
        )

    ireland = capabilities.region_feature_profile("eu-west-1")
    assert ireland.release_supported is True
    assert ireland.unsupported_features == ()


def test_regions_outside_the_reviewed_release_are_not_inferred_supported() -> None:
    stockholm = capabilities.region_feature_profile("eu-north-1")
    assert stockholm.release_supported is False
    assert capabilities.region_feature_profile("eu-south-1").release_supported is False
    assert capabilities.region_feature_profile("eu-south-2").release_supported is False


def test_unavailable_messages_name_region_feature_and_supported_fallback() -> None:
    web = capabilities.feature_unavailable_message("managed_web_search", "eu-central-1")
    assert "Europe (Frankfurt) (eu-central-1)" in web
    assert "Remove managed Web Search" in web
    aws_registry = capabilities.feature_unavailable_message("aws_agent_registry", "eu-west-2")
    assert "Europe (London) (eu-west-2)" in aws_registry
    assert "built-in DynamoDB agent registry remains available" in aws_registry


def test_unsupported_registry_region_constructs_no_aws_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        registry,
        "_make_client",
        lambda service, region: calls.append((service, region)),
    )

    adapter = registry.AwsAgentRegistry("registry-id", region="eu-central-1")

    assert adapter.control is None
    assert adapter.data is None
    assert calls == []


def test_supported_registry_region_constructs_both_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    clients: list[tuple[str, str]] = []

    def client(service: str, region: str) -> object:
        clients.append((service, region))
        return object()

    monkeypatch.setattr(registry, "_make_client", client)
    adapter = registry.AwsAgentRegistry("registry-id", region="eu-west-1")

    assert adapter.control is not None
    assert adapter.data is not None
    assert clients == [
        (registry.CONTROL_SERVICE, "eu-west-1"),
        (registry.DATA_SERVICE, "eu-west-1"),
    ]


def test_removing_frankfurt_from_release_selection_breaks_matrix_parity() -> None:
    matrix = json.loads(REGION_MATRIX.read_text(encoding="utf-8"))
    frankfurt = next(entry for entry in matrix["regions"] if entry["region"] == "eu-central-1")
    frankfurt["production_release"] = False
    selected = {entry["region"] for entry in matrix["regions"] if entry["production_release"]}

    assert selected != capabilities.SUPPORTED_EMEA_SINGLE_REGION_RELEASES
