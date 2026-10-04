"""Governance tags on the LIVE deploy path: one validation, one merge, both paths.

P0-B resolves a governance tag set per deploy (``tag_policy_store.resolve_governance``) and
``deployment_handler`` threads it through the Step Functions state as ``resource_tags``. The
live execution history confirms it reaches every state. What it did NOT do was reach the AWS
resources: of the ~40 ``owner_tags``/``owner_tag_list`` call sites in this codebase, exactly
ONE passed ``extra=resource_tags`` (``runtime_deployer.create_runtime_iam_role``, the runtime
EXEC ROLE). Measured live on ``acfe2e-p0920``: a governed deploy whose three platform tags
were accepted, resolved and written into the deployment record produced a runtime carrying
only ``ManagedBy`` and ``AgentCoreStack``.

That matters because of what the feature is FOR. ``runtime_deployer.py`` states the intent as
"cost attribution + ABAC work off real AWS resource tags" -- and an IAM role incurs no cost.
The one resource that was tagged is the one resource a cost report cannot use.

The export path, meanwhile, tags every taggable resource AND validates the tag set
(``cfn_template_generator._validated_tags``): key length, the reserved ``aws:`` prefix, the
AgentCore character class, the 50-tag ceiling, and a refusal for keys that designate
credential material. None of that ran on the live path, so a tag key named ``api_key`` with a
secret value was written verbatim onto a live IAM role -- the exact thing the export refuses.

So the validation lives HERE and both paths call it. ``_validated_tags`` delegates to
:func:`validated_governance_tags` and translates the error, which keeps the export's existing
message text byte-identical while making it impossible for the two paths to drift on what a
legal tag is.
"""

from __future__ import annotations

import os
import re

from app.services.resource_ownership import owner_tags

#: AgentCore's own ``TagsMap`` pattern, the strictest of the set any path emits, so a tag that
#: passes here is accepted by every service either path creates. A value outside it is rejected
#: by the service at CREATE time rather than at validate time, which on the live path means a
#: half-deployed agent and on the export path means a rolled-back stack.
_TAG_CHAR_PATTERN = re.compile(r"^[a-zA-Z0-9\s._:/=+@-]*$")

#: AWS's per-resource ceiling. Applies to the merged set, which is why the two ownership tags
#: are counted: a caller supplying 50 tags plus ManagedBy and AgentCoreStack is over it.
MAX_TAGS_PER_RESOURCE = 50

#: Set by the platform's CDK from ``GOVERNANCE_TAG_KEY_PREFIXES`` in
#: ``infra/stacks/platform/config.py``, which is the SAME value that builds the
#: ``aws:TagKeys`` allowlist on every tag-on-create grant the step roles hold. Comma-separated.
GOVERNANCE_TAG_KEY_PREFIXES_ENV = "GOVERNANCE_TAG_KEY_PREFIXES"

#: The default matches the CDK default, so an older stack whose step Lambdas predate the env
#: var behaves identically to a new one instead of refusing everything. Both namespaces are
#: needed, not just the product's own: ``platform:`` is reserved (``TagPolicy.is_platform`` is
#: ``key.startswith("platform:")``) and ``POST /api/settings/tags`` refuses to create a new key
#: in it, so ``org:`` is the namespace an admin can actually govern with. A default of
#: ``platform:`` alone refuses every key an admin is able to create.
_DEFAULT_GOVERNANCE_TAG_KEY_PREFIXES = ("platform:", "org:")


class GovernanceTagError(ValueError):
    """A supplied governance tag cannot be applied to an AWS resource.

    Raised BEFORE any resource is created. Refusing is the right answer rather than dropping
    the offending tag: an operator who asked for a cost-allocation tag and got a deployment
    without one has no way to discover that except an audit months later. The same argument
    the export path already makes, now made on the deploy path too.
    """


def tag_key_designates_credential(key: str) -> bool:
    """Whether a tag key would invite secret material onto a durable AWS resource.

    Reuses the deployment payload scanner's canonical token set rather than inventing a second
    secret vocabulary. Namespace separators are checked segment-by-segment as well, so
    ``platform:api-key`` cannot evade the rule ``apiKey`` triggers. Keeping the suffix intact
    matters: splitting ``platform.api-key`` into the isolated words ``api`` and ``key`` would
    miss the canonical ``apikey`` token. Values are never inspected or echoed.
    """
    from app.services.deployment_payload_validation import (  # noqa: PLC0415
        _FORBIDDEN_SECRET_TOKENS,
        _canon_key,
    )

    candidates = (
        key,
        *(key[match.end() :] for match in re.finditer(r"[:/._\-\s]+", key)),
    )
    return any(_canon_key(candidate) in _FORBIDDEN_SECRET_TOKENS for candidate in candidates if candidate)


