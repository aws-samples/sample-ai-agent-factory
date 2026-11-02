"""Fail-closed authority boundary for AgentCore agent execution roles.

Runtime or Harness code may invoke its assigned data planes. It must never create, update, or
delete the Gateway/Policy control plane that governs those calls. A compromised
agent holding ``UpdateGateway`` could detach Cedar ``ENFORCE`` and then invoke tools
under the weakened configuration.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

FORBIDDEN_RUNTIME_CONTROL_ACTIONS = frozenset(
    {
        "bedrock-agentcore:CreateGateway",
        "bedrock-agentcore:UpdateGateway",
        "bedrock-agentcore:DeleteGateway",
        "bedrock-agentcore:CreateGatewayTarget",
        "bedrock-agentcore:UpdateGatewayTarget",
        "bedrock-agentcore:DeleteGatewayTarget",
        "bedrock-agentcore:SynchronizeGatewayTargets",
        "bedrock-agentcore:CreatePolicyEngine",
        "bedrock-agentcore:UpdatePolicyEngine",
        "bedrock-agentcore:DeletePolicyEngine",
        "bedrock-agentcore:CreatePolicy",
        "bedrock-agentcore:UpdatePolicy",
        "bedrock-agentcore:DeletePolicy",
        "bedrock-agentcore:ManageResourceScopedPolicy",
        "bedrock-agentcore:ManageAdminPolicy",
    }
)


class RuntimePolicyEscalation(ValueError):
    """A Runtime role policy can mutate the control plane that governs it."""


def runtime_control_plane_deny_statement() -> dict[str, Any]:
    """Return a fresh explicit Deny for Gateway and Policy mutations."""

    return {
        "Sid": "NeverMutateGatewayOrPolicyControlPlane",
        "Effect": "Deny",
        "Action": sorted(FORBIDDEN_RUNTIME_CONTROL_ACTIONS),
        "Resource": "*",
    }


def _actions(statement: dict[str, Any]) -> list[str]:
    raw = statement.get("Action") or []
    values = [raw] if isinstance(raw, str) else raw
    return [str(value) for value in values if isinstance(value, str)]


def assert_runtime_policy_is_data_plane_only(policy: dict[str, Any]) -> dict[str, Any]:
    """Return *policy* or reject any Allow pattern matching a forbidden action."""

    violations: set[str] = set()
    for statement in policy.get("Statement") or []:
        if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
            continue
        for granted in _actions(statement):
            pattern = granted.lower()
            for forbidden in FORBIDDEN_RUNTIME_CONTROL_ACTIONS:
                if fnmatchcase(forbidden.lower(), pattern):
                    violations.add(forbidden)
    if violations:
        names = ", ".join(sorted(violations))
        raise RuntimePolicyEscalation(
            "AgentCore agent execution roles cannot mutate Gateway or Policy "
            f"control-plane resources; refused actions: {names}"
        )
    return policy
