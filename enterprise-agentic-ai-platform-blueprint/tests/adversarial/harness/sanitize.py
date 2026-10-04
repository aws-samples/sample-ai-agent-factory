"""Sanitization for adversarial evidence.

Every evidence record passes through :func:`sanitize_value` before it is
written, and through :func:`assert_sanitized` afterwards. The two are
deliberately separate: the first rewrites what it can (account ids become
manifest aliases), the second refuses to record anything that still matches a
sensitive pattern.

Findings never carry the matched text. A ``SanitizationError`` reports the
finding kind, the JSON-ish path, and the matched length only.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .errors import SanitizationError

REDACTED = "<redacted>"

# 12-digit AWS account id, not part of a longer digit run.
_ACCOUNT_ID = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")

# AWS unique-id prefixes (access keys, session keys, role/user ids).
_AWS_KEY_ID = re.compile(
    r"\b(?:AKIA|ASIA|AIDA|AROA|ANPA|ANVA|APKA|ABIA|ACCA)[A-Z0-9]{16}\b"
)

# STS session tokens are long base64 blobs; real ones start FwoG.../IQoJ...
_SESSION_TOKEN = re.compile(r"\b(?:FwoG|IQoJ|FQoG)[A-Za-z0-9/+=_-]{40,}")

# Any very long opaque blob is treated as a token regardless of prefix.
_LONG_OPAQUE = re.compile(r"\b[A-Za-z0-9/+=_-]{120,}\b")

_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")

_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# A 40-char secret-key-shaped string. Pure hex is excluded so that git commit
# SHAs survive untouched.
_SECRET_KEY_SHAPED = re.compile(r"\b[A-Za-z0-9/+=]{40}\b")
_HEX40 = re.compile(r"\A[0-9a-fA-F]{40}\Z")

# Credential-file references. The harness never opens these.
_CREDENTIAL_FILE = re.compile(
    r"(?i)[\w./\\-]*(?:credential|accesskey|access_key|secret)[\w./\\-]*\.(?:csv|txt|json)\b"
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", _PRIVATE_KEY),
    ("aws-key-id", _AWS_KEY_ID),
    ("session-token", _SESSION_TOKEN),
    ("bearer-token", _BEARER),
    ("long-opaque-token", _LONG_OPAQUE),
    ("credential-file-reference", _CREDENTIAL_FILE),
    ("email-address", _EMAIL),
    ("aws-account-id", _ACCOUNT_ID),
    ("secret-key-shaped", _SECRET_KEY_SHAPED),
)


@dataclass(frozen=True)
class Finding:
    """A sensitive-pattern hit. Carries no matched text."""

    kind: str
    path: str
    match_length: int

    def describe(self) -> str:
        return f"{self.kind} at {self.path} (length {self.match_length})"


def _is_commit_sha(text: str) -> bool:
    return bool(_HEX40.match(text))


def _scan_text(text: str, path: str) -> list[Finding]:
    findings: list[Finding] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            matched = match.group(0)
            if kind == "secret-key-shaped" and _is_commit_sha(matched):
                continue
            if kind == "long-opaque-token" and _is_commit_sha(matched):
                continue
            findings.append(Finding(kind=kind, path=path, match_length=len(matched)))
    return findings


def scan_for_secrets(value: Any, path: str = "$") -> list[Finding]:
    """Recursively scan ``value`` for sensitive patterns."""
    findings: list[Finding] = []
    if isinstance(value, str):
        findings.extend(_scan_text(value, path))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            findings.extend(_scan_text(str(key), f"{path}.<key>"))
            findings.extend(scan_for_secrets(item, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            findings.extend(scan_for_secrets(item, f"{path}[{index}]"))
    elif isinstance(value, (int, float, bool)) or value is None:
        # Numeric account ids would defeat the string scan; catch them here.
        if isinstance(value, int) and not isinstance(value, bool):
            if 10**11 <= abs(value) < 10**12:
                findings.append(
                    Finding(kind="aws-account-id", path=path, match_length=12)
                )
    else:
        findings.extend(_scan_text(str(value), path))
    return findings


def alias_map(account_aliases: Mapping[str, str]) -> dict[str, str]:
    """Build a ``{account_id: '<account:ALIAS>'}`` substitution table.

    ``account_aliases`` maps account id -> alias label, which is what
    :meth:`harness.manifest.ResolvedManifest.account_aliases` returns.
    """
    table: dict[str, str] = {}
    for account_id, alias in account_aliases.items():
        account_id = str(account_id)
        if not account_id:
            continue
        table[account_id] = f"<account:{alias}>"
    return table


def sanitize_text(text: str, aliases: Mapping[str, str] | None = None) -> str:
    """Rewrite a string so that it is safe to record.

    Known account ids become ``<account:ALIAS>``; unknown 12-digit runs become
    ``<account:REDACTED>``; every other sensitive pattern becomes
    ``<redacted>``.
    """
    out = text
    for account_id, replacement in (aliases or {}).items():
        out = out.replace(account_id, replacement)
    out = _PRIVATE_KEY.sub(REDACTED, out)
    out = _AWS_KEY_ID.sub(REDACTED, out)
    out = _SESSION_TOKEN.sub(REDACTED, out)
    out = _BEARER.sub(REDACTED, out)
    out = _CREDENTIAL_FILE.sub("<credential-file-reference-removed>", out)
    out = _EMAIL.sub("<principal-email>", out)
    out = _LONG_OPAQUE.sub(
        lambda m: m.group(0) if _is_commit_sha(m.group(0)) else REDACTED, out
    )
    out = _SECRET_KEY_SHAPED.sub(
        lambda m: m.group(0) if _is_commit_sha(m.group(0)) else REDACTED, out
    )
    out = _ACCOUNT_ID.sub("<account:REDACTED>", out)
    return out


def sanitize_value(value: Any, aliases: Mapping[str, str] | None = None) -> Any:
    """Recursively sanitize a JSON-compatible structure."""
    if isinstance(value, str):
        return sanitize_text(value, aliases)
    if isinstance(value, Mapping):
        return {
            sanitize_text(str(key), aliases): sanitize_value(item, aliases)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item, aliases) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, float):
        return value
    if isinstance(value, int):
        if 10**11 <= abs(value) < 10**12:
            return "<account:REDACTED>"
        return value
    return sanitize_text(str(value), aliases)


def assert_sanitized(value: Any, *, path: str = "$") -> None:
    """Raise :class:`SanitizationError` if anything sensitive remains."""
    findings = scan_for_secrets(value, path)
    if findings:
        raise SanitizationError([finding.describe() for finding in findings])


def forbid_credential_file(reference: str) -> None:
    """Reject a reference that looks like an on-disk credential export.

    Used by the manifest loader. The harness must never read access-key CSV
    files, so a manifest that names one is a configuration error rather than
    something to silently ignore.
    """
    if _CREDENTIAL_FILE.search(reference):
        raise SanitizationError(
            [
                "credential-file-reference in manifest "
                "(access-key exports must never be referenced or read)"
            ]
        )


def findings_by_kind(findings: Iterable[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.kind] = counts.get(finding.kind, 0) + 1
    return counts


def sanitized_sequence(
    values: Sequence[Any], aliases: Mapping[str, str] | None = None
) -> list[Any]:
    return [sanitize_value(value, aliases) for value in values]


__all__ = [
    "Finding",
    "REDACTED",
    "alias_map",
    "assert_sanitized",
    "findings_by_kind",
    "forbid_credential_file",
    "sanitize_text",
    "sanitize_value",
    "sanitized_sequence",
    "scan_for_secrets",
]
