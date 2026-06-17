"""DynamoDB-backed Agent Registry store — Phase 2 Gap 2A.

Org-wide catalog where users publish deployed agents (canvas snapshots) so
others can discover and clone them. Mirrors the storage patterns of
``tool_catalog_store.py`` and ``agent_versions_store.py``.

Table layout (``AgentRegistryTable``):
  PK  ``org_id``       — organisation scope (single "default-org" until Gap 2E
                          wires real Cognito-group-backed orgs)
  SK  ``agent_slug``   — URL-safe unique-within-org identifier
  GSI ``owner_sub-agent_slug-index`` — list-by-publisher
  GSI ``visibility-agent_slug-index`` — list public/org-visible entries

Tenant model:
  - ``visibility`` ∈ {private, org, public}. ``private`` is visible only to
    the owner_sub. ``org`` is visible to everyone in the same org_id.
    ``public`` is visible cross-org.
  - Mutations (update/delete/publish-new-version) require
    ``owner_sub == caller`` via ``assert_owner`` in the router layer.
  - ``clone`` copies the canvas snapshot into the caller's own workflow
    storage — it never mutates the registry entry, only bumps usage_count.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


DEFAULT_ORG_ID = "default-org"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class RegistryEntry(BaseModel):
    """A published agent in the org registry."""

    model_config = ConfigDict(populate_by_name=True)

    org_id: str = Field(default=DEFAULT_ORG_ID)
    agent_slug: str = Field(min_length=1, max_length=128)
    owner_sub: str
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list)
    visibility: str = Field(default="org")  # private | org | public
    latest_version_id: Optional[str] = None
    usage_count: int = 0
    canvas_snapshot: dict = Field(default_factory=dict)
    source_runtime_name: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    # Two-persona approval workflow. CRITICAL: status defaults to 'approved' so
    # pre-existing rows (which have no status attribute) deserialize as approved
    # and DO NOT disappear from listings. New publishes explicitly set 'pending'.
    status: str = "approved"  # pending | approved | rejected
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    rejection_reason: Optional[str] = None


def slugify(name: str) -> str:
    """Turn a display name into a URL-safe slug.

    Lowercase, non-alnum → hyphen, collapse repeats, trim, max 128.
    """
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    s = re.sub(r"-{2,}", "-", s)
    return s[:128] or "agent"


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
        # Registry usage_count is an int; keep ints as ints.
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: _decimals_to_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimals_to_floats(v) for v in obj]
    return obj


def _serialize(entry: RegistryEntry) -> dict:
    item = entry.model_dump(mode="json")
    # GSI keys can't be NULL — strip None.
    item = {k: v for k, v in item.items() if v is not None}
    return _floats_to_decimals(item)


def _deserialize(item: dict) -> RegistryEntry:
    return RegistryEntry.model_validate(_decimals_to_floats(dict(item)))


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class RegistryStore:
    """CRUD + discovery queries for the AgentRegistry DDB table."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._region = region
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    # -- writes ----------------------------------------------------------

    def put(self, entry: RegistryEntry) -> RegistryEntry:
        now = datetime.now(timezone.utc).isoformat()
        if not entry.created_at:
            entry.created_at = now
        entry.updated_at = now
        self._table.put_item(Item=_serialize(entry))
        logger.info(
            "Published registry entry %s/%s (visibility=%s)",
            entry.org_id,
            entry.agent_slug,
            entry.visibility,
        )
        return entry

    def update(self, org_id: str, agent_slug: str, updates: dict) -> Optional[RegistryEntry]:
        existing = self.get(org_id, agent_slug)
        if existing is None:
            return None
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_parts, names, values = [], {}, {}
        for i, (field, value) in enumerate(updates.items()):
            set_parts.append(f"#n{i} = :v{i}")
            names[f"#n{i}"] = field
            values[f":v{i}"] = _floats_to_decimals(value)
        self._table.update_item(
            Key={"org_id": org_id, "agent_slug": agent_slug},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return self.get(org_id, agent_slug)

    def increment_usage(self, org_id: str, agent_slug: str) -> None:
        """Atomically bump usage_count by 1. Used by clone()."""
        try:
            self._table.update_item(
                Key={"org_id": org_id, "agent_slug": agent_slug},
                UpdateExpression="SET usage_count = if_not_exists(usage_count, :zero) + :one",
                ExpressionAttributeValues={":one": 1, ":zero": 0},
            )
        except Exception:
            logger.warning(
                "increment_usage failed for %s/%s", org_id, agent_slug, exc_info=True
            )

    def delete(self, org_id: str, agent_slug: str) -> bool:
        try:
            self._table.delete_item(Key={"org_id": org_id, "agent_slug": agent_slug})
            logger.info("Deleted registry entry %s/%s", org_id, agent_slug)
            return True
        except Exception as e:
            logger.warning("Failed to delete registry entry %s/%s: %s", org_id, agent_slug, e)
            return False

    # -- reads -----------------------------------------------------------

    def get(self, org_id: str, agent_slug: str) -> Optional[RegistryEntry]:
        resp = self._table.get_item(Key={"org_id": org_id, "agent_slug": agent_slug})
        item = resp.get("Item")
        return _deserialize(item) if item else None

    def list_for_org(self, org_id: str) -> list[RegistryEntry]:
        """Every entry in the org (the router filters by visibility + caller)."""
        items: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("org_id").eq(org_id)}
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [_deserialize(i) for i in items]

    def list_pending(self, org_id: str) -> list[RegistryEntry]:
        """Entries in the org awaiting review (status == 'pending').

        Reuses list_for_org then filters in memory — no new GSI is added.
        """
        return [e for e in self.list_for_org(org_id) if e.status == "pending"]

    def list_for_owner(self, owner_sub: str) -> list[RegistryEntry]:
        items: list[dict] = []
        kwargs: dict = {
            "IndexName": "owner_sub-agent_slug-index",
            "KeyConditionExpression": Key("owner_sub").eq(owner_sub),
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [_deserialize(i) for i in items]

    def list_public(self) -> list[RegistryEntry]:
        items: list[dict] = []
        kwargs: dict = {
            "IndexName": "visibility-agent_slug-index",
            "KeyConditionExpression": Key("visibility").eq("public"),
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [_deserialize(i) for i in items]


# ---------------------------------------------------------------------------
# Lazy singleton from env
# ---------------------------------------------------------------------------

_registry_store: Optional[RegistryStore] = None


def get_registry_store() -> RegistryStore:
    global _registry_store
    if _registry_store is None:
        import os

        _registry_store = RegistryStore(
            table_name=os.environ.get("AGENT_REGISTRY_TABLE_NAME", "AgentRegistry"),
            region=os.environ.get(
                "APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")
            ),
        )
    return _registry_store
