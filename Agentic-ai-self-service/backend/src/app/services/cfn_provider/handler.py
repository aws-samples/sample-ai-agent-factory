"""Custom Resource Lambda for AgentCore CloudFormation stacks.

Handles four Custom Resource types — the complete set, enforced by
SUPPORTED_RESOURCE_TYPES below:

1. Custom::AgentCodePackage
   Merges pre-generated agent code with a pre-built dependency bundle
   (strands-mcp.zip or base.zip) into a single code.zip and uploads to S3.

   Properties:
       ArtifactsBucket  — S3 bucket for all artifacts
       AgentCodeKey     — S3 key of the agent code zip (contains agent.py)
       DependencyBundleKey — S3 key of the dependency bundle
       OutputKey        — S3 key for the merged output code.zip
       SourceDigest     — expected content digest of the agent code zip's members;
                          the merge is refused if the upload does not match
   Returns:
       CodeZipPrefix    — S3 key prefix of the assembled code.zip

2. Custom::OAuth2CredentialProvider
   Creates/deletes an OAuth2 credential provider via the bedrock-agentcore-control
   API. Required for MCP server gateway targets (GATEWAY_IAM_ROLE is not supported).

   Properties:
       ProviderName     — Name for the credential provider
       DiscoveryUrl     — OIDC discovery URL (Cognito)
       ClientId         — OAuth2 client ID
       UserPoolId       — Cognito user pool holding that client. The handler reads the
                          client secret from Cognito with DescribeUserPoolClient; the
                          secret is deliberately NOT a property, because CloudFormation
                          copies a resource's ResourceProperties into the stack event
                          stream — native types included, not just Custom:: — where
                          anyone with DescribeStackEvents can read them for 90 days.
                          See _resolve_client_secret.
       ClientSecret     — DEPRECATED, and the leak described above. Still accepted so a
                          stack built from an older template keeps updating, but re-export
                          to stop sending it.
   Returns:
       CredentialProviderArn — ARN of the created credential provider

3. Custom::AgentCorePolicy
   Creates/deletes a Cedar policy on an AgentCore PolicyEngine. Exists because the
   native AWS::BedrockAgentCore::Policy stabilization times out before the engine
   is ready in fresh accounts (Bug 72), so this waits and retries the bind.

   Properties:
       Name             — Policy name
       Statement        — Cedar policy statement
       PolicyEngineId   — Id of the engine to attach it to
       Description      — Optional description
   Returns:
       PolicyId, PolicyEngineId

4. Custom::RuntimeLogGroup
   Applies the stack's retention period and customer-managed key to the log groups
   the AgentCore runtime creates for itself. Exists because
   ``AWS::BedrockAgentCore::Runtime`` has no logging properties at all: the service
   creates ``/aws/bedrock-agentcore/runtimes/<runtimeId>-<qualifier>`` on its own,
   with no retention and the AWS-owned key, and CloudFormation cannot govern a group
   it did not declare. Declaring one as AWS::Logs::LogGroup is not an option either —
   the runtime creates it at stack-create time, before any invoke, so the declared
   resource would collide with a group that already exists.

   Properties:
       LogGroupNames    — the log group names to govern. One per endpoint qualifier,
                          because the service creates one group PER ENDPOINT (a
                          runtime with a named endpoint has both ``-DEFAULT`` and
                          ``-<endpointName>``, verified live) and the invoked
                          qualifier is the one that receives the conversation logs.
       RetentionInDays  — retention to apply; 0 means never expire
       KmsKeyArn        — optional customer-managed key ARN; empty reverts the group
                          to the AWS-owned key
   Returns:
       LogGroupNames    — the governed names, comma-separated
"""

import hashlib
import io
import logging
import os
import re
import time
import zipfile
from urllib.parse import quote, urlencode

import boto3
import cfn_response  # absolute import — this file is packaged as a flat Lambda zip, not a package
from botocore.exceptions import ClientError, ParamValidationError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _error_code(exc: BaseException) -> str:
    """AWS error code from a ClientError ('' for non-ClientError).

    Local copy of app.services.aws_errors.error_code — this module is packaged
    as a flat Lambda zip (handler.py + cfn_response.py only, see
    cfn_template_generator._package_cfn_provider) and cannot import app.*.
    """
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "")
    return ""


class ProviderError(Exception):
    """An error whose message this module authored, so it is safe to surface.

    ``_safe_failure_reason`` reduces every other exception to its class name,
    because botocore builds ClientError messages out of the API response and some
    calls in this module carry a Cognito app client secret in their request
    parameters. That protection costs the operator everything useful, though:
    "cfn-provider failed with RuntimeError" in a stack event is not something
    anybody can act on, and the failures this class exists for are precisely the
    actionable ones — a Cedar statement the service rejected, where the remedy is
    in ``statusReasons``.

    The contract for raising it is therefore narrow and must be kept: build the
    message from literals, resource ids, and service status fields only. Never
    from ``str(exception)``, and never from anything that passed through a request
    body.
    """


class _AgentCoreTagResourceNotFound(Exception):
    """Internal signal that a tagged provider vanished before an Update."""


def _stack_account_id(event: dict) -> str:
    """The account that owns the stack, from ``StackId``, or "" if unreadable.

    Used as ``ExpectedBucketOwner`` on every S3 call below. The bucket name arrives
    as a resource property, so without this the handler will read code from — and
    write the merged code.zip to — whatever account owns a bucket of that name,
    including one that is not the recipient's. S3 answers 403 instead when the owner
    does not match, which turns a bucket-name mix-up (or a name someone else claimed
    in another account) into a refusal rather than a cross-account artifact exchange.

    Deliberately the *stack's* account rather than the Lambda's: they are the same
    here, and StackId is available on every event without changing signatures.
    """
    stack_id = event.get("StackId", "")
    parts = stack_id.split(":")
    return parts[4] if len(parts) > 5 and parts[0] == "arn" else ""


def _owner_kwargs(event: dict) -> dict:
    """``{"ExpectedBucketOwner": ...}`` when the account is knowable, else ``{}``."""
    account = _stack_account_id(event)
    return {"ExpectedBucketOwner": account} if account else {}


_RESOURCE_TAG_CHAR_PATTERN = re.compile(r"^[a-zA-Z0-9\s._:/=+@-]*$")
_RESOURCE_TAG_SECRET_TOKENS = frozenset(
    {
        "secretvalue",
        "clientsecret",
        "apikey",
        "litellmapikey",
        "virtualkey",
        "secret",
        "password",
        "passwd",
        "pwd",
        "privatekey",
        "signingkey",
        "secretkey",
        "accesskey",
        "secretaccesskey",
        "credentials",
        "connectionstring",
        "authorization",
        "proxyauthorization",
        "xapikey",
        "xauthtoken",
        "accesstoken",
        "refreshtoken",
        "sessiontoken",
        "idtoken",
        "bearertoken",
        "token",
    }
)
_RESOURCE_TAG_KEY_SEPARATORS = str.maketrans({char: None for char in "_-. "})


def _tag_key_designates_credential(key: str) -> bool:
    candidates = (
        key,
        *(key[match.end() :] for match in re.finditer(r"[:/._\-\s]+", key)),
    )
    return any(
        candidate.casefold().translate(_RESOURCE_TAG_KEY_SEPARATORS) in _RESOURCE_TAG_SECRET_TOKENS
        for candidate in candidates
        if candidate
    )


def _resource_tags(props: dict) -> dict[str, str]:
    """Validated governance tags from one custom resource's properties.

    The generator validates these before it emits a template, but this Lambda is also
    the boundary for hand-edited templates and Terraform callers. Validate again before
    the first AWS write so a malformed tag cannot leave a partly-created resource.
    AgentCore's tag character set is the strictest one among the resources below, which
    gives all four handlers one deterministic contract.
    """
    raw = props.get("ResourceTags")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProviderError("ResourceTags must be a map of tag keys to string values")

    tags: dict[str, str] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key)
        value = "" if raw_value is None else str(raw_value)
        if not key or len(key) > 128:
            raise ProviderError(f"resource tag keys must contain 1 to 128 characters; got length {len(key)}")
        if key.lower().startswith("aws:"):
            raise ProviderError("resource tag keys must not use the reserved aws: prefix")
        if _tag_key_designates_credential(key):
            raise ProviderError(
                f"resource tag key {key!r} designates credential material; "
                "credentials must stay in Secrets Manager rather than resource tags"
            )
        if len(value) > 256:
            raise ProviderError(f"resource tag {key!r} has a value longer than 256 characters")
        if not _RESOURCE_TAG_CHAR_PATTERN.fullmatch(key) or not _RESOURCE_TAG_CHAR_PATTERN.fullmatch(value):
            raise ProviderError(f"resource tag {key!r} contains characters outside the common AWS tag character set")
        tags[key] = value

    if len(tags) > 50:
        raise ProviderError(f"{len(tags)} resource tags were supplied; these resources accept at most 50")
    return tags


def _old_resource_tag_keys(event: dict) -> set[str]:
    """Keys this custom resource managed before an Update.

    Values are deliberately ignored: they are not needed to remove a stale key and may
    contain material that must never be inspected or echoed. This parser is also more
    permissive than ``_resource_tags`` on purpose. If a prior release allowed a key that
    the current release now forbids (for example ``client-secret``), the secure migration
    is to remove it, not to make every future Update fail before cleanup can run.
    """
    old_props = event.get("OldResourceProperties")
    if old_props is None:
        old_props = {}
    if not isinstance(old_props, dict):
        raise ProviderError("OldResourceProperties must be a map when supplied")
    raw = old_props.get("ResourceTags")
    if raw is None:
        return set()
    if not isinstance(raw, dict):
        raise ProviderError("OldResourceProperties.ResourceTags must be a map when supplied")

    keys: set[str] = set()
    for raw_key in raw:
        key = str(raw_key)
        if not key or len(key) > 128 or key.lower().startswith("aws:") or not _RESOURCE_TAG_CHAR_PATTERN.fullmatch(key):
            raise ProviderError(
                "OldResourceProperties.ResourceTags contains a key that cannot be "
                "reconciled safely; the key is not repeated in stack events"
            )
        keys.add(key)
    return keys


def _reconcile_agentcore_tags(
    ctrl,
    resource_arn: str,
    desired: dict[str, str],
    old_keys: set[str] | None = None,
) -> None:
    """Apply our desired AgentCore tags, preserve foreign tags, and verify the result."""
    stale = sorted((old_keys or set()) - set(desired))
    if not desired and not stale:
        return
    if not resource_arn:
        raise ProviderError("AgentCore returned no resource ARN, so governance tags cannot be applied safely")

    def read_tags() -> dict[str, str]:
        try:
            response = ctrl.list_tags_for_resource(resourceArn=resource_arn)
        except Exception as e:
            code = _error_code(e) or type(e).__name__
            not_found = getattr(getattr(ctrl, "exceptions", None), "ResourceNotFoundException", ())
            if code == "ResourceNotFoundException" or (not_found and isinstance(e, not_found)):
                raise _AgentCoreTagResourceNotFound(resource_arn) from e
            raise ProviderError(
                f"AgentCore governance tags on {resource_arn} could not be read ({code}); "
                "the resource was not treated as correctly tagged"
            ) from e
        current = response.get("tags", {})
        if not isinstance(current, dict):
            raise ProviderError(f"AgentCore returned a non-map tag set for {resource_arn}")
        return {str(key): "" if value is None else str(value) for key, value in current.items()}

    current = read_tags()
    to_set = {key: value for key, value in desired.items() if current.get(key) != value}
    to_remove = [key for key in stale if key in current]
    new_key_count = sum(key not in current for key in to_set)
    capacity_needed = max(0, len(current) + new_key_count - 50)
    if capacity_needed > len(to_remove):
        raise ProviderError(
            f"AgentCore governance tags on {resource_arn} cannot converge without "
            "removing tags this stack does not own; reduce the requested tag set or "
            "remove unrelated tags from the provider and retry"
        )
    remove_first = to_remove[:capacity_needed]
    remove_after = to_remove[capacity_needed:]
    try:
        # At the 50-tag ceiling, a one-for-one key replacement must remove the stale
        # managed key before adding the new one. Without this ordering the service
        # rejects a perfectly valid final state as a transient 51st tag.
        if remove_first:
            ctrl.untag_resource(resourceArn=resource_arn, tagKeys=remove_first)
        if to_set:
            ctrl.tag_resource(resourceArn=resource_arn, tags=to_set)
        if remove_after:
            ctrl.untag_resource(resourceArn=resource_arn, tagKeys=remove_after)
    except Exception as e:
        # A capacity-safe replacement may have removed a stale key before the write
        # that failed. Restore this invocation's pre-update view best-effort so a
        # failed stack update does not leave governance weaker while CloudFormation
        # schedules its own rollback.
        try:
            after_failure = read_tags()
            introduced = sorted(key for key in desired if key not in current and key in after_failure)
            if introduced:
                ctrl.untag_resource(resourceArn=resource_arn, tagKeys=introduced)
                after_failure = {key: value for key, value in after_failure.items() if key not in introduced}
            restore = {key: value for key, value in current.items() if after_failure.get(key) != value}
            if restore:
                ctrl.tag_resource(resourceArn=resource_arn, tags=restore)
        except Exception:
            logger.exception(
                "Could not restore AgentCore tags after a failed reconciliation on %s",
                resource_arn,
            )
        code = _error_code(e) or type(e).__name__
        raise ProviderError(
            f"AgentCore governance tags on {resource_arn} could not be reconciled ({code}); "
            "the custom resource is failing rather than reporting an untagged success"
        ) from e

    if to_set or to_remove:
        current = read_tags()
    wrong = sorted(key for key, value in desired.items() if current.get(key) != value)
    present = sorted(key for key in stale if key in current)
    if wrong or present:
        raise ProviderError(
            f"AgentCore did not converge governance tags on {resource_arn}; "
            f"incorrect or missing keys: {wrong or '(none)'}, stale keys still present: "
            f"{present or '(none)'}"
        )


