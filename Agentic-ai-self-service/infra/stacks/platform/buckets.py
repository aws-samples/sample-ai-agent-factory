"""S3 buckets (logging + artifacts) and the AgentCore deps upload.

Audit #12: section banner — the artifacts bucket and the agentcore-deps
upload create S3 resources, distinct from the "Lambda Functions" group.
The frontend bucket lives in cloudfront_waf.py next to the distribution
that serves it.
"""

import os

import aws_cdk as cdk
from aws_cdk import Duration, Size
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3_deployment

from .config import PlatformConfig

#: Lifecycle rule id -> the key prefix it expires. The prefixes match
#: ``EXPORT_BUNDLE_PREFIXES`` in backend deployment_handler.py.
EXPORT_BUNDLE_LIFECYCLE_RULES = {
    "ExpireCfnTemplateExports": "cfn-templates/",
    "ExpirePythonExports": "python-exports/",
}


def build_logging_bucket(stack: cdk.Stack, cfg: PlatformConfig) -> s3.Bucket:
    """Create S3 bucket for access logs (S3 + CloudFront)."""
    return s3.Bucket(
        stack,
        "LoggingBucket",
        bucket_name=f"{cfg.project}-{cfg.env}-logs-{stack.region}-{stack.account}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        removal_policy=cfg.removal_policy,
        auto_delete_objects=cfg.allow_destroy,
        encryption=s3.BucketEncryption.S3_MANAGED,
        object_ownership=s3.ObjectOwnership.OBJECT_WRITER,
        lifecycle_rules=[
            s3.LifecycleRule(expiration=Duration.days(90)),
        ],
    )


def build_artifacts_bucket(stack: cdk.Stack, cfg: PlatformConfig, logging_bucket: s3.Bucket) -> s3.Bucket:
    """Create S3 bucket for deployment code artifacts."""
    bucket = s3.Bucket(
        stack,
        "ArtifactsBucket",
        bucket_name=f"{cfg.project}-{cfg.env}-artifacts-{stack.region}-{stack.account}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        removal_policy=cfg.removal_policy,
        auto_delete_objects=cfg.allow_destroy,
        encryption=s3.BucketEncryption.S3_MANAGED,
        server_access_logs_bucket=logging_bucket,
        server_access_logs_prefix="s3-artifacts/",
        lifecycle_rules=[
            s3.LifecycleRule(expiration=Duration.days(90), prefix="deployments/"),
            # The two export prefixes hold download bundles behind a one-hour
            # presigned URL: temporary platform artifacts, not workload code. A day is
            # the shortest S3 expiration, and it bounds how long a bundle -- which
            # embeds the caller's canvas -- outlives the link that was its only use.
            # Rule ids are stable because the live proof reads them back from HeadObject's
            # x-amz-expiration header. ARCC cnt_sfiNQzRGEcegFL (purge timeboxed data).
            *(
                s3.LifecycleRule(id=rule_id, prefix=prefix, expiration=Duration.days(1))
                for rule_id, prefix in EXPORT_BUNDLE_LIFECYCLE_RULES.items()
            ),
        ],
    )
    # No target-account principal is granted anything here. Cross-account deploys
    # write code.zip to a bucket IN the target account with the target session, and
    # read agentcore-deps/ from this bucket with the HOME session (codegen_step,
    # mcp_server_step), because AgentCore's runtime code-fetch ignores cross-account
    # S3 grants anyway. The grant that used to live here (GetObject + PutObject on
    # deployments/* and agentcore-deps/* for both target roles) was used by nothing,
    # and it let a target account overwrite the dependency bundles every platform
    # deploy ships. ARCC cnt_FDzqr5k4wxIKsZ: remove cross-account access that is not
    # essential. A stale `-c deploy_target_accounts` fails the synth loudly rather
    # than being silently ignored.
    if stack.node.try_get_context("deploy_target_accounts"):
        raise ValueError(
            "deploy_target_accounts is no longer used: cross-account deploys use a bucket in the "
            "target account, and the platform artifacts bucket grants target accounts nothing. "
            "Remove -c deploy_target_accounts and register the target through the admin API."
        )
    return bucket


