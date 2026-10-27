"""Region capability contract and unsupported-feature fail-closed behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.services import aws_agent_registry as registry
from app.services import region_capabilities as capabilities

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGION_MATRIX = PROJECT_ROOT / "release" / "agentcore-regions.json"
EXPECTED_RELEASE_REGIONS = frozenset(
    {
        "eu-central-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
)
EXPECTED_WEB_SEARCH_REGIONS = frozenset(
    {
        "ap-northeast-1",
        "ap-southeast-2",
        "eu-west-1",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
)
EXPECTED_AGENT_REGISTRY_REGIONS = frozenset(
    {
        "ap-northeast-1",
        "ap-southeast-2",
        "ca-central-1",
        "eu-central-1",
        "eu-west-1",
        "eu-west-2",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
)


def _matrix_entries() -> dict[str, dict]:
    matrix = json.loads(REGION_MATRIX.read_text(encoding="utf-8"))
    return {entry["region"]: entry for entry in matrix["regions"]}


def _assert_matrix_runtime_parity(entries: dict[str, dict]) -> None:
    assert {
        region for region, entry in entries.items() if entry["production_release"]
    } == capabilities.SUPPORTED_SINGLE_REGION_RELEASES
    for region, entry in entries.items():
        assert entry["web_search"] is (region in capabilities.MANAGED_WEB_SEARCH_SUPPORTED_REGIONS)
        assert entry["agent_registry"] is (region in capabilities.AWS_AGENT_REGISTRY_SUPPORTED_REGIONS)


def test_release_regions_and_feature_flags_match_the_reviewed_matrix() -> None:
    entries = _matrix_entries()

    _assert_matrix_runtime_parity(entries)
    assert capabilities.SUPPORTED_SINGLE_REGION_RELEASES == EXPECTED_RELEASE_REGIONS
    assert capabilities.MANAGED_WEB_SEARCH_SUPPORTED_REGIONS == EXPECTED_WEB_SEARCH_REGIONS
    assert capabilities.AWS_AGENT_REGISTRY_SUPPORTED_REGIONS == EXPECTED_AGENT_REGISTRY_REGIONS


def test_full_release_regions_enable_both_optional_services() -> None:
    entries = _matrix_entries()
    for region in ("us-east-1", "us-east-2", "us-west-2", "eu-west-1"):
        assert entries[region]["profile"] == "full"
        profile = capabilities.region_feature_profile(region)
        assert profile.release_supported is True
        assert profile.managed_web_search is True
        assert profile.aws_agent_registry is True
        assert profile.unsupported_features == ()


def test_reduced_release_regions_have_capability_accurate_profiles() -> None:
    entries = _matrix_entries()
    for region in ("eu-central-1", "eu-west-2"):
        assert entries[region]["profile"] == "reduced"
        profile = capabilities.region_feature_profile(region)
        assert profile.release_supported is True
        assert profile.managed_web_search is False
        assert profile.aws_agent_registry is True
        assert profile.unsupported_features == ("Amazon Bedrock AgentCore Web Search",)

    paris = capabilities.region_feature_profile("eu-west-3")
    assert entries["eu-west-3"]["profile"] == "reduced"
    assert paris.release_supported is True
    assert paris.managed_web_search is False
    assert paris.aws_agent_registry is False
    assert paris.unsupported_features == (
        "Amazon Bedrock AgentCore Web Search",
        "AWS Agent Registry federation",
    )


def test_northern_california_and_unknown_regions_are_not_inferred_supported() -> None:
    entries = _matrix_entries()
    northern_california = entries["us-west-1"]
    assert northern_california == {
        "region": "us-west-1",
        "name": "US West (N. California)",
        "profile": "excluded",
        "core": True,
        "production_release": False,
        "memory": False,
        "agent_registry": False,
        "web_search": False,
        "harness": False,
    }
    for region in ("us-west-1", "us-west-9", "eu-central-9", "unknown"):
        profile = capabilities.region_feature_profile(region)
        assert profile.release_supported is False
        assert profile.managed_web_search is False
        assert profile.aws_agent_registry is False


def test_unavailable_messages_name_region_feature_and_actual_release_alternatives() -> None:
    web = capabilities.feature_unavailable_message("managed_web_search", "eu-central-1")
    assert "Europe (Frankfurt) (eu-central-1)" in web
    assert "Remove managed Web Search" in web
    for region in ("eu-west-1", "us-east-1", "us-east-2", "us-west-2"):
        assert region in web
    assert "eu-central-1" in web.split("supported release Regions:", 1)[0]
    assert "eu-central-1" not in web.split("supported release Regions:", 1)[1]

    aws_registry = capabilities.feature_unavailable_message("aws_agent_registry", "eu-west-3")
    assert "Europe (Paris) (eu-west-3)" in aws_registry
    assert "built-in DynamoDB agent registry remains available" in aws_registry
    for region in ("eu-central-1", "eu-west-1", "eu-west-2", "us-east-1", "us-east-2", "us-west-2"):
        assert region in aws_registry


def test_unsupported_registry_region_constructs_no_aws_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        registry,
        "_make_client",
        lambda service, region: calls.append((service, region)),
    )

    adapter = registry.AwsAgentRegistry("registry-id", region="eu-west-3")

    assert adapter.control is None
    assert adapter.data is None
    assert calls == []


def test_newly_supported_registry_region_constructs_both_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    clients: list[tuple[str, str]] = []

    def client(service: str, region: str) -> object:
        clients.append((service, region))
        return object()

    monkeypatch.setattr(registry, "_make_client", client)
    adapter = registry.AwsAgentRegistry("registry-id", region="eu-central-1")

    assert adapter.control is not None
    assert adapter.data is not None
    assert clients == [
        (registry.CONTROL_SERVICE, "eu-central-1"),
        (registry.DATA_SERVICE, "eu-central-1"),
    ]


def test_stale_optional_feature_flag_mutation_breaks_matrix_parity() -> None:
    entries = _matrix_entries()
    entries["eu-central-1"]["agent_registry"] = False

    with pytest.raises(AssertionError):
        _assert_matrix_runtime_parity(entries)


def test_adding_northern_california_to_release_selection_breaks_matrix_parity() -> None:
    entries = _matrix_entries()
    entries["us-west-1"]["production_release"] = True

    with pytest.raises(AssertionError):
        _assert_matrix_runtime_parity(entries)
