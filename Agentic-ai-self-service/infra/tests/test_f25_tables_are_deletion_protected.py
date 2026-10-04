"""F-25: every DynamoDB table is deletion-protected wherever it is also retained.

``RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE`` protects a table from CloudFormation; it does
nothing against an out-of-band ``DeleteTable`` -- the ``AGENTCORE_ALLOW_DESTROY=true`` flip the
review names, a console click, or a scripted sweep. ``DeletionProtectionEnabled`` is the lever
for that, and it follows the same knob (``PlatformConfig.allow_destroy``) so dev/test/sandbox
stacks stay destroyable and the two settings can never disagree. ARCC cnt_h02wszR9St529D
(storage resources protected from accidental deletion).
"""

from __future__ import annotations

import os

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

TABLE = "AWS::DynamoDB::Table"


def _tables(environment_name: str) -> dict[str, dict]:
    os.environ.pop("AGENTCORE_ALLOW_DESTROY", None)
    app = cdk.App()
    stack = PlatformStack(
        app,
        f"F25Protect{environment_name.title()}",
        environment_name=environment_name,
        project_name="acf",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    res = Template.from_stack(stack).to_json()["Resources"]
    return {lid: r for lid, r in res.items() if r["Type"] == TABLE}


@pytest.fixture(scope="module")
def prod_like() -> dict[str, dict]:
    return _tables("p0925")  # not in platform_stack's _destroy_envs, like the review's stack


@pytest.fixture(scope="module")
def destroy_env() -> dict[str, dict]:
    return _tables("dev")


def test_the_walk_finds_the_tables(prod_like, destroy_env):
    assert len(prod_like) >= 15 and set(prod_like) == set(destroy_env), (len(prod_like), len(destroy_env))


def test_every_table_in_a_prod_like_env_is_deletion_protected(prod_like):
    """The assertion that fails on the pre-fix tree."""
    for lid, res in prod_like.items():
        assert res["Properties"].get("DeletionProtectionEnabled") is True, lid
        assert res.get("DeletionPolicy") == "RetainExceptOnCreate", (lid, res.get("DeletionPolicy"))


def test_destroy_envs_stay_destroyable(destroy_env):
    for lid, res in destroy_env.items():
        assert res["Properties"].get("DeletionProtectionEnabled") is not True, lid
        assert res.get("DeletionPolicy") == "Delete", (lid, res.get("DeletionPolicy"))
