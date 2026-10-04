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
from app.services.resource_tagging import GovernanceTagError  # noqa: E402
from app.services.runtime_deployer import create_runtime_iam_role  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402


def _role_absent(iam, role_name: str) -> bool:
    """True when the role was never created.

    The refusal tests below are only worth anything if the refusal costs nothing: a
    ``GovernanceTagError`` raised AFTER ``create_role`` would leave an untagged role
    behind and still satisfy a bare ``pytest.raises``.
    """
    try:
        iam.get_role(RoleName=role_name)
    except ClientError as exc:
        assert exc.response["Error"]["Code"] == "NoSuchEntity", exc
        return True
    return False


@mock_aws
def test_resource_tags_applied_to_role():
    """Both governance NAMESPACES reach the role, and an unnamespaced key does not.

    ``org:`` is asserted alongside ``platform:`` on purpose: ``platform:`` keys cannot be
    CREATED through POST /api/settings/tags (they are reserved), so ``org:`` is the only
    namespace an admin can actually add a key in. A test that covered ``platform:`` alone
    would pass with the admin-writable half of the feature broken.
    """
    iam = boto3.client("iam", region_name="us-east-1")
    create_runtime_iam_role(
        iam_client=iam,
        role_name="agentcore-tagtest-role",
        account_id="123456789012",
        region="us-east-1",
        resource_tags={"platform:owner": "alice", "org:cost-center": "cc-42"},
    )
    tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName="agentcore-tagtest-role")["Tags"]}
    assert tags["platform:owner"] == "alice"
    assert tags["org:cost-center"] == "cc-42"
    assert tags["ManagedBy"] == "agentcore-flows"  # mandatory tag preserved


@mock_aws
def test_an_unnamespaced_governance_key_is_refused_before_the_role_exists():
    """``cost-center`` with no namespace is refused here rather than by AWS mid-deploy.

    This used to be an accepted tag, and the change is deliberate: the step roles'
    ``aws:TagKeys`` allowlists enumerate the governance namespaces, so an unnamespaced key
    is an AccessDenied on ``iam:TagRole`` partway through a real deployment -- after other
    resources exist. Refusing at the stamping call turns that into a clean failure, and
    POST /api/settings/tags runs the same validator so an admin cannot create such a key in
    the first place (the deploy-time refusal only catches a legacy row or an internal caller
    that bypassed the route).
    """
    iam = boto3.client("iam", region_name="us-east-1")
    with pytest.raises(GovernanceTagError) as err:
        create_runtime_iam_role(
            iam_client=iam,
            role_name="agentcore-tagtest-unnamespaced",
            account_id="123456789012",
            region="us-east-1",
            resource_tags={"cost-center": "cc-42"},
        )
    # The KEY is named so the operator can fix it; the VALUE must not appear -- a tag value
    # is caller data and can be pasted credential material.
    assert "cost-center" in str(err.value)
    assert "cc-42" not in str(err.value)
    assert _role_absent(iam, "agentcore-tagtest-unnamespaced")


@mock_aws
def test_managed_by_not_overridable():
    """A caller-supplied ``ManagedBy`` is now REFUSED, where it used to be silently dropped.

    Both behaviours protect the ownership tag, and the refusal is the stronger one: silently
    dropping it meant a canvas could carry a tag that looked applied and was not, so the
    operator had no way to learn the platform had ignored them. The reason it is refused is
    the same namespace rule as the test above -- ``ManagedBy`` is unnamespaced -- which is
    also why that rule cannot be relaxed without restoring this hole.
    """
    iam = boto3.client("iam", region_name="us-east-1")
    with pytest.raises(GovernanceTagError):
        create_runtime_iam_role(
            iam_client=iam,
            role_name="agentcore-tagtest-role2",
            account_id="123456789012",
            region="us-east-1",
            resource_tags={"ManagedBy": "attacker"},
        )
    assert _role_absent(iam, "agentcore-tagtest-role2")


@mock_aws
def test_ownership_survives_a_collision_from_inside_the_platform():
    """Defence in depth: ownership wins even when the collision is NOT caller data.

    The two tests above are closed by the namespace rule, which only applies to
    ``resource_tags``. ``governed_tags``' other input -- ``extra`` -- is built by the
    platform itself and is deliberately NOT namespace-checked, because it carries the
    unnamespaced binding keys (``DeploymentId``, ``ToolScope``). So the ordering guarantee
    has to hold independently: ``owner_tags`` is merged LAST, after both. Without this test
    the whole protection would rest on input validation, and one internal call site passing
    an ownership key through ``extra`` would reassign a resource to another deployment.
    """
    from app.services.resource_tagging import governed_tags

    merged = governed_tags(
        "us-east-1",
        {"org:team": "blue"},
        extra={"ManagedBy": "attacker", "AgentCoreStack": "someone-elses-stack"},
    )
    assert merged["ManagedBy"] == "agentcore-flows"
    assert merged["AgentCoreStack"] == stack_id("us-east-1")
    assert merged["org:team"] == "blue"


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
    then delete them. Refused rather than dropped, for the reason given in
    ``test_managed_by_not_overridable``; the drop-side guarantee is pinned by
    ``test_ownership_survives_a_collision_from_inside_the_platform``.
    """
    iam = boto3.client("iam", region_name="us-east-1")
    with pytest.raises(GovernanceTagError):
        create_runtime_iam_role(
            iam_client=iam,
            role_name="agentcore-tagtest-role4",
            account_id="123456789012",
            region="us-east-1",
            resource_tags={"AgentCoreStack": "someone-elses-stack"},
        )
    assert _role_absent(iam, "agentcore-tagtest-role4")


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
