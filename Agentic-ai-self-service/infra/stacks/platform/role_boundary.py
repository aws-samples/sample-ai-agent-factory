"""The permissions boundary for every IAM role the platform's Lambdas mint (F-06).

Seven step roles and the deployment Lambda hold ``iam:CreateRole`` + ``iam:PutRolePolicy`` +
``iam:UpdateAssumeRolePolicy`` on ``role/AgentCore*``. Without a boundary, code running as any
of them -- the knowledge-base step parses customer-supplied KB config, codegen runs model-written
code one hop away -- can mint ``AgentCoreX`` trusting a foreign account and grant it ``*:*``. The
only IAM condition key that bounds what a CREATED role may ever do is ``iam:PermissionsBoundary``
(Service Reference feed: present on CreateRole, PutRolePolicy, AttachRolePolicy and
UpdateAssumeRolePolicy), and it can only be required if a boundary policy exists to name.

This module builds that policy and publishes its ARN to every Lambda as
``AGENTCORE_ROLE_PERMISSIONS_BOUNDARY_ARN``. The policy is a CAP, not a grant: a role's
effective permissions are the intersection of its own policies and this document, so the
allow-list below only needs to be a SUPERSET of what the backend writes into the roles it
creates. It is derived from the inline policies the backend actually puts on created roles
(runtime_deployer.create_runtime_iam_role, per_agent_identity.build_scoped_runtime_policy,
harness_deployer.create_harness_iam_role, gateway_deployer._build_gateway_role_policy and
_ensure_lambda_role, knowledge_base_step._put_kb_role_policy, memory_step, evaluation_step,
tool_tester) plus the two AWS-managed policies ATTACHABLE_MANAGED_POLICIES admits. The test
``infra/tests/test_f06_role_permissions_boundary.py`` re-derives that set from the backend
source so a new namespace in a created role's policy fails the build here instead of failing a
live deploy with AccessDenied.

The explicit Deny is the point: no created role may ever manage IAM, assume another role,
touch Organizations/Account, or drive CloudFormation, whatever its identity policy says.

ENFORCEMENT IS A SEPARATE SWITCH, and it defaults OFF. Requiring the boundary at CreateRole /
PutRolePolicy / UpdateAssumeRolePolicy / AttachRolePolicy means every ``iam_client.create_role``
in the backend must pass ``PermissionsBoundary=<this ARN>`` and every existing role must be
retrofitted with ``put_role_permissions_boundary``; until then the condition would fail-close
every deploy. The switch is the CDK context key ``enforce_role_permissions_boundary`` (snake_case,
like every other context key here -- a camelCase ``-c`` key is silently ignored). The policy and
the env var are unconditional so the backend can start passing the ARN before the switch is
flipped. ARCC cnt_SFJJhkOueCPRkd: condition keys on high-privilege IAM actions; ARCC
cnt_dwzZ05hLnqhYXQ: a shared execution role reaching IAM is the privilege-escalation
anti-pattern this caps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam

from .config import PlatformConfig

#: Environment variable carrying the boundary ARN into every platform Lambda.
BOUNDARY_ARN_ENV = "AGENTCORE_ROLE_PERMISSIONS_BOUNDARY_ARN"

#: CDK context key that turns the ``iam:PermissionsBoundary`` conditions on. Default off; see
#: the module docstring for what has to be true in the backend before it can be flipped.
ENFORCE_CONTEXT_KEY = "enforce_role_permissions_boundary"

#: Services a CREATED role may itself pass a role to. The gateway role policy carries
#: ``iam:PassRole`` (gateway_deployer._build_gateway_role_policy) for its Lambda targets.
PASS_ROLE_SERVICES: tuple[str, ...] = (
    "bedrock-agentcore.amazonaws.com",
    "lambda.amazonaws.com",
    "bedrock.amazonaws.com",
)

#: The cap. Namespace wildcards where the backend itself writes a wildcard into created roles
#: (``bedrock-agentcore:*`` on memory roles); exact verbs where it writes exact verbs. Anything
#: not listed is outside the cap even if a role's own policy grants it.
BOUNDARY_ALLOWED_ACTIONS: tuple[str, ...] = (
    # model + AgentCore planes (runtime, memory, gateway, harness, evaluation, policy roles)
    "bedrock:*",
    "bedrock-agentcore:*",
    # runtime/tool logging, evaluation log queries, AWSLambdaBasicExecutionRole
    "logs:*",
    # staged code read, KB source buckets, KB transform buckets
    "s3:*",
    "s3vectors:*",
    "aoss:*",
    "rds-data:*",
    # deployment-bound credential reads; the gateway role's credential-provider secrets
    "secretsmanager:CreateSecret",
    "secretsmanager:GetSecretValue",
    "secretsmanager:PutSecretValue",
    "secretsmanager:DeleteSecret",
    "secretsmanager:DescribeSecret",
    # evaluation roles read traces/signals; tool functions may emit X-Ray segments
    "xray:*",
    "application-signals:*",
    # gateway roles invoke their tool Lambdas
    "lambda:InvokeFunction",
    # a Cognito-gateway agent resolves its client secret from its one pool
    "cognito-idp:DescribeUserPoolClient",
    # AWSLambdaVPCAccessExecutionRole (tool sandbox role)
    "ec2:CreateNetworkInterface",
    "ec2:DescribeNetworkInterfaces",
    "ec2:DescribeSubnets",
    "ec2:DeleteNetworkInterface",
    "ec2:AssignPrivateIpAddresses",
    "ec2:UnassignPrivateIpAddresses",
    # encrypted buckets/secrets/log groups behind account KMS keys
    "kms:Decrypt",
    "kms:DescribeKey",
    "kms:Encrypt",
    "kms:GenerateDataKey",
    "kms:GenerateDataKeyWithoutPlaintext",
    "kms:ReEncryptFrom",
    "kms:ReEncryptTo",
    "cloudwatch:PutMetricData",
    "sts:GetCallerIdentity",
)

#: Denied whatever a created role's own policy says. IAM management (every verb but PassRole,
#: which is allow-listed above under a PassedToService condition), role assumption, and the
#: account-shaping services.
BOUNDARY_DENIED_ACTIONS: tuple[str, ...] = (
    "iam:Add*",
    "iam:Attach*",
    "iam:Change*",
    "iam:Create*",
    "iam:Deactivate*",
    "iam:Delete*",
    "iam:Detach*",
    "iam:Enable*",
    "iam:Put*",
    "iam:Remove*",
    "iam:Reset*",
    "iam:Resync*",
    "iam:Set*",
    "iam:Tag*",
    "iam:Untag*",
    "iam:Update*",
    "iam:Upload*",
    "sts:AssumeRole",
    "sts:AssumeRoleWithSAML",
    "sts:AssumeRoleWithWebIdentity",
    "organizations:*",
    "account:*",
    "cloudformation:*",
)


def boundary_enforced(stack: cdk.Stack) -> bool:
    """Whether the ``iam:PermissionsBoundary`` conditions are emitted. Off unless the context
    key is a truthy string; a missing key is the documented default, not an error."""
    raw = stack.node.try_get_context(ENFORCE_CONTEXT_KEY)
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def build_role_permissions_boundary(stack: cdk.Stack, cfg: PlatformConfig) -> iam.ManagedPolicy:
    """The customer-managed boundary policy. Account-global name so two regions coexist."""
    return iam.ManagedPolicy(
        stack,
        "AgentCoreRoleBoundary",
        managed_policy_name=cfg.global_resource_name(stack, "agentcore-role-boundary"),
        description=(
            "Permissions boundary for every IAM role minted by the AgentCore Flows platform "
            "(runtime, gateway, memory, harness, evaluation, KB, tool roles). A cap, not a grant."
        ),
        statements=[
            iam.PolicyStatement(
                sid="CapCreatedRoles",
                effect=iam.Effect.ALLOW,
                actions=list(BOUNDARY_ALLOWED_ACTIONS),
                resources=["*"],
            ),
            iam.PolicyStatement(
                sid="CreatedRolesPassOnlyToKnownServices",
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[f"arn:aws:iam::{stack.account}:role/AgentCore*"],
                conditions={"StringEquals": {"iam:PassedToService": list(PASS_ROLE_SERVICES)}},
            ),
            iam.PolicyStatement(
                sid="CreatedRolesNeverManageIdentity",
                effect=iam.Effect.DENY,
                actions=list(BOUNDARY_DENIED_ACTIONS),
                resources=["*"],
            ),
        ],
    )


def grant_boundary_retrofit(
    role: iam.IRole,
    stack: cdk.Stack,
    boundary: iam.ManagedPolicy,
    *,
    resources: list[str],
) -> None:
    """``iam:PutRolePermissionsBoundary`` on ``resources``, pinned to THIS boundary and nothing else.

    The backend's adopt branches (every "role already exists" path in the F-06 call-site list)
    call ``put_role_permissions_boundary(RoleName=..., PermissionsBoundary=<this ARN>)`` so roles
    minted before the boundary existed carry it before enforcement is switched on. Granted only
    to the roles that hold ``iam:CreateRole`` -- the same principals that mint -- and only for
    the platform boundary: PutRolePermissionsBoundary REPLACES a role's boundary, so a grant
    without the condition is "swap any AgentCore* role's cap for an empty one", which is the
    F-06 hole reopened. The condition is unconditional (not behind the enforcement switch)
    because ``iam:PermissionsBoundary`` is always present in this verb's request context (it is
    the boundary being set; Service Reference feed), so it never fails closed and there is no
    legitimate call with another value. ``iam:DeleteRolePermissionsBoundary`` is granted nowhere.
    The two shared runtime roles are excluded by the explicit Deny in shared_role_guard.py.
    ARCC cnt_SFJJhkOueCPRkd (condition keys on high-privilege IAM actions).
    """
    role.add_to_principal_policy(
        iam.PolicyStatement(
            sid="RetrofitOnlyThePlatformBoundary",
            actions=["iam:PutRolePermissionsBoundary"],
            resources=resources,
            conditions={"StringEquals": {"iam:PermissionsBoundary": boundary.managed_policy_arn}},
        )
    )


def boundary_conditions(stack: cdk.Stack, boundary: iam.ManagedPolicy) -> dict | None:
    """The condition block to put on CreateRole/PutRolePolicy/UpdateAssumeRolePolicy/
    AttachRolePolicy grants, or ``None`` while enforcement is off.

    Callers must put these actions in a statement of their own: ``iam:PermissionsBoundary`` is
    absent from the request context of GetRole, DeleteRole, PassRole and the List* verbs, and
    a StringEquals against an absent key evaluates false -- conditioning a mixed statement
    would deny every read and delete in it (the fail-closed outage, not a tightening).
    """
    if not boundary_enforced(stack):
        return None
    return {"StringEquals": {"iam:PermissionsBoundary": boundary.managed_policy_arn}}
