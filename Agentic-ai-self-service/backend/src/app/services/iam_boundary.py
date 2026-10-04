"""The permissions boundary every IAM role this platform mints must carry (F-06).

The platform's Lambdas hold ``iam:CreateRole`` and ``iam:PutRolePolicy`` on ``role/AgentCore*``.
Without a permissions boundary, a role they create is bounded only by what the caller's own
identity policy lets them write onto it, and the step roles can write anything: a deploy that
mints a role is a privilege escalation path for whoever controls the canvas. Infra ships a
managed policy (``AgentCoreRoleBoundary``) and hands its ARN to every Lambda as
``AGENTCORE_ROLE_PERMISSIONS_BOUNDARY_ARN``; once every role carries it, infra can condition
``CreateRole`` / ``PutRolePolicy`` / ``AttachRolePolicy`` on ``iam:PermissionsBoundary`` so an
unbounded role can no longer be created or widened at all.

This module is the ONE place the backend reads that variable. Every ``create_role`` call passes
``**create_role_kwargs()``; every "already exists -> adopt" branch calls ``ensure_role_boundary``
AFTER it has proven the role is this deployment's (``assert_this_deployment_may_mutate``), so a
role created before the boundary existed is retrofitted on its next deploy and a foreign role is
never touched.

Unset or empty variable == today's behaviour: no boundary is sent and no retrofit happens, so the
backend can land before infra sets the variable and nothing fails closed prematurely. A set
variable is honoured strictly: a ``PutRolePermissionsBoundary`` that IAM refuses propagates
(fail closed) rather than leaving an unbounded role that enforcement would reject one call later
at ``PutRolePolicy`` with a less actionable message.

ARCC could not be queried when this was written (CLI fallback returned an auth failure); the
existing guidance recorded in ``ARCC_GUIDANCE.md`` (``cnt_dwzZ05hLnqhYXQ``: allow-by-default and
caller-identity-only authorization are anti-patterns) and standard least-privilege practice apply.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Set by infra on the deployment Lambda and every step Lambda (infra ledger F-06).
BOUNDARY_ENV_VAR = "AGENTCORE_ROLE_PERMISSIONS_BOUNDARY_ARN"

#: The stack's shared runtime roles (Bug 60) are every tenant's execution identity, and infra
#: explicitly denies every role-mutating verb on them, the boundary put included. Their names
#: come from the same env vars the runtime code uses, from the suffix convention the Bug-62
#: teardown guard recognises (``AgentCoreRuntime-{project}-{env}[-{region}]-shared`` /
#: ``-mcp-shared``; the region appears outside the platform's home region), and
#: from the pre-Bug-60 name. ``routers/permissions.py`` (JIT approve) refuses the same set.
SHARED_ROLE_ENV_VARS = ("SHARED_RUNTIME_ROLE_ARN", "SHARED_MCP_RUNTIME_ROLE_ARN")
SHARED_ROLE_SUFFIXES = ("-shared", "-mcp-shared")
LEGACY_SHARED_ROLE_NAMES = frozenset({"AgentCoreFlowsRuntimeRole"})


class SharedRuntimeRoleRefused(RuntimeError):
    """A role-mutating call was about to target a shared runtime role.

    Raised BEFORE the call: the shared role carries the stack's own tags, so the ownership proof
    every adopt branch runs says "ours", and a per-deploy role name can resolve to it
    (``iam_step``'s legacy path names roles ``AgentCoreRuntime-{runtime_name}``). Infra would deny
    the put anyway; this names the cause instead of surfacing an AccessDenied.
    """


def shared_runtime_role_names() -> set[str]:
    names = set(LEGACY_SHARED_ROLE_NAMES)
    for var in SHARED_ROLE_ENV_VARS:
        arn = os.environ.get(var, "").strip()
        if arn:
            names.add(arn.rsplit("/", 1)[-1])
    return names


def is_shared_runtime_role(role_name: str) -> bool:
    return role_name in shared_runtime_role_names() or role_name.endswith(SHARED_ROLE_SUFFIXES)


def boundary_arn() -> str | None:
    """The boundary ARN to apply, or ``None`` when the platform has not configured one.

    Whitespace-only counts as unset: a template that renders an empty parameter must not
    produce ``PermissionsBoundary=""``, which IAM rejects and which would fail every deploy.
    """
    value = os.environ.get(BOUNDARY_ENV_VAR, "").strip()
    return value or None


def create_role_kwargs() -> dict:
    """Extra keyword arguments for ``iam_client.create_role``: the boundary when configured."""
    arn = boundary_arn()
    return {"PermissionsBoundary": arn} if arn else {}


def current_boundary_of(role: dict | None) -> str | None:
    """The boundary ARN a ``GetRole`` response's ``Role`` currently carries, if any."""
    if not isinstance(role, dict):
        return None
    boundary = role.get("PermissionsBoundary")
    if not isinstance(boundary, dict):
        return None
    current = boundary.get("PermissionsBoundaryArn")
    return str(current) if current else None


def ensure_role_boundary(iam_client, role_name: str, *, role: dict | None = None) -> bool:
    """Retrofit the configured boundary onto an EXISTING role that lacks it.

    Callers pass the ``Role`` dict they already read (every adopt branch calls ``get_role`` to
    prove ownership), so this costs no extra read; without one it reads once. The put is skipped
    when the role already carries exactly this boundary. Returns whether a put was issued.

    Call this only after ownership has been proven: it mutates the role. A shared runtime role
    is refused by name before any call (``SharedRuntimeRoleRefused``); a put IAM refuses
    propagates unchanged and is never retried, because a missing grant is not transient.
    """
    arn = boundary_arn()
    if arn is None:
        return False
    if is_shared_runtime_role(role_name):
        raise SharedRuntimeRoleRefused(
            f"Refusing to put a permissions boundary on shared runtime role {role_name}: it is every "
            "tenant's execution identity and is never mutated by a deployment; a per-deploy role name "
            "must not resolve to it"
        )
    if role is None:
        role = iam_client.get_role(RoleName=role_name)["Role"]
    if current_boundary_of(role) == arn:
        return False
    iam_client.put_role_permissions_boundary(RoleName=role_name, PermissionsBoundary=arn)
    logger.info("Applied the platform permissions boundary to existing IAM role %s", role_name)
    return True
