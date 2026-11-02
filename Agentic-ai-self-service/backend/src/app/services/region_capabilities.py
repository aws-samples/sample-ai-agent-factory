"""Regional feature profiles for supported single-Region deployments.

AWS commercial-Region capability availability is not uniform. These sets are
intentionally explicit and fail closed: a Region is unsupported for release or
an optional feature until AWS documents it and this reviewed contract is
updated.
"""

from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_SINGLE_REGION_RELEASES = frozenset(
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

MANAGED_WEB_SEARCH_SUPPORTED_REGIONS = frozenset(
    {
        "ap-northeast-1",
        "ap-southeast-2",
        "eu-west-1",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
)

AWS_AGENT_REGISTRY_SUPPORTED_REGIONS = frozenset(
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

_REGION_NAMES = {
    "eu-central-1": "Europe (Frankfurt)",
    "eu-north-1": "Europe (Stockholm)",
    "eu-south-1": "Europe (Milan)",
    "eu-south-2": "Europe (Spain)",
    "eu-west-1": "Europe (Ireland)",
    "eu-west-2": "Europe (London)",
    "eu-west-3": "Europe (Paris)",
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
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


class UnsupportedReleaseRegionError(ValueError):
    """The Region is outside the reviewed single-Region production envelope."""


def release_region_unavailable_message(region: object, *, surface: str) -> str:
    """One actionable message naming the exact selected release Regions.

    ``surface`` names what was refused ("this live deployment", "a deployment target",
    "the platform stack") so the same text is correct from every admission point.
    """

    selected = ", ".join(
        f"{region_display_name(candidate)} ({candidate})" for candidate in sorted(SUPPORTED_SINGLE_REGION_RELEASES)
    )
    shown = region if isinstance(region, str) and region else "<unset>"
    return (
        f"Region {shown} is not a selected production release Region, so {surface} is refused. "
        f"Deploy in exactly one of: {selected}."
    )


def require_release_region(region: object, *, surface: str) -> str:
    """Return ``region`` only when it is one of the selected release Regions.

    Fails closed on an empty, non-string, or unlisted value. This is the single
    predicate behind the request model, the live deploy handler, admin target
    registration, target resolution, the state-machine payload validator, and the
    deploy-target options the UI is offered; none of them keeps its own list.
    """

    if not isinstance(region, str) or region not in SUPPORTED_SINGLE_REGION_RELEASES:
        raise UnsupportedReleaseRegionError(release_region_unavailable_message(region, surface=surface))
    return region


def region_feature_profile(region: str) -> RegionFeatureProfile:
    """Resolve a Region without inferring availability from nearby Regions."""

    return RegionFeatureProfile(
        region=region,
        release_supported=region in SUPPORTED_SINGLE_REGION_RELEASES,
        managed_web_search=region in MANAGED_WEB_SEARCH_SUPPORTED_REGIONS,
        aws_agent_registry=region in AWS_AGENT_REGISTRY_SUPPORTED_REGIONS,
    )


def managed_web_search_region_supported(region: str) -> bool:
    """Whether AWS documents the managed Web Search connector in ``region``."""

    return region in MANAGED_WEB_SEARCH_SUPPORTED_REGIONS


def aws_agent_registry_region_supported(region: str) -> bool:
    """Whether AWS documents Agent Registry in ``region``."""

    return region in AWS_AGENT_REGISTRY_SUPPORTED_REGIONS


def _format_release_alternatives(regions: frozenset[str]) -> str:
    selected = sorted(SUPPORTED_SINGLE_REGION_RELEASES & regions)
    return ", ".join(f"{region_display_name(region)} ({region})" for region in selected)


def feature_unavailable_message(feature: str, region: str) -> str:
    """Return one actionable, Region-specific unsupported-feature message."""

    where = f"{region_display_name(region)} ({region})"
    if feature == "managed_web_search":
        alternatives = _format_release_alternatives(MANAGED_WEB_SEARCH_SUPPORTED_REGIONS)
        return (
            f"Amazon Bedrock AgentCore Web Search is not available in {where}. "
            "Remove managed Web Search from this workflow or deploy it in one of "
            f"these supported release Regions: {alternatives}."
        )
    if feature == "aws_agent_registry":
        alternatives = _format_release_alternatives(AWS_AGENT_REGISTRY_SUPPORTED_REGIONS)
        return (
            f"AWS Agent Registry federation is not available in {where}. "
            "The built-in DynamoDB agent registry remains available; federation is "
            f"available in these supported release Regions: {alternatives}."
        )
    raise ValueError(f"Unknown regional feature: {feature}")
