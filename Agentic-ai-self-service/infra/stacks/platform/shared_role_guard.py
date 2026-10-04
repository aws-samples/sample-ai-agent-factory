"""No platform Lambda may change what the SHARED runtime roles are allowed to do (F-05).

``AgentCoreRuntime-{project}-{env}-shared`` is the execution role every agent deployed in the
default ``shared`` identity mode runs as, and ``...-mcp-shared`` is its model-free sibling.
Both match every ``role/AgentCore*`` grant in this stack, so the deployment Lambda's JIT
approver (``iam:PutRolePolicy``) and the seven step roles' ``iam:PutRolePolicy`` +
``iam:UpdateAssumeRolePolicy`` could rewrite the role every tenant shares -- the review's
chain: put ``{"Action":"*","Resource":"*"}`` on the shared role, then run as it.

The backend already refuses this in code (the Bug-62 guard skips the shared role by name in
iam_step and destroy_runtime) and the JIT router only rejects ``iam:``/``sts:``/
``organizations:``/``account:`` prefixes. A code guard is not an authorization boundary; this
explicit Deny is. It names the two roles exactly AND by the ``-shared`` naming convention, so
another stack's shared roles in the same account are covered too (the review's F-06 note).

Per-agent roles are ``AgentCoreRuntime-{sanitized agent name}`` and the backend's own teardown
guard already treats any name ending in ``-shared`` as stack-owned and never deletes it, so the
pattern below adds no new refusal the product does not already make. ARCC cnt_dwzZ05hLnqhYXQ:
a tenant-facing shared role that its control plane can widen is the "modifying security
configurations" anti-pattern; ARCC cnt_SFJJhkOueCPRkd: explicit Deny is the right shape for a
high-privilege IAM action that must never reach one resource.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam

#: Every verb that changes a role's authority, trust, lifecycle or the tags its gates read.
SHARED_ROLE_MUTATING_ACTIONS: tuple[str, ...] = (
    "iam:PutRolePolicy",
    "iam:AttachRolePolicy",
    "iam:DeleteRolePolicy",
    "iam:DetachRolePolicy",
    "iam:UpdateAssumeRolePolicy",
    "iam:UpdateRole",
    "iam:UpdateRoleDescription",
    "iam:DeleteRole",
    "iam:PutRolePermissionsBoundary",
    "iam:DeleteRolePermissionsBoundary",
    "iam:TagRole",
    "iam:UntagRole",
)

#: Name shapes of every stack-managed shared runtime role in this account: the model-capable
#: ``-shared`` and the model-free ``-mcp-shared`` (the first pattern also matches the second).
SHARED_ROLE_NAME_PATTERNS: tuple[str, ...] = (
    "role/AgentCoreRuntime-*-shared",
    "role/AgentCoreRuntime-*-mcp-shared",
)


def deny_mutating_the_shared_runtime_roles(
    role: iam.Role,
    stack: cdk.Stack,
    *,
    shared_runtime_role: iam.Role,
    shared_mcp_runtime_role: iam.Role,
) -> None:
    """Attach the Deny to *role*. Exact ARNs first so a rename of the convention still pins
    this stack's own two roles; the patterns cover every other stack in the account."""
    role.add_to_policy(
        iam.PolicyStatement(
            sid="NeverWidenTheSharedRuntimeRoles",
            effect=iam.Effect.DENY,
            actions=list(SHARED_ROLE_MUTATING_ACTIONS),
            resources=[
                shared_runtime_role.role_arn,
                shared_mcp_runtime_role.role_arn,
                *[f"arn:aws:iam::{stack.account}:{pattern}" for pattern in SHARED_ROLE_NAME_PATTERNS],
            ],
        )
    )
