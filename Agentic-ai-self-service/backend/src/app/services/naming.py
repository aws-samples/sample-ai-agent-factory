"""Shared AgentCore resource-name sanitization.

AgentCore resource names follow ONE OF TWO regexes depending on the resource:

* UNDERSCORE style ``[a-zA-Z][a-zA-Z0-9_]{0,MAX}`` — Runtime, Harness, Memory.
  Must start with a letter; only letters/digits/underscore; no hyphens.
* HYPHEN style ``[0-9a-zA-Z]([-]?[0-9a-zA-Z])*`` — Gateway, Cognito-derived names.
  Letters/digits/hyphens; no underscores; no leading/trailing/double hyphens.

Before this module, ~5 slightly-different sanitizers lived across deployment.py,
runtime_deployer.py, harness_deployer.py, gateway_deployer.py, and (raw, unsanitized)
memory_step.py — which is how a user-typed name like "My Memory" reached CreateMemory
and hard-failed a deploy (Bug 155). This is the single source of truth; callers pass
the style that matches the target service.
"""

from __future__ import annotations

import hashlib
import os
import re

# Max length is service-specific; these are the conservative documented caps.
MAX_UNDERSCORE = 48  # runtime/harness names cap at 48; memory allows 48 too
MAX_HYPHEN = 48  # gateway names cap at 48
MAX_IAM_ROLE_NAME = 64

_INVALID_IAM_ROLE_CHAR = re.compile(r"[^A-Za-z0-9+=,.@_-]")


def sanitize_agentcore_name(
    name: str | None,
    *,
    style: str = "underscore",
    max_len: int | None = None,
    fallback: str = "agentcore",
    prefix: str = "r",
) -> str:
    """Return a name valid for the given AgentCore *style*.

    Args:
        name: the raw, possibly user-typed name (may be None/empty).
        style: ``"underscore"`` (Runtime/Harness/Memory) or ``"hyphen"`` (Gateway).
        max_len: override the default length cap for the style.
        fallback: value used when *name* sanitizes to empty.
        prefix: prepended (with the style's separator) when the sanitized name
            does not start with a letter, to satisfy the leading-letter rule.

    The result is guaranteed to match the target regex.
    """
    raw = (name or "").strip()

    if style == "hyphen":
        cap = max_len or MAX_HYPHEN
        s = re.sub(r"[^a-zA-Z0-9-]", "-", raw)
        s = re.sub(r"-{2,}", "-", s).strip("-")[:cap].strip("-")
        if not s:
            s = fallback
        if not s[0].isalnum():
            s = (f"{prefix}-{s}")[:cap].strip("-")
        return s or fallback

    # underscore style (default)
    cap = max_len or MAX_UNDERSCORE
    s = re.sub(r"[^a-zA-Z0-9_]", "_", raw)[:cap]
    if not s or not s[0].isalpha():
        s = (f"{prefix}_{s}")[:cap]
    return s or fallback


def is_valid_agentcore_name(name: str, *, style: str = "underscore") -> bool:
    """True when *name* already satisfies the target regex (for shift-left 422s)."""
    if not name:
        return False
    if style == "hyphen":
        return bool(re.fullmatch(r"[0-9a-zA-Z]([-]?[0-9a-zA-Z])*", name)) and len(name) <= MAX_HYPHEN
    return bool(re.fullmatch(rf"[a-zA-Z][a-zA-Z0-9_]{{0,{MAX_UNDERSCORE - 1}}}", name))


#: Every Lambda function and execution role this platform creates for a gateway's tools
#: carries the owning stack in its NAME, not only in its tags (F-7d).
FUNCTION_NAME_PREFIX = "AgentCore"
MAX_LAMBDA_FUNCTION_NAME = 64
STACK_SCOPE_TOKEN_LEN = 10


def stack_scope_token(stack_identity: str) -> str:
    """A short, deterministic token for one stack in one region.

    ``stack_identity`` is ``resource_ownership.stack_id(region)`` (``{project}-{env}-{region}``),
    passed in rather than imported so this module stays dependency-free. Ten hex characters of
    its SHA-256: bounded, so every derived name fits Lambda's and IAM's 64-character limits
    with room for the resource's own suffix; deterministic, so the infra computes the same
    token from project/env/region and can grant this stack exactly its own prefix and
    nothing else. Forty bits is collision-resistant for the population it has to separate
    (the stacks in one AWS account: a birthday collision needs on the order of a million
    of them), not collision-proof -- so nothing downstream TRUSTS the token as proof of
    ownership. It only makes a collision rare; the ownership tag still decides, and a
    colliding name whose tags are not this stack's is refused, never adopted.
    """
    if not stack_identity or not stack_identity.strip():
        raise ValueError("a stack scope token needs the stack identity; an empty one would be shared by every stack")
    return hashlib.sha256(stack_identity.strip().encode()).hexdigest()[:STACK_SCOPE_TOKEN_LEN]


def scoped_function_name(kind: str, stack_identity: str, suffix: str = "") -> str:
    """``AgentCore-{token}-{kind}[-{suffix}]`` for a platform-created tool Lambda.

    ``kind`` is a fixed word (``DynamicTools``, ``CustomerSupportTools``, ``KBTool``,
    ``CustomTool``); ``suffix`` is the per-resource part. The suffix is clipped, never the
    prefix, so the stack token is always intact and the infra's ``AgentCore-{token}-*`` grant
    always matches.
    """
    token = stack_scope_token(stack_identity)
    safe_kind = re.sub(r"[^A-Za-z0-9]", "", kind or "")
    if not safe_kind:
        raise ValueError("a scoped function name needs a kind")
    head = f"{FUNCTION_NAME_PREFIX}-{token}-{safe_kind}"
    safe_suffix = re.sub(r"[^A-Za-z0-9_-]", "-", suffix or "").strip("-")
    if not safe_suffix:
        return head
    room = MAX_LAMBDA_FUNCTION_NAME - len(head) - 1
    return f"{head}-{safe_suffix[:room]}".rstrip("-")


