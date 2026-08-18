"""Shared Bedrock region / geo-prefix helper for the LLM Gateway workshop.

This module is the single source of truth for turning an AWS region into a
correct Bedrock model identifier. It is imported by ``setup_keys.py`` and
copy-able into notebooks and agent ``_create_model()`` paths so that every
surface produces an identical model id for a given region.

There are THREE flavors of Bedrock model identifier, and choosing the wrong
one yields a ``ValidationException`` at invoke time:

1. ``global.`` cross-region inference profiles
   --------------------------------------------
   ``global.`` inference profiles exist in ALL regions for the Claude 4.x
   family (verified live: us-east-1, us-west-2, eu-west-1, eu-central-1,
   ap-southeast-1, ap-northeast-1). They are region-agnostic and require zero
   geo derivation, so they are the MOST ROBUST choice and the PREFERRED option
   for the core workshop models (e.g. ``anthropic.claude-sonnet-4-6``). Pass
   ``prefer_global=True`` to get one. Example:
   ``bedrock/global.anthropic.claude-sonnet-4-6``.

2. ``us.`` / ``eu.`` / ``apac.`` geo-scoped inference profiles
   -----------------------------------------------------------
   Models that need a cross-region inference profile but do NOT have a
   ``global.`` profile (older Nova v1, Llama, Mistral, etc.) must use the geo
   prefix derived from the deploy region. The geo prefix is geo-bounded:
   us-*/ca-*/sa- -> "us.", eu-* -> "eu.", ap-*/me-*/af-* -> "apac.".
   NOTE: the Asia-Pacific prefix is "apac." NOT "ap." despite the ``ap-*``
   region codes. Example: ``bedrock/eu.meta.llama3-3-70b-instruct-v1:0``.

3. bare (non-profile) foundation models
   -------------------------------------
   Models invoked directly without any inference profile (DeepSeek, Mistral,
   Qwen, etc.) take NO prefix ever. Pass ``needs_profile=False``.
   Example: ``bedrock/deepseek.v3.2``.

   The example used to be a Cohere Command R+ id, which reached end-of-life on
   2026-08-19 and now reports LEGACY. It was only ever an illustration --
   nothing here invokes it -- but an example that 404s teaches the wrong thing,
   so it is now an id verified ACTIVE and ON_DEMAND by
   ``list-foundation-models``.

The ``suffix`` argument to :func:`model_id` is always the BARE model id WITHOUT
any geo/global prefix and WITHOUT the ``bedrock/`` provider segment, e.g.
``anthropic.claude-sonnet-4-6``.

Which flavor a given model needs is NOT stable: models are retired, and a model
that needs an inference profile in one region is often invocable bare (or not
present at all) in another. Verified live on 2026-08-15, of the 23 models the
workshop table then carried, 4 resolved to a non-existent id in us-west-2 and 11
in eu-west-1 (one, ``claude-3.5-haiku``, was retired in all three). So a
hardcoded table cannot be correct everywhere.

:func:`resolve` therefore treats the ``needs_profile`` / ``prefer_global`` flags
as *preference hints* and validates the result against the model inventory the
deploy region actually reports (:func:`available_ids`), returning ``None`` when
a model has no invocable form there. Callers skip those instead of registering
endpoints that fail at invoke time.

Pure stdlib + boto3 only.
"""

import os

try:
    import boto3
except ImportError:  # boto3 is optional for the pure-string helpers
    boto3 = None


# Map of region prefix -> Bedrock geo inference-profile prefix.
# Canada (ca-) and South America (sa-) route via the US geo today.
# NOTE: Asia-Pacific (ap-), Middle East (me-) and Africa (af-) all use "apac."
# (not "ap.").
_GEO_MAP = {
    "us": "us.",
    "ca": "us.",
    "sa": "us.",
    "eu": "eu.",
    "ap": "apac.",
    "me": "apac.",
    "af": "apac.",
}


def geo_prefix(region):
    """Return the Bedrock cross-region inference-profile geo prefix for a region.

    us-*/ca-*/sa-* -> "us.", eu-* -> "eu.", ap-*/me-*/af-* -> "apac.".
    Defaults to "us." for anything unrecognized.
    """
    prefix = (region or "").split("-")[0]
    return _GEO_MAP.get(prefix, "us.")


def resolve_region(explicit=None):
    """Resolve the active AWS region.

    Resolution order:
      explicit > AWS_REGION env > AWS_DEFAULT_REGION env > boto3 Session().region_name.

    Raises a clear ``ValueError`` if none can be determined. Refuses ONLY when
    empty; never refuses on inequality to a literal.
    """
    candidates = [
        explicit,
        os.environ.get("AWS_REGION"),
        os.environ.get("AWS_DEFAULT_REGION"),
    ]
    if boto3 is not None:
        try:
            candidates.append(boto3.Session().region_name)
        except Exception:
            pass

    for candidate in candidates:
        if candidate:
            return candidate

    raise ValueError(
        "Could not resolve an AWS region. Provide one explicitly, or set the "
        "AWS_REGION / AWS_DEFAULT_REGION environment variable, or configure a "
        "default region (e.g. `aws configure set region <region>`)."
    )


