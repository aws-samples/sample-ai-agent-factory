"""Which deployment owns a dynamically-created AWS resource.

Why this exists: the platform creates resources whose names are *account-global*
and carry no stack identity — Cognito pools named ``AgentCore-{gateway_name}``,
secrets under ``agentcore-connector/`` and ``agentcore-otel/``, IAM roles named
``AgentCoreMemory-{memory_name}``. ``scripts/cleanup.sh`` swept those namespaces by
name prefix, so a customer running two deployments in one account — dev + prod, or
two teams — destroyed the *other* deployment's resources on teardown, including
secrets holding raw customer credentials. Customers deploy and delete this often,
so that is a routine operation, not an edge case.

``ManagedBy=agentcore-flows`` (already on runtime exec roles) cannot fix it: it
names the *product*, so two deployments of this product are indistinguishable. The
owner tag here names the *stack instance*, which is the granularity teardown needs.

The value matches ``config.py``'s regional naming scheme (``{project}-{env}-{region}``)
so the same identity can be recomputed in bash by ``cleanup.sh`` without a lookup.

Ownership is a hard gate on deletion, and the safe default is to REFUSE: an
untagged resource is treated as foreign, because a resource predating this tag and
a resource belonging to someone else are indistinguishable, and only one of those
two mistakes is recoverable.
"""

from __future__ import annotations

import os

OWNER_TAG_KEY = "AgentCoreStack"

# Kept on every resource alongside the owner tag: existing IAM policy conditions
# and the Bug 139 ABAC grant match on it, so dropping it would break them.
PRODUCT_TAG_KEY = "ManagedBy"
PRODUCT_TAG_VALUE = "agentcore-flows"


def _region(region: str | None = None) -> str:
    if region:
        return region
    return os.environ.get("APP_AWS_REGION") or os.environ.get("AWS_REGION") or "us-east-1"


def stack_id(region: str | None = None) -> str:
    """Identity of the deployment that owns a resource: ``{project}-{env}-{region}``.

    Region is part of it because the same ``{project}-{env}`` is deliberately
    deployable to two regions (see ``config.py``), and a teardown in one region must
    not claim the other region's account-global resources — IAM roles in particular
    are not regional at all.
    """
    project = os.environ.get("PROJECT_NAME") or "agentcore-workflow"
    env = os.environ.get("ENVIRONMENT") or os.environ.get("ENVIRONMENT_NAME") or "dev"
    return f"{project}-{env}-{_region(region)}"


def owner_tags(region: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Tag map to stamp on a created resource, as ``{Key: Value}``.

    The owner and product tags are applied LAST so a caller-supplied governance tag
    (owner/cost-center/…) can never overwrite the two keys teardown depends on.
    """
    tags = {str(k): str(v) for k, v in (extra or {}).items() if k and k not in (OWNER_TAG_KEY, PRODUCT_TAG_KEY)}
    tags[PRODUCT_TAG_KEY] = PRODUCT_TAG_VALUE
    tags[OWNER_TAG_KEY] = stack_id(region)
    return tags


def owner_tag_list(region: str | None = None, extra: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Same tags in the ``[{"Key": k, "Value": v}]`` shape IAM/SecretsManager want."""
    return [{"Key": k, "Value": v} for k, v in owner_tags(region, extra).items()]


def is_owned_by_this_stack(tags: dict[str, str] | list[dict[str, str]] | None, region: str | None = None) -> bool:
    """True only when *tags* carry this stack's owner tag.

    Returns False for an untagged resource. That is the whole point: teardown must
    not delete something it cannot prove it created. Deleting a foreign secret is
    unrecoverable; skipping a legacy orphan costs an operator one manual delete.
    """
    if not tags:
        return False
    if isinstance(tags, list):
        mapped = {str(t.get("Key")): str(t.get("Value")) for t in tags if isinstance(t, dict)}
    else:
        mapped = {str(k): str(v) for k, v in tags.items()}
    return mapped.get(OWNER_TAG_KEY) == stack_id(region)
