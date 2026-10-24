"""Regional feature profiles for supported single-Region deployments.

The platform can run its core AgentCore stack in several EMEA Regions, but AWS
service availability is not uniform. These sets are intentionally explicit and
fail closed: a Region is unsupported for an optional feature until AWS documents
it and this reviewed contract is updated.
"""

from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_EMEA_SINGLE_REGION_RELEASES = frozenset(
    {
        "eu-central-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
    }
)

MANAGED_WEB_SEARCH_SUPPORTED_REGIONS = frozenset(
    {
        "ap-northeast-1",
        "eu-west-1",
        "us-east-1",
    }
)

AWS_AGENT_REGISTRY_SUPPORTED_REGIONS = frozenset(
    {
        "ap-northeast-1",
        "ap-southeast-2",
        "eu-west-1",
        "us-east-1",
        "us-west-2",
    }
)

_REGION_NAMES = {
    "eu-central-1": "Europe (Frankfurt)",
    "eu-west-1": "Europe (Ireland)",
    "eu-west-2": "Europe (London)",
    "eu-west-3": "Europe (Paris)",
}


@dataclass(frozen=True)
class RegionFeatureProfile:
    """The reviewed feature profile for one deployment Region."""

    region: str
    release_supported: bool
    managed_web_search: bool
    aws_agent_registry: bool

    @property
    def unsupported_features(self) -> tuple[str, ...]:
        features: list[str] = []
        if not self.managed_web_search:
            features.append("Amazon Bedrock AgentCore Web Search")
        if not self.aws_agent_registry:
            features.append("AWS Agent Registry federation")
        return tuple(features)


def region_display_name(region: str) -> str:
    """Return a stable human-readable Region label."""

    return _REGION_NAMES.get(region, region)


def region_feature_profile(region: str) -> RegionFeatureProfile:
    """Resolve a Region without inferring availability from nearby Regions."""

    return RegionFeatureProfile(
        region=region,
        release_supported=region in SUPPORTED_EMEA_SINGLE_REGION_RELEASES,
        managed_web_search=region in MANAGED_WEB_SEARCH_SUPPORTED_REGIONS,
        aws_agent_registry=region in AWS_AGENT_REGISTRY_SUPPORTED_REGIONS,
    )


def managed_web_search_region_supported(region: str) -> bool:
    """Whether AWS documents the managed Web Search connector in ``region``."""

    return region in MANAGED_WEB_SEARCH_SUPPORTED_REGIONS


def aws_agent_registry_region_supported(region: str) -> bool:
    """Whether AWS documents Agent Registry in ``region``."""

    return region in AWS_AGENT_REGISTRY_SUPPORTED_REGIONS


def feature_unavailable_message(feature: str, region: str) -> str:
    """Return one actionable, Region-specific unsupported-feature message."""

    where = f"{region_display_name(region)} ({region})"
    if feature == "managed_web_search":
        return (
            f"Amazon Bedrock AgentCore Web Search is not available in {where}. "
            "Remove managed Web Search from this workflow or deploy it in eu-west-1."
        )
    if feature == "aws_agent_registry":
        return (
            f"AWS Agent Registry federation is not available in {where}. "
            "The built-in DynamoDB agent registry remains available."
        )
    raise ValueError(f"Unknown regional feature: {feature}")
