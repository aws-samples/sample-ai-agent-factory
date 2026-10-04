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

import hashlib
import os
from collections.abc import Callable
from typing import Any

from app.services.aws_errors import error_code
from app.services.aws_pagination import list_all

OWNER_TAG_KEY = "AgentCoreStack"

# Kept on every resource alongside the owner tag: existing IAM policy conditions
# and the Bug 139 ABAC grant match on it, so dropping it would break them.
PRODUCT_TAG_KEY = "ManagedBy"
PRODUCT_TAG_VALUE = "agentcore-flows"

# A caller-supplied AWS identifier is inventory, not authority.  Resources the
# platform did not create must carry this explicit opt-in before a deployment may
# grant its Bedrock KB role access to them.  ``OwnerSubHash`` may additionally
# scope that opt-in to one caller; omitting it intentionally makes the resource
# shared across authenticated users of this platform stack.
ACCESS_TAG_KEY = "AgentCoreFlowsAccess"
ACCESS_TAG_VALUE = "allow"
OWNER_SUB_HASH_TAG_KEY = "OwnerSubHash"
DEPLOYMENT_ID_TAG_KEY = "DeploymentId"


class OwnershipConfigurationError(RuntimeError):
    """The process lacks the identity inputs required for a destructive decision."""


def _region(region: str | None = None) -> str:
    resolved = region or os.environ.get("APP_AWS_REGION") or os.environ.get("AWS_REGION")
    if not resolved:
        raise OwnershipConfigurationError(
            "Cannot compute AgentCoreStack: neither an explicit region nor APP_AWS_REGION/AWS_REGION is configured"
        )
    return resolved


def stack_id(region: str | None = None) -> str:
    """Identity of the deployment that owns a resource: ``{project}-{env}-{region}``.

    Region is part of it because the same ``{project}-{env}`` is deliberately
    deployable to two regions (see ``config.py``), and a teardown in one region must
    not claim the other region's account-global resources — IAM roles in particular
    are not regional at all.

    No defaults are permitted here. A missing environment variable is not evidence
    that the process owns the default development stack; fabricating that identity
    would turn a deployment mistake into deletion authority over a real stack.
    """
    project = os.environ.get("PROJECT_NAME")
    env = os.environ.get("ENVIRONMENT") or os.environ.get("ENVIRONMENT_NAME")
    missing = []
    if not project:
        missing.append("PROJECT_NAME")
    if not env:
        missing.append("ENVIRONMENT/ENVIRONMENT_NAME")
    if missing:
        raise OwnershipConfigurationError("Cannot compute AgentCoreStack without " + " and ".join(missing))
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


