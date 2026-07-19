"""Phase 2: governance tags are applied to the runtime exec IAM role.

Verifies create_runtime_iam_role merges resolved resource_tags alongside the
mandatory ManagedBy tag (moto-backed IAM).
"""

from __future__ import annotations

import boto3
import pytest

moto = pytest.importorskip("moto")
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
    assert tags == {"ManagedBy": "agentcore-flows"}