def validated_governance_tags(resource_tags: dict | None) -> dict[str, str]:
    """Normalize and validate a governance tag set, or raise :class:`GovernanceTagError`.

    The error messages are the export path's, verbatim, because the export path's tests assert
    on them and because a caller who hits the same bad tag on both paths should read the same
    sentence. ``_validated_tags`` in ``cfn_template_generator`` re-raises these as
    ``CfnExportUnsupportedError`` with the message unchanged.
    """
    if resource_tags is None:
        return {}
    if not isinstance(resource_tags, dict):
        raise GovernanceTagError(
            "resourceTags must be a map of tag keys to values. The export was refused "
            "instead of guessing how to translate a malformed tag collection."
        )
    if not resource_tags:
        return {}
    tags: dict[str, str] = {}
    for raw_key, raw_value in resource_tags.items():
        key, value = str(raw_key), "" if raw_value is None else str(raw_value)
        if not key or len(key) > 128:
            raise GovernanceTagError(
                f"The resource tag key {key!r} is {len(key)} characters. AWS accepts 1 to 128. "
                "Fix the tag in the deploy panel and try again."
            )
        if key.lower().startswith("aws:"):
            raise GovernanceTagError(
                f"The resource tag key {key!r} uses the reserved 'aws:' prefix, which AWS "
                "refuses on create. Rename the tag and try again."
            )
        if tag_key_designates_credential(key):
            raise GovernanceTagError(
                f"The resource tag key {key!r} designates credential material. Tag values "
                "are copied into template resources and custom-resource properties, which "
                "CloudFormation retains in stack events; use a non-secret classification "
                "key and keep credentials in Secrets Manager."
            )
        if len(value) > 256:
            raise GovernanceTagError(
                f"The value of resource tag {key!r} is {len(value)} characters. AWS accepts "
                "up to 256. Shorten it and try again."
            )
        if not _TAG_CHAR_PATTERN.fullmatch(key):
            raise GovernanceTagError(
                f"The resource tag key {key!r} contains a character AgentCore rejects. "
                "Allowed: letters, digits, whitespace and . _ : / = + @ -. The stack would "
                "fail partway through creation, so the export is refused instead."
            )
        if not _TAG_CHAR_PATTERN.fullmatch(value):
            # Never repeat a tag value in an error. Tags cross the Custom Resource boundary
            # through ResourceProperties and therefore enter durable stack events; a malformed
            # value is exactly where a caller may accidentally have pasted credential material.
            raise GovernanceTagError(
                f"The value of resource tag {key!r} contains a character AgentCore "
                "rejects. The value is not repeated here. Allowed: letters, digits, "
                "whitespace and . _ : / = + @ -. The export is refused before any "
                "stack artifact is staged."
            )
        tags[key] = value
    if len(tags) > MAX_TAGS_PER_RESOURCE:
        raise GovernanceTagError(
            f"{len(tags)} resource tags were supplied; AWS accepts at most 50 per resource. Remove some and try again."
        )
    return tags


def governance_tag_key_prefixes() -> tuple[str, ...]:
    """The tag-key namespaces this platform's deploy roles are allowed to stamp.

    Read from the environment on every call rather than cached at import: the step Lambdas are
    long-lived and a platform redeploy changes the value without replacing the function.
    """
    raw = os.environ.get(GOVERNANCE_TAG_KEY_PREFIXES_ENV, "")
    prefixes = tuple(p.strip() for p in raw.split(",") if p.strip())
    return prefixes or _DEFAULT_GOVERNANCE_TAG_KEY_PREFIXES


