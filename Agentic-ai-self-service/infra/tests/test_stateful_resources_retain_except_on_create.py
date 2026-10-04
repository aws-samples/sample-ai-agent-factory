"""Every stateful platform resource must survive a stack delete or a replacing update, but a
FAILED FIRST CREATE must clean up after itself.

Measured on 2026-09-25 (stack acfe2e-p0925, us-east-1): a fresh stack died at its VPC (regional
quota 5/5). Plain ``DeletionPolicy: Retain`` also applies to the rollback of a failed initial
create, so CloudFormation emitted 20 DELETE_SKIPPED events (15 DynamoDB tables, two Cognito
pools, a bucket, a DLQ, the tool-sandbox flow-log group). Three of those had already been
physically created and PERSISTED after the rollback and the stack delete -- both Cognito pools
and the DLQ -- and had to be removed by hand before a retry could reuse the names; the others
had been cancelled before they physically existed. ``RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE``
synthesizes ``DeletionPolicy: RetainExceptOnCreate`` + ``UpdateReplacePolicy: Retain``: the
initial rollback deletes what it created; later deletes and replacements still retain.

This is the all-stateful-resources oracle. It covers every type that showed up as
DELETE_SKIPPED, including log groups, and fails if ANY of them regresses to plain Retain, loses
protection in a prod-like env, or is retained in a destroy env (dev/test/sandbox/preview use
DESTROY per platform_stack.py -- including the gateway-auth pool, which follows the same knob).
"""

from __future__ import annotations

import os

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

# Types whose instances hold state and which carry the environment's removal policy.
STATEFUL_TYPES = {
    "AWS::DynamoDB::Table",
    "AWS::Cognito::UserPool",
    "AWS::Cognito::UserPoolDomain",
    "AWS::S3::Bucket",
    "AWS::SQS::Queue",
}
# Log groups split by design: the tool-sandbox flow-log group is data (network evidence) and
# follows the knob; Lambda log groups are deliberately DESTROY in every env because Lambda
# recreates its own group on first invoke and a retained one blocks a same-name redeploy.
FLOW_LOG_GROUP_PREFIX = "ToolSandboxFlowLogs"


def _resources(environment_name: str) -> dict:
    # The env-var override would turn a prod-like env into DESTROY and make the
    # prod-like assertions vacuous; make sure it is not leaking in from the shell.
    os.environ.pop("AGENTCORE_ALLOW_DESTROY", None)
    app = cdk.App()
    stack = PlatformStack(
        app,
        f"Retention{environment_name.title()}",
        environment_name=environment_name,
        project_name="agentcore-workflow",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    return Template.from_stack(stack).to_json()["Resources"]


@pytest.fixture(scope="module")
def prod_like() -> dict:
    # "p0925" is deliberately NOT in platform_stack's _destroy_envs -- it is the
    # environment class the 2026-09-25 incident happened in.
    return _resources("p0925")


@pytest.fixture(scope="module")
def destroy_env() -> dict:
    return _resources("dev")


def _governed(resources: dict):
    """(lid, res) for every stateful resource plus the flow-log group."""
    for lid, res in resources.items():
        if res["Type"] in STATEFUL_TYPES or (
            res["Type"] == "AWS::Logs::LogGroup" and lid.startswith(FLOW_LOG_GROUP_PREFIX)
        ):
            yield lid, res


def test_prod_like_stateful_resources_retain_except_on_create(prod_like):
    seen: set[str] = set()
    for lid, res in _governed(prod_like):
        seen.add(res["Type"])
        assert res.get("DeletionPolicy") == "RetainExceptOnCreate", (
            f"{res['Type']} {lid}: DeletionPolicy={res.get('DeletionPolicy')!r}. Plain Retain persists the "
            "resource through the rollback of a failed first create; a missing policy destroys it on delete."
        )
        assert res.get("UpdateReplacePolicy") == "Retain", (
            f"{res['Type']} {lid}: UpdateReplacePolicy={res.get('UpdateReplacePolicy')!r}; a replacing "
            "update must not destroy state"
        )
    assert seen == STATEFUL_TYPES | {"AWS::Logs::LogGroup"}, f"oracle reach: types missing from the synth: {seen}"


def test_no_resource_carries_plain_retain_in_any_env(prod_like, destroy_env):
    for name, resources in (("prod-like", prod_like), ("destroy", destroy_env)):
        plain = sorted(f"{r['Type']} {lid}" for lid, r in resources.items() if r.get("DeletionPolicy") == "Retain")
        assert not plain, (
            f"[{name}] plain DeletionPolicy: Retain would persist these through a failed first create: {plain}"
        )


def test_lambda_log_groups_are_destroyed_in_every_env(prod_like, destroy_env):
    """Deliberate: Lambda recreates its own log group on first invoke, and a retained one
    blocks a same-name redeploy; only the flow-log group is data and follows the knob."""
    for resources in (prod_like, destroy_env):
        lambda_groups = [
            (lid, r)
            for lid, r in resources.items()
            if r["Type"] == "AWS::Logs::LogGroup" and not lid.startswith(FLOW_LOG_GROUP_PREFIX)
        ]
        assert lambda_groups, "oracle reach: no Lambda log groups found"
        for lid, r in lambda_groups:
            assert r.get("DeletionPolicy") in (None, "Delete"), f"{lid}: {r.get('DeletionPolicy')}"


def test_destroy_envs_retain_nothing(destroy_env):
    """dev/test/sandbox/preview/ephemeral use DESTROY (platform_stack.py). That contract has no
    exceptions: the gateway-auth pool and its domain follow the same knob, so a throwaway
    environment really is throwaway. cleanup.sh keeps its retained-pool path for older stacks."""
    for lid, res in _governed(destroy_env):
        assert res.get("DeletionPolicy") in (None, "Delete"), f"{lid}: {res.get('DeletionPolicy')}"
        assert res.get("UpdateReplacePolicy") in (None, "Delete"), f"{lid}: {res.get('UpdateReplacePolicy')}"
    retained = [lid for lid, r in destroy_env.items() if r.get("DeletionPolicy") in ("Retain", "RetainExceptOnCreate")]
    assert not retained, f"a destroy env must not retain anything: {retained}"


def test_the_governed_resource_set_is_identical_across_env_classes(prod_like, destroy_env):
    """Only the VALUE of the policy may differ between environment classes, never WHICH
    resources are governed -- otherwise a prod-only resource could slip in unprotected."""
    prod_set = {lid for lid, _ in _governed(prod_like)}
    dev_set = {lid for lid, _ in _governed(destroy_env)}
    assert prod_set == dev_set, f"governed resource sets differ: {prod_set ^ dev_set}"
    assert len(prod_set) >= 19, (
        f"expected tables+pools+domain+buckets+queues+flow-log group (>=19), saw {len(prod_set)}"
    )
