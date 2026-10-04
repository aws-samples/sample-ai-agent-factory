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

# The complete set of AWS-managed policies this platform ever attaches to a role
# it creates, and therefore the only ones `iam:AttachRolePolicy` is granted for.
#
# Without the condition, a principal that can attach ANY managed policy to any
# `AgentCore*` role can attach AdministratorAccess to one, and several of those
# roles are passable to a service or assumable by code the platform itself
# generates. ARCC guidance on managed policies (cnt_pXauQr9E6bKwke) states that
# an incorrectly scoped managed policy is a privilege-escalation path, and the
# least-privilege guidance (cnt_SFJJhkOueCPRkd, cnt_L4ZLZgjrCctfxl) requires
# condition keys rather than action-plus-resource alone. `iam:PolicyARN` is a
# real condition key for both AttachRolePolicy and DetachRolePolicy — confirmed
# against the AWS Service Reference feed, not the docs.
#
# This is a CLOSED set, derived from every attach site in the application:
#   backend/src/app/services/tool_tester.py     BASIC_EXECUTION_POLICY, VPC_ACCESS_POLICY
#   backend/src/app/services/gateway_deployer.py:1234  AWSLambdaBasicExecutionRole
# There is no third site. infra/tests/test_attach_role_policy_allowlist.py fails
# if the application grows one, because a new attach site under this condition
# would otherwise fail at runtime with an AccessDenied that names a policy ARN
# and no cause.
#
# DELIBERATELY NOT applied to iam:DetachRolePolicy. Detach is a cleanup verb: the
# delete paths iterate whatever list_attached_role_policies returns, which on a
# role created by an older build can include a policy no longer in this set.
# Conditioning detach would turn that into a role that can never be deleted,
# trading a real escalation path for a guaranteed leak. Detach also cannot grant
# permission — no AWS-managed policy carries a Deny.
ATTACHABLE_MANAGED_POLICIES = (
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole",
)


# The deployment state machine's hard ceiling, and the ONLY available bound on how long an
# AgentVersion row can legitimately sit in "pending".
#
# Read by two places that must not drift: step_functions.py sets the state machine's top-level
# timeout from it, and lambdas.py publishes it to the deploy API as
# DEPLOY_PENDING_CLAIM_TTL_SECONDS.
#
# Why the deploy API needs to know. deployment_handler writes a "pending" AgentVersion row
# BEFORE starting the execution, and the H-1 cross-tenant name guard treats a foreign "pending"
# row as a live claim on the friendly runtime name (Bug 192b deliberately kept "pending" in
# _LIVE_CLAIM because an in-flight deploy must hold its name). The only thing that ever flips
# that row is status_update_step -- and a top-level state machine timeout, exactly like a manual
# abort, terminates the execution WITHOUT running the Catch, so status_update_step never runs.
# A deploy that hits this ceiling therefore leaves the row "pending" forever and locks that
# friendly name against every other tenant permanently, with no product path to release it.
# Measured live: acfe2e-p0920-agent-versions held `sfx0920_abort` pending from 2026-09-20 with
# its deployment still reading in_progress four days later.
#
# Past this bound the execution is provably dead, so the claim must stop blocking. Hardcoding
# the number on the backend side instead would let a future change here silently reintroduce the
# permanent lock (if the timeout were raised) or let the guard release a genuinely in-flight
# claim and admit a cross-tenant race (if it were lowered).
DEPLOYMENT_STATE_MACHINE_TIMEOUT_MINUTES = 30

# Slack added on top of the ceiling before a "pending" claim is considered dead. The row is
# written before the execution starts and flipped by the last state in it, so the legitimate
# age of a pending row is the ceiling plus the API's own latency; this covers that plus clock
# skew between the writer and the reader. Erring long only delays releasing a dead name, while
# erring short would let one tenant take a name another tenant is still deploying.
PENDING_CLAIM_SLACK_SECONDS = 300


# The tag-key namespaces the platform's own roles may stamp on the AWS resources a deploy
# creates, beyond the ownership/provenance keys each grant enumerates exactly.
#
# P0-B resolves a governance tag set per deploy from admin-created tag policies and the live
# path now applies it to the billable resources. Those keys cannot be enumerated in an IAM
# condition: an admin creates them at runtime through POST /api/settings/tags, so an
# enumeration would need a platform redeploy per tag policy. Leaving `aws:TagKeys` off the
# tag-on-create grants was the other option and is worse: ARCC cnt_L4ZLZgjrCctfxl lists
# "create/update tags" among the powerful operations that yield elevated privilege, and
# cnt_6gBImtb08AJqCB gives the mechanism -- tags carry ABAC decisions, so a role that can
# write any tag key can write whichever key some other policy in this account authorizes on.
#
# A NAMESPACE is the shape that satisfies both: knowable at synth time, open at runtime. An
# admin may add `org:anything` with no redeploy; nothing can reach `Environment`, `Project` or
# `Team`.
#
# TWO namespaces, because the product already had one and it is not the admin's:
#
#   `platform:` is the product's own reserved designation (`TagPolicy.is_platform` is
#   `key.startswith("platform:")`; the three seeded policies are platform:application/owner
#   /group) and `POST /api/settings/tags` REFUSES to create a new key in it, precisely so an
#   admin-created key cannot read as product-seeded.
#
#   `org:` is therefore the namespace an admin can actually create in. Without it this tuple
#   would name only a namespace the admin API forbids writing to, which is a governance feature
#   that cannot govern anything: every admin-created key would be refused at deploy time. That
#   was the state when the namespace bound first shipped, and it is why the two are listed
#   together here rather than one being "the" namespace.
#
# TWO consumers, and they must agree or a governed deploy fails closed: the `aws:TagKeys`
# allowlists in step_lambdas.py, and GOVERNANCE_TAG_KEY_PREFIXES in the backend's environment
# (services/resource_tagging.py), which refuses an out-of-namespace key at the API boundary
# instead of letting it become an AccessDenied inside a half-built deployment. Changing this
# tuple requires a platform redeploy, which is inherent: one half of it is an IAM condition.
GOVERNANCE_TAG_KEY_PREFIXES: tuple[str, ...] = ("platform:", "org:")

#: Env var carrying the above to the Lambdas that validate a tag set.
GOVERNANCE_TAG_KEY_PREFIXES_ENV = "GOVERNANCE_TAG_KEY_PREFIXES"


def governance_tag_key_prefixes_env_value() -> str:
    """The env-var form the backend parses (comma-separated, no spaces)."""
    return ",".join(GOVERNANCE_TAG_KEY_PREFIXES)


def governance_tag_key_globs() -> list[str]:
    """The ``aws:TagKeys`` patterns for a ``ForAllValues:StringLike`` condition.

    IAM has no regex here; ``StringLike`` with a trailing ``*`` is the only way to express
    "any key inside this namespace". The exact ownership keys are passed alongside these and
    contain no wildcard character, so they keep matching literally under ``StringLike``.
    """
    return [f"{prefix}*" for prefix in GOVERNANCE_TAG_KEY_PREFIXES]


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
