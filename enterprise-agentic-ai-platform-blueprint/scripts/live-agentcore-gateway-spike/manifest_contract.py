#!/usr/bin/env python3
"""Byte-compatible implementation of buildAgentManifest().

The field order and compact UTF-8 JSON encoding intentionally match
packages/evaluation-gates/src/agent-manifest.ts. Do not replace this with
``sort_keys=True``: JavaScript's JSON.stringify preserves insertion order.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

_GIT_SHA = re.compile(r"^[a-f0-9]{7,40}$")
_TOOL_ID = re.compile(r"^[a-z0-9-]{3,50}$")


class ManifestContractError(ValueError):
    """Raised when manifest input violates the shared contract."""


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ManifestContractError(f"{name} must be a string")
    # Well-formed JSON.stringify escapes lone UTF-16 surrogates. Rejecting them
    # keeps the cross-language byte contract explicit rather than lossy.
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ManifestContractError(f"{name} must not contain lone surrogates")
    return value


def _stable_payload(input_value: Mapping[str, Any]) -> dict[str, Any]:
    git_sha = _require_string(input_value.get("gitSha"), "gitSha")
    if not _GIT_SHA.fullmatch(git_sha):
        raise ManifestContractError(f"gitSha must be 7-40 hex chars, got: {git_sha}")

    raw_tools = input_value.get("toolPermissions")
    if not isinstance(raw_tools, Sequence) or isinstance(raw_tools, (str, bytes)):
        raise ManifestContractError("toolPermissions must be an array")
    tools = sorted(_require_string(tool, "toolPermissions entry") for tool in raw_tools)
    if any(not _TOOL_ID.fullmatch(tool) for tool in tools):
        raise ManifestContractError("toolPermissions ids must be kebab-case 3-50 chars")

    raw_prompts = input_value.get("promptHashes")
    if not isinstance(raw_prompts, Mapping):
        raise ManifestContractError("promptHashes must be an object")
    prompts: dict[str, str] = {}
    for key in sorted(raw_prompts):
        prompt_key = _require_string(key, "promptHashes key")
        prompts[prompt_key] = _require_string(raw_prompts[key], f"promptHashes[{key!r}]")

    # Keep this insertion order byte-identical to the TypeScript stable object.
    return {
        "manifestVersion": 1,
        "agentId": _require_string(input_value.get("agentId"), "agentId"),
        "tenantId": _require_string(input_value.get("tenantId"), "tenantId"),
        "gitSha": git_sha,
        "promptHashes": prompts,
        "toolPermissions": tools,
        "configHash": _require_string(input_value.get("configHash"), "configHash"),
        "thresholdsHash": _require_string(
            input_value.get("thresholdsHash"), "thresholdsHash"
        ),
    }


def javascript_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Serialize this contract's value subset like JSON.stringify(value)."""
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def build_agent_manifest(
    input_value: Mapping[str, Any], *, emitted_at: str | None = None
) -> dict[str, Any]:
    stable = _stable_payload(input_value)
    manifest_sha = hashlib.sha256(javascript_json_bytes(stable)).hexdigest()
    timestamp = emitted_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # Validate caller-supplied timestamps without normalizing their exact bytes.
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ManifestContractError("emittedAt must be ISO-8601") from error
    return {**stable, "manifestSha": manifest_sha, "emittedAt": timestamp}


def main() -> int:
    request = json.load(sys.stdin)
    if not isinstance(request, Mapping) or not isinstance(request.get("input"), Mapping):
        raise ManifestContractError("stdin must be {\"input\": {...}, \"now\": ...}")
    manifest = build_agent_manifest(request["input"], emitted_at=request.get("now"))
    sys.stdout.buffer.write(javascript_json_bytes(manifest) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
