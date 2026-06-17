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
   Models invoked directly without any inference profile (titan, cohere, ai21,
   etc.) take NO prefix ever. Pass ``needs_profile=False``. Example:
   ``bedrock/amazon.titan-text-premier-v1:0``.

The ``suffix`` argument to :func:`model_id` is always the BARE model id WITHOUT
any geo/global prefix and WITHOUT the ``bedrock/`` provider segment, e.g.
``anthropic.claude-sonnet-4-6``.

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
        ("amazon.titan-text-premier-v1:0", "us-east-1",
         {"needs_profile": False}, "bedrock/amazon.titan-text-premier-v1:0"),
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
