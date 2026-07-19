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
        ],
    )
    # Phase 7 (opt-in) cross-account deploy: a target account's
    # AgentCoreFlowsDeploymentRole (assumed by the step Lambdas) must
    # read/write code artifacts here — the artifacts bucket is a PLATFORM
    # resource, not per-account. Grant scoped to that exact role name in ANY
    # account, on the deployments/ prefix only (not the agentcore-deps/
    # bundles). No effect until cross-account deploy is used. IAM on the
    # assuming side is still name-scoped, so this is a two-sided, bounded grant.
    # Phase 7 (opt-in) cross-account deploy: a target account's
    # AgentCoreFlowsDeploymentRole (assumed by the step Lambdas) must
    # read/write code artifacts here — the artifacts bucket is a PLATFORM
    # resource, not per-account.
    #
    # S3 resource policies (unlike IAM) require CONCRETE account principals:
    #   * a wildcard-account ARN ("arn:aws:iam::*:role/...") → "Invalid principal"
    #   * Principal:* (even condition-gated) → blocked by BlockPublicPolicy
    #     (the bucket is correctly BLOCK_ALL).
    # So trusted deploy-target account IDs are supplied at CDK-deploy time via
    # context (`-c deploy_target_accounts=111111111111,222222222222`) and we
    # add a CONCRETE-principal grant per account, scoped to the deployments/
    # prefix + that exact role name. Empty by default → no cross-account
    # grant (feature dormant). Registering a NEW target account is therefore
    # a two-step, deliberate action: add the id to context + redeploy, THEN
    # register via the admin API. This is the security-correct topology under
    # BlockPublicAccess; documented in docs/RBAC_ROLLOUT.md / cross-account.
    _target_accounts_ctx = stack.node.try_get_context("deploy_target_accounts") or ""
    _target_accounts = [a.strip() for a in str(_target_accounts_ctx).split(",") if a.strip()]
    if _target_accounts:
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CrossAccountDeployRoleArtifacts",
                effect=iam.Effect.ALLOW,
                principals=[
                    iam.ArnPrincipal(f"arn:aws:iam::{acct}:role/{_rname}")
                    for acct in _target_accounts
                    # Both the deploy role (uploads the code zip) AND the
                    # runtime role (reads it at agent boot) need artifacts
                    # access across the account boundary.
                    for _rname in ("AgentCoreFlowsDeploymentRole", "AgentCoreFlowsRuntimeRole")
                ],
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[
                    # code artifacts written/read per deploy
                    bucket.arn_for_objects("deployments/*"),
                    # pre-built dependency bundles the codegen step downloads
                    # to bundle into the runtime zip (read-only in practice).
                    bucket.arn_for_objects("agentcore-deps/*"),
                ],
            )
        )
    return bucket


def upload_agentcore_deps(stack: cdk.Stack, artifacts_bucket: s3.Bucket) -> s3_deployment.BucketDeployment | None:
    """Upload pre-built aarch64 dependency bundles to S3 artifacts bucket.

    Uses s3_deployment.BucketDeployment to sync backend/agentcore-deps/*.zip
    to s3://{artifacts_bucket}/agentcore-deps/

    Gracefully skips if the bundle directory does not exist (e.g. local dev).

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
