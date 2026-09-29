"""Per-agent identity — least-privilege IAM execution-role policy builder.

Gap P3.3B. This module is a *pure-function* policy builder used by
``step_handlers/iam_step.py`` ONLY when the canvas Identity node sets
``mode == 'per_agent'``. The Bug-60 shared runtime role remains the default
and is 100% unchanged — per-agent is strictly opt-in.

Design goals
------------
* **Least privilege.** The returned IAM policy carries ONLY the statements the
  runtime's wired resources actually require. A runtime with no connected
  tools gets exactly the three irreducible baseline statements (Bedrock model
  access, S3 code access, CloudWatch Logs) and nothing else.
* **ARN scoping.** Where a wired resource exposes an ARN (gateway, memory,
  knowledge base, OTEL secret), the statement's ``Resource`` is scoped to that
  ARN — not ``"*"``. When the caller can't supply the ARN (e.g. an id was
  missing on the SFN event) the statement falls back to ``"*"`` for that ONE
  tool only, so a misconfigured deploy still works rather than silently
  denying the runtime access to its own resource.
* **Stays in sync with the shared role.** The per-tool action lists mirror
  ``runtime_deployer.create_runtime_iam_role`` so a ``per_agent`` runtime never
  silently loses a capability the shared role would have granted (see the
  cross-tool ACL-drift test in tests/test_per_agent_identity.py).

The two residual broad grants are intentional and documented:
* ``BedrockModelAccess`` uses ``Resource "*"`` — model inference-profile ARNs
  are dynamic per region/account, the irreducible AWS shape (same justification
  as the shared role and ``create_runtime_iam_role``).
* ``CloudWatchLogs`` uses ``Resource "*"`` — log-group ARNs are minted
  dynamically by the runtime.

No boto3, no I/O — fully unit-testable.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Trust policy — bedrock-agentcore assumes the per-agent execution role.
# Mirrors runtime_deployer.create_runtime_iam_role's trust_policy.
# ---------------------------------------------------------------------------

BEDROCK_AGENTCORE_TRUST_POLICY: dict = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


def build_trust_policy() -> dict:
    """Return a fresh copy of the bedrock-agentcore AssumeRole trust policy.

    Returns a deep-ish copy so callers can mutate without affecting the
    module-level constant.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Role-name convention — single source of truth shared by iam_step (mint) and
# runtime_deployer.destroy_runtime (cleanup). Both must agree on this exact
# format or per-agent roles leak on delete (Bug 25/124 cascade).
# ---------------------------------------------------------------------------

# IAM role names: up to 64 chars, [\w+=,.@-]. We sanitize the runtime name to
# the AgentCore-permitted alphabet (letters/digits/underscore) — already done
# upstream by sanitize_runtime_name — and prefix with the convention.
_ROLE_NAME_PREFIX = "AgentCoreRuntime-"
_IAM_ROLE_NAME_CHARSET = re.compile(r"[^A-Za-z0-9+=,.@_-]")


def build_per_agent_role_name(agentcore_runtime_name: str) -> str:
    """Return the IAM role name for a per-agent runtime: ``AgentCoreRuntime-{name}``.

    This is the SAME convention ``runtime_deployer.destroy_runtime`` derives via
    its ``AgentCoreRuntime-{name_for_role}`` candidate, so per-agent roles are
    cleaned up on delete and are NOT wrongly skipped by the Bug-62 shared-role
    guard (which only skips names == shared_role_name or ending in ``-shared``).

    The result is sanitized to the IAM role-name charset and truncated to the
    64-char IAM limit (prefix included).
    """
    name = agentcore_runtime_name or "agent_default"
    # Strip anything outside the IAM role-name charset (defense in depth — the
    # name is normally already AgentCore-sanitized to [a-z0-9_]).
    name = _IAM_ROLE_NAME_CHARSET.sub("_", name)
    role_name = f"{_ROLE_NAME_PREFIX}{name}"
    if len(role_name) > 64:
        role_name = role_name[:64]
    return role_name


# ---------------------------------------------------------------------------
# Per-tool statement builders. Each returns ONE statement scoped to the wired
# resource's ARN when supplied, else "*" (fallback for that one tool only).
# Action lists mirror runtime_deployer.create_runtime_iam_role.
# ---------------------------------------------------------------------------


def _scoped_resource(arn: str | None) -> object:
    """Return ``arn`` (as a single-element list) if supplied, else ``"*"``."""
    return [arn] if arn else "*"


