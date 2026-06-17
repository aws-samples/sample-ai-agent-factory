"""Deploy-time prompt-reference resolution — Phase 3 Gap 3H.

When a runtime config references a library prompt instead of inlining the
body, this hook resolves the reference to the actual prompt text BEFORE the
config is serialized into the Step Functions input. The resolved body then
flows through codegen unchanged, where ``_escape_triple_quotes`` already makes
multi-line bodies injection-safe.

Two reference forms are supported:
  - the string ``"prompt://<name>[@<version>]"`` (the realistic form, since
    ``RuntimeConfig.system_prompt`` is typed ``str``)
  - a dict ``{"promptId": <name>, "versionId": <version?>}`` (defensive — only
    reachable if the model is ever loosened to accept a dict)

An inline-string systemPrompt (anything not matching a ref) is left
byte-identical (back-compat).

Resolution is tenant-scoped: it uses the same owner/org visibility predicate
as the ``/api/prompts/{name}/resolve`` endpoint (``resolve_visible_body``), so
a consumer can resolve a shared org prompt but never a foreign private one.

FAILURE MODE: a deploy must NEVER hard-fail on a missing/foreign/unresolvable
ref. The hook is wrapped so any error (or a None/empty resolution) leaves the
original systemPrompt value untouched and logs a warning.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


_PROMPT_URI_RE = re.compile(r"^prompt://([^@]+)(?:@(.+))?$")


def _parse_ref(value: Any) -> tuple[str, str | None] | None:
    """Return ``(name, version|None)`` if *value* is a prompt ref, else None."""
    if isinstance(value, str):
        m = _PROMPT_URI_RE.match(value.strip())
        if m:
            name = m.group(1).strip()
            version = (m.group(2) or "").strip() or None
            return (name, version) if name else None
        return None
    if isinstance(value, dict):
        name = value.get("promptId") or value.get("prompt_id")
        if name:
            version = value.get("versionId") or value.get("version_id")
            return (str(name).strip(), str(version).strip() if version else None)
    return None


def resolve_system_prompt(config: Any, caller_sub: str | None) -> None:
    """If ``config.system_prompt`` is a library-prompt ref, replace it in place
    with the resolved body. Otherwise leave it unchanged.

    Never raises — a resolution failure logs and keeps the original value so a
    deploy never hard-fails on a missing/foreign prompt reference.
    """
    try:
        raw = getattr(config, "system_prompt", None)
        ref = _parse_ref(raw)
        if ref is None:
            return  # inline string — leave byte-identical (back-compat).

        name, version = ref

        # Import lazily so this module stays import-safe without the store env.
        from app.services.prompt_library_store import DEFAULT_ORG_ID, slugify
        from app.routers.prompts import resolve_visible_body

        resolved = resolve_visible_body(
            org_id=DEFAULT_ORG_ID,
            prompt_name=slugify(name),
            version_id=version,
            caller_sub=caller_sub or "",
        )
        if not resolved:
            logger.warning(
                "Prompt ref %r could not be resolved for caller %s; keeping "
                "original systemPrompt value.",
                raw,
                caller_sub,
            )
            return
        _version_id, body = resolved
        if not isinstance(body, str) or not body.strip():
            logger.warning(
                "Resolved prompt ref %r produced an empty body; keeping "
                "original systemPrompt value.",
                raw,
            )
            return
        config.system_prompt = body
        logger.info(
            "Resolved prompt ref %r to library version %s (caller=%s).",
            raw,
            _version_id,
            caller_sub,
        )
    except Exception:
        logger.warning(
            "resolve_system_prompt failed; keeping original systemPrompt value.",
            exc_info=True,
        )