def _reconcile_log_group_tags(
    logs,
    event: dict,
    name: str,
    desired: dict[str, str],
    old_keys: set[str] | None = None,
) -> None:
    """Apply managed log-group tags, preserve foreign tags, and verify convergence."""
    stale = sorted((old_keys or set()) - set(desired))
    if not desired and not stale:
        return

    stack_parts = str(event.get("StackId", "")).split(":")
    region = str(getattr(getattr(logs, "meta", None), "region_name", "") or "")
    if len(stack_parts) < 6 or stack_parts[0] != "arn" or not stack_parts[1] or not stack_parts[4] or not region:
        raise ProviderError(
            "CloudWatch log-group tags cannot be verified because the stack partition, "
            "account, or provider region is unavailable"
        )
    arn = f"arn:{stack_parts[1]}:logs:{region}:{stack_parts[4]}:log-group:{name}"

    def read_tags() -> dict[str, str]:
        try:
            response = logs.list_tags_for_resource(resourceArn=arn)
        except Exception as e:
            code = _error_code(e) or type(e).__name__
            raise ProviderError(
                f"CloudWatch governance tags on log group {name} could not be read "
                f"({code}); the group was not treated as correctly tagged"
            ) from e
        current = response.get("tags", {})
        if not isinstance(current, dict):
            raise ProviderError(f"CloudWatch returned a non-map tag set for log group {name}")
        return {str(key): "" if value is None else str(value) for key, value in current.items()}

    current = read_tags()
    to_set = {key: value for key, value in desired.items() if current.get(key) != value}
    to_remove = [key for key in stale if key in current]
    new_key_count = sum(key not in current for key in to_set)
    capacity_needed = max(0, len(current) + new_key_count - 50)
    if capacity_needed > len(to_remove):
        raise ProviderError(
            f"CloudWatch governance tags on log group {name} cannot converge without "
            "removing tags this stack does not own; reduce the requested tag set or "
            "remove unrelated tags from the group and retry"
        )
    remove_first = to_remove[:capacity_needed]
    remove_after = to_remove[capacity_needed:]

    try:
        if remove_first:
            logs.untag_log_group(logGroupName=name, tags=remove_first)
        if to_set:
            logs.tag_log_group(logGroupName=name, tags=to_set)
        if remove_after:
            logs.untag_log_group(logGroupName=name, tags=remove_after)
    except Exception as e:
        try:
            after_failure = read_tags()
            introduced = sorted(key for key in desired if key not in current and key in after_failure)
            if introduced:
                logs.untag_log_group(logGroupName=name, tags=introduced)
                after_failure = {key: value for key, value in after_failure.items() if key not in introduced}
            restore = {key: value for key, value in current.items() if after_failure.get(key) != value}
            if restore:
                logs.tag_log_group(logGroupName=name, tags=restore)
        except Exception:
            logger.exception(
                "Could not restore CloudWatch tags after a failed reconciliation on %s",
                name,
            )
        code = _error_code(e) or type(e).__name__
        raise ProviderError(
            f"CloudWatch governance tags on log group {name} could not be reconciled "
            f"({code}); the custom resource is failing rather than reporting an "
            "untagged success"
        ) from e

    if to_set or to_remove:
        current = read_tags()
    wrong = sorted(key for key, value in desired.items() if current.get(key) != value)
    present = sorted(key for key in stale if key in current)
    if wrong or present:
        raise ProviderError(
            f"CloudWatch did not converge governance tags on log group {name}; "
            f"incorrect or missing keys: {wrong or '(none)'}, stale keys still present: "
            f"{present or '(none)'}"
        )


# ---------------------------------------------------------------------------
# Custom::AgentCodePackage
# ---------------------------------------------------------------------------


def _merge_deps_into_zip(target_zf: zipfile.ZipFile, bundle_bytes: bytes) -> None:
    """Extract dependency bundle into target zip, excluding __pycache__/.pyc."""
    with zipfile.ZipFile(io.BytesIO(bundle_bytes), "r") as bundle_zf:
        for item in bundle_zf.namelist():
            if "__pycache__" in item or item.endswith(".pyc"):
                continue
            target_zf.writestr(item, bundle_zf.read(item))


def _merge_code_and_deps(agent_zip_bytes: bytes, bundle_bytes: bytes) -> bytes:
    """Merge agent code zip and dependency bundle into a single zip.

    Starts from the pre-built bundle and appends agent code files on top.
    This preserves the bundle's original compression, avoiding a full
    re-compress with ZIP_DEFLATED that can push runtime init past the
    30-second timeout.
    """
    buf = io.BytesIO(bundle_bytes)
    with zipfile.ZipFile(buf, "a") as out_zf:
        with zipfile.ZipFile(io.BytesIO(agent_zip_bytes), "r") as code_zf:
            for item in code_zf.namelist():
                if "__pycache__" in item or item.endswith(".pyc"):
                    continue
                out_zf.writestr(item, code_zf.read(item))
    buf.seek(0)
    return buf.read()


def _content_digest(files: dict[str, bytes]) -> str:
    """Reproducible digest over a set of named source files.

    Third copy of one algorithm, and they must agree: cfn_template_generator
    .content_digest computes the template's parameter default, the generated
    deploy.sh recomputes it in bash from the operator's local files, and this
    recomputes it from the uploaded zip. Keep it boringly simple — one
    ``<name>  <sha256>`` line per file, sorted by name, hashed.

    Hashes the members rather than the archive on purpose: deploy.sh builds the zip
    with ``zip -r``, which embeds mtimes, so identical code produces different
    archive bytes on every run.
    """
    lines = "".join(f"{name}  {hashlib.sha256(body).hexdigest()}\n" for name, body in sorted(files.items()))
    return "sha256:" + hashlib.sha256(lines.encode()).hexdigest()


def _zip_source_members(zip_bytes: bytes) -> dict[str, bytes]:
    """Source members of *zip_bytes*, named as the digest expects.

    Skips directory entries and the compiled-Python noise the merge step drops
    anyway, and normalizes the leading "./" that ``zip -r .`` may or may not store
    depending on the zip implementation the operator has.
    """
    members = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or "__pycache__" in name or name.endswith(".pyc"):
                continue
            members[name[2:] if name.startswith("./") else name] = zf.read(info)
    return members


def _verify_source_digest(agent_zip: bytes, expected: str) -> None:
    """Refuse to deploy code that is not the code the stack was told to deploy.

    The merged output runs with the runtime role, so whoever controls the bytes at
    AgentCodeKey controls what that role executes. Staging-bucket write access is a
    much lower bar than cloudformation:UpdateStack, so without this check a
    principal holding only s3:PutObject could substitute the agent's code. The
    expected digest arrives as a resource property, i.e. from whoever ran the
    deploy, which is the trust boundary we want it to come from.
    """
    if not expected:
        # An older template that predates the property. Nothing to compare
        # against, and failing every such stack would be worse than a warning.
        logger.warning("No SourceDigest on this resource — skipping code integrity check")
        return

    actual = _content_digest(_zip_source_members(agent_zip))
    if actual != expected:
        raise ValueError(
            f"Agent code digest mismatch: the stack expects {expected} but the zip "
            f"at the staging key hashes to {actual}. The code was changed after the "
            f"deploy computed its digest, so it has NOT been deployed. Re-run "
            f"deploy.sh to publish the current code, or investigate who wrote to the "
            f"bucket if you did not change it."
        )
    logger.info("Agent code digest verified: %s", expected)


def _verify_bundle_digest(bundle: bytes, expected: str) -> None:
    """Refuse to merge a dependency bundle that is not the one the deploy uploaded.

    The bundle is every third-party package the agent imports — by far the larger
    part of what ends up executing under the runtime role — and it was previously
    merged in on trust alone (CWE-494, Download of Code Without Integrity Check).
    The same staging-bucket write access that this rejects for agent.py bought
    unchecked code substitution here.

    Whole-archive hash rather than the per-member digest the source zips use: the
    bundle is uploaded exactly as built, so there is no re-zip in between to
    perturb the bytes, and the operator can reproduce the value with a single
    ``shasum -a 256``.

    "none", or absent, means no digest was supplied. That is a real case, not a
    defect — a recipient whose platform team pre-staged the bundle into the bucket
    has nothing to hash locally — so it warns rather than failing, and says which
    of the two it is so the log cannot be read as "verified".
    """
    if not expected or expected == "none":
        logger.warning(
            "No DependencyBundleDigest on this resource: the dependency bundle was merged "
            "WITHOUT an integrity check. Supply one with DEPENDENCY_BUNDLE_DIGEST to enable it."
        )
        return

    actual = "sha256:" + hashlib.sha256(bundle).hexdigest()
    if actual != expected:
        raise ValueError(
            f"Dependency bundle digest mismatch: the stack expects {expected} but the "
            f"object at the bundle key hashes to {actual}. The bundle in the bucket is "
            f"not the one this deploy published, so it has NOT been deployed. Re-run "
            f"deploy.sh to republish it, or investigate who wrote to the bucket."
        )
    logger.info("Dependency bundle digest verified: %s", expected)


#: The exact Secrets Manager ARN grammar the generator's AllowedPattern enforces on the template parameter. The handler
#: enforces the SAME grammar itself because a hand-edited or Terraform-embedded template bypasses parameter constraints:
#: partition arn:aws[...], service secretsmanager, a region token, a 12-digit account, and a `secret:<name>` resource.
LITELLM_SECRET_ARN_RE = re.compile(
    r"arn:aws[a-zA-Z-]*:secretsmanager:[a-z0-9-]{1,32}:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]{1,512}"
)


def _verify_litellm_secret_region(props: dict) -> None:
    """Refuse a LiteLLM key secret that lives in another region.

    Only present on a LiteLLM export. The agent runtime builds its Secrets Manager client
    from its OWN region -- ``AWS_REGION`` inside the container -- and not from the ARN it
    was handed, so a stack deployed to region B holding a region-A ARN deploys green and
    then fails on the first tool call. Measured live against a real secret: the error is
    ``ResourceNotFoundException: Secrets Manager can't find the specified secret``, which
    never mentions a region even though the call passed a full ARN naming one. Hours after
    a successful deploy, that is close to undiagnosable.

    Refused rather than resolved, on purpose, and on guidance rather than on local taste.
    ARCC guidance on regional isolation (cnt_IMvJNFIFGGpGIU, "[Service Credentials - Single
    Region Credentials]") requires that any credential used by a service in a region be
    stored, deployed and used solely for that region. ARCC guidance on secrets management
    (cnt_LuG2TKuO0errRp) says the same thing from the other side: secrets "must not be
    shared globally or between regions and partitions... do not create global secrets." So
    a cross-region read is not a thing to make work.

    This runs here rather than in deploy.sh alone because deploy.sh is not the only
    consumer: the generated README documents wrapping template.yaml in Terraform's
    ``aws_cloudformation_stack``, and that path runs no script of ours. The provider does
    run, on every create and every update, in the stack's own region -- which is why the
    region to compare against is simply this Lambda's.

    Account is deliberately NOT checked here, and the asymmetry is the point. Cross-region
    cannot be made to work; cross-account can, once the owning account grants a resource
    policy on the secret and a key policy on the customer-managed KMS key that encrypts it
    (ARCC cnt_LuG2TKuO0errRp names exactly those two mechanisms for scoping access to a
    secret and to a key). An operator who has done that setup is not making a mistake, so
    failing their stack from inside the template -- the one path Terraform cannot bypass --
    would break a legitimate configuration. deploy.sh therefore refuses cross-account by
    default and takes an explicit ``i-accept-cross-account-secret-setup`` acknowledgement to
    proceed, which is where a policy choice belongs rather than in a correctness check.

    Raised as ProviderError and not as a bare ValueError, which matters more here than it
    looks: ``_safe_failure_reason`` reduces every other exception class to its name, so a
    ValueError would reach the operator's stack events as "cfn-provider failed with
    ValueError" and the entire explanation below would exist only in a Lambda log group they
    have to go find. The remedy for this failure is a single specific action -- create the
    secret in the deploy region -- so it has to travel with the failure. It satisfies
    ProviderError's narrow contract: every part of the message is a literal or a region
    name, and no part of it came through a request body or a botocore message.
    """
    raw = str(props.get("LiteLLMApiKeySecretArn") or "")
    arn = raw.strip()
    if not arn:
        return  # absent or whitespace-only: not a LiteLLM export
    # exact parity with the template's AllowedPattern: a padded value is refused (CloudFormation would refuse the actual
    # padded parameter), never silently trimmed into acceptance
    grammar = LITELLM_SECRET_ARN_RE.fullmatch(arn) if raw == arn else None
    if grammar is None:
        # Malformed rather than cross-region. The template's AllowedPattern already
        # rejects this shape, so reaching here means the pattern changed; say what is
        # wrong instead of reading an out-of-range element.
        #
        # Deliberately WITHOUT echoing the value, unlike every other message in this
        # module that names a resource id. The likeliest way to arrive here is a virtual
        # key pasted where its ARN belonged -- the same mistake deploy.sh's own
        # not-an-ARN branch refuses to echo -- and this string is bound for stack events,
        # which are readable for 90 days and land in Terraform state. A malformed value is
        # exactly the value that must not be repeated back.
        raise ProviderError(
            "LiteLLMApiKeySecretArn is not a Secrets Manager ARN. Expected "
            "arn:<partition>:secretsmanager:<region>:<account>:secret:<name> -- the ARN of "
            "the secret holding the LiteLLM virtual key, not the key itself. The value is "
            "not repeated here in case it is the key."
        )
    arn_region = arn.split(":")[3]  # safe: the grammar above fixed the field layout
    # The provider Lambda is created by this stack, so its region IS the stack's.
    own_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    if own_region and arn_region != own_region:
        raise ProviderError(
            f"The LiteLLM key secret is in {arn_region} but this stack is deploying to "
            f"{own_region}. The agent reads the secret from its own region, so this stack "
            f"would reach CREATE_COMPLETE and then fail on its first tool call with a bare "
            f"'Secrets Manager can't find the specified secret'. Create the secret in "
            f"{own_region} and pass its ARN as the LiteLLMApiKeySecretArn parameter. The "
            f"ARN baked into template.yaml is the one from the environment the bundle was "
            f"exported from, which is the usual cause."
        )


