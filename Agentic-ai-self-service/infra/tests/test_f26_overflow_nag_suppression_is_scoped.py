"""F-26: the OverflowPolicy nag aspect suppresses only the reviewed Lambda roles' overflow.

The first version matched ANY construct whose id started with ``OverflowPolicy`` and swallowed
every exception, so a new wildcard in a policy nobody had reviewed -- or a suppression that
silently failed to apply -- both read as a clean ``cdk-nag`` gate. The aspect is now a timing
fix and nothing more: it reaches exactly the overflow children of the roles
``apply_nag_suppressions`` already suppresses by construct, and it has no exception path.

Tested directly against the aspect with two hand-built overflow-shaped policies, because the
platform template cannot exercise the negative case (every overflow policy in it belongs to a
Lambda role). ``Template.from_stack`` runs aspects, and cdk-nag records a suppression as
``Metadata.cdk_nag.rules_to_suppress`` on the CfnResource, which is what the CLI reads.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template
from stacks.platform.nag_suppressions import _OverflowPolicyNagSuppressor

REASONS = [("AwsSolutions-IAM5", "test reason")]


def _build():
    app = cdk.App()
    stack = cdk.Stack(app, "OverflowProbe")
    reviewed = iam.Role(stack, "ReviewedLambdaRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
    foreign = iam.Role(stack, "SomeOtherRole", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"))
    wildcard = iam.PolicyStatement(actions=["s3:GetObject"], resources=["*"])
    iam.ManagedPolicy(reviewed, "OverflowPolicy0", statements=[wildcard], roles=[reviewed])
    iam.ManagedPolicy(foreign, "OverflowPolicy0", statements=[wildcard], roles=[foreign])
    iam.ManagedPolicy(stack, "OverflowPolicyTopLevel", statements=[wildcard])
    return stack, reviewed


def _suppressed(tpl: dict) -> set[str]:
    out = set()
    for lid, res in tpl["Resources"].items():
        rules = ((res.get("Metadata") or {}).get("cdk_nag") or {}).get("rules_to_suppress") or []
        if any(r.get("id") == "AwsSolutions-IAM5" for r in rules):
            out.add(lid)
    return out


def test_only_the_reviewed_roles_overflow_is_suppressed():
    stack, reviewed = _build()
    cdk.Aspects.of(stack).add(_OverflowPolicyNagSuppressor(REASONS, roles=[reviewed]))
    suppressed = _suppressed(Template.from_stack(stack).to_json())
    assert suppressed, "the aspect suppressed nothing; a clean nag gate from it would be vacuous"
    assert all(lid.startswith("ReviewedLambdaRoleOverflowPolicy0") for lid in suppressed), suppressed
    assert not any(lid.startswith(("SomeOtherRoleOverflowPolicy0", "OverflowPolicyTopLevel")) for lid in suppressed), (
        suppressed
    )


def test_an_empty_allowlist_suppresses_nothing():
    stack, _reviewed = _build()
    cdk.Aspects.of(stack).add(_OverflowPolicyNagSuppressor(REASONS, roles=[]))
    assert _suppressed(Template.from_stack(stack).to_json()) == set()


def test_a_none_role_is_tolerated():
    """apply_nag_suppressions passes ``stream_lambda.role if stream_lambda else None``."""
    stack, reviewed = _build()
    cdk.Aspects.of(stack).add(_OverflowPolicyNagSuppressor(REASONS, roles=[None, reviewed]))
    assert _suppressed(Template.from_stack(stack).to_json())
