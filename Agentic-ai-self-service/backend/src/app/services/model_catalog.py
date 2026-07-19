"""Live model catalog (Loom-study 5.1).

The frontend model picker was a hardcoded list — every new Bedrock model or
retirement needed a code change. This service discovers text-capable models
LIVE from Bedrock (list_foundation_models + list_inference_profiles) and merges
them with a small curated overlay (friendly labels + context sizes the AWS APIs
don't return), degrading to the curated list if the API is unavailable.

Import-safe: boto3 is imported but no client is CREATED until list_models() runs.
"""

from __future__ import annotations

import logging
import os

import boto3

logger = logging.getLogger(__name__)

# Curated overlay: friendly label + context window for known models (the AWS APIs
# don't return a display label or max-tokens). Keyed by a substring of the modelId
# so it matches both bare ids and inference-profile ids.
_CURATED: dict[str, dict] = {
    "claude-opus-4": {"label": "Claude Opus", "maxTokens": 200000},
    "claude-sonnet-5": {"label": "Claude Sonnet 5", "maxTokens": 200000},
    "claude-sonnet-4": {"label": "Claude Sonnet 4", "maxTokens": 200000},
    "claude-haiku-4": {"label": "Claude Haiku 4.5", "maxTokens": 200000},
    "nova-premier": {"label": "Amazon Nova Premier", "maxTokens": 300000},
    "nova-2-lite": {"label": "Amazon Nova 2 Lite", "maxTokens": 300000},
    "nova-pro": {"label": "Amazon Nova Pro", "maxTokens": 300000},
}

# Fallback set when Bedrock discovery is unavailable (mirrors the frontend static
# list so the picker is never empty).
_FALLBACK: list[dict] = [
    {"provider": "bedrock", "modelId": "us.anthropic.claude-sonnet-5", "label": "Claude Sonnet 5", "maxTokens": 200000},
    {"provider": "bedrock", "modelId": "us.anthropic.claude-opus-4-8", "label": "Claude Opus 4.8", "maxTokens": 200000},
    {
        "provider": "bedrock",
        "modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "label": "Claude Haiku 4.5",
        "maxTokens": 200000,
    },
]


def _curated_for(model_id: str) -> dict:
    for key, meta in _CURATED.items():
        if key in model_id:
            return meta
    return {}


def _friendly_label(model_id: str, fallback_name: str) -> str:
    meta = _curated_for(model_id)
    return meta.get("label") or fallback_name or model_id


def list_models(region: str | None = None) -> list[dict]:
    """Return the merged, deduped live model catalog for the picker.

    Each entry: {provider, modelId, label, maxTokens, source}. TEXT models only
    (a chat/agent picker). Inference-profile ids (us./eu./ap. prefixed) are
    preferred where present since those are what agents actually invoke.
    """
    region = region or os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))
    try:
        client = boto3.client("bedrock", region_name=region)
    except Exception:  # noqa: BLE001
        return list(_FALLBACK)

    out: dict[str, dict] = {}

    # 1. Inference profiles first (the cross-region ids agents invoke: us.*, eu.*).
    try:
        resp = client.list_inference_profiles()
        for p in resp.get("inferenceProfileSummaries", []):
            if (p.get("status") or "ACTIVE") != "ACTIVE":
                continue
            pid = p.get("inferenceProfileId", "")
            if not pid or ("claude" not in pid and "nova" not in pid):
                continue  # keep the picker to the well-known text families
            out[pid] = {
                "provider": "bedrock",
                "modelId": pid,
                "label": _friendly_label(pid, p.get("inferenceProfileName", "")),
                "maxTokens": _curated_for(pid).get("maxTokens", 200000),
                "source": "inference_profile",
            }
    except Exception as e:  # noqa: BLE001
        logger.info("list_inference_profiles unavailable: %s", str(e)[:120])

    # 2. Foundation models — TEXT output, on-demand, that we don't already have.
    try:
        resp = client.list_foundation_models(byOutputModality="TEXT")
        for mdl in resp.get("modelSummaries", []):
            mid = mdl.get("modelId", "")
            if not mid:
                continue
            lifecycle = (mdl.get("modelLifecycle") or {}).get("status", "ACTIVE")
            if lifecycle != "ACTIVE":
                continue
            if "ON_DEMAND" not in (mdl.get("inferenceTypesSupported") or ["ON_DEMAND"]):
                continue
            if mid in out:
                continue
            out[mid] = {
                "provider": "bedrock",
                "modelId": mid,
                "label": _friendly_label(mid, mdl.get("modelName", "")),
                "maxTokens": _curated_for(mid).get("maxTokens", 200000),
                "source": "foundation_model",
            }
    except Exception as e:  # noqa: BLE001
        logger.info("list_foundation_models unavailable: %s", str(e)[:120])

    if not out:
        return list(_FALLBACK)
    # Curated/known families first, then the rest, alphabetized within.
    models = list(out.values())
    models.sort(key=lambda m: (0 if _curated_for(m["modelId"]) else 1, m["label"].lower()))
    return models