def _handle_code_package_create_update(event: dict) -> tuple[dict, str]:
    """Handle CREATE/UPDATE for AgentCodePackage."""
    props = event["ResourceProperties"]
    bucket = props["ArtifactsBucket"]
    agent_code_key = props["AgentCodeKey"]
    bundle_key = props["DependencyBundleKey"]
    output_key = props["OutputKey"]

    # First, before any other helper can grow into a pre-flight side effect and before
    # a single byte is downloaded or written: a stack that cannot work is better
    # stopped than packaged.
    _verify_litellm_secret_region(props)

    resource_tags = _resource_tags(props)
    if len(resource_tags) > 10:
        raise ProviderError(
            f"{len(resource_tags)} resource tags were supplied for the merged code object; "
            "S3 object tagging accepts at most 10. Remove some tags and retry."
        )

    s3 = boto3.client("s3")
    owner = _owner_kwargs(event)

    logger.info("Downloading agent code: s3://%s/%s", bucket, agent_code_key)
    agent_zip = s3.get_object(Bucket=bucket, Key=agent_code_key, **owner)["Body"].read()
    logger.info("Agent code zip: %d bytes", len(agent_zip))

    # Before the merge, not after: nothing untrusted should reach the output key.
    _verify_source_digest(agent_zip, props.get("SourceDigest", ""))

    logger.info("Downloading dependency bundle: s3://%s/%s", bucket, bundle_key)
    bundle = s3.get_object(Bucket=bucket, Key=bundle_key, **owner)["Body"].read()
    logger.info("Dependency bundle: %d bytes", len(bundle))

    # Also before the merge: _merge_code_and_deps builds the output on top of these
    # bytes, so a bundle checked afterwards would already be at the output key.
    _verify_bundle_digest(bundle, props.get("BundleDigest", ""))

    merged = _merge_code_and_deps(agent_zip, bundle)
    logger.info("Merged code.zip: %d bytes", len(merged))

    logger.info("Uploading to s3://%s/%s", bucket, output_key)
    s3.put_object(
        Bucket=bucket,
        Key=output_key,
        Body=merged,
        **({"Tagging": urlencode(sorted(resource_tags.items()))} if resource_tags else {}),
        **owner,
    )

    physical_id = f"{bucket}/{output_key}"
    return {"CodeZipPrefix": output_key}, physical_id


def _handle_code_package_delete(event: dict) -> tuple[dict, str]:
    """Handle DELETE for AgentCodePackage.

    Every property is read with ``.get``. They used to be indexed, and a Delete is
    exactly the event that can arrive without them: CloudFormation sends Delete with
    whatever properties it has recorded, so a resource that failed before its
    properties were recorded, or one created by an older template, produced a
    KeyError here. That is the worst place for one — the resource is being deleted,
    so the KeyError becomes a FAILED Delete, and a FAILED Delete leaves the stack in
    DELETE_FAILED over an S3 object nobody needs.

    A failed delete is logged and reported as success on purpose: failing the resource
    would block the whole stack's deletion on a cleanup step. The policy handler makes
    the opposite choice, and for a concrete reason — a leftover policy blocks
    DeletePolicyEngine, so swallowing that one only trades a clear failure for an
    obscure one.

    This used to be a bare ``delete_object`` on the key, justified in a comment that
    called the object "a build artifact, not data". Both halves of that were wrong.

    The object is the merged ``code.zip`` the runtime executes, so it contains the
    recipient's ``agent.py`` and therefore their system prompt — customer content, not
    a build artifact. And on a *versioned* bucket a ``delete_object`` without a
    ``VersionId`` does not delete anything: it adds a delete marker, the object stays
    fully readable by ``VersionId``, and ``aws s3 ls`` then reports the prefix empty.
    Measured live: after a clean teardown that reported success, the merged zip was
    downloaded by ``VersionId`` and the system prompt recovered from it in full.

    Per ARCC ``cnt_NfWe8fYjVfR6Gs``, hard deletion is "the irreversible removal of all
    affected data" and applies to "all copies of the data", and it names this exact
    anti-pattern: "removing pointers to S3 objects and orphaning data in your service
    account does not meet the bar". A delete marker is precisely a removed pointer over
    live data. So delete every version of the key, by id.

    The bucket's ``NoncurrentVersionExpiration`` is not a substitute. It is 30 days
    rather than immediate, and per ARCC ``cnt_XL9e2sbGgxAvce`` a bucket the tool did not
    create carries no lifecycle at all — which is the normal case here, since the
    recipient may pass a bucket they already had.
    """
    props = event.get("ResourceProperties", {})
    bucket = props.get("ArtifactsBucket", "")
    output_key = props.get("OutputKey", "")
    physical_id = event.get("PhysicalResourceId", event.get("LogicalResourceId", ""))

    if not bucket or not output_key:
        logger.warning(
            "Delete for %s carries no ArtifactsBucket/OutputKey; nothing to clean up",
            event.get("LogicalResourceId", ""),
        )
        return {}, physical_id

    s3 = boto3.client("s3")
    owner = _owner_kwargs(event)
    try:
        deleted = _delete_every_version(s3, bucket, output_key, owner)
        # Report the count, because "Deleted" on its own was the misleading part: the
        # old log line said that while the object was still downloadable.
        logger.info("Deleted %d version(s) of s3://%s/%s", deleted, bucket, output_key)
        remaining = _count_versions(s3, bucket, output_key, owner)
        if remaining:
            # Not a failure of the stack delete, but it must not be silent either: this
            # is customer content still readable in their bucket.
            logger.warning(
                "s3://%s/%s still has %d version(s) after cleanup; the agent source "
                "remains readable. Needs s3:DeleteObjectVersion and "
                "s3:ListBucketVersions, which are distinct from s3:DeleteObject and "
                "s3:ListBucket.",
                bucket,
                output_key,
                remaining,
            )
    except Exception as e:
        # The same sentence as the branch above, deliberately, and this is not
        # belt-and-braces. Measured live: when s3:ListBucketVersions is the missing
        # permission, the enumeration inside _delete_every_version raises before any
        # delete is attempted, so _count_versions never runs and the warning above
        # cannot fire. The only operator-facing line left used to be "Failed to
        # delete ...: AccessDenied", which says nothing about what is still readable.
        # The diagnostic was gated by the very permission whose absence it exists to
        # report, and it failed quietest on the harder of the two grants to spot.
        #
        # There is no count here because obtaining one is the thing that failed; per
        # ARCC cnt_Hr4zJD4KntOWIt a service must state the activity of a deletion
        # clearly, and "could not confirm" is the honest statement, not "failed".
        logger.warning(
            "Could not confirm deletion of s3://%s/%s: %s. Treat the agent source as "
            "still readable there. Needs s3:DeleteObjectVersion and "
            "s3:ListBucketVersions, which are distinct from s3:DeleteObject and "
            "s3:ListBucket.",
            bucket,
            output_key,
            e,
        )

    return {}, physical_id


# One key's versions, at 1000 per page. A hundred pages is 100k versions of a single
# code.zip, which no real deployment reaches; the bound exists to stop a malformed
# pagination response spinning, not to cap legitimate work.
_MAX_VERSION_PAGES = 100


def _iter_versions(s3, bucket: str, key: str, owner: dict):
    """Every version and delete marker whose key is exactly ``key``.

    ``list_object_versions`` takes a *prefix*, so it also returns siblings that merely
    start with this key — ``code.zip.sha256`` beside ``code.zip``. The equality filter is
    not there to protect the neighbour from deletion (a ``delete_object`` naming *this*
    key with the neighbour's ``VersionId`` does not remove the neighbour): it is there
    because this same function is the re-list that reports the outcome, and an unfiltered
    count would report a neighbour as a surviving version and warn that the agent source
    is still readable when it is not.

    Pagination is bounded rather than ``while True``. A response that reports
    ``IsTruncated`` while returning no usable marker would otherwise spin forever inside a
    stack Delete, and a custom resource that hangs costs an hour before CloudFormation
    gives up on it.
    """
    token: dict = {}
    for _ in range(_MAX_VERSION_PAGES):
        page = s3.list_object_versions(Bucket=bucket, Prefix=key, **owner, **token)
        for collection in ("Versions", "DeleteMarkers"):
            for entry in page.get(collection, []):
                if entry.get("Key") == key:
                    yield entry["VersionId"]
        next_token = {
            "KeyMarker": page.get("NextKeyMarker") or "",
            "VersionIdMarker": page.get("NextVersionIdMarker") or "",
        }
        if not page.get("IsTruncated") or next_token == token:
            return
        token = next_token


def _delete_every_version(s3, bucket: str, key: str, owner: dict) -> int:
    """Hard-delete the key. Returns how many versions were removed.

    ``VersionId`` is the literal string ``"null"`` on a bucket that was never versioned
    and ``delete_object`` accepts it, so this one path covers versioned and unversioned
    buckets alike and there is no branch to get wrong.

    A failing delete is tolerated rather than propagated, and that is deliberate: one
    denied version must not skip the re-list below, which is the only thing that can tell
    the operator their agent source is still readable. The caller's success signal is
    ``_count_versions``, never this return value.
    """
    count = 0
    for version_id in list(_iter_versions(s3, bucket, key, owner)):
        try:
            s3.delete_object(Bucket=bucket, Key=key, VersionId=version_id, **owner)
            count += 1
        except Exception as e:
            logger.warning("Could not delete version %s of s3://%s/%s: %s", version_id, bucket, key, e)
    return count


def _count_versions(s3, bucket: str, key: str, owner: dict) -> int:
    """What the bucket says is left, which is the only honest success signal.

    The deletes above are individually tolerant so one missing permission cannot wedge a
    stack deletion, which means their completing proves nothing. Re-listing is what
    distinguishes a real purge from a loop that swallowed every error.
    """
    return sum(1 for _ in _iter_versions(s3, bucket, key, owner))


# ---------------------------------------------------------------------------
# Custom::OAuth2CredentialProvider
# ---------------------------------------------------------------------------


def _get_agentcore_ctrl():
    """Get bedrock-agentcore-control client."""
    return boto3.client("bedrock-agentcore-control")


def _resolve_client_secret(props: dict) -> str:
    """The Cognito app client's secret, read from Cognito rather than from the event.

    The template hands over ``UserPoolId`` and ``ClientId`` and this reads the secret
    with ``DescribeUserPoolClient``. It deliberately does NOT arrive as a property.

    CloudFormation copies a resource's resolved ``ResourceProperties`` into the stack's
    event stream, in every status, and keeps those events for 90 days. A secret placed
    there is therefore readable by any principal holding
    ``cloudformation:DescribeStackEvents`` — far wider than the set trusted with the
    credential — and it cannot be scrubbed afterwards. That was not a theory: the
    52-character secret was recovered verbatim from three events on a deployed stack.
    ``NoEcho`` is no defence, because it covers template parameters and this was a
    ``GetAtt`` on a resource.

    This paragraph used to say "of every ``Custom::`` resource", and that was wrong in a
    way worth recording, because it is what kept the sibling leak open: measured on a
    live stack, 78 of 110 events carried ``ResourceProperties``, covering every logical
    id including NATIVE types. The same secret was sitting in the events of
    ``AWS::BedrockAgentCore::Runtime`` at the same time as this one. Custom resources are
    not special here; anything a property resolves to is echoed.

    The legacy ``ClientSecret`` property is still honoured, and only as a fallback, so
    that a stack created by an older template keeps updating cleanly while its
    ``cfn-provider.zip`` is newer than its template. That path is the insecure one; it
    warns, and it disappears once the stack is next exported.
    """
    user_pool_id = props.get("UserPoolId", "")
    client_id = props.get("ClientId", "")
    if user_pool_id:
        resp = boto3.client("cognito-idp").describe_user_pool_client(UserPoolId=user_pool_id, ClientId=client_id)
        secret = resp["UserPoolClient"].get("ClientSecret", "")
        if not secret:
            # A client created without GenerateSecret cannot do client_credentials, so
            # fail here with the cause rather than letting AgentCore reject the config.
            raise ValueError(
                f"Cognito app client {client_id} in pool {user_pool_id} has no client "
                "secret. The OAuth2 credential provider needs one for the "
                "client_credentials flow; recreate the client with GenerateSecret: true."
            )
        return secret

    legacy = props.get("ClientSecret", "")
    if legacy:
        logger.warning(
            "Using the legacy ClientSecret property. This stack's template predates the "
            "fix that stopped sending the secret through CloudFormation, so the value is "
            "exposed in this stack's events; re-export to remove it."
        )  # nosemgrep: python-logger-credential-disclosure -- logs no secret value
        return legacy

    raise ValueError(
        "OAuth2 credential provider needs UserPoolId (preferred) or ClientSecret in its "
        "ResourceProperties, and neither was supplied."
    )


