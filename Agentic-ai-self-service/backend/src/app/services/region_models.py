"""Bedrock cross-region inference profile IDs, derived from the deploy region.

Bedrock's cross-region inference profiles are namespaced by geography:
``us.anthropic.claude-sonnet-5``, ``eu.anthropic.claude-sonnet-5``,
``ap.anthropic.claude-sonnet-5``, and the geography-independent
``global.anthropic.…``. **A ``us.`` profile does not exist in eu-central-1.**
The runtime accepts ``MODEL_ID=us.anthropic.claude-sonnet-5`` without complaint
and then fails on every single invoke — which is why this module exists.

The platform's own catalogs, defaults, and every workflow saved before the
platform became region-agnostic carry ``us.`` baked in, so the prefix has to be
*re-pointed* at read time rather than merely defaulted.

This module is deliberately dependency-free (stdlib only, no boto3, no models)
so that step-handler Lambdas can import it as cheaply as the API Lambda does.

Four implementations of this prefix rule exist and must agree, or the model a
user picks in the UI is not the model the deployed agent invokes:

  * this module — the backend's single source of truth
  * ``getRegionPrefixFor()`` in ``frontend/src/utils/awsRegion.ts``
  * the ``TOOL_GENERATOR_MODEL_ID`` expression in ``infra/stacks/platform/lambdas.py``
  * the ``eu``/``ap``/``us`` branch in ``infra/stacks/platform/config.py``'s region logic

``backend/tests/test_region_model_prefix.py`` pins the first, and
``infra/tests/test_region_agnostic.py`` pins the third.
"""

from __future__ import annotations

import os
import re

# Prefixes that already denote a cross-region inference profile.
# ``apac.`` is the real APAC family (see region_inference_prefix). ``ap.`` is
# kept alongside it purely as an input we must tolerate: it exists in no region,
# but it is what this repo told people to type before, so a hand-written
# ``ap.anthropic.…`` has to be recognised as already-prefixed — otherwise
# regionalizing it would produce ``eu.ap.anthropic.…``.
CROSS_REGION_PREFIXES = ("us.", "global.", "eu.", "apac.", "ap.")

# Geography-scoped prefixes, i.e. the ones that are wrong in the wrong region.
# ``global.`` is deliberately excluded: it is region-independent by design and
# rewriting it to ``eu.`` would point at a different, possibly nonexistent,
# profile. Order is irrelevant — "apac.x" does not start with "ap.".
_GEO_PREFIXES = ("us.", "eu.", "apac.", "ap.")

_DATE_SUFFIX = re.compile(r"-\d{8}$")
_VERSION_SUFFIX = re.compile(r"-v\d+:\d+$")


def current_region() -> str:
    """The region this code is running in.

    ``APP_AWS_REGION`` is the platform's own variable and wins over Lambda's
    injected ``AWS_REGION``, matching the ~70 other call sites in the backend.
    """
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))


def region_inference_prefix(region: str | None = None) -> str:
    """``us-*`` → ``us``, ``eu-*`` → ``eu``, ``ap-*`` → ``apac``, else ``us``.

    Regions with no cross-region profile family of their own (ca-, sa-, me-, …)
    fall back to ``us``, which is both the widest-available family and the
    pre-existing behaviour.

    APAC is ``apac``, not ``ap``. The two pre-existing sites in this repo both
    said ``ap``, and that prefix does not exist in any region — verified against
    ``bedrock list-inference-profiles``, whose distinct prefixes are:

        us-east-1        us, global
        eu-central-1     eu, global
        ap-northeast-1   apac, global, jp
        ap-southeast-2   apac, global, au

    Two caveats that this function cannot express, recorded here so the next
    reader does not mistake them for bugs:

    * The ``apac.`` family only covers the OLDER Claude models. For the models
      this platform actually permits (Oct 2025 – May 2026), APAC regions publish
      COUNTRY-scoped profiles instead — ``jp.`` in ap-northeast-1, ``au.`` in
      ap-southeast-2 — and ap-south-1 publishes only ``global.``. So an APAC
      deployment on a current model needs a country or ``global`` prefix that a
      region→prefix map cannot derive. ``apac`` is still the right answer here
      because it is correct for the family this maps to and is strictly better
      than ``ap``, which resolves to nothing anywhere.
    * ``global.`` exists in every region checked and would be the portable
      choice, but switching the default to it is a behaviour change for existing
      us-east-1 deployments and is out of scope for the region work.
    """
    region = region or current_region()
    if region.startswith("eu-"):
        return "eu"
    if region.startswith("ap-"):
        return "apac"
    return "us"


