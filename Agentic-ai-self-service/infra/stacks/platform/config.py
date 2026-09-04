"""Shared configuration passed to every PlatformStack builder module."""

from dataclasses import dataclass

import aws_cdk as cdk
from aws_cdk import RemovalPolicy

# The region this platform was originally built for, and the incumbent for
# naming purposes. Two facts make it special:
#
#   * CLOUDFRONT-scoped WAFv2 WebACLs only exist here, and a CloudFront
#     distribution accepts no other scope (see platform/cloudfront_waf.py).
#   * There is a live deployment here. Account-global resource names therefore
#     stay un-suffixed in this region so that making the stack region-agnostic
#     does not rename — and thus replace — anything already running.
HOME_REGION = "us-east-1"


def is_home_region(stack: cdk.Stack) -> bool:
    """True when this stack is being synthesized for the incumbent region."""
    return stack.region == HOME_REGION


@dataclass(frozen=True)
class PlatformConfig:
    """Environment-level knobs shared by every builder.

    ``removal_policy`` / ``allow_destroy`` implement audit issue #9: gate
    RemovalPolicy.DESTROY on environment so prod-like envs don't lose data on
    teardown. dev/test/sandbox/preview environments use DESTROY; everything
    else uses RETAIN. Override via env var AGENTCORE_ALLOW_DESTROY=true.
    """

    env: str
    project: str
    removal_policy: RemovalPolicy
    allow_destroy: bool

    def global_resource_name(self, stack: cdk.Stack, suffix: str) -> str:
        """Name an ACCOUNT-GLOBAL resource so two regions can coexist in one account.

        IAM roles and CloudFront resources (response-headers policies,
        functions, origin access controls) share a single account-wide
        namespace, so ``{project}-{env}-{suffix}`` collides the moment the same
        environment is deployed to a second region. Every region except
        ``HOME_REGION`` qualifies itself out of the incumbent's way.

        Regional namespaces — Lambda functions, log groups, DynamoDB tables,
        SNS topics, alarms, state machines — deliberately do NOT go through
        this; adding a region there would be churn with no collision to fix.
        """
        if is_home_region(stack):
            return f"{self.project}-{self.env}-{suffix}"
        return f"{self.project}-{self.env}-{stack.region}-{suffix}"