def _mcp_endpoint_data(runtime_arn: str) -> dict:
    """``{"McpEndpointUrl": ...}`` for a runtime ARN, or ``{}`` when there is none.

    Shared by create and update so an updated provider returns the same attributes a
    created one does. It did not, when update was a delete-plus-create by another
    name, and any GetAtt on this resource would have gone unresolved after an update.
    """
    if not runtime_arn:
        return {}
    region = runtime_arn.split(":")[3] if ":" in runtime_arn else "us-east-1"
    encoded_arn = quote(runtime_arn, safe="")
    endpoint_url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    )
    logger.info("MCP endpoint URL: %s", endpoint_url)
    return {"McpEndpointUrl": endpoint_url}


def _provider_config(discovery_url: str, client_id: str, client_secret: str) -> dict:
    """The ``oauth2ProviderConfigInput`` both create and update take.

    One builder for both calls: the update path used to be a delete followed by a
    create, so the two shapes could not drift. Now that it is a real update they can,
    and a mismatch would show up as a provider that works after a create and stops
    working after an update.
    """
    return {
        "customOauth2ProviderConfig": {
            "oauthDiscovery": {"discoveryUrl": discovery_url},
            "clientId": client_id,
            "clientSecret": client_secret,
        }
    }


def _existing_provider_client(ctrl, name: str) -> tuple[str, str, str]:
    """``(arn, client_id, discovery_url)`` of an existing provider, from the service.

    The secret is not readable — ``get`` returns ``clientSecretArn``, never the
    secret — which is exactly why the client id is the discriminator below.
    """
    resp = ctrl.get_oauth2_credential_provider(name=name)
    custom = (resp.get("oauth2ProviderConfigOutput") or {}).get("customOauth2ProviderConfig") or {}
    return (
        resp.get("credentialProviderArn", ""),
        custom.get("clientId", ""),
        (custom.get("oauthDiscovery") or {}).get("discoveryUrl", ""),
    )


def _adopt_existing_provider(
    ctrl,
    name: str,
    discovery_url: str,
    client_id: str,
    client_secret: str,
    resource_tags: dict[str, str],
) -> str:
    """Take over a same-named provider only when it is demonstrably this stack's.

    Create used to answer "already exists" by fetching the ARN and adopting the
    provider unconditionally — and Delete then destroys whatever it adopted. Two ways
    that ends badly, and neither is exotic. The provider name is derived from the
    deployment name, so the same name is reached by the platform's own live deploy of
    the same agent (gateway_step) and by any other stack using that deployment name;
    adopting there means a teardown of THIS stack deletes the credential provider a
    different, working deployment depends on. And a stale provider left over from a
    previous incarnation points at a Cognito app client that no longer exists, so
    adopting it yields a green stack whose gateway target cannot get a token — the
    kind of failure that only shows up on the first real call.

    The client id settles it. Every incarnation of this stack creates its own Cognito
    user pool and app client, so a provider whose client id matches the one we were
    asked to configure is bound to this very stack's pool and nothing else's. On a
    match the provider is updated rather than merely fetched, which also pushes the
    current secret — the case where a retry follows a client-secret rotation.

    On a mismatch this refuses, and the message names what to delete. A refusal costs
    the recipient one CLI call; the alternative costs somebody else their deployment.
    """
    arn, existing_client_id, existing_discovery = _existing_provider_client(ctrl, name)
    if existing_client_id != client_id:
        raise ProviderError(
            f"an OAuth2 credential provider named {name} already exists in this account and is "
            f"bound to a different OAuth client (existing clientId {existing_client_id or 'unknown'}, "
            f"discovery {existing_discovery or 'unknown'}), so it belongs to another deployment or "
            "to a previous incarnation of this one. It was NOT taken over, because deleting this "
            "stack would then delete it. Confirm nothing else uses it and remove it with "
            f"'aws bedrock-agentcore-control delete-oauth2-credential-provider --name {name}', or "
            "deploy this stack with a different DeploymentName."
        )
    # The client-id check above is the ownership proof. Only after it passes may this
    # handler write tags to an existing provider; a matching name alone is not authority.
    _reconcile_agentcore_tags(ctrl, arn, resource_tags)
    logger.info(
        "OAuth2 provider %s already exists for this stack's client; updating it in place",
        name,
    )
    resp = ctrl.update_oauth2_credential_provider(
        name=name,
        credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput=_provider_config(discovery_url, client_id, client_secret),
    )
    return resp.get("credentialProviderArn", "") or arn


def _handle_oauth2_cred_create(event: dict) -> tuple[dict, str]:
    """Create an OAuth2 credential provider via bedrock-agentcore-control API."""
    props = event["ResourceProperties"]
    resource_tags = _resource_tags(props)
    name = props["ProviderName"]
    discovery_url = props["DiscoveryUrl"]
    client_id = props["ClientId"]
    client_secret = _resolve_client_secret(props)

    ctrl = _get_agentcore_ctrl()

    logger.info("Creating OAuth2 provider resource: %s", name)
    created_here = False
    try:
        resp = ctrl.create_oauth2_credential_provider(
            name=name,
            credentialProviderVendor="CustomOauth2",
            oauth2ProviderConfigInput=_provider_config(discovery_url, client_id, client_secret),
            **({"tags": resource_tags} if resource_tags else {}),
        )
        created_here = True
        cred_arn = resp.get("credentialProviderArn", "")
    except ctrl.exceptions.ValidationException as e:
        if "already exists" not in str(e):
            raise
        cred_arn = _adopt_existing_provider(
            ctrl,
            name,
            discovery_url,
            client_id,
            client_secret,
            resource_tags,
        )
    logger.info("Created OAuth2 provider resource: %s", cred_arn)

    try:
        # Wait a few seconds for IAM propagation
        time.sleep(5)
        # Create accepts tags atomically, and the read-back is the oracle that catches a
        # service or permission regression instead of returning CREATE_COMPLETE untagged.
        _reconcile_agentcore_tags(ctrl, cred_arn, resource_tags)
    except Exception as verify_error:
        if created_here:
            # A FAILED Create response cannot carry the ARN returned above through this
            # function, so CloudFormation's rollback Delete would receive only the
            # logical id and could not find the provider. Remove what this invocation
            # just created before reporting the failed governance control.
            try:
                ctrl.delete_oauth2_credential_provider(name=name)
                logger.info(
                    "Deleted newly-created OAuth2 provider %s after tag verification failed",
                    name,
                )
            except Exception as cleanup_error:
                cleanup_code = _error_code(cleanup_error) or type(cleanup_error).__name__
                if cleanup_code not in ("ResourceNotFoundException", "ValidationException"):
                    raise ProviderError(
                        f"OAuth2 provider {name} tag verification failed and its "
                        f"compensating delete also failed ({cleanup_code}); remove the "
                        "provider before retrying the stack"
                    ) from verify_error
        if isinstance(verify_error, _AgentCoreTagResourceNotFound):
            raise ProviderError(
                f"OAuth2 provider {name} could not be read back after creation, so the "
                "custom resource removed it and failed instead of reporting an "
                "unverified success"
            ) from verify_error
        raise

    data = {"CredentialProviderArn": cred_arn}
    data.update(_mcp_endpoint_data(props.get("RuntimeArn", "")))
    return data, cred_arn


def _handle_oauth2_cred_update(event: dict) -> tuple[dict, str]:
    """Update the provider in place. Never delete first.

    This used to delete the existing provider and then create a replacement, which is
    the worst available ordering and was proven so live: when the create leg failed,
    CloudFormation reported UPDATE_ROLLBACK_COMPLETE — a green-looking stack — over a
    provider that no longer existed at all, because rollback has nothing to restore
    a resource the handler itself destroyed. Any window in which the provider is gone
    is also a window in which the gateway target cannot fetch a token, so even the
    successful path broke live traffic for as long as the create took.

    ``UpdateOauth2CredentialProvider`` takes the same input as create and keeps the
    name, so the ARN — this resource's PhysicalResourceId — does not change. That
    matters beyond tidiness: returning a *different* physical id tells CloudFormation
    the resource was replaced, and it then deletes what it thinks is the old one, so
    the previous shape could destroy the provider it had just created. A stable id
    means a secret rotation is an in-place update, which is what the template says it
    is.

    Falls back to create only when the provider is genuinely absent — someone removed
    it out of band — rather than treating every update failure as a reason to create.

    AND IT MUST ONLY EVER UPDATE THE PROVIDER **THIS RESOURCE** CREATED. Create refuses
    to take over a same-named provider bound to a different OAuth client
    (``_adopt_existing_provider``); Update had no such check, so the two disagreed and
    the weaker one was reachable from the stronger one's own resource. Change
    ``ProviderName`` in the template to a name another deployment is using and
    CloudFormation sends an Update — whereupon this handler overwrote that provider's
    client id, discovery URL and secret with ours. The victim's gateway target then
    mints tokens against our Cognito pool, and because the returned PhysicalResourceId
    became the victim's ARN, our eventual stack Delete deleted *their* provider.

    The PhysicalResourceId is the authority on what this resource owns: CloudFormation
    assigned it from our own Create. A requested name that does not match the name
    inside it is not an in-place update at all, it is a rename — which CloudFormation
    already models as a REPLACEMENT — so it routes to Create, which performs the
    ownership check and refuses a foreign provider by name.

    The check is deliberately on the name and not on the client id: a legitimate
    redeploy recreates the Cognito app client, so the client id DOES change on an
    honest rotation, and requiring it to match would reject exactly the case this
    handler exists to serve. Ownership comes from the physical id; the client id is
    only the discriminator Create has to fall back on, because Create has no physical
    id yet.
    """
    props = event["ResourceProperties"]
    resource_tags = _resource_tags(props)
    old_tag_keys = _old_resource_tag_keys(event)
    name = props["ProviderName"]
    discovery_url = props["DiscoveryUrl"]
    client_id = props["ClientId"]

    physical_id = event.get("PhysicalResourceId", "")
    owned_name = physical_id.rsplit("/", 1)[-1] if physical_id.startswith("arn:") else ""
    if owned_name != name:
        # Either the name changed (a replacement) or there is no owned ARN to compare
        # against at all. Both mean "we cannot prove this resource owns the provider
        # called *name*", and the only safe answer to that is the Create path, which
        # creates it if absent and otherwise refuses anything bound to a different
        # OAuth client. An unproven update is the takeover; an unproven create is not.
        logger.info(
            "Update names provider %s but this resource owns %s; routing to create so "
            "ownership is checked instead of overwriting a provider we may not own",
            name,
            owned_name or "(no ARN)",
        )
        return _handle_oauth2_cred_create(event)

    client_secret = _resolve_client_secret(props)

    ctrl = _get_agentcore_ctrl()
    try:
        _reconcile_agentcore_tags(ctrl, physical_id, resource_tags, old_tag_keys)
    except _AgentCoreTagResourceNotFound:
        # The pre-tagging implementation reached Update first and recreated a provider
        # that had been removed out of band. Tag reconciliation reads before writing;
        # without this branch, adding tags accidentally removed that recovery path.
        logger.info("OAuth2 provider %s no longer exists; creating it", name)
        return _handle_oauth2_cred_create(event)
    try:
        resp = ctrl.update_oauth2_credential_provider(
            name=name,
            credentialProviderVendor="CustomOauth2",
            oauth2ProviderConfigInput=_provider_config(discovery_url, client_id, client_secret),
        )
    except Exception as e:
        if _error_code(e) != "ResourceNotFoundException" and not isinstance(
            e, ctrl.exceptions.ResourceNotFoundException
        ):
            raise
        logger.info("OAuth2 provider %s no longer exists; creating it", name)
        return _handle_oauth2_cred_create(event)

    cred_arn = resp.get("credentialProviderArn", "") or event.get("PhysicalResourceId", "")
    logger.info("Updated OAuth2 provider resource: %s", cred_arn)

    # IAM/secret propagation, same reason as the create path.
    time.sleep(5)

    data = {"CredentialProviderArn": cred_arn}
    data.update(_mcp_endpoint_data(props.get("RuntimeArn", "")))
    return data, cred_arn


_BENIGN_DELETE_CODES = ("ResourceNotFoundException", "ValidationException")