def _gateway_statement(gateway_arn: str | None) -> dict:
    return {
        "Sid": "GatewayAccess",
        "Effect": "Allow",
        "Action": [
            "bedrock-agentcore:InvokeGateway",
            "bedrock-agentcore:ListGateways",
            "bedrock-agentcore:GetGateway",
        ],
        "Resource": _scoped_resource(gateway_arn),
    }


# ---------------------------------------------------------------------------
# The AgentCore IAM prefix
# ---------------------------------------------------------------------------
#
# Every AgentCore action below is prefixed ``bedrock-agentcore:``. There is no
# ``bedrock-agentcore-control:`` IAM prefix, and no ``agent-credential-provider:``
# service at all. Both mistakes were in this file.
#
# ``bedrock-agentcore`` and ``bedrock-agentcore-control`` are both real API
# endpoints with their own botocore client, so a control-plane prefix reads as
# correct -- but an IAM service prefix is the SigV4 *signing name*, not the endpoint
# prefix, and both clients sign as ``bedrock-agentcore``. One namespace, both planes.
#
# This fails silently, which is why it survived. IAM accepts a policy naming a
# service that does not exist: no error on PutRolePolicy, no error at deploy, and
# the statement authorizes nothing. The first symptom is an AccessDeniedException at
# runtime on a call whose permission is visibly granted. The policy-engine statement
# here was void in its entirety.
#
# Verified with two oracles: botocore's service models settle the prefix
# (``signingName``), and IAM Access Analyzer settles individual actions, because IAM
# actions do not map 1:1 to API operations -- AuthorizeAction,
# PartiallyAuthorizeActions and InvokeGateway are real IAM actions with no SDK
# operation. Access Analyzer reports the prefix error as INVALID_SERVICE_IN_ACTION.
# The same rule is enforced over the exported CloudFormation template by
# tests/test_cfn_export_contract.py::TestEmittedActionsAreRealIamActions.


def _memory_statement(memory_arn: str | None) -> dict:
    return {
        "Sid": "MemoryAccess",
        "Effect": "Allow",
        "Action": [
            "bedrock-agentcore:CreateEvent",
            # RetrieveMemoryRecords, not RetrieveMemories, and there is no
            # GetLastKTurns action at all -- turn history is read through
            # ListEvents. Both were inert. See the prefix note below.
            "bedrock-agentcore:RetrieveMemoryRecords",
            "bedrock-agentcore:ListSessions",
            "bedrock-agentcore:ListActors",
            "bedrock-agentcore:ListEvents",
            "bedrock-agentcore:GetMemory",
            "bedrock-agentcore:ListMemories",
        ],
        "Resource": _scoped_resource(memory_arn),
    }


def _knowledge_base_statement(kb_arn: str | None) -> dict:
    return {
        "Sid": "KnowledgeBaseAccess",
        "Effect": "Allow",
        "Action": [
            "bedrock:Retrieve",
            "bedrock:RetrieveAndGenerate",
        ],
        "Resource": _scoped_resource(kb_arn),
    }


def _guardrails_statement() -> dict:
    # Guardrail ARNs are not wired into the iam_step event (no guardrail_result
    # ARN exposed); the shared role uses "*" here too.
    return {
        "Sid": "GuardrailsAccess",
        "Effect": "Allow",
        "Action": ["bedrock:ApplyGuardrail", "bedrock:GetGuardrail"],
        "Resource": "*",
    }


def _browser_statement() -> dict:
    return {
        "Sid": "BrowserAccess",
        "Effect": "Allow",
        "Action": ["bedrock-agentcore:*Browser*"],
        "Resource": "*",
    }


def _code_interpreter_statement() -> dict:
    return {
        "Sid": "CodeInterpreterAccess",
        "Effect": "Allow",
        "Action": ["bedrock-agentcore:*CodeInterpreter*"],
        "Resource": "*",
    }


def _evaluation_statement() -> dict:
    return {
        "Sid": "EvaluationAccess",
        "Effect": "Allow",
        "Action": [
            "bedrock-agentcore:Evaluate",
            "bedrock-agentcore:CreateOnlineEvaluationConfig",
            "bedrock-agentcore:GetOnlineEvaluationConfig",
            "bedrock-agentcore:ListOnlineEvaluationConfigs",
            "bedrock-agentcore:ListEvaluators",
            "bedrock-agentcore:GetEvaluator",
            "logs:StartQuery",
            "logs:GetQueryResults",
        ],
        "Resource": "*",
    }


