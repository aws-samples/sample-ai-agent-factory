"""The one way to grant a role access to the REGIONAL artifact buckets (F-41b).

WHY THIS EXISTS. A same-account deploy to a non-home region stages code in a bucket in
that region -- ``agentcore-flows-artifacts-{account}-{region}`` by default
(``backend/src/app/services/deploy_target.DEFAULT_TARGET_ARTIFACT_BUCKET_PREFIX``) --
because AgentCore reads the zip from the runtime's own region. Every role that touches
the home ``ArtifactsBucket`` used to hold grants on that bucket ONLY, so region
registration failed at HeadBucket with a 403 (measured live, 2026-09-22). A bucket
policy naming the account root cannot close the gap: within one account an account
principal delegates to IAM, so the caller still needs an identity-policy Allow of its own.

THE SCOPE. The bucket name is not known at synth time (one per registered region), so
the grant names the NAMESPACE: ``arn:aws:s3:::agentcore-flows-artifacts-{account}-*``.
S3 bucket names are global, so another account can create a bucket inside that
namespace. Every statement is therefore conditioned on ``aws:ResourceAccount`` equal to
this account, and a squatted bucket matches nothing. Registration refuses a bucket
outside the namespace (``routers/admin.add_region_target``), so any bucket the product
will use is one these grants cover.

THE ACTIONS mirror what each role does against the home bucket, narrowed to the S3 calls
the backend actually makes: get/put/delete object (including version deletes), HeadBucket
(which IAM authorizes as ``s3:ListBucket``), GetBucketLocation, and presigned GETs.
Never ``arn:aws:s3:::*`` for write or delete.

Object TAGS are separate actions and easy to miss. ``put_object(Tagging=...)`` -- how
``runtime_deployer.upload_code_to_s3`` and gateway spec staging mark an object as this
deployment's -- is authorized as ``s3:PutObject`` AND ``s3:PutObjectTagging``, and the
first live non-home deploy failed on exactly the second (2026-09-22). The pre-delete
ownership proof (``resource_ownership.assert_s3_object_owned``) reads the tags with
``s3:GetObjectTagging``. Only the roles that do each get it.

``infra/tests/test_regional_artifact_bucket_grants.py`` asserts the grant on every role
that holds a home-bucket grant, so a role added later cannot silently regress to
home-only.

ARCC: ``cnt_dwzZ05hLnqhYXQ`` (tight resource scoping), ``cnt_SFJJhkOueCPRkd`` (condition
keys on a wildcard that cannot be removed), ``cnt_SAaWVJg7wZDudD`` (restrict S3 access to
specific principals).
"""

from __future__ import annotations

from typing import Literal

import aws_cdk as cdk
from aws_cdk import aws_iam as iam

#: Must stay equal to ``deploy_target.DEFAULT_TARGET_ARTIFACT_BUCKET_PREFIX``; asserted
#: in the infra test, because a drift makes every grant here match nothing.
BUCKET_NAMESPACE_PREFIX = "agentcore-flows-artifacts"

READ_BUCKET_ACTIONS = ["s3:ListBucket", "s3:GetBucketLocation"]
READ_OBJECT_ACTIONS = ["s3:GetObject", "s3:GetObjectVersion"]
DELETE_OBJECT_ACTIONS = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
WRITE_OBJECT_ACTIONS = ["s3:PutObject", "s3:AbortMultipartUpload", *DELETE_OBJECT_ACTIONS]
#: A tagged upload. Only for a role that passes ``Tagging=`` to put_object.
WRITE_TAG_ACTIONS = ["s3:PutObjectTagging"]
#: The pre-delete ownership proof, per version (F-60): delete_owned_s3_object lists the
#: key's versions and reads each one's tags. Only for a role that runs it.
READ_TAG_ACTIONS = ["s3:GetObjectTagging", "s3:GetObjectVersionTagging"]
LIST_VERSION_ACTIONS = ["s3:ListBucketVersions"]

Access = Literal["read", "read_write", "delete"]


def bucket_namespace_arn(stack: cdk.Stack) -> str:
    return f"arn:aws:s3:::{BUCKET_NAMESPACE_PREFIX}-{stack.account}-*"


def grant_regional_artifact_buckets(
    role: iam.IRole,
    stack: cdk.Stack,
    access: Access,
    *,
    write_tags: bool = False,
    read_tags: bool = False,
) -> None:
    """Grant *role* ``access`` on this account's regional artifact buckets.

    Args:
        write_tags: the role uploads with ``Tagging=`` (s3:PutObjectTagging).
        read_tags: the role proves ownership of every version before deleting it
            (tag reads on objects, s3:ListBucketVersions on the bucket).
    """
    bucket = bucket_namespace_arn(stack)
    owned = {"StringEquals": {"aws:ResourceAccount": stack.account}}
    if access == "delete":
        object_actions = DELETE_OBJECT_ACTIONS
    elif access == "read":
        object_actions = READ_OBJECT_ACTIONS
    else:
        object_actions = READ_OBJECT_ACTIONS + WRITE_OBJECT_ACTIONS
    object_actions = [
        *object_actions,
        *(WRITE_TAG_ACTIONS if write_tags else []),
        *(READ_TAG_ACTIONS if read_tags else []),
    ]
    bucket_actions = [
        *(READ_BUCKET_ACTIONS if access != "delete" else []),
        *(LIST_VERSION_ACTIONS if read_tags else []),
    ]
    if bucket_actions:
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="RegionalArtifactBuckets",
                actions=bucket_actions,
                resources=[bucket],
                conditions=owned,
            )
        )
    role.add_to_principal_policy(
        iam.PolicyStatement(
            sid="RegionalArtifactObjects",
            actions=object_actions,
            resources=[f"{bucket}/*"],
            conditions=owned,
        )
    )
