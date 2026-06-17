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
        model_id("amazon.titan-text-premier-v1:0", "us-east-1", needs_profile=False)
        == "bedrock/amazon.titan-text-premier-v1:0"
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