def _delete_oauth2_cred(cred_arn: str) -> None:
    """Delete an OAuth2 credential provider by ARN, and say so if it did not work.

    Every failure used to be logged at warning level and swallowed, so the resource
    reported a successful Delete while the provider was still there. That is not a
    tidiness point any more: Create now refuses to take over a same-named provider
    bound to a different OAuth client (see _adopt_existing_provider), so a provider
    left behind by a swallowed delete blocks the next deployment of the same
    DeploymentName — and it does so much later, in a different stack, with no
    connection to the delete that actually failed.

    "Already gone" stays benign, because that is the state a Delete is trying to
    reach: a not-found provider on teardown means an earlier attempt succeeded, or the
    create never got that far. Everything else — AccessDenied above all — raises, so
    it lands in the stack events while the operator is still looking at them.
    """
    ctrl = _get_agentcore_ctrl()
    # Extract the name from ARN
    # ARN format: arn:aws:bedrock-agentcore:region:account:token-vault/default/oauth2credentialprovider/name
    cred_name = cred_arn.rsplit("/", 1)[-1] if "/" in cred_arn else cred_arn
    try:
        ctrl.delete_oauth2_credential_provider(name=cred_name)
        logger.info("Deleted OAuth2 provider resource: %s", cred_arn)
    except Exception as e:
        code = _error_code(e)
        if code in _BENIGN_DELETE_CODES:
            logger.info("OAuth2 provider %s is already gone (%s)", cred_name, code)
            return
        # type(e).__name__ and the AWS error code only. str(e) is a botocore message
        # built from the request, and the create/update requests for this resource
        # carry a Cognito app client secret — see _safe_failure_reason.
        logger.error("Failed to delete OAuth2 provider %s: %s %s", cred_name, type(e).__name__, code)
        raise ProviderError(
            f"the OAuth2 credential provider {cred_name} could not be deleted ({code or type(e).__name__}), "
            "so it still exists. Delete it with 'aws bedrock-agentcore-control "
            f"delete-oauth2-credential-provider --name {cred_name}' and retry the stack deletion; "
            "leaving it in place will block the next deployment that uses this DeploymentName."
        ) from e


def _handle_oauth2_cred_delete(event: dict) -> tuple[dict, str]:
    """Handle DELETE for OAuth2CredentialProvider."""
    cred_arn = event.get("PhysicalResourceId", "")
    if cred_arn and cred_arn.startswith("arn:"):
        _delete_oauth2_cred(cred_arn)
    physical_id = event.get("PhysicalResourceId", event.get("LogicalResourceId", ""))
    return {}, physical_id


# ---------------------------------------------------------------------------
# Custom::AgentCorePolicy — Cedar policy attached to a PolicyEngine
# Native AWS::BedrockAgentCore::Policy has a stabilization timeout that
# fires before the policy engine is ready in fresh accounts (Bug 72). This
# Custom Resource gives us a longer wait + retries on the bind step.
#
# It also has to do what the native resource would have done and this one did
# not: check that the policy reached a terminal ACTIVE status before reporting
# SUCCESS. CreatePolicy is asynchronous. It returns 200 with status CREATING for
# a Cedar statement the service will go on to reject, and the rejection lands in
# statusReasons some seconds later. Reporting SUCCESS on the 200 produces the
# worst possible outcome for an operator: a CREATE_COMPLETE stack whose agent has
# no authorization policy and therefore no tools, with nothing in the stack
# events to say so. Fail the resource instead, and put the service's own reason
# in the failure.
# ---------------------------------------------------------------------------


def _deadline(context, reserve_seconds: float = 45.0) -> float:
    """The monotonic instant after which this invocation must stop waiting.

    Every retry budget here has to be strictly shorter than the Lambda's own
    timeout, because the two ways of running out of time are not remotely
    comparable. If this budget expires first, the handler sends FAILED and
    CloudFormation begins rolling back within seconds. If the Lambda timeout
    expires first, the process is killed mid-``sleep``, no response is ever sent,
    and CloudFormation sits on the resource for the full custom-resource timeout —
    one hour — before it will even start to roll back.

    The wait loop below used to be 30 attempts x 10s == 300s against a Timeout of
    300, so any account that genuinely took five minutes hit the one-hour hang by
    construction, and the retry loops after it could add 100s more. The budget is
    read from the invocation rather than hardcoded so that raising Timeout in the
    emitted template actually raises it here; ``reserve_seconds`` is what is left
    for the final create/poll and for delivering the response.
    """
    remaining = 300.0
    try:
        remaining = context.get_remaining_time_in_millis() / 1000.0
    except Exception:  # no context: a local invocation or a test stub
        remaining = 300.0
    return time.monotonic() + max(remaining - reserve_seconds, 0.0)


# AgentCore reports these for both policy engines and policies. "READY" is not in
# the API model's enum but was accepted by the previous implementation, so it stays
# in the success set rather than turning a working deploy into a failure.
_STATUS_OK = ("ACTIVE", "READY")

# CreatePolicy/UpdatePolicy's own enum, in the order least to most strict. The default
# is the lenient one for the reason set out at length in _write_policy; the template
# offers the other through its PolicyValidationMode parameter.
_DEFAULT_VALIDATION_MODE = "IGNORE_ALL_FINDINGS"
_VALIDATION_MODES = (_DEFAULT_VALIDATION_MODE, "FAIL_ON_ANY_FINDINGS")


def _is_failed_status(status: str) -> bool:
    return status.endswith("_FAILED") or status in ("DELETING", "DELETED")


def _await_status(read, deadline: float, *, what: str, poll_seconds: float = 10.0) -> None:
    """Poll ``read`` until it reports a terminal status, or time runs out.

    ``read`` returns ``(status, reasons)`` and returns ``("", [])`` when the status
    could not be read at all. A read that fails is treated as "not yet", because
    the call this exists for races with resource propagation and answers
    ResourceNotFoundException for a few seconds; a read that succeeds and says
    ``*_FAILED`` is terminal and raises immediately rather than burning the budget.
    """
    last = ""
    while True:
        status, reasons = read()
        if status:
            last = status
            if status in _STATUS_OK:
                return
            if _is_failed_status(status):
                detail = "; ".join(reasons) if reasons else "the service reported no reason"
                raise ProviderError(f"{what} is {status}: {detail}")
        if deadline - time.monotonic() <= poll_seconds:
            raise ProviderError(
                f"{what} did not reach ACTIVE within the time available (last status: {last or 'could not be read'})"
            )
        time.sleep(poll_seconds)


def _engine_status(ctrl, engine_id: str):
    """Status of one policy engine, or ``("", [])`` if it cannot be read.

    ``get_policy_engine``, not ``list_policy_engines``. The list call was chosen
    here because "Lambda's bundled boto3 may not include the per-engine getter" —
    true of an older runtime, not of python3.13 — and it carries a defect the
    getter does not. ListPolicyEngines is account-wide, and it fails closed with
    AccessDeniedException on a KMS forward-access-session check when ANY engine in
    the account is encrypted with a customer-managed key this role cannot decrypt.
    In a shared account that is somebody else's key, and it makes the call
    permanently unusable from a role that is otherwise entirely correct. This wait
    loop then spent its whole budget logging attempts. GetPolicyEngine touches only
    the engine this stack owns.
    """
    try:
        resp = ctrl.get_policy_engine(policyEngineId=engine_id)
    except Exception as e:
        # Not truncated. The previous [:200] cut AWS's messages exactly where they
        # start explaining what to do about them.
        logger.info("get_policy_engine(%s) not readable yet: %s: %s", engine_id, type(e).__name__, e)
        return "", []
    return resp.get("status", ""), list(resp.get("statusReasons") or [])


def _policy_status(ctrl, engine_id: str, policy_id: str):
    """Status of one policy, or ``("", [])`` if it cannot be read."""
    try:
        resp = ctrl.get_policy(policyEngineId=engine_id, policyId=policy_id)
    except Exception as e:
        logger.info("get_policy(%s) not readable yet: %s: %s", policy_id, type(e).__name__, e)
        return "", []
    return resp.get("status", ""), list(resp.get("statusReasons") or [])


_MAX_POLICY_PAGES = 100


