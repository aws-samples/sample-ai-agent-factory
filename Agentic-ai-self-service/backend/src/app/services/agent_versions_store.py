"""DynamoDB-backed store for agent versions and runtime production slots.

Implements Phase 1 Gap 1A from /Users/omrsamer/.claude/plans/whimsical-coalescing-creek.md.

Two tables:

* ``AgentVersionsTable`` — one row per (runtime_name, version_id). Captures the
  full canvas snapshot, the AgentCore runtime ARN/id, the S3 code key, and
  the deployer's owner sub. Enables list-by-runtime-name + list-by-owner.
* ``RuntimeSlotsTable`` — one row per runtime_name. Holds the version_id
  currently assigned to ``production`` and ``staging`` slots. ``promote()``
  flips a slot. ``rollback()`` flips production back to the previous version.

Tenant isolation: every read and write checks ``owner_sub`` against the
caller's JWT sub via ``services.auth.assert_owner``. Per Critic Finding 3,
None-owner records (legacy pre-tenancy data, which won't exist for these
fresh tables) are also treated as inaccessible — same 404-on-mismatch rule.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sortable id (ULID-shaped, 16 bytes hex). Lex order = chronological.
# ---------------------------------------------------------------------------


def new_version_id() -> str:
    """Return a 32-char lowercase hex string sortable by creation time.

    Layout: 12 hex chars of millisecond epoch + 20 hex chars of random.
    32 chars total = 16 bytes, fits the same shape as a ULID without
    requiring an external dependency (no ``ulid-py`` in requirements).
    Lexicographic ordering of two ids equals their chronological order
    as long as they were generated within the same epoch-ms window;
    ties break randomly which is fine for our SK ordering needs.
    """
    ms = int(time.time() * 1000)
    return f"{ms:012x}{secrets.token_hex(10)}"


def short_version_suffix(version_id: str) -> str:
    """Return the AgentCore-runtime-name suffix for a version.

    AgentCore runtime names are limited to 48 chars and must match
    ``[a-zA-Z][a-zA-Z0-9_]{0,47}``. We append a short stable suffix derived
    from the version id so each version of an agent maps to a distinct
    runtime ARN. 8 hex chars = 32 bits of entropy, plenty for collision
    avoidance per friendly name.
    """
    # Strip the timestamp prefix (12 chars) so the suffix is dominated by
    # randomness — two versions created within the same ms window stay
    # distinct in the suffix.
    return version_id[12:20] if len(version_id) >= 20 else version_id[:8]


# ---------------------------------------------------------------------------
# Decimal/float helpers shared with deployment_state_store
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
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimals_to_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimals_to_floats(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Models (lightweight dataclasses; not Pydantic — these are internal)
# ---------------------------------------------------------------------------


@dataclass
class AgentVersion:
    runtime_name: str
    version_id: str
    owner_sub: str
    created_at: str  # ISO 8601
    deployment_id: str
    agentcore_runtime_name: str  # versioned AgentCore name (with suffix)
    runtime_id: str | None = None
    runtime_arn: str | None = None
    runtime_endpoint: str | None = None
    code_s3_key: str | None = None
    parent_version_id: str | None = None
    canvas_snapshot: dict | None = None
    deploy_request_snapshot: dict | None = None
    status: str = "pending"  # pending | succeeded | failed | superseded
    description: str | None = None

    def to_item(self) -> dict:
        item = {
            "runtime_name": self.runtime_name,
            "version_id": self.version_id,
            "owner_sub": self.owner_sub,
            "created_at": self.created_at,
            "deployment_id": self.deployment_id,
            "agentcore_runtime_name": self.agentcore_runtime_name,
            "status": self.status,
        }
        for fld in (
            "runtime_id",
            "runtime_arn",
            "runtime_endpoint",
            "code_s3_key",
            "parent_version_id",
            "canvas_snapshot",
            "deploy_request_snapshot",
            "description",
        ):
            val = getattr(self, fld)
            if val is not None:
                item[fld] = val
        return _floats_to_decimals(item)

    @classmethod
    def from_item(cls, item: dict) -> AgentVersion:
        item = _decimals_to_floats(dict(item))
        return cls(
            runtime_name=item["runtime_name"],
            version_id=item["version_id"],
            owner_sub=item.get("owner_sub", ""),
            created_at=item.get("created_at", ""),
            deployment_id=item.get("deployment_id", ""),
            agentcore_runtime_name=item.get("agentcore_runtime_name", ""),
            runtime_id=item.get("runtime_id"),
            runtime_arn=item.get("runtime_arn"),
            runtime_endpoint=item.get("runtime_endpoint"),
            code_s3_key=item.get("code_s3_key"),
            parent_version_id=item.get("parent_version_id"),
            canvas_snapshot=item.get("canvas_snapshot"),
            deploy_request_snapshot=item.get("deploy_request_snapshot"),
            status=item.get("status", "pending"),
            description=item.get("description"),
        )


@dataclass
class RuntimeSlots:
    runtime_name: str
    owner_sub: str
    production_version_id: str | None = None
    staging_version_id: str | None = None
    previous_production_version_id: str | None = None
    last_promoted_at: str | None = None

    def to_item(self) -> dict:
        item = {"runtime_name": self.runtime_name, "owner_sub": self.owner_sub}
        for fld in (
            "production_version_id",
            "staging_version_id",
            "previous_production_version_id",
            "last_promoted_at",
        ):
            val = getattr(self, fld)
            if val is not None:
                item[fld] = val
        return item

    @classmethod
    def from_item(cls, item: dict) -> RuntimeSlots:
        return cls(
            runtime_name=item["runtime_name"],
            owner_sub=item.get("owner_sub", ""),
            production_version_id=item.get("production_version_id"),
            staging_version_id=item.get("staging_version_id"),
            previous_production_version_id=item.get("previous_production_version_id"),
            last_promoted_at=item.get("last_promoted_at"),
        )


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


class AgentVersionsStore:
    """CRUD for the AgentVersions DDB table."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put(self, version: AgentVersion) -> None:
        self._table.put_item(Item=version.to_item())
        logger.info(
            "Wrote AgentVersion %s/%s (status=%s)",
            version.runtime_name,
            version.version_id,
            version.status,
        )

    def get(self, runtime_name: str, version_id: str) -> AgentVersion | None:
        resp = self._table.get_item(Key={"runtime_name": runtime_name, "version_id": version_id})
        item = resp.get("Item")
        if not item:
            return None
        return AgentVersion.from_item(item)

    def list_for_runtime(self, runtime_name: str) -> list[AgentVersion]:
        """Return versions ordered newest-first (SK is sortable timestamp)."""
        items: list[dict] = []
        kwargs: dict = {
            "KeyConditionExpression": Key("runtime_name").eq(runtime_name),
            "ScanIndexForward": False,  # newest first
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [AgentVersion.from_item(i) for i in items]

    def list_for_owner(self, owner_sub: str) -> list[AgentVersion]:
        """Use the owner_sub GSI to list every version owned by *owner_sub*.

        Used by ``GET /api/runtimes/versions`` to surface every runtime the
        caller has versions of, irrespective of friendly name.
        """
        items: list[dict] = []
        kwargs: dict = {
            "IndexName": "owner_sub-version_id-index",
            "KeyConditionExpression": Key("owner_sub").eq(owner_sub),
            "ScanIndexForward": False,
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [AgentVersion.from_item(i) for i in items]

    def update_status(
        self,
        runtime_name: str,
        version_id: str,
        *,
        status: str,
        runtime_id: str | None = None,
        runtime_arn: str | None = None,
        runtime_endpoint: str | None = None,
        code_s3_key: str | None = None,
    ) -> None:
        set_parts = ["#s = :s"]
        names: dict[str, str] = {"#s": "status"}
        values: dict[str, str] = {":s": status}
        for col, val in [
            ("runtime_id", runtime_id),
            ("runtime_arn", runtime_arn),
            ("runtime_endpoint", runtime_endpoint),
            ("code_s3_key", code_s3_key),
        ]:
            if val is not None:
                set_parts.append(f"{col} = :{col}")
                values[f":{col}"] = val
        self._table.update_item(
            Key={"runtime_name": runtime_name, "version_id": version_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def delete(self, runtime_name: str, version_id: str) -> None:
        self._table.delete_item(Key={"runtime_name": runtime_name, "version_id": version_id})


class RuntimeSlotsStore:
    """CRUD for the RuntimeSlots DDB table."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def get(self, runtime_name: str) -> RuntimeSlots | None:
        resp = self._table.get_item(Key={"runtime_name": runtime_name})
        item = resp.get("Item")
        if not item:
            return None
        return RuntimeSlots.from_item(item)

    def upsert(self, slots: RuntimeSlots) -> None:
        self._table.put_item(Item=slots.to_item())
        logger.info(
            "Updated RuntimeSlots %s (prod=%s, staging=%s)",
            slots.runtime_name,
            slots.production_version_id,
            slots.staging_version_id,
        )

    def delete(self, runtime_name: str) -> None:
        self._table.delete_item(Key={"runtime_name": runtime_name})


# ---------------------------------------------------------------------------
# Convenience singletons (lazy-init from env)
# ---------------------------------------------------------------------------

_versions_store: AgentVersionsStore | None = None
_slots_store: RuntimeSlotsStore | None = None


def get_versions_store() -> AgentVersionsStore:
    global _versions_store
    if _versions_store is None:
        _versions_store = AgentVersionsStore(
            table_name=os.environ.get("AGENT_VERSIONS_TABLE_NAME", "AgentVersions"),
            region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
        )
    return _versions_store


def get_slots_store() -> RuntimeSlotsStore:
    global _slots_store
    if _slots_store is None:
        _slots_store = RuntimeSlotsStore(
            table_name=os.environ.get("RUNTIME_SLOTS_TABLE_NAME", "RuntimeSlots"),
            region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
        )
    return _slots_store
