"""The one way to grant ``cognito-idp:DescribeUserPoolClient`` in this stack.

WHO MAY USE THIS. Only the ``gateway`` and ``harness`` STEP roles
(``step_lambdas._create_step_role``). They CREATE the app client, so reading it back is
their own business function.

**Not the shared AgentCore runtime role, and not any tenant-facing role.** It held this
grant and no longer does (``lambdas.build_shared_runtime_role``), because the scope below
cannot be made per-tenant: ``aws:ResourceTag/AgentCoreStack``'s value names the STACK, and
every deployment in the stack stamps the identical value, so the condition that reads like
"only pools this deployment created" authorizes reading every CO-RESIDENT deployment's
gateway client secret. Cognito's IAM resource type is ``userpool`` with no granularity
below it, so there is no narrower version to retreat to. A tenant-facing role gets its
credential from Secrets Manager instead, which scopes per ARN
(``gateway_deployer._mint_client_secret_ref``).

WHY THIS IS A MODULE AND NOT THREE INLINE STATEMENTS. Three different roles used to need
to re-read a Cognito app client's secret at the moment of use -- the shared AgentCore
runtime role and the two step roles. Each was written separately and each got the scoping
wrong in a DIFFERENT direction:

* The runtime role scoped ``userpool/*`` with a condition on
  ``aws:ResourceTag/AgentCoreStack``. Correct for a pool the gateway step creates at
  deploy time -- and UNSATISFIABLE for the shared gateway-auth pool, which is a CDK
  construct in this stack and therefore carries CloudFormation's own tags plus
  ``Project``/``Environment``, but not ``AgentCoreStack``. Measured on the live pool
  ``us-east-1_qiYLOs3Ij``: tags were exactly ``Environment``, ``Project`` and the three
  ``aws:cloudformation:*`` keys. So in the DEFAULT ``shared`` identity mode a deployed
  agent could not resolve its gateway client secret at all, and exposed no gateway
  tools -- a green deploy with no working tool plane.
* The two step roles scoped bare ``userpool/*`` with NO condition, which let either
  role read the app-client secret of every Cognito pool in the account, including
  pools belonging to other products.

Neither would have been caught by a test that read only one role. So the grant lives
here, both call sites use it, and
``infra/tests/test_the_client_secret_grant_is_scoped_and_satisfiable.py`` asserts the
property over EVERY ``DescribeUserPoolClient`` statement in the synthesized template --
so a fourth role added later cannot reintroduce either mistake.

THE INVARIANT. Two statements, and every grant must be one of them:

1. The shared gateway-auth pool by EXACT ARN. Known at synth time (it is a construct in
   this stack), so nothing wider is justified.
2. ``userpool/*`` CONDITIONED on this STACK's owner tag, for the pools the gateway
   and mcp_server steps create at deploy time, whose ids cannot be known at synth. This
   is a STACK boundary, not a deployment or tenant boundary -- see WHO MAY USE THIS above;
   it is acceptable only for a role that is already stack-scoped rather than acting on
   behalf of one tenant. The value matches ``services/resource_ownership.stack_id()``
   exactly --
   ``{project}-{env}-{region}`` -- verified live: every Lambda in the stack resolves
   ``PROJECT_NAME``/``ENVIRONMENT``/``APP_AWS_REGION`` to ``acfe2e-p0920-us-east-1``,
   which is the value the gateway step stamps via ``owner_tags(region)`` at
   ``gateway_deployer.py:1469``.

WHAT NOT TO DO INSTEAD. Do not tag the shared pool with ``AgentCoreStack`` to make
statement 2 cover it. That key is the teardown OWNERSHIP MARKER --
``resource_ownership.is_owned_by_this_stack`` treats it as proof this stack created a
resource, and the shared pool is deliberately ``RETAIN``ed because it holds every
deployed gateway's app client and its hosted domain takes >381s to reprovision. Adding
the tag would make the one pool that must never be deleted look deletable.

ARCC: ``cnt_AGx9pUNpmdOVZB`` (scope an identity to its business function),
``cnt_1ZPqVzeASDHlO7`` / ``cnt_dwzZ05hLnqhYXQ`` (tight resource scoping),
``cnt_SFJJhkOueCPRkd`` (condition keys on a wildcard that cannot be removed),
``cnt_LuG2TKuO0errRp`` (grant access to the specific secret the principal needs).
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam

from .config import PlatformConfig

#: The action. Named once because it is easy to confuse with ``DescribeUserPool``, which
#: is a DIFFERENT IAM action -- for a long time only the pool one was granted, and the
#: client-secret read failed closed with AccessDeniedException on a green deploy.
ACTION = "cognito-idp:DescribeUserPoolClient"

#: The tag key teardown uses as proof of ownership. Must stay equal to
#: ``backend/src/app/services/resource_ownership.OWNER_TAG_KEY``.
OWNER_TAG_KEY = "AgentCoreStack"


def owner_tag_value(stack: cdk.Stack, cfg: PlatformConfig) -> str:
    """The ``AgentCoreStack`` value the backend stamps: ``{project}-{env}-{region}``.

    Mirrors ``services/resource_ownership.stack_id()``. If these two ever disagree the
    condition below silently matches nothing and every client-secret read fails closed,
    so the value is asserted against the backend function in
    ``test_the_client_secret_grant_is_scoped_and_satisfiable.py``.
    """
    return f"{cfg.project}-{cfg.env}-{stack.region}"


def owner_tag_condition_value(cfg: PlatformConfig) -> str:
    """The same value as a policy condition, for whichever region the request targets.

    A same-account deployment to a non-home region stamps its pool with
    ``{project}-{env}-{target_region}``, so a condition frozen to ``stack.region``
    denies the read even once the Resource ARN is region-wide (F-41). IAM resolves
    ``${aws:RequestedRegion}`` per request, which keeps the value exact: a home-region
    request still needs precisely ``owner_tag_value()``.
    """
    return f"{cfg.project}-{cfg.env}-${{aws:RequestedRegion}}"


def grant_client_secret_read(
    role: iam.IRole,
    stack: cdk.Stack,
    cfg: PlatformConfig,
    *,
    gateway_auth_pool: cognito.IUserPool | None,
) -> None:
    """Grant *role* the narrowest ``DescribeUserPoolClient`` that actually works.

    Args:
        gateway_auth_pool: the shared gateway-auth pool construct. When supplied, its
            exact ARN is granted. Passing ``None`` omits statement 1 entirely rather
            than widening statement 2 -- an absent pool must degrade to "cannot read
            the shared pool", never to "can read any pool".
    """
    if gateway_auth_pool is not None:
        # Statement 1: the shared pool, by exact ARN. This is the pool the default
        # `shared` identity mode uses, so this statement is the one that carries the
        # common path.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SharedGatewayAuthPoolClientSecret",
                actions=[ACTION],
                resources=[gateway_auth_pool.user_pool_arn],
            )
        )
    # Statement 2: pools minted at deploy time, whose ids cannot be known at synth.
    # ABAC on the owner tag is the scope. Read the value before reusing this: it is
    # `{project}-{env}-{region}`, so this authorizes reads of every pool THIS STACK
    # created -- which is every co-resident DEPLOYMENT's pool, not just the caller's.
    # That is why only the two step roles hold it. Verified against the IAM service
    # reference: DescribeUserPoolClient acts on the `userpool` resource type, which
    # supports aws:ResourceTag.
    role.add_to_policy(
        iam.PolicyStatement(
            sid="DeployTimePoolClientSecretByOwnerTag",
            actions=[ACTION],
            resources=[f"arn:aws:cognito-idp:*:{stack.account}:userpool/*"],
            conditions={"StringEquals": {f"aws:ResourceTag/{OWNER_TAG_KEY}": owner_tag_condition_value(cfg)}},
        )
    )