def stampable_governance_tags(resource_tags: dict | None) -> dict[str, str]:
    """Legal per AWS (:func:`validated_governance_tags`) AND inside a namespace IAM allows.

    This exists because of a live regression, and the regression is the whole argument for it.
    Adding ``tags`` to ``CreateAgentRuntime`` made every governed deploy on ``acfe2e-p0920``
    fail:

        AccessDeniedException ... not authorized to perform: bedrock-agentcore:TagResource on
        resource: arn:aws:bedrock-agentcore:us-east-1:...:runtime/*

    Not a missing action. The grant is there, with
    ``ForAllValues:StringEquals aws:TagKeys: [ManagedBy, AgentCoreStack]`` -- a deliberate
    tripwire (``infra/stacks/platform/step_lambdas.py``, "Adding another key at any create call
    site will be DENIED here rather than silently widening what these roles may stamp"). A
    governance key is exactly that: another key. ARCC ``cnt_SaTYaDCgBBJTcv`` describes this
    outcome precisely -- "If a customer's stack does not have permissions to tag these managed
    resources, their deployments will start to fail ... even though the customer has not made
    any changes to their code/stack."

    Dropping the ``aws:TagKeys`` bound was the easy fix and is the wrong one. ARCC
    ``cnt_L4ZLZgjrCctfxl`` (Prevent Privilege Escalation) lists "create/update tags" among the
    powerful operations that can be leveraged to gain elevated privilege, and
    ``cnt_6gBImtb08AJqCB`` is why: tags carry ABAC decisions, so a role that can write ANY tag
    key can write whichever key some other policy in the account authorizes on. An unbounded
    allowlist would let a compromised step Lambda stamp ``Environment``, ``Project`` or
    ``Team`` onto a resource and read whatever that unlocks.

    So the keys stay bounded, and the bound is a NAMESPACE rather than an enumeration.
    Governance policies are admin-created at runtime through ``POST /api/settings/tags``;
    enumerating them in an IAM condition would require a platform redeploy per tag policy,
    which is the same outage in slower motion. A namespace is knowable at synth time and open
    at runtime: an admin may add ``platform:anything`` with no redeploy, and cannot reach a key
    outside the namespaces the deployed policy names.

    The refusal lands at the API boundary, before any resource exists, which is the other half
    of the fix: a tag key IAM will reject must not first be discovered by a half-built
    deployment. The IAM condition remains the backstop, not the gate.
    """
    tags = validated_governance_tags(resource_tags)
    prefixes = governance_tag_key_prefixes()
    for key in tags:
        if not key.startswith(prefixes):
            raise GovernanceTagError(
                f"The resource tag key {key!r} is outside the tag namespaces this platform is "
                f"permitted to write on live AWS resources ({', '.join(prefixes)}). The deploy "
                "roles' IAM policies enumerate those namespaces, so a key outside them is "
                "refused here rather than denied by AWS partway through a deployment. Rename "
                f"the tag policy to start with one of them, or widen "
                f"{GOVERNANCE_TAG_KEY_PREFIXES_ENV} in the platform's CDK configuration and "
                "redeploy the platform."
            )
    return tags


def governed_tags(
    region: str | None,
    resource_tags: dict | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """``{Key: Value}`` for a resource this deployment creates: governance + ownership.

    Call this instead of ``owner_tags(region)`` at any site that creates a billable or
    auditable AWS resource. ``owner_tags`` applies ``ManagedBy`` and ``AgentCoreStack`` LAST
    and strips those two keys from ``extra``, so a caller-supplied governance tag can never
    overwrite the two keys every teardown gate reads -- the merge is safe by construction and
    does not need a second guard here.

    The caller's keys go through :func:`stampable_governance_tags`, not
    :func:`validated_governance_tags`: this is the LIVE path, so a key the step role's IAM
    policy will not authorize must be refused here rather than half-way through a deployment.
    The export path deliberately keeps the wider rule -- an exported template is deployed under
    the customer's own role, and refusing a tag namespace on their behalf would be inventing a
    constraint their account does not have.

    ``extra`` is for PRODUCT-INTERNAL tags the caller already had to stamp (``DeploymentId``,
    ``OwnerSubHash``, ``Purpose``). It wins over ``resource_tags`` and is deliberately NOT put
    through :func:`validated_governance_tags`: those keys are ours, not the caller's, so the
    credential-key refusal would be checking our own vocabulary against a rule written for
    untrusted input. Precedence matters more than it looks -- several of those keys are read
    back as ownership evidence on teardown, so a governance tag of the same name must not be
    able to displace one.

    The 50-tag ceiling is re-checked on the MERGED set: ``validated_governance_tags`` bounds
    the caller's own keys, and the ownership and internal tags are added after it.
    """
    merged = owner_tags(region, extra={**stampable_governance_tags(resource_tags), **(extra or {})})
    if len(merged) > MAX_TAGS_PER_RESOURCE:
        raise GovernanceTagError(
            f"{len(merged)} tags would be applied (governance tags plus the two ownership "
            f"tags this product requires); AWS accepts at most {MAX_TAGS_PER_RESOURCE} per "
            "resource. Remove some governance tags and deploy again."
        )
    return merged


def governed_tag_list(
    region: str | None,
    resource_tags: dict | None = None,
    extra: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Same set in the ``[{"Key": k, "Value": v}]`` shape IAM, SecretsManager and Bedrock want."""
    return [{"Key": k, "Value": v} for k, v in governed_tags(region, resource_tags, extra).items()]


def governed_lower_tag_list(
    region: str | None,
    resource_tags: dict | None = None,
    extra: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Same set in the lowercase ``[{"key": k, "value": v}]`` shape a few AgentCore APIs want."""
    return [{"key": k, "value": v} for k, v in governed_tags(region, resource_tags, extra).items()]
