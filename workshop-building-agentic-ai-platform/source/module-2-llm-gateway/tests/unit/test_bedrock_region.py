"""Unit tests for the shared Bedrock region/geo-prefix helper."""

import importlib.util
import os

import pytest

# Load bedrock_region.py from the sibling scripts/ directory without requiring
# a package layout.
_HELPER_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "..", "scripts", "bedrock_region.py",
    )
)
_spec = importlib.util.spec_from_file_location("bedrock_region", _HELPER_PATH)
bedrock_region = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bedrock_region)

geo_prefix = bedrock_region.geo_prefix
model_id = bedrock_region.model_id
resolve_region = bedrock_region.resolve_region
resolve = bedrock_region.resolve
available_ids = bedrock_region.available_ids


class _FakeBedrock:
    """Minimal stand-in for the bedrock client used by available_ids()."""

    def __init__(self, profiles=(), models=(), pages=1):
        self._profiles = list(profiles)
        self._models = list(models)
        self._pages = pages
        self.list_inference_profiles_calls = 0

    def list_inference_profiles(self, **kwargs):
        self.list_inference_profiles_calls += 1
        # Emit one profile per page to exercise nextToken pagination.
        idx = self.list_inference_profiles_calls - 1
        chunk = self._profiles[idx : idx + 1]
        resp = {"inferenceProfileSummaries": [{"inferenceProfileId": p} for p in chunk]}
        if self.list_inference_profiles_calls < len(self._profiles):
            resp["nextToken"] = "more"
        return resp

    def list_foundation_models(self, **kwargs):
        return {
            "modelSummaries": [
                {"modelId": m, "inferenceTypesSupported": types}
                for m, types in self._models
            ]
        }


@pytest.mark.parametrize(
    "region,expected",
    [
        ("us-east-1", "us."),
        ("eu-west-1", "eu."),
        ("ap-southeast-2", "apac."),
        ("me-central-1", "apac."),
        ("af-south-1", "apac."),
        ("ca-central-1", "us."),
    ],
)
def test_geo_prefix(region, expected):
    assert geo_prefix(region) == expected


def test_model_id_prefer_global():
    assert (
        model_id("anthropic.claude-sonnet-4-6", "ap-southeast-1", prefer_global=True)
        == "bedrock/global.anthropic.claude-sonnet-4-6"
    )


def test_model_id_bare_no_profile():
    assert (
        model_id("deepseek.v3.2", "us-east-1", needs_profile=False)
        == "bedrock/deepseek.v3.2"
    )


def test_model_id_geo_profile():
    assert (
        model_id("meta.llama3-3-70b-instruct-v1:0", "eu-west-1")
        == "bedrock/eu.meta.llama3-3-70b-instruct-v1:0"
    )


def test_resolve_region_explicit_wins():
    assert resolve_region("eu-central-1") == "eu-central-1"


def test_resolve_region_empty_raises(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    # Force boto3 session resolution to return nothing.
    if bedrock_region.boto3 is not None:
        monkeypatch.setattr(
            bedrock_region.boto3,
            "Session",
            lambda *a, **k: type("S", (), {"region_name": None})(),
        )
    with pytest.raises(ValueError):
        resolve_region()


def test_available_ids_unions_profiles_and_on_demand_models():
    client = _FakeBedrock(
        profiles=["global.anthropic.claude-sonnet-4-6", "us.deepseek.r1-v1:0"],
        models=[
            ("deepseek.v3.2", ["ON_DEMAND"]),
            # INFERENCE_PROFILE-only: the bare id is NOT invocable.
            ("anthropic.claude-opus-4-6-v1", ["INFERENCE_PROFILE"]),
            ("amazon.titan-text-express-v1", ["PROVISIONED"]),
        ],
    )
    ids = available_ids("test-region-1", client=client)
    assert "global.anthropic.claude-sonnet-4-6" in ids  # paginated page 1
    assert "us.deepseek.r1-v1:0" in ids  # paginated page 2
    assert "deepseek.v3.2" in ids
    assert "anthropic.claude-opus-4-6-v1" not in ids
    assert "amazon.titan-text-express-v1" not in ids


def test_available_ids_caches_per_region():
    client = _FakeBedrock(profiles=["global.x"], models=[])
    first = available_ids("test-region-cache-1", client=client)
    calls = client.list_inference_profiles_calls
    second = available_ids("test-region-cache-1", client=client)
    assert first is second
    assert client.list_inference_profiles_calls == calls  # served from cache


def test_resolve_falls_back_to_a_flavor_that_exists():
    # Hint says "global profile", but only the geo profile exists.
    available = frozenset({"us.anthropic.claude-opus-4-1-20250805-v1:0"})
    assert (
        resolve(
            "anthropic.claude-opus-4-1-20250805-v1:0",
            "us-west-2",
            prefer_global=True,
            available=available,
        )
        == "bedrock/us.anthropic.claude-opus-4-1-20250805-v1:0"
    )


def test_resolve_falls_back_from_profile_to_bare_model():
    # Hint says "needs a profile", but the model is a bare ON_DEMAND one.
    available = frozenset({"deepseek.v3-v1:0"})
    assert (
        resolve("deepseek.v3-v1:0", "us-west-2", needs_profile=True, available=available)
        == "bedrock/deepseek.v3-v1:0"
    )


def test_resolve_prefers_the_hinted_flavor_when_several_exist():
    available = frozenset(
        {"global.anthropic.claude-sonnet-4-6", "us.anthropic.claude-sonnet-4-6"}
    )
    assert (
        resolve("anthropic.claude-sonnet-4-6", "us-west-2",
                prefer_global=True, available=available)
        == "bedrock/global.anthropic.claude-sonnet-4-6"
    )
    assert (
        resolve("anthropic.claude-sonnet-4-6", "us-west-2",
                prefer_global=False, available=available)
        == "bedrock/us.anthropic.claude-sonnet-4-6"
    )


def test_resolve_returns_none_for_a_retired_model():
    # claude-3.5-haiku is absent from us-west-2/us-east-1/eu-west-1 (verified live).
    available = frozenset({"global.anthropic.claude-sonnet-4-6"})
    assert (
        resolve("anthropic.claude-3-5-haiku-20241022-v1:0", "us-west-2",
                prefer_global=True, available=available)
        is None
    )


def test_resolve_unvalidated_when_inventory_unknown(monkeypatch):
    # No inventory (e.g. missing bedrock:ListFoundationModels) -> documented
    # behaviour, never a silent "everything is unavailable".
    monkeypatch.setattr(bedrock_region, "available_ids", lambda *a, **k: None)
    assert (
        resolve("meta.llama3-3-70b-instruct-v1:0", "eu-west-1")
        == "bedrock/eu.meta.llama3-3-70b-instruct-v1:0"
    )