def _find_policy_by_name(ctrl, engine_id: str, name: str) -> dict | None:
    """Find one policy by name without truncating or looping on malformed pages.

    This provider is packaged as a flat, standalone Lambda zip, so it cannot import
    the application's shared pagination helper. Keep the same fail-closed guarantees
    locally: validate the collection and token, reject token cycles, and put an upper
    bound on a service response that never terminates.
    """
    token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(_MAX_POLICY_PAGES):
        kwargs = {"policyEngineId": engine_id, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = ctrl.list_policies(**kwargs)
        policies = resp.get("policies")
        if policies is None:
            policies = resp.get("policySummaries", [])
        if not isinstance(policies, list):
            raise ProviderError("list_policies returned a non-list policy collection")
        for policy in policies:
            if policy.get("name") == name:
                return policy

        candidate = resp.get("nextToken")
        if candidate in (None, ""):
            return None
        if not isinstance(candidate, str):
            raise ProviderError("list_policies returned a non-string nextToken")
        if candidate in seen_tokens:
            raise ProviderError(f"list_policies repeated pagination token {candidate!r}")
        seen_tokens.add(candidate)
        token = candidate

    raise ProviderError(f"list_policies exceeded {_MAX_POLICY_PAGES} pages while looking for {name!r}")


def _await_policy_gone(ctrl, engine_id: str, name: str, deadline: float, poll_seconds: float = 5.0) -> None:
    """Wait until no policy called *name* exists on *engine_id*.

    A policy mid-delete holds its name: ``create_policy`` answers
    ConflictException and ``update_policy`` has nothing usable to update. Deletion
    is quick in practice (seconds), so this is a short wait rather than a reason to
    fail the deploy.
    """
    while True:
        try:
            still_there = bool(_find_policy_id_by_name(ctrl, engine_id, name))
        except Exception as e:
            # Unreadable is not "gone". Treat it as "not yet" and let the deadline
            # decide, exactly as _await_status does.
            logger.info("list_policies while waiting for %s to go: %s: %s", name, type(e).__name__, e)
            still_there = True
        if not still_there:
            return
        if deadline - time.monotonic() <= poll_seconds:
            raise ProviderError(f"policy {name} was still being deleted when the time available ran out")
        time.sleep(poll_seconds)


def _write_policy(ctrl, call, deadline: float, validation_mode: str = _DEFAULT_VALIDATION_MODE, **kwargs) -> dict:
    """Call ``create_policy``/``update_policy``, retrying only the propagation race.

    *validation_mode* defaults to ``IGNORE_ALL_FINDINGS``, and NOT the stricter mode
    it looks like it should be. Asking AgentCore to fail on findings sounds like the
    safer choice and is the wrong one here, for a reason established live on the Step Functions
    path (step_handlers/policy_step.py): the validation step calls the *gateway* to
    resolve action schemas, and on a gateway created moments earlier — which is
    always the case here, since the policy DependsOn AgentCoreGateway — that call
    fails with "Insufficient permissions to call gateway" because the
    engine-to-gateway authorization has not converged yet. Under
    FAIL_ON_ANY_FINDINGS the policy then sits CREATE_FAILED for 8+ minutes; the same
    statement under IGNORE_ALL_FINDINGS reaches ACTIVE immediately.

    The default is a default, not a policy: the emitted template exposes it as the
    ``PolicyValidationMode`` parameter so an account that wants the findings analysis
    can ask for FAIL_ON_ANY_FINDINGS and accept the propagation race. Only the
    strictness-increasing direction is offered — see the parameter's own comment in
    cfn_template_generator._add_policy_parameters for why ``enforcementMode`` is not.

    This is not a weakening. It skips the *analysis findings* — the
    overly-permissive and overly-restrictive warnings — not enforcement. What makes
    the emitted policy safe is that it names each permitted tool explicitly
    (cfn_template_generator._cedar_permit), and AgentCore is default-deny, so every
    tool not named is denied by omission. The status poll below is what catches a
    genuinely broken statement, and it catches it either way.

    What IGNORE_ALL_FINDINGS does *not* skip, measured against the live service, so
    that nobody reads it as "validation off": a statement with an unconstrained
    resource is still rejected synchronously ("a wildcard resource was detected...
    constrain the resource to a specific AgentCore::Gateway"), and a statement naming
    a gateway that does not exist yet is still rejected with ResourceNotFoundException
    ("Gateway with ID ... does not exist"). Both are hard errors on the call itself,
    not findings — which is why the emitted policy names its gateway with Fn::GetAtt
    and DependsOn every target whose actions it mentions. Under FAIL_ON_ANY_FINDINGS
    the extra rejection is asynchronous: the call returns 200/CREATING and the policy
    then settles CREATE_FAILED with reasons like "Overly Permissive: Policy Engine
    will allow every request for the specified principal, action (Any Future Tools)
    and resource (gateway/*)". A handler that trusted the 200 would report success.

    ``enforcementMode=ACTIVE`` is stated rather than left to the service. It is the
    documented default and was confirmed to be the observed one — a policy created
    without the parameter comes back ``enforcementMode: ACTIVE`` — but the
    alternative value is ``LOG_ONLY``, which *records* a denial instead of enforcing
    it. A policy engine whose policies are all LOG_ONLY permits everything while
    looking exactly like one that does not, and this template's whole authorization
    story is that the gateway denies unnamed tools. That is not a default worth
    inheriting.

    Both parameters are passed opportunistically, because each is newer than some
    botocore builds and which botocore this Lambda gets is not guaranteed: the export
    bundles a recent boto3 next to this file when backend/lib is populated
    (cfn_template_generator._package_cfn_provider) and otherwise the Lambda falls
    back to the runtime's own. An older botocore rejects an unknown key locally with
    ParamValidationError before any request is sent. They are dropped one at a time,
    newest first, so an old botocore loses only what it cannot express: dropping
    ``validationMode`` means the service applies FAIL_ON_ANY_FINDINGS and the
    propagation race above becomes possible again, which is worth avoiding for as
    long as the build allows. The status poll covers every one of these paths.
    """
    # Ordered most to least capable. ParamValidationError advances one step.
    optional_params = [
        {"validationMode": validation_mode, "enforcementMode": "ACTIVE"},
        {"validationMode": validation_mode},
        {},
    ]
    attempt = 0
    while True:
        attempt += 1
        params = {**kwargs, **optional_params[0]}
        try:
            return call(**params)
        except ParamValidationError as e:
            if len(optional_params) == 1:
                raise
            dropped = sorted(set(optional_params[0]) - set(optional_params[1]))
            if not any(parameter in str(e) for parameter in dropped):
                # Not about the parameter this step would drop. Dropping it anyway
                # would turn a real mistake in the call into an unexplained failure
                # two attempts later: exactly how update_policy's `description`
                # — a {"optionalValue": str} structure, not the bare string
                # create_policy takes — stayed hidden.
                raise
            logger.info("this boto3 has no %s parameter; relying on the status poll instead", ", ".join(dropped))
            optional_params.pop(0)
            continue
        except Exception as e:
            propagating = _error_code(e) == "ResourceNotFoundException" or (
                "PolicyEngine" in str(e) and "not found" in str(e).lower()
            )
            if not propagating:
                raise
            if deadline - time.monotonic() <= 10.0:
                raise ProviderError(
                    f"policy engine {kwargs.get('policyEngineId', '')} was still propagating "
                    f"after {attempt} attempts and the time available ran out"
                ) from e
            logger.info("policy engine still propagating (attempt %d), retrying", attempt)
            time.sleep(10)


def _handle_policy_create_update(event: dict, context) -> tuple[dict, str]:
    props = event["ResourceProperties"]
    name = props["Name"]
    statement = props["Statement"]
    engine_id = props["PolicyEngineId"]
    description = props.get("Description", "")

    # Absent means an older emitted template, which had no such property and got the
    # lenient mode implicitly; keep that. A *present but unrecognised* value is a
    # typo in a hand-edited template, and the two failure modes are not symmetric —
    # silently substituting the lenient default would hand back a policy validated
    # less than asked, while failing here costs one readable stack event.
    validation_mode = props.get("ValidationMode") or _DEFAULT_VALIDATION_MODE
    if validation_mode not in _VALIDATION_MODES:
        raise ProviderError(f"ValidationMode {validation_mode!r} is not one of {', '.join(_VALIDATION_MODES)}")

    ctrl = boto3.client("bedrock-agentcore-control")
    deadline = _deadline(context)

    # 1. The engine has to be ACTIVE before a policy can bind to it. In a fresh
    #    account this genuinely takes minutes (Bug 72), which is the whole reason
    #    this custom resource exists instead of the native resource.
    _await_status(
        lambda: _engine_status(ctrl, engine_id),
        deadline,
        what=f"policy engine {engine_id}",
    )

    # 2. Idempotency. A leftover from an earlier attempt is adopted by *updating* it,
    #    whatever state it is in, including CREATE_FAILED. Both halves of that were
    #    established live against AgentCore:
    #      - create_policy over an existing name answers ConflictException ("Policy
    #        with the same name already exists") even when the existing policy is
    #        CREATE_FAILED. So treating a failed leftover as absent and re-creating
    #        it, which is what this used to do, fails every retry of a broken deploy
    #        rather than recovering it.
    #      - update_policy on a CREATE_FAILED policy is accepted and takes it to
    #        ACTIVE. It also keeps the policy id, so the custom resource's
    #        PhysicalResourceId does not change under an Update — delete-and-recreate
    #        would change it, and CloudFormation would then send a Delete for an id
    #        that no longer exists.
    #    Adopting a failed leftover is only safe because of the status poll in step 4:
    #    that is what stops a retry turning into a green stack with no working policy.
    policy_id = ""
    reusable = False
    try:
        existing = _find_policy_by_name(ctrl, engine_id, name)
        if existing:
            policy_id = existing.get("policyId") or existing.get("id") or ""
            status = existing.get("status", "")
            if status in ("DELETING", "DELETED"):
                # Going away on its own. Neither update nor create works while
                # it is mid-delete, so wait for the name to free up and create.
                logger.warning("policy %s is %s; waiting for it to go before creating", name, status)
                _await_policy_gone(ctrl, engine_id, name, deadline)
                policy_id = ""
            reusable = bool(policy_id)
            if reusable and _is_failed_status(status):
                logger.warning(
                    "policy %s exists in status %s; updating it in place to recover it",
                    name,
                    status or "unknown",
                )
    except ProviderError:
        # Raised by _await_policy_gone. Not an unreadable-list problem, so it must
        # not be swallowed into "will create" — that create would only Conflict.
        raise
    except Exception as e:
        logger.info("list_policies (idempotency check) failed, will create: %s: %s", type(e).__name__, e)

    # 3. Write it. On Update the statement is the thing that changed, so an
    #    existing policy is updated rather than left alone — reusing it untouched
    #    made a changed Cedar statement a silent no-op while the stack reported
    #    UPDATE_COMPLETE.
    #
    #    `description` is shaped differently on each call and is omitted entirely
    #    when empty, which is what a policy the canvas gave no description has:
    #      - create_policy takes a bare string with a minimum length of 1, so ""
    #        is rejected outright. Omitted, the policy simply has no description.
    #      - update_policy takes {"optionalValue": str} with PATCH semantics: the
    #        wrapper present replaces the description, the wrapper absent leaves it
    #        alone, and the wrapper present with no value CLEARS it. A bare string
    #        is a ParamValidationError, which is how the same mistake on the Step
    #        Functions path left a policy CREATE_FAILED indefinitely
    #        (services/policy_promoter.py).
    if reusable:
        logger.info("policy %s exists (id=%s), applying the current statement", name, policy_id)
        resp = _write_policy(
            ctrl,
            ctrl.update_policy,
            deadline,
            validation_mode,
            policyEngineId=engine_id,
            policyId=policy_id,
            definition={"cedar": {"statement": statement}},
            **({"description": {"optionalValue": description}} if description else {}),
        )
    else:
        resp = _write_policy(
            ctrl,
            ctrl.create_policy,
            deadline,
            validation_mode,
            policyEngineId=engine_id,
            name=name,
            definition={"cedar": {"statement": statement}},
            **({"description": description} if description else {}),
        )
        policy_id = resp.get("policyId") or resp.get("id") or resp.get("policy", {}).get("policyId", "")

    if not policy_id:
        raise ProviderError(f"AgentCore accepted the policy {name} but returned no policy id")

    # 4. The check this handler used to be missing entirely. CreatePolicy answers
    #    200/CREATING for a statement the service will reject; without this poll
    #    the stack goes CREATE_COMPLETE and the agent silently has no policy.
    status = resp.get("status", "")
    if status and status not in _STATUS_OK and not _is_failed_status(status):
        _await_status(
            lambda: _policy_status(ctrl, engine_id, policy_id),
            deadline,
            what=f"policy {name} ({policy_id})",
        )
    elif _is_failed_status(status):
        detail = "; ".join(resp.get("statusReasons") or []) or "the service reported no reason"
        raise ProviderError(f"policy {name} is {status}: {detail}")

    physical_id = f"{engine_id}/policies/{policy_id}"
    return {"PolicyId": policy_id, "PolicyEngineId": engine_id}, physical_id


def _find_policy_id_by_name(ctrl, engine_id: str, name: str) -> str:
    """The id of the policy called *name* on *engine_id*, or "".

    Needed because the physical id is not always parseable on the path that matters
    most. When a Create FAILS, the handler reports a physical id of the logical
    resource name — there is no other one to report — and CloudFormation then sends
    Delete with exactly that. So the rollback of a failed policy create arrived here
    with no "/policies/" in the id, skipped delete_policy entirely, and left the
    policy behind. The engine's own deletion then failed with a 409 because it still
    had a policy on it, and the stack settled in ROLLBACK_FAILED: a stack the
    recipient cannot delete without finding and removing an AgentCore policy by hand.

    Matching by name is safe here in a way it would not be in general: the engine id
    comes from this resource's own properties, and the emitted template always points
    it at the PolicyEngine the same stack creates, so the search is confined to this
    stack's engine.
    """
    policy = _find_policy_by_name(ctrl, engine_id, name)
    if not policy:
        return ""
    return policy.get("policyId") or policy.get("id") or ""


def _handle_policy_delete(event: dict) -> tuple[dict, str]:
    physical_id = event.get("PhysicalResourceId", "")
    props = event.get("ResourceProperties", {})
    engine_id = props.get("PolicyEngineId", "")
    name = props.get("Name", "")
    # Parse policy_id out of physical_id format "engine_id/policies/policy_id"
    policy_id = ""
    if "/policies/" in physical_id:
        policy_id = physical_id.rsplit("/", 1)[-1]

    ctrl = boto3.client("bedrock-agentcore-control") if engine_id else None
    if ctrl is not None and not policy_id and name:
        # The failed-create rollback path. Look the policy up by name rather than
        # giving up: a policy the create left behind is the thing that wedges the
        # stack, and this is the only chance to remove it.
        try:
            policy_id = _find_policy_id_by_name(ctrl, engine_id, name)
            if policy_id:
                logger.info("resolved policy %s to id %s by name for deletion", name, policy_id)
        except Exception as e:
            logger.warning("could not list policies on %s to find %s: %s", engine_id, name, type(e).__name__)

    if ctrl is not None and policy_id:
        try:
            ctrl.delete_policy(policyEngineId=engine_id, policyId=policy_id)
            logger.info("deleted policy %s (%s)", name or policy_id, policy_id)
        except Exception as e:
            code = _error_code(e)
            if code in _BENIGN_DELETE_CODES:
                logger.info("policy %s is already gone (%s)", policy_id, code)
            else:
                # Not swallowed, unlike the S3 artifact. A policy that survives
                # blocks DeletePolicyEngine with a 409, so the stack fails either
                # way; the only question is whether it fails here, naming the policy
                # and the reason, or three resources later on an engine that cannot
                # explain why it will not delete.
                logger.error("delete_policy(%s) failed: %s %s", policy_id, type(e).__name__, code)
                raise ProviderError(
                    f"policy {name or policy_id} ({policy_id}) on policy engine {engine_id} could not "
                    f"be deleted ({code or type(e).__name__}). It will block deletion of the policy "
                    "engine, so remove it with 'aws bedrock-agentcore-control delete-policy "
                    f"--policy-engine-id {engine_id} --policy-id {policy_id}' and retry the stack "
                    "deletion."
                ) from e

    return {}, physical_id or event.get("LogicalResourceId", "")


# ---------------------------------------------------------------------------
# Custom::RuntimeLogGroup
# ---------------------------------------------------------------------------


def _governed_log_group_names(props: dict) -> list[str]:
    """The log group names to govern, tolerating a single string and empty entries.

    CloudFormation renders custom-resource properties as strings, and every name here
    arrives from an ``Fn::Sub`` over the runtime id, so a name that came out empty
    means the substitution produced nothing rather than that there is a group called
    "". Dropping those beats calling CloudWatch Logs with an invalid name.
    """
    names = props.get("LogGroupNames") or []
    if isinstance(names, str):
        names = [names]
    seen = []
    for name in names:
        candidate = str(name).strip()
        # Deduplicated because the template derives the names from a runtime id and
        # an endpoint name, and a deployment whose endpoint is literally called
        # "DEFAULT" would otherwise be governed twice.
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen


def _retention_days(props: dict) -> int:
    """``RetentionInDays`` as an int; 0 (never expire) when absent or unreadable."""
    raw = str(props.get("RetentionInDays", "")).strip()
    try:
        return int(raw)
    except ValueError:
        logger.warning("RetentionInDays %r is not a number; treating as never-expire", raw)
        return 0


def _logs_kms_denied(logs, name: str, key_arn: str, code: str) -> ProviderError:
    """The actionable failure for a key whose policy does not allow CloudWatch Logs.

    Verified live: both CreateLogGroup and AssociateKmsKey answer
    ``AccessDeniedException: The specified KMS key does not exist or is not allowed to
    be used with Arn '<log group arn>'`` when the key policy has no statement for the
    logs service principal — a message that names neither the missing statement nor
    the fact that the key itself is fine.

    Built only from literals, the log group name and the key ARN, per ProviderError's
    contract. Neither is a secret: the ARN is a template parameter value that is
    already in the stack's parameter list, and no request body passed through here.
    """
    region = logs.meta.region_name
    return ProviderError(
        f"CloudWatch Logs refused customer-managed key {key_arn} for log group {name} ({code}). "
        f"The key policy must allow the logs.{region}.amazonaws.com service principal "
        "kms:Encrypt*, kms:Decrypt*, kms:ReEncrypt*, kms:GenerateDataKey* and kms:Describe*, "
        "conditioned on ArnLike kms:EncryptionContext:aws:logs:arn "
        f"arn:aws:logs:{region}:<account>:log-group:*. See README.md > Encryption for the "
        "exact statement, add it to the key, and retry the stack operation."
    )


def _apply_log_group_governance(
    logs,
    event: dict,
    name: str,
    retention: int,
    key_arn: str,
    resource_tags: dict[str, str],
    old_tag_keys: set[str],
) -> None:
    """Bring one log group to *retention* and *key_arn*, creating it if it is absent.

    Create-first, then fall back to adopting what is there. That order is deliberate:
    the runtime creates these groups itself at stack-create time, so the normal case
    is adoption — but a group can legitimately be missing (a runtime whose named
    endpoint has not been created yet, or a group an operator deleted), and creating
    it with the key and retention already set is both fewer calls and the only way to
    govern a group BEFORE the service writes its first event into it.
    """
    create_kwargs = {"logGroupName": name}
    if key_arn:
        create_kwargs["kmsKeyId"] = key_arn
    if resource_tags:
        # Tags on CreateLogGroup are atomic with creation. Do not retry without
        # them: an untagged group would make a failed governance control look green.
        create_kwargs["tags"] = resource_tags

    try:
        logs.create_log_group(**create_kwargs)
        logger.info("created log group %s (retention %s, key %s)", name, retention, key_arn or "aws-owned")
        if resource_tags:
            # CreateLogGroup accepts tags atomically. A read-back turns that documented
            # contract into an observed postcondition and catches a missing IAM grant or
            # service regression before retention/encryption changes make the operation
            # look otherwise successful.
            _reconcile_log_group_tags(logs, event, name, resource_tags)
    except ClientError as e:
        code = _error_code(e)
        if code == "AccessDeniedException" and key_arn:
            raise _logs_kms_denied(logs, name, key_arn, code) from e
        if code != "ResourceAlreadyExistsException":
            raise
        # The runtime normally creates the group first. Reconcile its tags before
        # changing retention or encryption so a denied tag write leaves those other
        # settings untouched and the stack failure points at the first unmet control.
        _reconcile_log_group_tags(logs, event, name, resource_tags, old_tag_keys)
        # The expected path: the runtime already made the group, so the key has to be
        # applied to it separately.
        #
        # Both calls are made unconditionally, without first reading the group's
        # current key, and that is a deliberate change from the obvious design. Reading
        # it means DescribeLogGroups, which cannot be granted here: it is a list
        # operation, so IAM authorizes it against
        # `arn:aws:logs:<region>:<account>:log-group::log-stream:` — an EMPTY log group
        # name — and no resource-scoped grant can ever match that. Verified live: a
        # grant on `log-group:/aws/bedrock-agentcore/runtimes/*` failed the stack with
        # "not authorized to perform: logs:DescribeLogGroups on resource:
        # arn:aws:logs:us-east-1:...:log-group::log-stream:". The alternatives were to
        # widen the grant to every log group in the account, or to stop reading. Both
        # calls are idempotent — also verified live: AssociateKmsKey with the key that
        # is already attached succeeds, and DisassociateKmsKey on a group with no key
        # succeeds — so not reading costs one no-op call per group per stack update and
        # keeps DisassociateKmsKey scoped to the runtime's own groups.
        if key_arn:
            try:
                logs.associate_kms_key(logGroupName=name, kmsKeyId=key_arn)
                logger.info("associated key %s with existing log group %s", key_arn, name)
            except ClientError as assoc_error:
                assoc_code = _error_code(assoc_error)
                if assoc_code == "AccessDeniedException":
                    raise _logs_kms_denied(logs, name, key_arn, assoc_code) from assoc_error
                raise
        else:
            # The stack dropped CustomerManagedKeyArn on an update, or never had one.
            # Reverting to the AWS-owned key keeps the group matching the template
            # rather than leaving it on a key the recipient may be about to delete —
            # which would make every existing event in it unreadable.
            logs.disassociate_kms_key(logGroupName=name)
            logger.info("log group %s left on the AWS-owned key", name)

    if retention > 0:
        logs.put_retention_policy(logGroupName=name, retentionInDays=retention)
    else:
        # 0 is CloudWatch's "never expire", which is the ABSENCE of a retention
        # policy rather than a value. Unreachable from the emitted template, whose
        # LogRetentionInDays has AllowedValues, but reachable from a hand-edited one.
        try:
            logs.delete_retention_policy(logGroupName=name)
        except ClientError as e:
            if _error_code(e) not in _BENIGN_DELETE_CODES:
                raise
    logger.info("log group %s governed: retention %s, key %s", name, retention or "never", key_arn or "aws-owned")


SWEEP_BUDGET_SECONDS = int(os.environ.get("RUNTIME_LOG_GROUP_SWEEP_BUDGET_SECONDS", "240"))
SWEEP_QUIESCENCE_SECONDS = int(os.environ.get("RUNTIME_LOG_GROUP_SWEEP_QUIESCENCE_SECONDS", "20"))


def _is_sweeper(props: dict) -> bool:
    return str(props.get("Mode", "")).strip().lower() == "sweeper"


def _stack_name_and_digest(event: dict) -> tuple[str, str]:
    """(stack name, 16-hex digest of the full StackId ARN). The digest keeps a same-name
    redeploy apart from a retained predecessor's ledger entries."""
    stack_id = str(event.get("StackId") or "")
    name = stack_id.split("/")[1] if stack_id.count("/") >= 2 else stack_id
    return name, hashlib.sha256(stack_id.encode("utf-8")).hexdigest()[:16]


def _ledger_prefix(event: dict, props: dict) -> str:
    name, digest = _stack_name_and_digest(event)
    return f"/agentcore-cfn/{name}/{digest}/{props.get('RuntimeLogicalId', '')}/{props.get('Generation', '')}/groups"


def _ledger_add(ssm, prefix: str, names: list[str]) -> None:
    """One parameter per group, so parallel governance resources never race on a shared
    document. Fail closed: a ledger the sweeper cannot read later is how residue comes back."""
    for name in names:
        try:
            ssm.put_parameter(
                Name=f"{prefix}/{hashlib.sha256(name.encode('utf-8')).hexdigest()[:16]}",
                Type="String",
                Overwrite=True,
                Value=name,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            raise ProviderError(
                f"could not record runtime log group {name} in the ledger under {prefix} ({code}); "
                "the sweeper would miss it"
            ) from exc


def _ledger_entries(ssm, prefix: str) -> list[tuple[str, str]]:
    """[(parameter name, log group name)] under *prefix*, all pages."""
    out: list[tuple[str, str]] = []
    token = None
    while True:
        kwargs = {"Path": prefix, "Recursive": True}
        if token:
            kwargs["NextToken"] = token
        page = ssm.get_parameters_by_path(**kwargs)
        out.extend((p["Name"], p["Value"]) for p in page.get("Parameters", []))
        token = page.get("NextToken")
        if not token:
            return out


def _handle_runtime_log_group_create_update(event: dict) -> tuple[dict, str]:
    props = event.get("ResourceProperties", {})
    if _is_sweeper(props):
        # Nothing exists yet when the sweeper is created (the runtime depends on it);
        # its only work is on Delete, after the runtime is gone.
        return {}, _runtime_log_group_physical_id(event)
    resource_tags = _resource_tags(props)
    old_tag_keys = _old_resource_tag_keys(event)
    names = _governed_log_group_names(props)
    retention = _retention_days(props)
    key_arn = str(props.get("KmsKeyArn", "") or "").strip()

    logs = boto3.client("logs")
    for name in names:
        _apply_log_group_governance(
            logs,
            event,
            name,
            retention,
            key_arn,
            resource_tags,
            old_tag_keys,
        )

    if not names:
        logger.warning("no log group names to govern for %s", event.get("LogicalResourceId", ""))
    if names and props.get("Generation"):
        _ledger_add(boto3.client("ssm"), _ledger_prefix(event, props), names)

    return {"LogGroupNames": ",".join(names)}, _runtime_log_group_physical_id(event)


def _runtime_log_group_physical_id(event: dict) -> str:
    """The physical id encodes WHICH group this resource governs, so a changed group is a
    replacement and an unchanged one never is.

    One resource governs one log group (the generator emits one per endpoint plus
    ``-DEFAULT``). The id is derived from the governed name, so:

    * Create: ``runtime-log-group/<sha256(name)[:16]>``. Two resources for two groups
      get two ids; the same group always gets the same id.
    * Update with the same name: the same id, so CloudFormation sees no replacement.
    * Update where the resolved name changed (the runtime was replaced, or an endpoint
      renamed): a NEW id. CloudFormation then treats it as a replacement and, after the
      update has stabilized, sends Delete for the OLD resource -- honoured or skipped by
      its ``UpdateReplacePolicy`` -- while a failed update's rollback deletes the new
      resource and keeps the old group. That is the only way ``UpdateReplacePolicy``
      can mean anything for groups the service names after the runtime id.
    * Legacy resources (``runtime-log-groups/<LogicalId>``, from templates that governed
      every group with one resource) keep their id on Update, whatever the names now
      are: replacing them would make CloudFormation Delete the old resource, and that
      Delete now really deletes -- the groups of a runtime that is still running.
    """
    existing = str(event.get("PhysicalResourceId") or "")
    props = event.get("ResourceProperties", {})
    if _is_sweeper(props):
        # Generation-specific: a changed AgentRuntimeName replaces the sweeper, so the old
        # one is deleted after the old runtime and sweeps the old runtime's groups.
        return f"runtime-log-group-sweeper/{props.get('RuntimeLogicalId') or event.get('LogicalResourceId', '')}/{props.get('Generation', '')}"
    if existing.startswith("runtime-log-groups/"):
        return existing
    names = _governed_log_group_names(event.get("ResourceProperties", {}))
    if len(names) == 1:
        return "runtime-log-group/" + hashlib.sha256(names[0].encode("utf-8")).hexdigest()[:16]
    # No single governed name (nothing recorded, or a legacy multi-group shape): fall
    # back to the stable per-logical-id form so nothing is ever replaced by accident.
    return existing or f"runtime-log-groups/{event.get('LogicalResourceId', '')}"


def _handle_runtime_log_group_delete(event: dict) -> tuple[dict, str]:
    """Delete the runtime's governed log groups -- but ONLY when CloudFormation asks.

    The generator stamps this resource with ``DeletionPolicy: RetainExceptOnCreate``
    (Retain mode) or ``Delete`` (Delete mode), so CloudFormation sends Delete in exactly
    two cases: the rollback of the stack operation that CREATED the runtime -- the groups
    this resource pre-created for a runtime that is itself being rolled back -- and a
    deliberate Delete-mode teardown. A retained stack's deletion SKIPS this resource
    (DELETE_SKIPPED), so the record of what a real agent was asked and what it answered
    is kept for as long as its retention says (ARCC cnt_bO6I1SM60fP0J4: security-relevant
    logs are retained for years, and an investigation that starts after a teardown is
    exactly when they are needed). Before this, Delete was an unconditional no-op, so a
    failed first create left every pre-created group behind, and a replaced runtime under
    Delete orphaned the old runtime's groups.

    Deletion is verified, not assumed: the role deliberately has no DescribeLogGroups, so
    a second DeleteLogGroup must answer ResourceNotFoundException. Anything else -- a
    group that survives, an AccessDenied -- fails the resource with the remedy in the
    message rather than reporting a success that did not happen.
    """
    props = event.get("ResourceProperties", {})
    if _is_sweeper(props):
        return _sweep_runtime_log_groups(event, props)
    names = _governed_log_group_names(props)
    if not names:
        # A rollback of a failed Create sends Delete with whatever properties it had;
        # nothing recorded means nothing to remove, and no client is built at all.
        logger.info(
            "Delete for %s: no log group names recorded; nothing to remove.", event.get("LogicalResourceId", "")
        )
        return {}, _runtime_log_group_physical_id(event)
    logs = boto3.client("logs")
    removed: list[str] = []
    absent: list[str] = []
    for name in names:
        remedy = f"aws logs delete-log-group --log-group-name {name}"
        try:
            logs.delete_log_group(logGroupName=name)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                absent.append(name)
                continue
            raise ProviderError(
                f"could not delete runtime log group {name} ({code}); it will survive this "
                f"rollback/teardown as residue. Remedy once the cause is fixed: {remedy}"
            ) from exc
        try:
            logs.delete_log_group(logGroupName=name)
        except ClientError as exc:
            verify_code = exc.response.get("Error", {}).get("Code", "")
            if verify_code == "ResourceNotFoundException":
                removed.append(name)
                continue
            raise ProviderError(
                f"could not verify deletion of runtime log group {name} ({verify_code}). Remedy: {remedy}"
            ) from exc
        raise ProviderError(
            f"runtime log group {name} still exists after DeleteLogGroup; refusing to report a "
            f"deletion that did not happen. Remedy: {remedy}"
        )
    logger.info(
        "Delete for %s: removed runtime log group(s) %s; already absent: %s. This runs only on the "
        "rollback of the creating operation or a Delete-mode teardown -- a retained stack's "
        "deletion skips this resource.",
        event.get("LogicalResourceId", ""),
        ", ".join(removed) or "(none)",
        ", ".join(absent) or "(none)",
    )
    return {
        "LogGroupNames": ",".join(names),
        "DeletedLogGroups": ",".join(removed),
        "AlreadyAbsent": ",".join(absent),
    }, _runtime_log_group_physical_id(event)


def _sweep_runtime_log_groups(event: dict, props: dict) -> tuple[dict, str]:
    """Runs LAST in a stack delete (the runtime depended on this resource), i.e. after
    AgentCore has stopped. Sources of truth, in order:

    1. This generation's ledger entries (one SSM parameter per group, written by the
       per-group resources as they governed).
    2. If there are none: CloudFormation's own record of the runtime resource. A runtime
       can create its groups and the stack can fail before any governance resource ran,
       so an empty ledger is NOT proof. The recovered id must carry THIS generation's
       AgentRuntimeName (ids are "<name>-<suffix>"); otherwise -- a replacement whose old
       generation recorded nothing, say -- we refuse rather than sweep another runtime's
       groups. If CloudFormation has no physical id for the runtime, none was created and
       no group can exist.

    Then delete each group, wait, and re-check until nothing comes back within the
    budget; finally remove the ledger entries. CloudFormation only sends this Delete
    under Delete or on a failed first create.
    """
    physical = _runtime_log_group_physical_id(event)
    prefix = _ledger_prefix(event, props)
    ssm = boto3.client("ssm")
    try:
        entries = _ledger_entries(ssm, prefix)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        raise ProviderError(
            f"could not read the runtime log-group ledger under {prefix} ({code}); refusing to report a clean sweep"
        ) from exc
    names = sorted({value for _n, value in entries})
    source = "ledger"
    logs = boto3.client("logs")
    if not names:
        source = "cloudformation"
        runtime_id = _runtime_id_from_stack(event, props)
        if runtime_id is None:
            logger.info(
                "sweeper %s: no ledger entries and CloudFormation records no runtime; nothing can exist",
                event.get("LogicalResourceId", ""),
            )
            return {"SweptLogGroups": "", "Source": "none"}, physical
        names = _list_runtime_log_groups(logs, runtime_id)
    swept: list[str] = []
    deadline = time.monotonic() + SWEEP_BUDGET_SECONDS
    for name in names:
        remedy = f"aws logs delete-log-group --log-group-name {name}"
        while True:
            try:
                logs.delete_log_group(logGroupName=name)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code != "ResourceNotFoundException":
                    raise ProviderError(
                        f"could not delete runtime log group {name} ({code}). Remedy: {remedy}"
                    ) from exc
            # quiescence: wait, then confirm it stayed gone. A successful delete on the
            # re-check means the service recreated it -- loop while the budget allows.
            time.sleep(SWEEP_QUIESCENCE_SECONDS)
            try:
                logs.delete_log_group(logGroupName=name)
                recreated = True
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code != "ResourceNotFoundException":
                    raise ProviderError(
                        f"could not verify runtime log group {name} stayed deleted ({code}). Remedy: {remedy}"
                    ) from exc
                recreated = False
            if not recreated:
                swept.append(name)
                break
            if time.monotonic() > deadline:
                raise ProviderError(
                    f"runtime log group {name} keeps being recreated after deletion ({SWEEP_BUDGET_SECONDS}s budget); "
                    f"refusing to report a clean sweep. Remedy once the runtime is fully gone: {remedy}"
                )
    param_names = [n for n, _v in entries]
    for i in range(0, len(param_names), 10):
        try:
            ssm.delete_parameters(Names=param_names[i : i + 10])
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            raise ProviderError(
                f"swept the log groups but could not delete ledger entries under {prefix} ({code}); remove them with aws ssm delete-parameters"
            ) from exc
    logger.info(
        "sweeper %s: swept %s (source: %s) and removed %d ledger entries",
        event.get("LogicalResourceId", ""),
        ", ".join(swept) or "(none)",
        source,
        len(param_names),
    )
    return {"SweptLogGroups": ",".join(swept), "Source": source}, physical


def _runtime_id_from_stack(event: dict, props: dict) -> str | None:
    """The runtime's physical id as CloudFormation recorded it, or None if it never had
    one. Refuses (ProviderError) unless the id carries this generation's AgentRuntimeName."""
    cfn = boto3.client("cloudformation")
    logical = str(props.get("RuntimeLogicalId") or "")
    try:
        detail = cfn.describe_stack_resource(StackName=event.get("StackId", ""), LogicalResourceId=logical)[
            "StackResourceDetail"
        ]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        message = str(exc.response.get("Error", {}).get("Message", ""))
        if code == "ValidationError" and "does not exist" in message:
            return None
        raise ProviderError(
            f"could not read the runtime resource {logical} from this stack ({code}); refusing to report a clean sweep"
        ) from exc
    runtime_id = str(detail.get("PhysicalResourceId") or "")
    if not runtime_id or runtime_id == logical:
        return None
    expected_name = str(props.get("AgentRuntimeName") or "")
    if not expected_name or not runtime_id.startswith(f"{expected_name}-"):
        raise ProviderError(
            f"the stack's runtime id {runtime_id} does not belong to this sweeper's generation ({expected_name}); "
            "refusing to sweep another runtime's log groups"
        )
    return runtime_id


def _list_runtime_log_groups(logs, runtime_id: str) -> list[str]:
    prefix = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-"
    names: list[str] = []
    token = None
    while True:
        kwargs = {"logGroupNamePrefix": prefix}
        if token:
            kwargs["nextToken"] = token
        try:
            page = logs.describe_log_groups(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            raise ProviderError(
                f"could not list runtime log groups under {prefix} ({code}); refusing to report a clean sweep"
            ) from exc
        names.extend(g["logGroupName"] for g in page.get("logGroups", []))
        token = page.get("nextToken")
        if not token:
            return sorted(names)


# ---------------------------------------------------------------------------
# Router — dispatches by resource type
# ---------------------------------------------------------------------------


def _get_resource_type(event: dict) -> str:
    """Determine the custom resource type from the event."""
    return event.get("ResourceType", event.get("ResourceProperties", {}).get("ServiceToken", ""))


# The complete set of resource types this Lambda serves. Must match the
# "Type": "Custom::..." values emitted by cfn_template_generator.py; there is no
# fifth one. Kept as an explicit set so dispatch can fail closed — see handler().
SUPPORTED_RESOURCE_TYPES = frozenset(
    {
        "Custom::AgentCodePackage",
        "Custom::OAuth2CredentialProvider",
        "Custom::AgentCorePolicy",
        "Custom::RuntimeLogGroup",
    }
)


def _safe_failure_reason(e: Exception) -> str:
    """A failure reason safe to put in CloudFormation stack events.

    Stack events are visible to anyone who can DescribeStackEvents and, under a
    Terraform ``aws_cloudformation_stack`` wrapper, land in Terraform state. A
    raw ``str(e)`` is not safe to put there: botocore builds ClientError messages
    from the API response and some of these calls carry a Cognito app client
    secret in their request parameters, so an echoed message could put a live
    credential into the stack's permanent event history.

    Emit the exception class only. The full message is already in CloudWatch via
    logger.exception, which is access-controlled, and cfn_response.send appends
    the log stream name so the operator can find it.

    The one exception is ProviderError, whose message this module wrote itself
    from literals and service status fields — see its docstring. Without it the
    single most actionable failure in this Lambda, "AgentCore rejected your Cedar
    statement, and here is what it said", would reach the operator as
    "cfn-provider failed with ProviderError".
    """
    if isinstance(e, ProviderError):
        return f"cfn-provider: {e}"[:1000]
    return f"cfn-provider failed with {type(e).__name__}"


class ResponseDeliveryError(Exception):
    """Raised when CloudFormation could not be told the outcome at all.

    Deliberately not a ProviderError: nothing about the resource went wrong, so
    this must never be reported to an operator as a resource failure.
    """


def _raise_if_undelivered(event: dict, delivered: bool, status: str, request_type: str, logical_id: str) -> None:
    """Turn an undelivered response into a Lambda invocation error.

    CloudFormation invokes a custom resource asynchronously, and an async
    invocation that ends in an exception is retried by Lambda (twice, by default).
    A response that was never delivered is the one failure where that retry is
    worth more than a clean exit: the alternative is the stack blocking on this
    resource for the full one-hour custom-resource timeout, unable to be updated
    or deleted in the meantime, and then rolling back. Returning normally throws
    that retry away, so the whole invocation is failed instead and the handler
    runs again — which is safe precisely because every path here is idempotent
    (the policy write adopts a leftover, a delete treats "already gone" as
    success, and a code package re-uploads the same key).

    A ResponseURL that is missing or not a CloudFormation-issued S3 URL is the
    one case not worth retrying: it cannot start working on the second attempt,
    so the work would simply be repeated twice for nothing.
    """
    if delivered:
        return

    if not cfn_response.is_usable_response_url(
        event.get("ResponseURL", ""),
        event.get("StackId", ""),
    ):
        logger.error(
            "%s %s finished (%s) but the event carries no usable CloudFormation "
            "S3 ResponseURL, "
            "so CloudFormation cannot be signalled at all and a retry would not help.",
            request_type,
            logical_id,
            status,
        )
        return

    raise ResponseDeliveryError(
        f"{request_type} {logical_id} finished ({status}) but the response could not be "
        "delivered to CloudFormation; failing the invocation so Lambda retries it"
    )


def handler(event: dict, context) -> None:
    """CloudFormation Custom Resource entry point."""
    request_type = event.get("RequestType", "")
    logical_id = event.get("LogicalResourceId", "")
    resource_type = _get_resource_type(event)
    logger.info("CFN %s for %s (type: %s)", request_type, logical_id, resource_type)

    try:
        # Fail closed on anything unrecognized. This used to fall through to the
        # code-packaging handler as a default, which meant a typo in a template's
        # "Type" — or a fourth custom resource added without wiring it up here —
        # ran the WRONG handler and reported SUCCESS. A stack that silently built
        # the wrong resource is much harder to diagnose than one that refuses.
        if resource_type not in SUPPORTED_RESOURCE_TYPES and request_type == "Delete":
            # Except on Delete, where failing closed is what wedges a stack.
            #
            # Found live. Adding Custom::RuntimeLogGroup to a stack whose provider
            # Lambda predated it failed the create, and the rollback then reverted the
            # Lambda's code BEFORE sending the Delete — so the Delete arrived at a
            # handler that had never heard of the type. Three DELETE_FAILED retries
            # later the stack finished UPDATE_ROLLBACK_COMPLETE with "One or more
            # resources could not be deleted". The same thing happens to a recipient
            # rolling back any update that introduced a new custom resource type.
            #
            # Succeeding here is safe in a way that succeeding on Create/Update is not:
            # a Delete this code cannot interpret names nothing it could destroy, so
            # the only thing it can get wrong is leaving a resource behind — the
            # direction this handler already errs in deliberately.
            logger.warning(
                "Delete for %s of unsupported type %r: reporting success so the stack is not "
                "wedged. Nothing was deleted; if this type owned anything, remove it by hand.",
                logical_id,
                resource_type,
            )
            data, physical_id = {}, event.get("PhysicalResourceId") or logical_id
        elif resource_type not in SUPPORTED_RESOURCE_TYPES:
            raise ValueError(
                f"Unsupported custom resource type {resource_type!r}. "
                f"This Lambda serves only: {', '.join(sorted(SUPPORTED_RESOURCE_TYPES))}."
            )
        elif resource_type == "Custom::OAuth2CredentialProvider":
            if request_type == "Create":
                data, physical_id = _handle_oauth2_cred_create(event)
            elif request_type == "Update":
                data, physical_id = _handle_oauth2_cred_update(event)
            elif request_type == "Delete":
                data, physical_id = _handle_oauth2_cred_delete(event)
            else:
                raise ValueError(f"Unknown RequestType: {request_type}")
        elif resource_type == "Custom::AgentCorePolicy":
            if request_type in ("Create", "Update"):
                data, physical_id = _handle_policy_create_update(event, context)
            elif request_type == "Delete":
                data, physical_id = _handle_policy_delete(event)
            else:
                raise ValueError(f"Unknown RequestType: {request_type}")
        elif resource_type == "Custom::RuntimeLogGroup":
            if request_type in ("Create", "Update"):
                data, physical_id = _handle_runtime_log_group_create_update(event)
            elif request_type == "Delete":
                data, physical_id = _handle_runtime_log_group_delete(event)
            else:
                raise ValueError(f"Unknown RequestType: {request_type}")
        else:
            # Custom::AgentCodePackage — the only remaining supported type.
            if request_type in ("Create", "Update"):
                data, physical_id = _handle_code_package_create_update(event)
            elif request_type == "Delete":
                data, physical_id = _handle_code_package_delete(event)
            else:
                raise ValueError(f"Unknown RequestType: {request_type}")

    except Exception as e:
        logger.exception("Custom resource handler failed")
        delivered = cfn_response.send(
            event,
            context,
            cfn_response.FAILED,
            reason=_safe_failure_reason(e),
            physical_resource_id=event.get("PhysicalResourceId", logical_id),
        )
        _raise_if_undelivered(event, delivered, cfn_response.FAILED, request_type, logical_id)
        return

    # Deliberately outside the try. The work has already succeeded at this point,
    # so a failure to DELIVER the success response must not be caught by the
    # handler above and turned into a FAILED response — that would roll back a
    # resource that was created correctly. cfn_response.send retries internally
    # and never raises, so its False return is the only signal available here.
    delivered = cfn_response.send(
        event,
        context,
        cfn_response.SUCCESS,
        data=data,
        physical_resource_id=physical_id,
    )
    _raise_if_undelivered(event, delivered, cfn_response.SUCCESS, request_type, logical_id)
