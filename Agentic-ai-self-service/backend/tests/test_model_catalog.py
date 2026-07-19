"""Tests for the live model catalog (Loom-study 5.1)."""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

import app.services.model_catalog as mc  # noqa: E402


class _FakeBedrock:
    def __init__(self, profiles=None, models=None, fail=False):
        self._profiles = profiles or []
        self._models = models or []
        self._fail = fail

    def list_inference_profiles(self):
        if self._fail:
            raise RuntimeError("nope")
        return {"inferenceProfileSummaries": self._profiles}

    def list_foundation_models(self, byOutputModality=None):  # noqa: N803, ARG002
        if self._fail:
            raise RuntimeError("nope")
        return {"modelSummaries": self._models}


def _patch(monkeypatch, fake):
    import types

    monkeypatch.setattr(mc, "boto3", types.SimpleNamespace(client=lambda *a, **k: fake), raising=False)


def test_merges_profiles_and_models_with_curated_labels(monkeypatch):
    fake = _FakeBedrock(
        profiles=[
            {
                "inferenceProfileId": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "inferenceProfileName": "raw",
                "status": "ACTIVE",
            }
        ],
        models=[
            {
                "modelId": "us.amazon.nova-2-lite-v1:0",
                "modelName": "Nova2Lite",
                "modelLifecycle": {"status": "ACTIVE"},
                "inferenceTypesSupported": ["ON_DEMAND"],
            }
        ],
    )
    _patch(monkeypatch, fake)
    out = mc.list_models("us-east-1")
    ids = {m["modelId"] for m in out}
    assert "us.anthropic.claude-haiku-4-5-20251001-v1:0" in ids
    assert "us.amazon.nova-2-lite-v1:0" in ids
    # curated label applied to the profile id
    haiku = next(m for m in out if "haiku" in m["modelId"])
    assert haiku["label"] == "Claude Haiku 4.5"


def test_filters_non_active_and_non_ondemand(monkeypatch):
    fake = _FakeBedrock(
        profiles=[],
        models=[
            {
                "modelId": "us.anthropic.claude-x",
                "modelName": "x",
                "modelLifecycle": {"status": "LEGACY"},
                "inferenceTypesSupported": ["ON_DEMAND"],
            },
            {
                "modelId": "us.anthropic.claude-y",
                "modelName": "y",
                "modelLifecycle": {"status": "ACTIVE"},
                "inferenceTypesSupported": ["PROVISIONED"],
            },
        ],
    )
    _patch(monkeypatch, fake)
    out = mc.list_models("us-east-1")
    # both filtered out → falls back to the non-empty fallback list
    assert out == mc._FALLBACK


def test_falls_back_when_bedrock_unavailable(monkeypatch):
    _patch(monkeypatch, _FakeBedrock(fail=True))
    out = mc.list_models("us-east-1")
    assert out == mc._FALLBACK


def test_profile_preferred_over_duplicate_foundation_model(monkeypatch):
    # Same id in both → only one entry, sourced from the profile pass.
    fake = _FakeBedrock(
        profiles=[
            {"inferenceProfileId": "us.anthropic.claude-sonnet-5", "inferenceProfileName": "p", "status": "ACTIVE"}
        ],
        models=[
            {
                "modelId": "us.anthropic.claude-sonnet-5",
                "modelName": "m",
                "modelLifecycle": {"status": "ACTIVE"},
                "inferenceTypesSupported": ["ON_DEMAND"],
            }
        ],
    )
    _patch(monkeypatch, fake)
    out = mc.list_models("us-east-1")
    matches = [m for m in out if m["modelId"] == "us.anthropic.claude-sonnet-5"]
    assert len(matches) == 1
    assert matches[0]["source"] == "inference_profile"