def owner_lower_tag_list(
    region: str | None = None,
    extra: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Same tags in the lower-case list shape Bedrock/AOSS create APIs use."""
    return [{"key": k, "value": v} for k, v in owner_tags(region, extra).items()]


def owner_sub_hash(owner_sub: str) -> str:
    """Stable, non-reversible tenant binding for AWS resource tags.

    The caller's Cognito ``sub`` only needs to be compared, never displayed.
    Keeping the raw identifier out of resource tags avoids copying tenant identity
    into Config history, billing exports, and every inventory response.
    """
    return hashlib.sha256(str(owner_sub).encode("utf-8")).hexdigest()[:32]


def tag_map(tags: dict[str, str] | list[dict[str, str]] | None) -> dict[str, str]:
    """Normalize either tag shape AWS uses into ``{Key: Value}``."""
    if not tags:
        return {}
    if isinstance(tags, list):
        normalized: dict[str, str] = {}
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            key = tag.get("Key", tag.get("key"))
            value = tag.get("Value", tag.get("value"))
            if key is not None and value is not None:
                normalized[str(key)] = str(value)
        return normalized
    return {str(k): str(v) for k, v in tags.items()}


def is_owned_by_this_stack(tags: dict[str, str] | list[dict[str, str]] | None, region: str | None = None) -> bool:
    """True only when *tags* carry this stack's owner tag.

    Returns False for an untagged resource. That is the whole point: teardown must
    not delete something it cannot prove it created. Deleting a foreign secret is
    unrecoverable; skipping a legacy orphan costs an operator one manual delete.

    Deliberately does NOT accept the CDK ``Project``/``Environment`` pair that
    ``can_this_deployment_mutate`` below accepts. This predicate authorizes
    *deletion*, and CDK owns the lifecycle of anything it tagged — widening this one
    would have teardown delete resources CloudFormation still believes it manages.
    """
    try:
        expected = stack_id(region)
    except OwnershipConfigurationError:
        return False
    return tag_map(tags).get(OWNER_TAG_KEY) == expected


# ---------------------------------------------------------------------------
# Mutation, which is a different question from deletion.
#
# IAM role names are account-global and ours are derived from a user-chosen agent
# name (``AgentCoreRuntime-{agent}``, ``AgentCoreMemory-{memory}``, …). So
# ``create_role`` raising ``EntityAlreadyExists`` has two completely different
# causes that the exception cannot tell apart: this deployment is redeploying its
# own agent, or something else in the account already holds that name. Every
# already-exists branch used to assume the first, then ``tag_role`` + overwrite the
# role's inline policy -- which for the second case silently replaces a live
# foreign role's permissions with ours, and records it in our manifest so teardown
# later deletes it.
#
# Measured in the live account before writing this: of the 7 roles named
# ``AgentCore*``, four are foreign (``AgentCoreGateway-omargw``,
# ``AgentCoreDynamicToolsLambdaRole``, ``AgentCoreMcpExtGatewayRole``,
# ``AgentCoreToolTestRole``) and the step roles hold ``iam:TagRole`` +
# ``iam:PutRolePolicy`` + ``iam:DeleteRole`` on ``role/AgentCore*``, so all four
# were reachable.
#
# ARCC guidance ``cnt_GURZvDLm6pRn1K`` ("Prevent S3 Bucket Sniping Attacks") is the
# same shape for a globally-unique name and its exit criterion applies verbatim
# here: *before performing actions, ensure ownership has not changed*. Related:
# ``cnt_vBC0kXE8PNHqrW`` (confused deputy) and ``cnt_4uCsExIwIeSUub`` (resolve
# resource references consistently between the authorization decision and the act).
# ---------------------------------------------------------------------------

# Applied stack-wide by ``infra/stacks/platform_stack.py:67-68`` via
# ``cdk.Tags.of(self)``, so every role CDK provisions for this deployment carries
# them -- and nothing the runtime code creates does.
CDK_PROJECT_TAG_KEY = "Project"
CDK_ENVIRONMENT_TAG_KEY = "Environment"


class ForeignResourceError(RuntimeError):
    """The platform was about to mutate a resource it cannot prove it created.

    Carries an operator-actionable message: the deploy fails, which is recoverable,
    instead of overwriting a role that something else in the account is using, which
    is not.
    """


class ResourceDeletionRefused(RuntimeError):
    """A destructive action lacked live proof that this stack owns the resource."""


class ResourceAccessRefused(ValueError):
    """A deployment tried to use a resource whose owner did not opt it in."""


def assert_resource_access_allowed(
    resource_label: str,
    tags: dict[str, str] | list[dict[str, str]] | None,
    *,
    owner_sub: str,
    region: str | None = None,
    deployment_id: str | None = None,
    require_explicit_opt_in: bool = False,
) -> None:
    """Require explicit owner consent or exact platform/caller ownership.

    This is deliberately separate from deletion ownership.  A customer-owned
    bucket, cluster, key, collection, function, or Knowledge Base must never
    become reachable merely because a caller pasted its ARN into a deployment
    request.  Its owner opts in with ``AgentCoreFlowsAccess=allow``.  If the
    resource also carries ``OwnerSubHash``, that scope is mandatory and must
    match the authenticated caller.

    Platform-created resources can instead prove exact stack ownership plus
    either the current caller or the server-minted deployment id.  Secrets that
    originate outside the platform use ``require_explicit_opt_in=True`` so the
    application check and the IAM resource-tag condition enforce the same rule.
    """
    mapped = tag_map(tags)
    expected_owner_hash = owner_sub_hash(owner_sub) if owner_sub else ""
    actual_owner_hash = mapped.get(OWNER_SUB_HASH_TAG_KEY, "")

    if actual_owner_hash and (not expected_owner_hash or actual_owner_hash != expected_owner_hash):
        raise ResourceAccessRefused(
            f"Access refused for {resource_label}: its caller binding does not match the authenticated caller."
        )

    if mapped.get(ACCESS_TAG_KEY) == ACCESS_TAG_VALUE:
        return

    if not require_explicit_opt_in:
        try:
            stack_matches = mapped.get(OWNER_TAG_KEY) == stack_id(region)
        except OwnershipConfigurationError:
            stack_matches = False
        caller_matches = bool(expected_owner_hash and actual_owner_hash == expected_owner_hash)
        deployment_matches = bool(deployment_id and mapped.get(DEPLOYMENT_ID_TAG_KEY) == str(deployment_id))
        if stack_matches and (caller_matches or deployment_matches):
            return

    scope_hint = f" and {OWNER_SUB_HASH_TAG_KEY} for this caller" if actual_owner_hash else ""
    raise ResourceAccessRefused(
        f"Access refused for {resource_label}: the owner must tag it "
        f"{ACCESS_TAG_KEY}={ACCESS_TAG_VALUE}{scope_hint} before this deployment "
        "may grant a Bedrock Knowledge Base role access to it."
    )


def assert_resource_bound_to_deployment(
    resource_label: str,
    tags: dict[str, str] | list[dict[str, str]] | None,
    *,
    deployment_id: str,
    owner_sub: str,
    region: str | None = None,
) -> None:
    """Require exact stack, deployment, and (when present) caller binding.

    Used for credentials already copied into ``agentcore-connector/`` and for
    create-conflict recovery.  An opt-in tag is intentionally insufficient:
    these resources are supposed to be private lifecycle members of one
    deployment, not shared customer inventory.
    """
    mapped = tag_map(tags)
    try:
        expected_stack = stack_id(region)
    except OwnershipConfigurationError as exc:
        raise ResourceAccessRefused(
            f"Access refused for {resource_label}: this process has no configured stack identity."
        ) from exc

    if mapped.get(OWNER_TAG_KEY) != expected_stack:
        raise ResourceAccessRefused(f"Access refused for {resource_label}: exact stack ownership could not be proven.")
    if mapped.get(DEPLOYMENT_ID_TAG_KEY) != str(deployment_id):
        raise ResourceAccessRefused(
            f"Access refused for {resource_label}: exact deployment ownership could not be proven."
        )
    if owner_sub:
        expected_owner_hash = owner_sub_hash(owner_sub)
        if mapped.get(OWNER_SUB_HASH_TAG_KEY) != expected_owner_hash:
            raise ResourceAccessRefused(
                f"Access refused for {resource_label}: exact caller ownership could not be proven."
            )


_MISSING_RESOURCE_CODES = {
    "NoSuchEntity",
    "NoSuchEntityException",
    "NoSuchKey",
    "NotFound",
    "NotFoundException",
    "ResourceNotFound",
    "ResourceNotFoundException",
}


def _resource_is_missing(exc: Exception) -> bool:
    """Whether *exc* is strong evidence that the target no longer exists."""
    code = error_code(exc)
    if code in _MISSING_RESOURCE_CODES:
        return True
    text = str(exc).lower()
    if any(
        marker in text
        for marker in (
            "nosuchentity",
            "nosuchkey",
            "notfoundexception",
            "resource not found",
            "resourcenotfound",
        )
    ):
        return True
    return ("validationexception" in text or code == "ValidationException") and (
        "not found" in text or "does not exist" in text
    )


def resource_is_missing(exc: Exception) -> bool:
    """Public missing-resource predicate for destructive helper boundaries.

    Deletion helpers often need to distinguish the one safe idempotent outcome
    (the resource is conclusively absent) from every ambiguous read failure
    (AccessDenied, throttling, timeout, malformed response).  Keep that
    distinction centralized so a caller cannot accidentally revive the old
    ``AccessDenied == already gone`` behavior.
    """
    return _resource_is_missing(exc)


def _read_for_deletion(resource_label: str, read: Callable[[], Any]) -> Any:
    """Read live ownership evidence, retaining on every ambiguous failure."""
    try:
        return read()
    except Exception as exc:  # noqa: BLE001
        if _resource_is_missing(exc):
            raise
        raise ResourceDeletionRefused(
            f"Deletion refused for {resource_label}: live ownership could not be "
            f"read ({type(exc).__name__}). The resource was left in place."
        ) from exc


def _nested(mapping: dict, *path: str) -> Any:
    value: Any = mapping
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


_AGENTCORE_OWNERSHIP_READS: dict[
    str,
    tuple[str, str, tuple[tuple[str, ...], ...]],
] = {
    "agent_runtime": (
        "get_agent_runtime",
        "agentRuntimeId",
        (("agentRuntimeArn",),),
    ),
    "gateway": (
        "get_gateway",
        "gatewayIdentifier",
        (("gatewayArn",),),
    ),
    "harness": (
        "get_harness",
        "harnessId",
        (("harness", "arn"), ("harness", "harnessArn"), ("arn",)),
    ),
    "memory": (
        "get_memory",
        "memoryId",
        (("memory", "arn"), ("memory", "memoryArn"), ("arn",)),
    ),
    "policy_engine": (
        "get_policy_engine",
        "policyEngineId",
        (("policyEngineArn",),),
    ),
    "oauth2_credential_provider": (
        "get_oauth2_credential_provider",
        "name",
        (("credentialProviderArn",),),
    ),
    "api_key_credential_provider": (
        "get_api_key_credential_provider",
        "name",
        (("credentialProviderArn",), ("apiKeyCredentialProviderArn",)),
    ),
}


def assert_agentcore_resource_owned(
    client,
    resource_type: str,
    identifier: str,
    region: str | None = None,
) -> dict:
    """Re-read an AgentCore resource and require its exact live owner tag."""
    try:
        method_name, id_field, arn_paths = _AGENTCORE_OWNERSHIP_READS[resource_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported AgentCore resource type: {resource_type}") from exc
    label = f"{resource_type.replace('_', ' ')} {identifier}"
    detail = _read_for_deletion(
        label,
        lambda: getattr(client, method_name)(**{id_field: identifier}),
    )
    arn = next(
        (str(value) for path in arn_paths if (value := _nested(detail, *path))),
        "",
    )
    if not arn:
        raise ResourceDeletionRefused(
            f"Deletion refused for {label}: the live resource response contained "
            "no ARN, so ownership could not be verified."
        )
    tag_response = _read_for_deletion(
        label,
        lambda: client.list_tags_for_resource(resourceArn=arn),
    )
    assert_owned_for_deletion(label, tag_response.get("tags"), region)
    return detail


def delete_owned_credential_provider(
    client,
    provider_name: str,
    region: str | None = None,
) -> list[str]:
    """Delete every existing provider namespace only after exact tag proof.

    Older manifests sometimes recorded an API-key provider as OAuth. Probing
    both namespaces preserves that compatibility without treating the recorded
    type or a shared name as deletion authority.
    """
    deleted: list[str] = []
    for resource_type, delete_method in (
        ("oauth2_credential_provider", "delete_oauth2_credential_provider"),
        ("api_key_credential_provider", "delete_api_key_credential_provider"),
    ):
        try:
            assert_agentcore_resource_owned(
                client,
                resource_type,
                provider_name,
                region,
            )
        except Exception as exc:  # noqa: BLE001
            if _resource_is_missing(exc):
                continue
            raise
        getattr(client, delete_method)(name=provider_name)
        deleted.append(resource_type)
    return deleted


def assert_guardrail_owned(client, guardrail_id: str, region: str | None = None) -> dict:
    """Require the exact live owner tag before deleting a Bedrock guardrail."""
    label = f"guardrail {guardrail_id}"
    detail = _read_for_deletion(
        label,
        lambda: client.get_guardrail(guardrailIdentifier=guardrail_id),
    )
    arn = str(detail.get("guardrailArn") or "")
    if not arn:
        raise ResourceDeletionRefused(
            f"Deletion refused for {label}: the live resource response contained "
            "no ARN, so ownership could not be verified."
        )
    tags = _read_for_deletion(
        label,
        lambda: client.list_tags_for_resource(resourceARN=arn),
    )
    assert_owned_for_deletion(label, tags.get("tags"), region)
    return detail


def assert_knowledge_base_owned(
    client,
    knowledge_base_id: str,
    region: str | None = None,
) -> dict:
    """Require the exact live owner tag before deleting a Bedrock Knowledge Base."""
    label = f"knowledge base {knowledge_base_id}"
    response = _read_for_deletion(
        label,
        lambda: client.get_knowledge_base(knowledgeBaseId=knowledge_base_id),
    )
    detail = response.get("knowledgeBase") or {}
    arn = str(detail.get("knowledgeBaseArn") or "")
    if not arn:
        raise ResourceDeletionRefused(
            f"Deletion refused for {label}: the live resource response contained "
            "no ARN, so ownership could not be verified."
        )
    tags = _read_for_deletion(
        label,
        lambda: client.list_tags_for_resource(resourceArn=arn),
    )
    assert_owned_for_deletion(label, tags.get("tags"), region)
    return response


def assert_vector_bucket_owned(
    client,
    bucket_name: str,
    region: str | None = None,
) -> dict:
    """Require the exact live owner tag before deleting an S3 Vectors bucket."""
    label = f"S3 Vectors bucket {bucket_name}"
    response = _read_for_deletion(
        label,
        lambda: client.get_vector_bucket(vectorBucketName=bucket_name),
    )
    detail = response.get("vectorBucket") or {}
    arn = str(detail.get("vectorBucketArn") or "")
    if not arn:
        raise ResourceDeletionRefused(
            f"Deletion refused for {label}: the live resource response contained "
            "no ARN, so ownership could not be verified."
        )
    tags = _read_for_deletion(
        label,
        lambda: client.list_tags_for_resource(resourceArn=arn),
    )
    assert_owned_for_deletion(label, tags.get("tags"), region)
    return response


def get_owned_aoss_collection(
    client,
    collection_name: str,
    region: str | None = None,
    deployment_id: str | None = None,
) -> dict | None:
    """Return a verified AOSS collection detail, or ``None`` if already absent.

    New auto-provisioned collections carry both the stack identity and the exact
    deployment id.  The latter matters on create-conflict and teardown: two
    deployments in the same stack must not gain authority over each other's
    billable vector stores merely because their names collide.
    """
    label = f"OpenSearch Serverless collection {collection_name}"
    response = _read_for_deletion(
        label,
        lambda: client.batch_get_collection(names=[collection_name]),
    )
    details = response.get("collectionDetails") or []
    if not details:
        return None
    detail = details[0]
    arn = str(detail.get("arn") or "")
    if not arn:
        raise ResourceDeletionRefused(
            f"Deletion refused for {label}: the live resource response contained "
            "no ARN, so ownership could not be verified."
        )
    tags = _read_for_deletion(
        label,
        lambda: client.list_tags_for_resource(resourceArn=arn),
    )
    assert_owned_for_deletion(label, tags.get("tags"), region)
    if deployment_id and tag_map(tags.get("tags")).get("DeploymentId") != str(deployment_id):
        raise ResourceDeletionRefused(
            f"Deletion refused for {label}: exact DeploymentId={deployment_id} ownership could not be proven."
        )
    return detail


def aoss_policy_owner_description(
    region: str | None,
    purpose: str,
    deployment_id: str | None = None,
) -> str:
    """Description marker used because AOSS policies do not support tags."""
    marker = f"{OWNER_TAG_KEY}={stack_id(region)}"
    if deployment_id:
        marker += f"; DeploymentId={deployment_id}"
    return f"{purpose}; {marker}"


def assert_aoss_policy_owned(
    client,
    policy_name: str,
    policy_type: str,
    region: str | None = None,
    deployment_id: str | None = None,
) -> dict:
    """Require an immutable owner marker before deleting an untaggable AOSS policy."""
    label = f"OpenSearch Serverless {policy_type} policy {policy_name}"
    method_name = "get_access_policy" if policy_type == "data" else "get_security_policy"
    detail_key = "accessPolicyDetail" if policy_type == "data" else "securityPolicyDetail"
    response = _read_for_deletion(
        label,
        lambda: getattr(client, method_name)(type=policy_type, name=policy_name),
    )
    detail = response.get(detail_key) or {}
    expected = f"{OWNER_TAG_KEY}={stack_id(region)}"
    if deployment_id:
        expected += f"; DeploymentId={deployment_id}"
    description = str(detail.get("description") or "")
    if not description.endswith(expected):
        raise ResourceDeletionRefused(
            f"Deletion refused for {label}: its live description contains no exact {expected} ownership marker."
        )
    return detail


def assert_s3_object_owned(
    client,
    bucket: str,
    key: str,
    *,
    region: str | None = None,
    deployment_id: str | None = None,
    expected_bucket_owner: str | None = None,
    version_id: str | None = None,
) -> None:
    """Require stack + deployment tags before deleting a staged S3 object.

    With ``version_id`` the tags of that one version are read: every version of a key
    carries its own tag set, so the current version's tags prove nothing about the rest.
    """
    label = f"S3 object s3://{bucket}/{key}" + (f" version {version_id}" if version_id else "")
    request: dict[str, str] = {"Bucket": bucket, "Key": key}
    if expected_bucket_owner:
        request["ExpectedBucketOwner"] = str(expected_bucket_owner)
    if version_id:
        request["VersionId"] = str(version_id)
    response = _read_for_deletion(
        label,
        lambda: client.get_object_tagging(**request),
    )
    tags = response.get("TagSet") or []
    assert_owned_for_deletion(label, tags, region)
    if not deployment_id or tag_map(tags).get("DeploymentId") != str(deployment_id):
        raise ResourceDeletionRefused(
            f"Deletion refused for {label}: exact DeploymentId ownership could not be proven."
        )


# A hundred pages is 100k versions of one deployment-scoped key; the bound stops a
# malformed pagination response spinning inside a teardown, not legitimate work.
_MAX_S3_VERSION_PAGES = 100


def _exact_key_versions(client, bucket: str, key: str, owner: dict[str, str]) -> tuple[list[str], list[str]]:
    """``(data version ids, delete marker ids)`` whose key is exactly *key*.

    ``list_object_versions`` takes a prefix, so ``code.zip.sha256`` would match
    ``code.zip``; the equality filter keeps a sibling out of both the delete and the
    re-list that reports what is left.
    """
    versions: list[str] = []
    markers: list[str] = []
    token: dict[str, str] = {}
    for _ in range(_MAX_S3_VERSION_PAGES):
        page = client.list_object_versions(Bucket=bucket, Prefix=key, **owner, **token)
        versions += [v["VersionId"] for v in page.get("Versions") or [] if v.get("Key") == key]
        markers += [m["VersionId"] for m in page.get("DeleteMarkers") or [] if m.get("Key") == key]
        next_token = {
            "KeyMarker": page.get("NextKeyMarker") or "",
            "VersionIdMarker": page.get("NextVersionIdMarker") or "",
        }
        if not page.get("IsTruncated") or next_token == token:
            return versions, markers
        token = next_token
    raise ResourceDeletionRefused(
        f"Deletion refused for S3 object s3://{bucket}/{key}: its version listing did not terminate."
    )


def delete_owned_s3_object(
    client,
    bucket: str,
    key: str,
    *,
    region: str | None = None,
    deployment_id: str | None = None,
    expected_bucket_owner: str | None = None,
) -> int:
    """Hard-delete every version of *key* this deployment provably wrote (F-60).

    ``delete_object`` without a VersionId on a versioned (or suspended) bucket only
    writes a delete marker: the call succeeds, a plain listing shows nothing, and the
    agent source stays readable by VersionId. Data is deleted only once no copy remains
    (ARCC cnt_Hr4zJD4KntOWIt), so each data version is deleted by id, after its OWN tags
    prove stack and DeploymentId ownership. On a bucket that was never versioned the
    single version id is the literal ``"null"``, so the same path covers both.

    All or nothing. If ANY data version cannot be proven ours, nothing is deleted: removing
    our newer version would promote the foreign one to current, and removing a marker
    would resurface it -- either way changing an object we do not own. The re-list
    afterwards is the success signal, not the delete calls.

    Returns the number of data versions deleted; 0 means none existed. Raises
    ``ResourceDeletionRefused`` when anything is left.
    """
    label = f"S3 object s3://{bucket}/{key}"
    owner = {"ExpectedBucketOwner": str(expected_bucket_owner)} if expected_bucket_owner else {}
    versions, markers = _read_for_deletion(label, lambda: _exact_key_versions(client, bucket, key, owner))
    owned: list[str] = []
    unproven = 0
    for version_id in versions:
        try:
            assert_s3_object_owned(
                client,
                bucket,
                key,
                region=region,
                deployment_id=deployment_id,
                expected_bucket_owner=expected_bucket_owner,
                version_id=version_id,
            )
        except ResourceDeletionRefused:
            unproven += 1
            continue
        except Exception as exc:  # noqa: BLE001
            if _resource_is_missing(exc):
                continue  # removed between the listing and the read
            raise
        owned.append(version_id)
    if unproven:
        raise ResourceDeletionRefused(
            f"Deletion refused for {label}: {unproven} of {len(versions)} version(s) could not be "
            "proven this deployment's, so no version or delete marker was removed."
        )
    for version_id in [*owned, *markers]:
        client.delete_object(Bucket=bucket, Key=key, VersionId=version_id, **owner)
    left_versions, left_markers = _read_for_deletion(label, lambda: _exact_key_versions(client, bucket, key, owner))
    if left_versions or left_markers:
        raise ResourceDeletionRefused(
            f"Deletion incomplete for {label}: {len(owned)} version(s) deleted, but "
            f"{len(left_versions)} version(s) and {len(left_markers)} delete marker(s) are still listed."
        )
    return len(owned)


def assert_owned_for_deletion(
    resource_label: str,
    tags: dict[str, str] | list[dict[str, str]] | None,
    region: str | None = None,
) -> None:
    """Require the exact runtime-owner tag before deleting a dynamic resource.

    A manifest row records what a deployment intended to create; it is not a
    capability over whatever may now occupy an account-global name. CDK's
    ``Project``/``Environment`` pair is deliberately insufficient here because
    CloudFormation, not runtime teardown, owns those resources' lifecycle.
    """
    try:
        expected = stack_id(region)
    except OwnershipConfigurationError as exc:
        raise ResourceDeletionRefused(
            f"Deletion refused for {resource_label}: this process has no configured "
            "stack identity. The resource was left in place."
        ) from exc
    if tag_map(tags).get(OWNER_TAG_KEY) == expected:
        return
    mapped = tag_map(tags)
    owner = mapped.get(OWNER_TAG_KEY)
    whose = f"it is tagged for {owner}" if owner else "it has no exact runtime ownership tag"
    raise ResourceDeletionRefused(
        f"Deletion refused for {resource_label}: {whose}. Expected "
        f"{OWNER_TAG_KEY}={expected}. The resource was left in place."
    )


def _iam_pages(iam_client, method_name: str, result_key: str, role_name: str) -> list:
    """Return every IAM list result without trusting one page as complete."""
    pagination_marker = "Marker"
    return list_all(
        iam_client,
        method_name,
        item_keys=(result_key,),
        request={"RoleName": role_name},
        request_token=pagination_marker,
        response_token=pagination_marker,
        continuation_flag="IsTruncated",
    )


def delete_owned_iam_role(iam_client, role_name: str, region: str | None = None) -> None:
    """Delete an IAM role only after re-proving exact ownership from live tags.

    The order in this helper is the security boundary: ``GetRole`` and
    :func:`assert_owned_for_deletion` run before *any* policy listing, detach, or
    delete call. A manifest entry or a deterministic role name is inventory, not
    authority over whatever account-global role may occupy that name now.

    Exceptions intentionally propagate. Callers decide whether a missing role is
    idempotent success and whether a protected/failed delete makes the enclosing
    cleanup partial, but none may weaken the ownership proof or mutate first.
    """
    role = iam_client.get_role(RoleName=role_name)["Role"]
    assert_owned_for_deletion(f"IAM role {role_name}", role.get("Tags"), region)

    # Read the complete attachment graph before the first mutation. If IAM denies
    # a later page or returns a malformed/repeated marker, the role stays exactly
    # as it was instead of being left half-detached.
    attached_policies = _iam_pages(
        iam_client,
        "list_attached_role_policies",
        "AttachedPolicies",
        role_name,
    )
    inline_policies = _iam_pages(
        iam_client,
        "list_role_policies",
        "PolicyNames",
        role_name,
    )
    for policy in attached_policies:
        iam_client.detach_role_policy(RoleName=role_name, PolicyArn=policy["PolicyArn"])
    for policy_name in inline_policies:
        iam_client.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
    iam_client.delete_role(RoleName=role_name)


def can_this_deployment_mutate(tags: dict[str, str] | list[dict[str, str]] | None, region: str | None = None) -> bool:
    """True when *tags* prove the resource belongs to THIS deployment.

    Two accepted proofs, because the platform's roles arrive by two routes:

    1. ``AgentCoreStack == stack_id()`` -- stamped by ``owner_tags`` on everything
       the runtime code creates.
    2. ``Project`` + ``Environment`` both matching this deployment -- stamped by CDK
       on everything the platform stack provisions, including the shared runtime
       exec role the legacy path adopts by name.

    The second is not optional. Measured live: ``AgentCoreRuntime-acfe2e-p0920-shared``
    carries ``Project=acfe2e``/``Environment=p0920`` and no ``AgentCoreStack`` tag at
    all, so a check that accepted only proof 1 would refuse the platform's own role
    and fail every deploy -- fail-closed over an incomplete ownership table removes
    the feature rather than securing it.

    ``Project``/``Environment`` carry no region, unlike ``stack_id()``. That is
    correct here and not an oversight: IAM roles are global, and the same
    project/environment deployed to two regions gives its roles region-distinct
    *names* (``…-dev-shared`` vs ``…-dev-eu-central-1-shared``), so there is nothing
    for a region component to disambiguate.
    """
    mapped = tag_map(tags)
    if not mapped:
        return False
    try:
        if mapped.get(OWNER_TAG_KEY) == stack_id(region):
            return True
    except OwnershipConfigurationError:
        # The independent CDK Project/Environment proof below can still be
        # evaluated without inventing a runtime owner identity.
        pass
    project = os.environ.get("PROJECT_NAME")
    environment = os.environ.get("ENVIRONMENT") or os.environ.get("ENVIRONMENT_NAME")
    if not project or not environment:
        # Without both env vars there is nothing to compare, and defaulting them
        # would turn "unconfigured" into "matches", which is the wrong direction.
        return False
    return mapped.get(CDK_PROJECT_TAG_KEY) == project and mapped.get(CDK_ENVIRONMENT_TAG_KEY) == environment


def assert_this_deployment_may_mutate(
    resource_label: str,
    tags: dict[str, str] | list[dict[str, str]] | None,
    region: str | None = None,
) -> None:
    """Raise ``ForeignResourceError`` unless *tags* prove the resource is ours.

    *resource_label* names the thing in the message, e.g. ``IAM role
    AgentCoreRuntime-support``. The message states the remedy, because a refusal a
    user cannot act on is a dead end: either rename the agent, or adopt the existing
    resource by tagging it.
    """
    if can_this_deployment_mutate(tags, region):
        return
    mapped = tag_map(tags)
    owner = mapped.get(OWNER_TAG_KEY) or (
        f"{mapped.get(CDK_PROJECT_TAG_KEY)}/{mapped.get(CDK_ENVIRONMENT_TAG_KEY)}"
        if mapped.get(CDK_PROJECT_TAG_KEY)
        else None
    )
    whose = f"it belongs to {owner}" if owner else "it carries no ownership tag"
    try:
        expected = stack_id(region)
    except OwnershipConfigurationError:
        expected = "<unconfigured>"
    raise ForeignResourceError(
        f"{resource_label} already exists in this account and {whose}, so this "
        f"deployment will not modify it. IAM role names are account-global. "
        f"To proceed, either rename the agent/resource so a new name is used, or -- "
        f"if this really is a resource of yours from an earlier deployment -- adopt it "
        f"by tagging it {OWNER_TAG_KEY}={expected} and "
        f"{PRODUCT_TAG_KEY}={PRODUCT_TAG_VALUE}, then redeploy."
    )
