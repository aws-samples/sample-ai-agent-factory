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
import time
import zipfile
from urllib.parse import quote

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


def _handle_code_package_create_update(event: dict) -> tuple[dict, str]:
    """Handle CREATE/UPDATE for AgentCodePackage."""
    props = event["ResourceProperties"]
    bucket = props["ArtifactsBucket"]
    agent_code_key = props["AgentCodeKey"]
    bundle_key = props["DependencyBundleKey"]
    output_key = props["OutputKey"]

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
    s3.put_object(Bucket=bucket, Key=output_key, Body=merged, **owner)

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


def _adopt_existing_provider(ctrl, name: str, discovery_url: str, client_id: str, client_secret: str) -> str:
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
    logger.info(
        "Credential provider %s already exists for this stack's OAuth client; updating it in place",
        name,
    )  # nosemgrep: python-logger-credential-disclosure -- logs resource name, not secret
    resp = ctrl.update_oauth2_credential_provider(
        name=name,
        credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput=_provider_config(discovery_url, client_id, client_secret),
    )
    return resp.get("credentialProviderArn", "") or arn


def _handle_oauth2_cred_create(event: dict) -> tuple[dict, str]:
    """Create an OAuth2 credential provider via bedrock-agentcore-control API."""
    props = event["ResourceProperties"]
    name = props["ProviderName"]
    discovery_url = props["DiscoveryUrl"]
    client_id = props["ClientId"]
    client_secret = _resolve_client_secret(props)

    ctrl = _get_agentcore_ctrl()

    logger.info(
        "Creating OAuth2 credential provider: %s", name
    )  # nosemgrep: python-logger-credential-disclosure -- logs resource name, not secret
    try:
        resp = ctrl.create_oauth2_credential_provider(
            name=name,
            credentialProviderVendor="CustomOauth2",
            oauth2ProviderConfigInput=_provider_config(discovery_url, client_id, client_secret),
        )
        cred_arn = resp.get("credentialProviderArn", "")
    except ctrl.exceptions.ValidationException as e:
        if "already exists" not in str(e):
            raise
        cred_arn = _adopt_existing_provider(ctrl, name, discovery_url, client_id, client_secret)
    logger.info(
        "Created OAuth2 credential provider: %s", cred_arn
    )  # nosemgrep: python-logger-credential-disclosure -- logs resource ARN, not secret

    # Wait a few seconds for IAM propagation
    time.sleep(5)

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
    """
    props = event["ResourceProperties"]
    name = props["ProviderName"]
    discovery_url = props["DiscoveryUrl"]
    client_id = props["ClientId"]
    client_secret = _resolve_client_secret(props)

    ctrl = _get_agentcore_ctrl()
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
        logger.info(
            "Credential provider %s no longer exists; creating it", name
        )  # nosemgrep: python-logger-credential-disclosure -- logs resource name, not secret
        return _handle_oauth2_cred_create(event)

    cred_arn = resp.get("credentialProviderArn", "") or event.get("PhysicalResourceId", "")
    logger.info(
        "Updated OAuth2 credential provider: %s", cred_arn
    )  # nosemgrep: python-logger-credential-disclosure -- logs resource ARN, not secret

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
        logger.info(
            "Deleted OAuth2 credential provider: %s", cred_arn
        )  # nosemgrep: python-logger-credential-disclosure -- logs resource ARN, not secret
    except Exception as e:
        code = _error_code(e)
        if code in _BENIGN_DELETE_CODES:
            logger.info("Credential provider %s is already gone (%s)", cred_name, code)
            return
        # type(e).__name__ and the AWS error code only. str(e) is a botocore message
        # built from the request, and the create/update requests for this resource
        # carry a Cognito app client secret — see _safe_failure_reason.
        logger.error("Failed to delete credential provider %s: %s %s", cred_name, type(e).__name__, code)
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
        pass
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
        next_token = None
        for _ in range(10):
            kw = {"policyEngineId": engine_id}
            if next_token:
                kw["nextToken"] = next_token
            resp = ctrl.list_policies(**kw)
            for p in resp.get("policies", resp.get("policySummaries", [])):
                if p.get("name") != name:
                    continue
                policy_id = p.get("policyId") or p.get("id") or ""
                status = p.get("status", "")
                if status in ("DELETING", "DELETED"):
                    # Going away on its own. Neither update nor create works while
                    # it is mid-delete, so wait for the name to free up and create.
                    logger.warning("policy %s is %s; waiting for it to go before creating", name, status)
                    _await_policy_gone(ctrl, engine_id, name, deadline)
                    policy_id = ""
                    break
                reusable = bool(policy_id)
                if reusable and _is_failed_status(status):
                    logger.warning(
                        "policy %s exists in status %s; updating it in place to recover it",
                        name,
                        status or "unknown",
                    )
                break
            if policy_id or not resp.get("nextToken"):
                break
            next_token = resp.get("nextToken")
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
    next_token = None
    for _ in range(10):
        kw = {"policyEngineId": engine_id}
        if next_token:
            kw["nextToken"] = next_token
        resp = ctrl.list_policies(**kw)
        for p in resp.get("policies", resp.get("policySummaries", [])):
            if p.get("name") == name:
                return p.get("policyId") or p.get("id") or ""
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return ""


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


def _apply_log_group_governance(logs, name: str, retention: int, key_arn: str) -> None:
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

    try:
        logs.create_log_group(**create_kwargs)
        logger.info("created log group %s (retention %s, key %s)", name, retention, key_arn or "aws-owned")
    except ClientError as e:
        code = _error_code(e)
        if code == "AccessDeniedException" and key_arn:
            raise _logs_kms_denied(logs, name, key_arn, code) from e
        if code != "ResourceAlreadyExistsException":
            raise
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


def _handle_runtime_log_group_create_update(event: dict) -> tuple[dict, str]:
    props = event.get("ResourceProperties", {})
    names = _governed_log_group_names(props)
    retention = _retention_days(props)
    key_arn = str(props.get("KmsKeyArn", "") or "").strip()

    logs = boto3.client("logs")
    for name in names:
        _apply_log_group_governance(logs, name, retention, key_arn)

    if not names:
        logger.warning("no log group names to govern for %s", event.get("LogicalResourceId", ""))

    return {"LogGroupNames": ",".join(names)}, _runtime_log_group_physical_id(event)


def _runtime_log_group_physical_id(event: dict) -> str:
    """A physical id that never changes, so an update is never a replacement.

    If this returned a different id on an update, CloudFormation would follow up with
    a Delete for the old one. The Delete below is a no-op, so nothing would break
    today — but the id is what the next reader would reasonably use to name the
    groups, and an id that changes is how a future Delete-that-does-something ends up
    deleting the log groups of the resource that just replaced it.
    """
    return event.get("PhysicalResourceId") or f"runtime-log-groups/{event.get('LogicalResourceId', '')}"


def _handle_runtime_log_group_delete(event: dict) -> tuple[dict, str]:
    """Deliberately a no-op: a stack deletion must not delete the agent's logs.

    The groups belong to the runtime, not to this resource — it only sets two
    properties on them — and they hold the record of what the agent was asked and
    what it answered. Per ARCC cnt_bO6I1SM60fP0J4 security-relevant logs are retained
    for years, and an incident investigation that starts after a teardown is exactly
    when they are needed. Whatever retention was last applied still expires them on
    schedule; the recipient can delete them explicitly if they want them gone sooner.
    """
    names = _governed_log_group_names(event.get("ResourceProperties", {}))
    logger.info(
        "Delete for %s: leaving log group(s) %s in place, including their retention and "
        "encryption settings. This resource governs the runtime's own log groups and never "
        "deletes them; delete them explicitly if they are no longer wanted.",
        event.get("LogicalResourceId", ""),
        ", ".join(names) or "(none recorded)",
    )
    return {}, _runtime_log_group_physical_id(event)


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

    A ResponseURL that is missing or not https is the one case not worth
    retrying: it cannot start working on the second attempt, so the work would
    simply be repeated twice for nothing.
    """
    if delivered:
        return

    if not event.get("ResponseURL", "").startswith("https://"):
        logger.error(
            "%s %s finished (%s) but the event carries no usable https ResponseURL, "
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
