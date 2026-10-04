"""F-21: no platform Lambda may write under ``agentcore-deps/`` in the artifacts bucket.

The deployment Lambda and the four S3-writing step roles (codegen, gateway, knowledge_base,
mcp_server) hold ``grant_read_write`` on the whole bucket. ``agentcore-deps/`` is where the CDK
BucketDeployment ships the dependency bundles every deployed runtime is built from, so a
bucket-wide write is a supply-chain write into code that later runs as the shared runtime
role. The Lambdas only ever READ that prefix; ``buckets.deny_writes_to_agentcore_deps`` makes
the write impossible with an explicit Deny, per role. The bundles' missing digest pin (the
other half of the review's F-21) is backend/codegen scope and is recorded in the ledger.
"""

from __future__ import annotations

import json

import pytest
from stacks.platform.buckets import AGENTCORE_DEPS_PREFIX, S3_OBJECT_WRITE_ACTIONS

from tests.iam_attachment import statements_by_role
from tests.p1_synth import actions, synth


@pytest.fixture(scope="module")
def tpl() -> dict:
    return synth("F21DepsPrefixStack")


def _artifacts_bucket_lid(tpl: dict) -> str:
    (lid,) = [
        k for k, v in tpl["Resources"].items() if v["Type"] == "AWS::S3::Bucket" and k.startswith("ArtifactsBucket")
    ]
    return lid


def _writes_artifact_objects(st: dict, bucket_lid: str) -> bool:
    if st.get("Effect") != "Allow":
        return False
    if not any(
        a.startswith(("s3:Put", "s3:Delete")) or a in {"s3:*", "s3:PutObject*", "s3:DeleteObject*"} for a in actions(st)
    ):
        return False
    return bucket_lid in json.dumps(st.get("Resource"))


def _denies_deps_prefix(st: dict, bucket_lid: str) -> bool:
    if st.get("Effect") != "Deny" or not set(S3_OBJECT_WRITE_ACTIONS) <= set(actions(st)):
        return False
    text = json.dumps(st.get("Resource"))
    return bucket_lid in text and f"/{AGENTCORE_DEPS_PREFIX}/*" in text


def test_the_walk_finds_the_bucket_writers(tpl):
    bucket = _artifacts_bucket_lid(tpl)
    writers = [
        lid
        for lid, sts in statements_by_role(tpl).items()
        if any(_writes_artifact_objects(st, bucket) for _p, st in sts)
    ]
    # deployment Lambda + codegen, gateway, knowledge_base, mcp_server (the BucketDeployment's
    # own custom-resource role is CDK's and legitimately writes the prefix).
    assert len(writers) >= 5, writers


def test_every_lambda_role_that_writes_the_bucket_is_denied_the_dependency_prefix(tpl):
    """The assertion that fails on the pre-fix tree."""
    bucket = _artifacts_bucket_lid(tpl)
    for lid, sts in statements_by_role(tpl).items():
        if lid.startswith("CustomCDKBucketDeployment"):
            continue  # CDK's own deployment role: the one legitimate writer of agentcore-deps/
        if not any(_writes_artifact_objects(st, bucket) for _p, st in sts):
            continue
        assert any(_denies_deps_prefix(st, bucket) for _p, st in sts), (
            f"{lid} can write the artifacts bucket but is not denied {AGENTCORE_DEPS_PREFIX}/*"
        )


def test_the_deny_never_touches_reads(tpl):
    """codegen/mcp_server must still GET the bundles; a Deny carrying a read verb is an outage."""
    for lid, sts in statements_by_role(tpl).items():
        for _p, st in sts:
            if st.get("Effect") == "Deny" and AGENTCORE_DEPS_PREFIX in json.dumps(st.get("Resource")):
                assert not any(a.startswith(("s3:Get", "s3:List")) or a == "s3:*" for a in actions(st)), (
                    lid,
                    actions(st),
                )


def test_the_bucket_deployment_role_is_not_denied(tpl):
    bucket = _artifacts_bucket_lid(tpl)
    for lid, sts in statements_by_role(tpl).items():
        if lid.startswith("CustomCDKBucketDeployment"):
            assert not any(_denies_deps_prefix(st, bucket) for _p, st in sts), lid