def has_date_suffix(model_id: str) -> bool:
    """True for legacy dated IDs like ``…claude-haiku-4-5-20251001``.

    Current-generation IDs (``claude-sonnet-5``, ``claude-opus-4-8``) carry no
    date segment and must not receive a ``-v1:0`` suffix.
    """
    return bool(_DATE_SUFFIX.search(model_id))


def has_version_suffix(model_id: str) -> bool:
    """True when the ID already ends in a version suffix like ``-v1:0``."""
    return bool(_VERSION_SUFFIX.search(model_id))


def _add_legacy_version_suffix(model_id: str) -> str:
    """Only LEGACY dated Anthropic profiles need a ``-v1:0`` suffix.

    Appending one to a dateless current-generation ID
    (``us.anthropic.claude-sonnet-5-v1:0``) produces an invalid model ID.
    """
    if "anthropic." in model_id and has_date_suffix(model_id) and not has_version_suffix(model_id):
        return f"{model_id}-v1:0"
    return model_id


def to_cross_region_model_id(model_id: str, region: str | None = None) -> str:
    """Give an on-demand model ID a cross-region prefix; leave existing ones be.

    On-demand IDs like ``anthropic.claude-sonnet-5`` fail with
    ValidationException on the Bedrock converse API. An ID that already carries
    an explicit prefix is respected — use :func:`to_regional_model_id` when the
    stored prefix should be overridden by the region instead.
    """
    if not model_id:
        return model_id
    if not model_id.startswith(CROSS_REGION_PREFIXES):
        model_id = f"{region_inference_prefix(region)}.{model_id}"
    return _add_legacy_version_suffix(model_id)


def to_regional_model_id(model_id: str, region: str | None = None) -> str:
    """Re-point a model ID at ``region``, overriding any geography prefix.

    This is the one to use for anything read out of storage or off a default:
    the platform's catalogs are written with ``us.``, and a workflow saved while
    the frontend pointed at a US region carries ``us.`` too. ``global.`` passes
    through untouched.
    """
    if not model_id:
        return model_id
    prefix = region_inference_prefix(region)
    for geo in _GEO_PREFIXES:
        if model_id.startswith(geo):
            return _add_legacy_version_suffix(f"{prefix}.{model_id[len(geo) :]}")
    return to_cross_region_model_id(model_id, region)


def repoint_regional_prefix(model_id: str, region: str | None = None) -> str:
    """Re-point an EXISTING geography prefix at ``region``; never add one.

    Use this — not :func:`to_regional_model_id` — wherever the ID may legitimately
    be a plain on-demand foundation model. Embedding models
    (``amazon.titan-embed-text-v2:0``, ``cohere.embed-english-v3``) have no
    cross-region inference profiles at all, so giving one a ``eu.`` prefix
    produces a ``foundation-model/eu.amazon.titan-…`` ARN that Bedrock rejects.
    """
    if not model_id:
        return model_id
    prefix = region_inference_prefix(region)
    for geo in _GEO_PREFIXES:
        if model_id.startswith(geo):
            return _add_legacy_version_suffix(f"{prefix}.{model_id[len(geo) :]}")
    return model_id


def regionalize_catalog(models: list[dict], region: str | None = None, key: str = "modelId") -> list[dict]:
    """Re-point every geography-prefixed model ID in a catalog list.

    Returns new dicts; the input is not mutated (module-level catalog constants
    are shared across requests).
    """
    prefix_region = region
    out = []
    for entry in models:
        model_id = entry.get(key)
        if isinstance(model_id, str) and model_id.startswith(_GEO_PREFIXES):
            out.append({**entry, key: to_regional_model_id(model_id, prefix_region)})
        else:
            out.append(entry)
    return out