def model_id(suffix, region, needs_profile=True, prefer_global=False):
    """Build a Bedrock model identifier from a bare model suffix.

    ``suffix`` is the bare model id WITHOUT geo/global prefix and WITHOUT the
    ``bedrock/`` provider segment (e.g. ``anthropic.claude-sonnet-4-6``).

    - ``prefer_global=True``  -> ``bedrock/global.<suffix>`` (region-agnostic,
      preferred for Claude 4.x core workshop models).
    - else ``needs_profile=True`` -> ``bedrock/<geo>.<suffix>`` where geo is
      derived from ``region`` via :func:`geo_prefix`.
    - else (bare model) -> ``bedrock/<suffix>`` with no prefix.
    """
    if prefer_global:
        return "bedrock/global." + suffix
    if needs_profile:
        return "bedrock/" + geo_prefix(region) + suffix
    return "bedrock/" + suffix


# Cache of region -> frozenset of invocable ids, so a caller resolving 17 models
# makes two Bedrock calls total rather than 34.
_AVAILABLE_CACHE = {}


def available_ids(region, client=None):
    """Return the set of Bedrock ids invocable on demand in ``region``.

    The set is the union of every cross-region inference profile id and every
    foundation model that advertises ``ON_DEMAND``. PROVISIONED-only and
    INFERENCE_PROFILE-only foundation models are deliberately excluded: their
    bare id cannot be invoked, only the matching profile id can.

    Returns ``None`` (meaning "unknown, do not filter") if boto3 is missing or
    Bedrock cannot be queried — callers must treat that as "skip validation"
    rather than "nothing is available".
    """
    if region in _AVAILABLE_CACHE:
        return _AVAILABLE_CACHE[region]
    if boto3 is None and client is None:
        return None
    try:
        bedrock = client or boto3.client("bedrock", region_name=region)
        ids = set()
        paginator_kwargs = {"maxResults": 100}
        token = None
        while True:
            if token:
                paginator_kwargs["nextToken"] = token
            resp = bedrock.list_inference_profiles(**paginator_kwargs)
            ids.update(
                p["inferenceProfileId"] for p in resp.get("inferenceProfileSummaries", [])
            )
            token = resp.get("nextToken")
            if not token:
                break
        for summary in bedrock.list_foundation_models().get("modelSummaries", []):
            if "ON_DEMAND" in (summary.get("inferenceTypesSupported") or []):
                ids.add(summary["modelId"])
    except Exception:
        return None
    available = frozenset(ids)
    _AVAILABLE_CACHE[region] = available
    return available


def resolve(suffix, region, needs_profile=True, prefer_global=False, available=None):
    """Return an invocable ``bedrock/<id>`` for ``suffix`` in ``region``, or None.

    ``needs_profile`` / ``prefer_global`` order the candidates by preference;
    every candidate flavor is then tried against ``available`` (defaults to
    :func:`available_ids` for the region) and the first invocable one wins. This
    is what makes a single model table work across regions with different
    inventories, and makes a retired model skip itself instead of registering an
    endpoint that fails at invoke time.

    Returns ``None`` when no flavor of the model exists in the region. When the
    inventory is unknown, falls back to the unvalidated :func:`model_id` result
    so an IAM-restricted caller still gets the documented behaviour.
    """
    if available is None:
        available = available_ids(region)
    preferred = model_id(
        suffix, region, needs_profile=needs_profile, prefer_global=prefer_global
    )
    if available is None:
        return preferred
    # Preference order: caller's choice first, then the remaining two flavors.
    candidates = [preferred]
    for flavor in (
        "bedrock/global." + suffix,
        "bedrock/" + geo_prefix(region) + suffix,
        "bedrock/" + suffix,
    ):
        if flavor not in candidates:
            candidates.append(flavor)
    for candidate in candidates:
        if candidate[len("bedrock/"):] in available:
            return candidate
    return None


if __name__ == "__main__":
    # Runnable self-test: prints derived ids for a few representative cases.
    print("geo_prefix self-test:")
    for r, expected in [
        ("us-east-1", "us."),
        ("ca-central-1", "us."),
        ("sa-east-1", "us."),
        ("eu-west-1", "eu."),
        ("eu-central-1", "eu."),
        ("ap-southeast-2", "apac."),
        ("me-central-1", "apac."),
        ("af-south-1", "apac."),
        ("unknown", "us."),
    ]:
        got = geo_prefix(r)
        status = "OK" if got == expected else "FAIL"
        print("  {:14s} -> {:6s} (expected {:6s}) [{}]".format(r, got, expected, status))
        assert got == expected, "geo_prefix({!r}) == {!r} != {!r}".format(r, got, expected)

    print("\nmodel_id self-test:")
    cases = [
        # (suffix, region, kwargs, expected)
        ("anthropic.claude-sonnet-4-6", "ap-southeast-1",
         {"prefer_global": True}, "bedrock/global.anthropic.claude-sonnet-4-6"),
        ("deepseek.v3.2", "us-east-1",
         {"needs_profile": False}, "bedrock/deepseek.v3.2"),
        ("meta.llama3-3-70b-instruct-v1:0", "eu-west-1",
         {}, "bedrock/eu.meta.llama3-3-70b-instruct-v1:0"),
        ("anthropic.claude-sonnet-4-6", "us-west-2",
         {}, "bedrock/us.anthropic.claude-sonnet-4-6"),
    ]
    for suffix, region, kwargs, expected in cases:
        got = model_id(suffix, region, **kwargs)
        status = "OK" if got == expected else "FAIL"
        print("  {} -> {} [{}]".format(suffix, got, status))
        assert got == expected, "model_id mismatch: {!r} != {!r}".format(got, expected)

    print("\nresolve_region:")
    try:
        print("  resolved ->", resolve_region("eu-west-1"))
    except ValueError as exc:
        print("  ValueError:", exc)

    print("\nAll self-tests passed.")