def scoped_role_name(kind: str, stack_identity: str, suffix: str = "") -> str:
    """The execution role for :func:`scoped_function_name`: ``AgentCore-{token}-{kind}Role[-{suffix}]``.

    Account-global like every IAM role name, and stack-scoped through the same token, so the
    role can never be a stale unowned singleton from another install. Digest-clipped like
    :func:`regional_iam_role_name` when the suffix would push it past 64 characters.
    """
    token = stack_scope_token(stack_identity)
    safe_kind = re.sub(r"[^A-Za-z0-9]", "", kind or "")
    if not safe_kind:
        raise ValueError("a scoped role name needs a kind")
    head = f"{FUNCTION_NAME_PREFIX}-{token}-{safe_kind}Role"
    safe_suffix = _INVALID_IAM_ROLE_CHAR.sub("-", suffix or "").strip("-")
    if not safe_suffix:
        return head
    candidate = f"{head}-{safe_suffix}"
    if len(candidate) <= MAX_IAM_ROLE_NAME:
        return candidate
    digest = hashlib.sha256(candidate.encode()).hexdigest()[:8]
    return f"{head}-{safe_suffix[: MAX_IAM_ROLE_NAME - len(head) - len(digest) - 2]}-{digest}"


DEPLOYMENT_SCOPE_SUFFIX_LEN = 12


def deployment_scope_suffix(deployment_id: str) -> str:
    """The per-deployment part of a per-deployment resource name, from the FULL id.

    The KB tool function and role used ``deployment_id[:8]``: eight hex of a uuid4, so two
    deployments sharing a prefix produced the SAME function and role, with the same
    ownership tags -- and the second deployment's redeploy was then "owned" by the first's
    function (peer 82, F-7d). Twelve hex of the id's SHA-256 is collision-resistant for a
    stack's deployments, and the exact ``DeploymentId`` tag still decides ownership.
    """
    if not deployment_id or not deployment_id.strip():
        raise ValueError("a deployment scope suffix needs the deployment id")
    return hashlib.sha256(deployment_id.strip().encode()).hexdigest()[:DEPLOYMENT_SCOPE_SUFFIX_LEN]


def function_name_prefix(stack_identity: str) -> str:
    """``AgentCore-{token}-``: the exact prefix the infra grants this stack on Lambda."""
    return f"{FUNCTION_NAME_PREFIX}-{stack_scope_token(stack_identity)}-"


def regional_iam_role_name(
    base_name: str,
    region: str | None,
    *,
    home_region: str | None = None,
) -> str:
    """Return a deterministic account-global IAM role name.

    AgentCore Runtime, Gateway, Memory, Harness, and Evaluation resources are
    regional while IAM role names are account-global.  Keep the historical name
    in the platform's home region, but add the target region everywhere else so
    an identical canvas can be deployed into two regions in one account.

    Names over IAM's 64-character limit retain a readable prefix and a digest of
    the complete intended identity.  A plain truncation is unsafe: two long
    resources that differ only after the cut would silently target one role.
    """
    normalized = _INVALID_IAM_ROLE_CHAR.sub("_", (base_name or "").strip())
    if not normalized:
        normalized = "AgentCoreRole"

    resolved_home = (
        (home_region or "").strip()
        or os.environ.get("APP_AWS_REGION", "").strip()
        or os.environ.get("AWS_REGION", "").strip()
    )
    target_region = (region or resolved_home).strip()
    # With no configured home region, preserve the historical spelling.  The
    # Lambda/CDK path always injects APP_AWS_REGION; this fallback keeps the pure
    # helper safe for local callers and older tests.
    if not resolved_home:
        resolved_home = target_region

    safe_region = _INVALID_IAM_ROLE_CHAR.sub("-", target_region)
    suffix = f"-{safe_region}" if safe_region and target_region != resolved_home else ""
    candidate = f"{normalized}{suffix}"
    if len(candidate) <= MAX_IAM_ROLE_NAME:
        return candidate

    digest = hashlib.sha256(f"{normalized}\0{target_region}".encode()).hexdigest()[:8]
    available = MAX_IAM_ROLE_NAME - len(suffix) - len(digest) - 1
    stem = normalized[: max(1, available)].rstrip("+=,.@_-") or "AgentCore"
    return f"{stem}-{digest}{suffix}"


def scoped_mcp_code_s3_key(
    primary_runtime_name: str | None,
    mcp_name: str | None,
) -> str:
    """Stable MCP bundle key isolated by the primary runtime.

    The prefix remains stable across redeploys of one agent (avoiding the
    AgentCore role/S3-prefix cache race), while two agents that use the same MCP
    server name no longer overwrite one another's executable bundle.
    """
    runtime_scope = sanitize_agentcore_name(
        primary_runtime_name,
        style="underscore",
        prefix="agent",
        fallback="agent_default",
    )
    mcp_scope = sanitize_agentcore_name(
        mcp_name,
        style="underscore",
        prefix="mcp",
        fallback="mcp_server",
    )
    return f"deployments/by-name/{runtime_scope}/mcp/{mcp_scope}/mcp-server-code.zip"
