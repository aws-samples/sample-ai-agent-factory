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

import re

# Max length is service-specific; these are the conservative documented caps.
MAX_UNDERSCORE = 48  # runtime/harness names cap at 48; memory allows 48 too
MAX_HYPHEN = 48  # gateway names cap at 48


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
