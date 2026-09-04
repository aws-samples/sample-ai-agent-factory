"""Phase 2: governance tags are applied to the runtime exec IAM role.

Verifies create_runtime_iam_role merges resolved resource_tags alongside the two
mandatory tags — ManagedBy (the product) and AgentCoreStack (the deployment
instance; see services/resource_ownership.py) — on moto-backed IAM. The owner tag
is what lets scripts/cleanup.sh sweep AgentCoreRuntime-* roles at teardown
without deleting a co-resident deployment's.
"""

from __future__ import annotations

import boto3
import pytest

moto = pytest.importorskip("moto")
from app.services.resource_ownership import stack_id  # noqa: E402
from app.services.runtime_deployer import create_runtime_iam_role  # noqa: E402
from moto import mock_aws  # noqa: E402


@mock_aws
def test_resource_tags_applied_to_role():
    iam = boto3.client("iam", region_name="us-east-1")
    create_runtime_iam_role(
        iam_client=iam,
        role_name="agentcore-tagtest-role",
        account_id="123456789012",
        region="us-east-1",
        resource_tags={"platform:owner": "alice", "cost-center": "cc-42"},
    )
    tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName="agentcore-tagtest-role")["Tags"]}
    assert tags["platform:owner"] == "alice"
    assert tags["cost-center"] == "cc-42"
    assert tags["ManagedBy"] == "agentcore-flows"  # mandatory tag preserved


@mock_aws
def test_managed_by_not_overridable():
    iam = boto3.client("iam", region_name="us-east-1")
    create_runtime_iam_role(
        iam_client=iam,
        role_name="agentcore-tagtest-role2",
        account_id="123456789012",
        region="us-east-1",
        resource_tags={"ManagedBy": "attacker"},  # must be ignored
    )
    tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName="agentcore-tagtest-role2")["Tags"]}
    assert tags["ManagedBy"] == "agentcore-flows"


@mock_aws
def test_no_tags_still_gets_managed_by():
    iam = boto3.client("iam", region_name="us-east-1")
    create_runtime_iam_role(
        iam_client=iam,
        role_name="agentcore-tagtest-role3",
        account_id="123456789012",
        region="us-east-1",
    )
    tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName="agentcore-tagtest-role3")["Tags"]}
    # Two mandatory tags, and the pair is deliberate: ManagedBy names the PRODUCT
    # (the Bug 139 ABAC delete grant matches on it) while AgentCoreStack names the
    # deployment instance, which is the only thing that distinguishes two
    # deployments of this product sharing one account. cleanup.sh gates the
    # AgentCoreRuntime-* role sweep on the latter.
    assert tags == {
        "ManagedBy": "agentcore-flows",
        "AgentCoreStack": stack_id("us-east-1"),
    }


@mock_aws
def test_owner_tag_is_not_overridable_either():
    """A caller-supplied AgentCoreStack tag must not be able to reassign ownership.

    If it could, a governance tag on the canvas would let one deployment mark its
    roles as belonging to another — and a teardown of that other deployment would
    then delete them.
    """
    iam = boto3.client("iam", region_name="us-east-1")
    create_runtime_iam_role(
        iam_client=iam,
        role_name="agentcore-tagtest-role4",
        account_id="123456789012",
        region="us-east-1",
        resource_tags={"AgentCoreStack": "someone-elses-stack"},
    )
    tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName="agentcore-tagtest-role4")["Tags"]}
    assert tags["AgentCoreStack"] == stack_id("us-east-1")


@mock_aws
def test_owner_tag_carries_the_region_not_just_project_and_env():
    """IAM is account-global, so the same {project}-{env} in two regions must differ.

    Without the region, a us-east-1 teardown would claim ownership of the
    eu-central-1 deployment's roles — which is exactly the cross-region deletion
    cleanup.sh used to perform.
    """
    iam = boto3.client("iam", region_name="us-east-1")
    create_runtime_iam_role(
        iam_client=iam,
        role_name="agentcore-tagtest-role5",
        account_id="123456789012",
        region="eu-central-1",
    )
    tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName="agentcore-tagtest-role5")["Tags"]}
    assert tags["AgentCoreStack"].endswith("-eu-central-1")
    assert tags["AgentCoreStack"] != stack_id("us-east-1")