def _policy_statement() -> dict:
    return {
        "Sid": "PolicyAccess",
        "Effect": "Allow",
        "Action": [
            "bedrock-agentcore:CreatePolicyEngine",
            "bedrock-agentcore:GetPolicyEngine",
            "bedrock-agentcore:ListPolicyEngines",
            "bedrock-agentcore:CreatePolicy",
            "bedrock-agentcore:GetPolicy",
            "bedrock-agentcore:ListPolicies",
            "bedrock-agentcore:UpdateGateway",
        ],
        "Resource": "*",
    }


# Map a connected-tool key -> a builder. Builders that scope to an ARN accept
# the relevant arn; the rest ignore their argument. Keeping this dict keyed on
# the SAME tool strings as runtime_deployer.create_runtime_iam_role makes the
# cross-tool ACL-drift test trivial.
_TOOL_BUILDERS = {
    "gateway": lambda arns: _gateway_statement(arns.get("gateway_arn")),
    "memory": lambda arns: _memory_statement(arns.get("memory_arn")),
    "knowledge_base": lambda arns: _knowledge_base_statement(arns.get("kb_arn")),
    "guardrails": lambda arns: _guardrails_statement(),
    "browser": lambda arns: _browser_statement(),
    "code_interpreter": lambda arns: _code_interpreter_statement(),
    "evaluation": lambda arns: _evaluation_statement(),
    "observability": lambda arns: _evaluation_statement(),
    "policy": lambda arns: _policy_statement(),
}


def build_scoped_runtime_policy(
    connected_tools: list | None,
    kb_arn: str | None = None,
    gateway_arn: str | None = None,
    memory_arn: str | None = None,
    otel_secret_arn: str | None = None,
    artifacts_bucket: str | None = None,
) -> dict:
    """Build a least-privilege IAM policy document for a per-agent runtime role.

    Args:
        connected_tools: tool keys wired on the canvas (gateway, memory,
            knowledge_base, guardrails, browser, code_interpreter, evaluation,
            observability, policy). Unknown keys are ignored.
        kb_arn: knowledge-base ARN to scope ``knowledge_base`` to. ``"*"``
            fallback if omitted.
        gateway_arn: gateway ARN to scope ``gateway`` to. ``"*"`` fallback.
        memory_arn: memory ARN to scope ``memory`` to. ``"*"`` fallback.
        otel_secret_arn: OTEL OTLP auth-header secret ARN. When supplied, adds a
            scoped ``secretsmanager:GetSecretValue`` statement (no statement at
            all when omitted).
        artifacts_bucket: S3 artifacts bucket name. Scopes ``S3CodeAccess`` to
            that bucket; ``"*"`` fallback only if the name is unavailable
            (mirrors runtime_deployer's ``s3_resources``).

    Returns:
        A deterministic IAM policy document (dict) with the three baseline
        statements first, then one statement per wired tool (in the order the
        tools were supplied, deduplicated), then the OTEL secret statement.
    """
    # ----- Baseline (always present) -------------------------------------
    if artifacts_bucket:
        s3_resources: object = [
            f"arn:aws:s3:::{artifacts_bucket}",
            f"arn:aws:s3:::{artifacts_bucket}/*",
        ]
    else:
        s3_resources = "*"  # fallback only when bucket name unavailable

    statements: list[dict] = [
        {
            "Sid": "BedrockModelAccess",
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ],
            # Intentional "*": inference-profile ARNs are dynamic per
            # region/account — same justification as the shared role.
            "Resource": "*",
        },
        {
            "Sid": "S3CodeAccess",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:ListBucket"],
            "Resource": s3_resources,
        },
        {
            "Sid": "CloudWatchLogs",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
            ],
            # Intentional "*": log-group ARNs are minted dynamically by the
            # runtime.
            "Resource": "*",
        },
    ]

    # ----- Per-tool statements (least privilege) -------------------------
    arns = {
        "gateway_arn": gateway_arn,
        "memory_arn": memory_arn,
        "kb_arn": kb_arn,
    }
    seen: set[str] = set()
    for tool in connected_tools or []:
        if tool in seen:
            continue
        seen.add(tool)
        builder = _TOOL_BUILDERS.get(tool)
        if builder is None:
            continue  # unknown tool — grant nothing
        statements.append(builder(arns))

    # ----- OTEL auth-header secret (scoped) ------------------------------
    if otel_secret_arn:
        statements.append(
            {
                "Sid": "OtelAuthHeaderSecret",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [otel_secret_arn],
            }
        )

    return {"Version": "2012-10-17", "Statement": statements}