#: The prefix upload_agentcore_deps ships the platform's runtime dependency bundles under.
AGENTCORE_DEPS_PREFIX = "agentcore-deps"

#: Every S3 verb that can change or remove an object under a prefix.
S3_OBJECT_WRITE_ACTIONS: tuple[str, ...] = (
    "s3:PutObject",
    "s3:PutObjectAcl",
    "s3:PutObjectTagging",
    "s3:PutObjectVersionTagging",
    "s3:DeleteObject",
    "s3:DeleteObjectVersion",
    "s3:DeleteObjectTagging",
    "s3:DeleteObjectVersionTagging",
    "s3:AbortMultipartUpload",
)


def deny_writes_to_agentcore_deps(role: iam.IRole, artifacts_bucket: s3.Bucket) -> None:
    """Deny *role* every object write under ``agentcore-deps/`` (F-21, signoff-g10).

    The deployment Lambda and four step roles hold ``grant_read_write`` on the whole artifacts
    bucket, which includes the dependency bundles every AgentCore runtime this platform deploys
    is built from -- code that runs as the shared runtime role. Only the CDK BucketDeployment
    (its own custom-resource role) legitimately writes there; every Lambda only reads
    (codegen_step, mcp_server_step, services/deployment.py). An explicit Deny keeps a bug or a
    compromise in any Lambda from turning the bundle prefix into a supply-chain write. The
    bundles' missing digest pin (the other half of F-21) is codegen/backend scope.
    ARCC cnt_SFJJhkOueCPRkd. Enforced by tests/test_f21_no_lambda_writes_the_dependency_bundles.py.
    """
    role.add_to_principal_policy(
        iam.PolicyStatement(
            sid="NeverWriteTheDependencyBundles",
            effect=iam.Effect.DENY,
            actions=list(S3_OBJECT_WRITE_ACTIONS),
            resources=[artifacts_bucket.arn_for_objects(f"{AGENTCORE_DEPS_PREFIX}/*")],
        )
    )


def upload_agentcore_deps(stack: cdk.Stack, artifacts_bucket: s3.Bucket) -> s3_deployment.BucketDeployment | None:
    """Upload pre-built aarch64 dependency bundles to S3 artifacts bucket.

    Uses s3_deployment.BucketDeployment to sync backend/agentcore-deps/*.zip
    to s3://{artifacts_bucket}/agentcore-deps/

    Gracefully skips if the bundle directory does not exist (e.g. local dev).

    New bundles need no change here — Source.asset() picks up whatever is in the directory,
    which is how the eight provider-<extra>.zip bundles started shipping. What DOES have a
    limit is the ephemeral storage below: the deployment Lambda downloads the asset zip into
    /tmp and then extracts it beside itself, so peak usage is roughly twice the directory.
    Measured 2026-09-20 with the provider bundles in place: 131.8 MiB asset + 131.8 MiB
    extracted = 263.6 MiB against 1024 MiB, so ~3.9x headroom. The cliff is around 512 MiB
    of bundles, and it is not a graceful one — a BucketDeployment that fills /tmp fails the
    whole platform deploy. tests/test_agentcore_deps_fit_in_ephemeral_storage.py measures the
    real directory against the real value set here so the ceiling is found before a deploy
    finds it.

    Requirements: 2.1, 2.2, 2.3
    """
    deps_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend", "agentcore-deps"))
    if not os.path.isdir(deps_path):
        return None

    return s3_deployment.BucketDeployment(
        stack,
        "AgentCoreDepsDeployment",
        sources=[s3_deployment.Source.asset(deps_path)],
        destination_bucket=artifacts_bucket,
        destination_key_prefix="agentcore-deps",
        memory_limit=512,
        ephemeral_storage_size=Size.mebibytes(1024),
    )
