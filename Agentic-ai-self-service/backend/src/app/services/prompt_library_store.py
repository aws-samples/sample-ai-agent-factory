"""DynamoDB-backed Prompt Management Library store — Phase 3 Gap 3H.

A reusable, versioned catalog of system prompts. Authors create a named prompt
once, then append new versions over time and pin a default. At deploy time a
runtime config can reference a library prompt instead of inlining the body, so
prompt iteration is decoupled from agent redeploys.

Mirrors the storage patterns of ``registry_store.py`` (org_id PK + slugified
prompt_name SK, owner_sub GSI, Decimal helpers, lazy singleton).

Table layout (``PromptLibraryTable``):
  PK  ``org_id``       — organisation scope (single "default-org" until Gap 2E
                          wires real Cognito-group-backed orgs)
  SK  ``prompt_name``  — URL-safe, unique-within-org slug
  GSI ``owner_sub-prompt_name-index`` — list-by-author

Tenant model (see routers/prompts.py):
  - Visibility is owner OR same org (prompts have no public tier).
  - Mutations (update/delete/add-version/promote) require
    ``owner_sub == caller`` via ``assert_owner`` in the router layer.
  - Cross-tenant access returns 404 (existence-non-disclosure).

Versioning model:
  - ``versions`` is a list of dicts stored inline on the single prompt item,
    each ``{version_id, body, created_at, created_by}``.
  - ``default_version_id`` points at the version used when a consumer resolves
    the prompt without an explicit version.
  - ``add_version`` is a read-modify-write append (last-writer-wins under a
    concurrent add to the SAME prompt — acceptable for a low-write authoring
    workflow; see RISK note below).

RISK — concurrency + item size:
  ``versions`` is a list attribute on one DDB item. Concurrent adds to the same
  prompt race (last-writer-wins) and the 400KB item limit caps total history.
  Bodies are capped at 10000 chars (matching ``RuntimeConfig.system_prompt``).
  A separate versions table would be required for high-concurrency or very
  large histories — out of scope for this gap.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


DEFAULT_ORG_ID = "default-org"

# Match RuntimeConfig.system_prompt max_length so a resolved body always fits.
MAX_BODY_LEN = 10000


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class PromptVersion(BaseModel):
    """A single immutable version of a prompt body."""

    model_config = ConfigDict(populate_by_name=True)

    version_id: str
    body: str = Field(min_length=1, max_length=MAX_BODY_LEN)
    created_at: str = ""
    created_by: str = ""


class PromptEntry(BaseModel):
    """A named prompt in the org library with an inline version history."""

    model_config = ConfigDict(populate_by_name=True)

    org_id: str = Field(default=DEFAULT_ORG_ID)
    prompt_name: str = Field(min_length=1, max_length=128)
    owner_sub: str
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list)
    versions: list[PromptVersion] = Field(default_factory=list)
    default_version_id: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


def slugify(name: str) -> str:
    """Turn a display name into a URL-safe slug.

    Lowercase, non-alnum → hyphen, collapse repeats, trim, max 128.
    """
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    s = re.sub(r"-{2,}", "-", s)
    return s[:128] or "prompt"


def new_prompt_version_id() -> str:
    """Return a 32-char lowercase hex id, sortable by creation time.

    Same shape as ``agent_versions_store.new_version_id``: 12 hex chars of
    millisecond epoch + 20 hex chars of random. No external dependency.
    """
    ms = int(time.time() * 1000)
    return f"{ms:012x}{secrets.token_hex(10)}"


# ---------------------------------------------------------------------------
# Decimal helpers (shared shape with the other stores)
# ---------------------------------------------------------------------------


def _floats_to_decimals(obj):
    if isinstance(obj, float):
        if obj != 0.0 and abs(obj) < 1e-130:
            return Decimal("0")
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimals(v) for v in obj]
    return obj


def _decimals_to_floats(obj):
    if isinstance(obj, Decimal):
        # Keep ints as ints for any counters.
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: _decimals_to_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimals_to_floats(v) for v in obj]
    return obj


def _serialize(entry: PromptEntry) -> dict:
    item = entry.model_dump(mode="json")
    # GSI keys can't be NULL — strip None.
    item = {k: v for k, v in item.items() if v is not None}
    return _floats_to_decimals(item)


def _deserialize(item: dict) -> PromptEntry:
    return PromptEntry.model_validate(_decimals_to_floats(dict(item)))


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class PromptLibraryStore:
    """CRUD + version management for the PromptLibrary DDB table."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._region = region
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    # -- writes ----------------------------------------------------------

    def put(self, entry: PromptEntry) -> PromptEntry:
        now = datetime.now(timezone.utc).isoformat()
        if not entry.created_at:
            entry.created_at = now
        entry.updated_at = now
        self._table.put_item(Item=_serialize(entry))
        logger.info(
            "Wrote prompt %s/%s (versions=%d, default=%s)",
            entry.org_id,
            entry.prompt_name,
            len(entry.versions),
            entry.default_version_id,
        )
        return entry

    def update(
        self, org_id: str, prompt_name: str, updates: dict
    ) -> Optional[PromptEntry]:
        """Patch top-level metadata fields (display_name/description/tags)."""
        existing = self.get(org_id, prompt_name)
        if existing is None:
            return None
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_parts, names, values = [], {}, {}
        for i, (field, value) in enumerate(updates.items()):
            set_parts.append(f"#n{i} = :v{i}")
            names[f"#n{i}"] = field
            values[f":v{i}"] = _floats_to_decimals(value)
        self._table.update_item(
            Key={"org_id": org_id, "prompt_name": prompt_name},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return self.get(org_id, prompt_name)

    def add_version(
        self, org_id: str, prompt_name: str, body: str, created_by: str
    ) -> Optional[str]:
        """Append a new version and return its version_id.

        Read-modify-write on the inline ``versions`` list (last-writer-wins
        under a concurrent add to the same prompt — see module docstring).
        Returns ``None`` if the prompt does not exist.
        """
        existing = self.get(org_id, prompt_name)
        if existing is None:
            return None
        version_id = new_prompt_version_id()
        version = PromptVersion(
            version_id=version_id,
            body=body,
            created_at=datetime.now(timezone.utc).isoformat(),
            created_by=created_by,
        )
        existing.versions.append(version)
        # First version added to an empty prompt becomes the default.
        if not existing.default_version_id:
            existing.default_version_id = version_id
        self.put(existing)
        return version_id

    def promote(self, org_id: str, prompt_name: str, version_id: str) -> bool:
        """Point ``default_version_id`` at *version_id*.

        Returns False if the prompt or the target version doesn't exist.
        """
        existing = self.get(org_id, prompt_name)
        if existing is None:
            return False
        if not any(v.version_id == version_id for v in existing.versions):
            return False
        existing.default_version_id = version_id
        self.put(existing)
        return True

    def delete(self, org_id: str, prompt_name: str) -> bool:
        try:
            self._table.delete_item(
                Key={"org_id": org_id, "prompt_name": prompt_name}
            )
            logger.info("Deleted prompt %s/%s", org_id, prompt_name)
            return True
        except Exception as e:
            logger.warning(
                "Failed to delete prompt %s/%s: %s", org_id, prompt_name, e
            )
            return False

    # -- reads -----------------------------------------------------------

    def get(self, org_id: str, prompt_name: str) -> Optional[PromptEntry]:
        resp = self._table.get_item(
            Key={"org_id": org_id, "prompt_name": prompt_name}
        )
        item = resp.get("Item")
        return _deserialize(item) if item else None

    def list_for_org(self, org_id: str) -> list[PromptEntry]:
        """Every prompt in the org (the router filters by visibility + caller)."""
        items: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("org_id").eq(org_id)}
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [_deserialize(i) for i in items]

    def list_for_owner(self, owner_sub: str) -> list[PromptEntry]:
        items: list[dict] = []
        kwargs: dict = {
            "IndexName": "owner_sub-prompt_name-index",
            "KeyConditionExpression": Key("owner_sub").eq(owner_sub),
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [_deserialize(i) for i in items]

    def resolve_body(
        self, org_id: str, prompt_name: str, version_id: Optional[str] = None
    ) -> Optional[str]:
        """Return the body for *version_id* (or the default version).

        Returns ``None`` if the prompt, the requested version, or the default
        version can't be found. Does NOT enforce tenant visibility — that is
        the caller's responsibility (the router + deploy hook both
        visibility-check before calling this).
        """
        entry = self.get(org_id, prompt_name)
        if entry is None:
            return None
        target = version_id or entry.default_version_id
        if not target:
            return None
        for v in entry.versions:
            if v.version_id == target:
                return v.body
        return None


# ---------------------------------------------------------------------------
# Lazy singleton from env
# ---------------------------------------------------------------------------

_prompt_library_store: Optional[PromptLibraryStore] = None


def get_prompt_library_store() -> PromptLibraryStore:
    global _prompt_library_store
    if _prompt_library_store is None:
        import os

        _prompt_library_store = PromptLibraryStore(
            table_name=os.environ.get("PROMPT_LIBRARY_TABLE_NAME", "PromptLibrary"),
            region=os.environ.get(
                "APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")
            ),
        )
    return _prompt_library_store
