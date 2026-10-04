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
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# A sentinel in each runtime-name partition. ``!`` is outside the public version-id grammar
# (routers/versions.py accepts only [A-Za-z0-9_-]), so a customer-supplied id cannot collide with
# it. The row is deliberately sparse -- no owner_sub -- and therefore absent from the owner GSI.
NAME_CLAIM_VERSION_ID = "!name-claim"
NAME_CLAIM_RECORD_TYPE = "runtime_name_claim"
NAME_CLAIM_SNAPSHOT_ATTEMPTS = 3


def _name_claim_generation_update(
    table_name: str,
    runtime_name: str,
    *,
    claim_owner_sub: str | None = None,
    expected_epoch: str | None = None,
    expected_generation: int | None = None,
) -> dict:
    """Advance the runtime-name fence and, on first acquisition, bind its owner.

    ``claim_owner_sub`` turns this update into the serialization point for two deployments that
    both passed an empty pre-flight read. The first writer stores its owner; a different owner is
    then cancelled in the same transaction as its version-row put. The attribute is deliberately
    named ``claim_owner_sub`` rather than ``owner_sub`` so the sentinel stays out of the owner GSI.
    """
    names = {
        "#record_type": "record_type",
        "#epoch": "claim_epoch",
        "#generation": "claim_generation",
    }
    values: dict[str, object] = {
        ":record_type": NAME_CLAIM_RECORD_TYPE,
        ":new_epoch": secrets.token_hex(16),
        ":one": 1,
    }
    set_parts = [
        "#record_type = if_not_exists(#record_type, :record_type)",
        "#epoch = if_not_exists(#epoch, :new_epoch)",
    ]
    conditions = ["(attribute_not_exists(#record_type) OR #record_type = :record_type)"]
    if claim_owner_sub is not None:
        if not claim_owner_sub:
            raise ValueError("a name-claim owner cannot be empty")
        names["#claim_owner"] = "claim_owner_sub"
        values[":claim_owner"] = claim_owner_sub
        set_parts.append("#claim_owner = if_not_exists(#claim_owner, :claim_owner)")
        conditions.append("(attribute_not_exists(#claim_owner) OR #claim_owner = :claim_owner)")
    update: dict[str, object] = {
        "TableName": table_name,
        "Key": {
            "runtime_name": runtime_name,
            "version_id": NAME_CLAIM_VERSION_ID,
        },
        "UpdateExpression": "SET " + ", ".join(set_parts) + " ADD #generation :one",
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }
    if expected_epoch is not None or expected_generation is not None:
        if not expected_epoch or expected_generation is None:
            raise ValueError("an expected name-claim fence needs both epoch and generation")
        values[":expected_epoch"] = expected_epoch
        values[":expected_generation"] = expected_generation
        conditions.append(
            "#record_type = :record_type AND #epoch = :expected_epoch AND #generation = :expected_generation"
        )
    update["ConditionExpression"] = " AND ".join(conditions)
    return {"Update": update}


def _name_claim_delete(
    table_name: str,
    runtime_name: str,
    *,
    expected_epoch: str,
    expected_generation: int,
    expected_owner_sub: str | None,
) -> dict:
    """Delete a fully released name's sentinel, fenced against owner and ABA changes."""
    names = {
        "#record_type": "record_type",
        "#epoch": "claim_epoch",
        "#generation": "claim_generation",
        "#claim_owner": "claim_owner_sub",
    }
    values: dict[str, object] = {
        ":record_type": NAME_CLAIM_RECORD_TYPE,
        ":expected_epoch": expected_epoch,
        ":expected_generation": expected_generation,
    }
    conditions = [
        "#record_type = :record_type",
        "#epoch = :expected_epoch",
        "#generation = :expected_generation",
    ]
    if expected_owner_sub is None:
        conditions.append("attribute_not_exists(#claim_owner)")
    else:
        if not expected_owner_sub.strip():
            raise ValueError("an expected name-claim owner cannot be empty")
        values[":expected_owner"] = expected_owner_sub
        conditions.append("#claim_owner = :expected_owner")
    return {
        "Delete": {
            "TableName": table_name,
            "Key": {
                "runtime_name": runtime_name,
                "version_id": NAME_CLAIM_VERSION_ID,
            },
            "ConditionExpression": " AND ".join(conditions),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
    }


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


@dataclass(frozen=True)
class NameClaimSnapshot:
    """A stable version-partition read and the generation that fences it.

    DynamoDB gives a strongly consistent result for each Query request, but a paginated Query is
    not one transaction-wide snapshot. ``AgentVersionsStore.snapshot_for_name_release`` therefore
    reads the sentinel generation before and after the complete query and retries if any writer
    moved it. The release transaction then advances that exact generation conditionally, covering
    a writer that lands after the second read.
    """

    runtime_name: str
    versions: tuple[AgentVersion, ...]
    epoch: str
    generation: int
    claim_owner_sub: str | None


@dataclass
class RuntimeSlots:
    runtime_name: str
    owner_sub: str
    production_version_id: str | None = None
    staging_version_id: str | None = None
    previous_production_version_id: str | None = None
    last_promoted_at: str | None = None
    # F-81f — moved by every fenced trigger create (``TriggerStore.create_trigger``) inside the
    # same transaction that writes the trigger row, and pinned by the teardown release's slot
    # condition. It is what lets two writers that never read each other's table still exclude each
    # other: a trigger created after the release read this row changes the value the release
    # conditions on, so the release is cancelled instead of deleting the only handle on that
    # trigger. Opaque; only equality matters. Absent on rows written before it existed.
    trigger_fence: str | None = None

    def to_item(self) -> dict:
        item = {"runtime_name": self.runtime_name, "owner_sub": self.owner_sub}
        for fld in (
            "production_version_id",
            "staging_version_id",
            "previous_production_version_id",
            "last_promoted_at",
            "trigger_fence",
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
            trigger_fence=item.get("trigger_fence"),
        )


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


class AgentVersionsStore:
    """CRUD for the AgentVersions DDB table."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    @property
    def table_name(self) -> str:
        return self._table_name

    def _claim_key(self, runtime_name: str) -> dict[str, str]:
        return {"runtime_name": runtime_name, "version_id": NAME_CLAIM_VERSION_ID}

    def _ensure_name_claim(self, runtime_name: str) -> None:
        """Create the generation sentinel without overwriting a concurrent writer.

        ``if_not_exists`` makes this safe in both orderings: if bootstrap wins, the next writer
        increments zero; if a writer creates/increments the row first, bootstrap preserves that
        generation. Existing pre-rollout version rows are captured by the stable double-read that
        follows, so they do not need a one-action-per-row migration.
        """
        try:
            self._table.update_item(
                Key=self._claim_key(runtime_name),
                UpdateExpression=(
                    "SET #record_type = if_not_exists(#record_type, :record_type), "
                    "#epoch = if_not_exists(#epoch, :new_epoch), "
                    "#generation = if_not_exists(#generation, :zero)"
                ),
                ConditionExpression="attribute_not_exists(#record_type) OR #record_type = :record_type",
                ExpressionAttributeNames={
                    "#record_type": "record_type",
                    "#epoch": "claim_epoch",
                    "#generation": "claim_generation",
                },
                ExpressionAttributeValues={
                    ":record_type": NAME_CLAIM_RECORD_TYPE,
                    ":new_epoch": secrets.token_hex(16),
                    ":zero": 0,
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise NameClaimConflict(f"runtime name {runtime_name} has an incompatible name-claim record") from None
            raise

    def _read_name_claim_fence(self, runtime_name: str) -> tuple[str, int, str | None]:
        response = self._table.get_item(
            Key=self._claim_key(runtime_name),
            ConsistentRead=True,
            ProjectionExpression="#record_type, #epoch, #generation, #claim_owner",
            ExpressionAttributeNames={
                "#record_type": "record_type",
                "#epoch": "claim_epoch",
                "#generation": "claim_generation",
                "#claim_owner": "claim_owner_sub",
            },
        )
        item = response.get("Item")
        if not item:
            raise NameClaimConflict(f"runtime name {runtime_name} has no generation fence")
        if item.get("record_type") != NAME_CLAIM_RECORD_TYPE:
            raise NameClaimConflict(f"runtime name {runtime_name} has an invalid generation fence")
        epoch = item.get("claim_epoch")
        if not isinstance(epoch, str) or not epoch:
            raise NameClaimConflict(f"runtime name {runtime_name} has an invalid generation-fence epoch")
        raw = item.get("claim_generation")
        if isinstance(raw, bool) or not isinstance(raw, (int, Decimal)):
            raise NameClaimConflict(f"runtime name {runtime_name} has a non-numeric generation fence")
        generation = int(raw)
        if generation < 0 or Decimal(generation) != raw:
            raise NameClaimConflict(f"runtime name {runtime_name} has an invalid generation fence")
        claim_owner_sub = item.get("claim_owner_sub")
        if claim_owner_sub is not None and (not isinstance(claim_owner_sub, str) or not claim_owner_sub.strip()):
            raise NameClaimConflict(f"runtime name {runtime_name} has an invalid owner fence")
        return epoch, generation, claim_owner_sub

    def snapshot_for_name_release(self, runtime_name: str) -> NameClaimSnapshot:
        """Return a stable partition snapshot suitable for a bounded release.

        Every supported version-liveness writer advances the sentinel in the same transaction as
        its row write. A generation change around the Query means the rows were not one coherent
        observation, so retry from the beginning. Churn beyond the small bounded retry count is a
        conflict, never permission to release from an uncertain snapshot.
        """
        if not runtime_name:
            raise ValueError("runtime_name is required")
        self._ensure_name_claim(runtime_name)
        for _attempt in range(NAME_CLAIM_SNAPSHOT_ATTEMPTS):
            before = self._read_name_claim_fence(runtime_name)
            versions = tuple(self.list_for_runtime(runtime_name, consistent=True))
            after = self._read_name_claim_fence(runtime_name)
            if before == after:
                return NameClaimSnapshot(
                    runtime_name=runtime_name,
                    versions=versions,
                    epoch=after[0],
                    generation=after[1],
                    claim_owner_sub=after[2],
                )
        raise NameClaimConflict(
            f"runtime name {runtime_name} changed while its versions were being read; releasing nothing"
        )

    def put(self, version: AgentVersion) -> None:
        if version.version_id == NAME_CLAIM_VERSION_ID:
            raise ValueError("the name-claim sentinel is not an agent version")
        try:
            self._table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table_name,
                            "Item": version.to_item(),
                        }
                    },
                    _name_claim_generation_update(
                        self.table_name,
                        version.runtime_name,
                        claim_owner_sub=version.owner_sub or None,
                    ),
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise NameClaimConflict(
                    f"version row {version.runtime_name}/{version.version_id} was not written; "
                    "the runtime name is owned by another tenant or its claim changed"
                ) from None
            raise
        logger.info(
            "Wrote AgentVersion %s/%s (status=%s)",
            version.runtime_name,
            version.version_id,
            version.status,
        )

    def get(self, runtime_name: str, version_id: str, *, consistent: bool = False) -> AgentVersion | None:
        """Return one version row.

        ``consistent`` is for a reader whose result becomes a WRITE CONDITION: the trigger API
        pins the row it read into the transaction that creates the trigger, and a stale read there
        pins values that the transaction then cannot find, which fails a legitimate create.
        """
        resp = self._table.get_item(
            Key={"runtime_name": runtime_name, "version_id": version_id},
            ConsistentRead=consistent,
        )
        item = resp.get("Item")
        if not item or item.get("version_id") == NAME_CLAIM_VERSION_ID:
            return None
        return AgentVersion.from_item(item)

    def list_for_runtime(self, runtime_name: str, *, consistent: bool = False) -> list[AgentVersion]:
        """Return versions ordered newest-first (SK is sortable timestamp).

        ``consistent`` forces a strongly consistent read. The teardown name release needs it:
        an eventually consistent query can miss a version row written moments earlier, and the
        release would then conclude that nothing live remains and hand the name away while a
        brand-new deploy is mid-flight under it.
        """
        items: list[dict] = []
        kwargs: dict = {
            "KeyConditionExpression": Key("runtime_name").eq(runtime_name),
            "ScanIndexForward": False,  # newest first
            "ConsistentRead": consistent,
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [AgentVersion.from_item(i) for i in items if i.get("version_id") != NAME_CLAIM_VERSION_ID]

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
        expected_owner_sub: str | None = None,
    ) -> None:
        """Move an EXISTING version row's status. Never create one.

        F-83 — this used to be a bare ``update_item``, which in DynamoDB is an upsert. A peer
        session measured the consequence with real stores: a late Step Functions finalizer calling
        this after teardown had deleted the row RESURRECTED it, as a partial item carrying only
        ``{runtime_name, version_id, status: succeeded, runtime_id, runtime_arn}``. The model then
        reads ``owner_sub=""`` and ``deployment_id=""`` -- the exact ownerless shape every guard in
        this module refuses to touch -- so the name was locked by a row that could never be
        released, pointing at a runtime that no longer exists.

        ``attribute_exists(runtime_name)`` on the partition key is the minimum: a status transition
        is meaningless without a row to transition. The row and the name sentinel must ALSO name the
        same owner. This matters after a complete release: failed history from Alice is retained,
        Bob can acquire the free name, and a delayed Alice finalizer can still address her old row.
        Updating that row without checking Bob's sentinel would make Alice live underneath Bob's
        namespace.

        ``expected_owner_sub`` lets a caller supply the owner it already authenticated. Older
        internal callers omit it, so the store recovers the persisted owner with a strongly
        consistent read and then pins that value on BOTH rows in the transaction. Omission never
        means "existence only". If the row moves after the read, or the name has since been acquired
        by another owner, the transaction is cancelled.

        Raises ``NameClaimConflict`` when the row is gone, ownerless, not the expected owner's, or
        no longer belongs to the current name claim. Every caller today is best-effort and already
        logs; the honest failure is what the resurrection cost us.
        """
        if version_id == NAME_CLAIM_VERSION_ID:
            raise ValueError("the name-claim sentinel is not an agent version")
        owner_for_fence = expected_owner_sub
        if owner_for_fence is None:
            observed = self.get(runtime_name, version_id, consistent=True)
            if observed is None or not observed.owner_sub:
                raise NameClaimConflict(
                    f"version row {runtime_name}/{version_id} was not updated; "
                    "it is absent or has no attributable owner"
                )
            owner_for_fence = observed.owner_sub
        elif not owner_for_fence:
            raise ValueError("expected_owner_sub cannot be empty")

        set_parts = ["#s = :s"]
        names: dict[str, str] = {"#s": "status"}
        values: dict[str, str] = {
            ":s": status,
            ":expected_owner": owner_for_fence,
        }
        for col, val in [
            ("runtime_id", runtime_id),
            ("runtime_arn", runtime_arn),
            ("runtime_endpoint", runtime_endpoint),
            ("code_s3_key", code_s3_key),
        ]:
            if val is not None:
                set_parts.append(f"{col} = :{col}")
                values[f":{col}"] = val
        conditions = [
            "attribute_exists(runtime_name)",
            "owner_sub = :expected_owner",
        ]
        try:
            self._table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.table_name,
                            "Key": {"runtime_name": runtime_name, "version_id": version_id},
                            "UpdateExpression": "SET " + ", ".join(set_parts),
                            "ConditionExpression": " AND ".join(conditions),
                            "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": values,
                        }
                    },
                    _name_claim_generation_update(
                        self.table_name,
                        runtime_name,
                        claim_owner_sub=owner_for_fence,
                    ),
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                # No botocore message in the raise: it can echo the transaction, which carries the
                # tenant's sub and every value being written. A transaction is all-or-nothing, so
                # any cancellation has the same safe outcome for this caller.
                raise NameClaimConflict(
                    f"version row {runtime_name}/{version_id} was not updated; "
                    "it is absent, owned by someone else, or no longer holds the runtime name"
                ) from None
            raise

    def delete(self, runtime_name: str, version_id: str) -> None:
        if version_id == NAME_CLAIM_VERSION_ID:
            raise ValueError("the name-claim sentinel is not an agent version")
        observed = self.get(runtime_name, version_id, consistent=True)
        if observed is None or not observed.owner_sub.strip():
            raise NameClaimConflict(
                f"version row {runtime_name}/{version_id} was not deleted; it is absent or has no attributable owner"
            )
        try:
            self._table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Delete": {
                            "TableName": self.table_name,
                            "Key": {"runtime_name": runtime_name, "version_id": version_id},
                            "ConditionExpression": "owner_sub = :expected_owner",
                            "ExpressionAttributeValues": {
                                ":expected_owner": observed.owner_sub,
                            },
                        }
                    },
                    _name_claim_generation_update(
                        self.table_name,
                        runtime_name,
                        claim_owner_sub=observed.owner_sub,
                    ),
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise NameClaimConflict(
                    f"version row {runtime_name}/{version_id} was not deleted; "
                    "it is owned by someone else or no longer holds the runtime name"
                ) from None
            raise


class RuntimeSlotsStore:
    """CRUD for the RuntimeSlots DDB table."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    @property
    def table_name(self) -> str:
        return self._table_name

    def get(self, runtime_name: str, *, consistent: bool = False) -> RuntimeSlots | None:
        resp = self._table.get_item(Key={"runtime_name": runtime_name}, ConsistentRead=consistent)
        item = resp.get("Item")
        if not item:
            return None
        return RuntimeSlots.from_item(item)

    def upsert(self, slots: RuntimeSlots) -> None:
        """Write the whole row unconditionally.

        F-83: every WRITER of an existing row must use ``set_slot_pointers_atomically`` instead.
        This method writes the row it is given, so it does two things a stale caller cannot want:
        it resurrects a row a teardown just deleted, and -- because ``to_item`` omits a None -- it
        DROPS any attribute the caller's read did not see. The measured case was
        ``trigger_fence``: a staging finalizer that read the slot before a trigger was registered
        re-put the same pointers without the fence, which made the teardown's release conditions
        match again, so the release deleted the slot and version out from under a live trigger and
        left a schedule firing at a destroyed runtime with no handle to delete it by.

        Kept for the two callers that are creating the row for the first time and for tests that
        seed a table; the conditional path refuses to create and this one refuses to condition, so
        neither can quietly stand in for the other.
        """
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


# ---------------------------------------------------------------------------
# Atomic name-claim release (F-81e)
# ---------------------------------------------------------------------------

SLOT_POINTER_FIELDS = (
    "production_version_id",
    "staging_version_id",
    "previous_production_version_id",
)


def slot_pointer_pairs(slot: RuntimeSlots) -> tuple[tuple[str, str | None], ...]:
    """The three slot pointers as explicit ``(attribute name, value)`` pairs.

    Every caller needs both halves -- the name goes into a DynamoDB condition expression and the
    value is what the condition pins -- and the obvious way to get them is
    ``getattr(slot, field) for field in SLOT_POINTER_FIELDS``. This function exists so that no
    caller does that. The export bundle is scanned for attribute reads whose name comes from a
    variable, because a name that comes from a variable can come from data, and the scan cannot tell
    a loop over a module constant apart from a loop over a request field. Spelling the three reads
    out keeps the read set fixed at import time and keeps the scan's finding set empty, which is
    what makes a NEW dynamic read in this area visible instead of routine.

    ``SLOT_POINTER_FIELDS`` stays as the name-only tuple because callers validate field names
    against it; a test pins the two to the same three names in the same order, since a drift would
    silently leave one pointer out of a compare-and-set.
    """
    return (
        ("production_version_id", slot.production_version_id),
        ("staging_version_id", slot.staging_version_id),
        ("previous_production_version_id", slot.previous_production_version_id),
    )


class NameClaimConflict(RuntimeError):
    """The rows changed between the release's read and its write, so nothing was written.

    Always a whole-transaction outcome: either every conditional check passed and both rows
    moved, or none of them did. A caller must re-read rather than retry blindly -- the usual
    cause is a concurrent deploy or promote, and in that case not releasing is the right answer.
    """


class NameClaimReleaseTooLarge(NameClaimConflict):
    """A legacy caller's per-row release would exceed one TransactWriteItems.

    A subclass of NameClaimConflict because the OUTCOME is identical -- nothing was written, and
    the name stays locked -- so every caller's existing handling stays correct. It is a separate
    type because the CAUSE is not a lost race and a retry will never help. Current callers use the
    bounded generation snapshot; this remains as a fail-closed compatibility path.
    """


# DynamoDB's own limit on a transaction, from the service API (not a tunable). The release builds
# one action per row whose non-liveness it depends on, so the ceiling is reachable by a name with
# enough version history -- a peer session hit it with 100 rows and the raw ValidationException
# escaped past ``except NameClaimConflict`` into the teardown.
MAX_TRANSACT_ITEMS = 100


def release_name_claim_atomically(
    runtime_name: str,
    *,
    delete_version: tuple[str, str, str, str, str | None] | None = None,
    require_unchanged_rows: tuple[tuple[str, str, str | None], ...] = (),
    claim_snapshot: NameClaimSnapshot | None = None,
    release_name: bool = False,
    slot_action: str = "none",
    slot_expected: RuntimeSlots | None = None,
    slot_clear_fields: tuple[str, ...] = (),
) -> None:
    """Delete one version row and settle its slot row in a single DynamoDB transaction.

    Two properties the teardown release needs and could not get from sequential writes:

    * **Atomicity across the two tables.** Deleting the version row first and then failing on the
      slot leaves a slot pointing at a version that no longer exists, and
      ``resolve_owned_runtime_target`` requires slot + version + deployment to agree, so the
      tenant's production alias stops resolving -- a live agent made uninvokable by a cleanup
      step. The inverse (slot settled, version row left) is merely an extra row that blocks a
      redeploy, which the next teardown or a support action can clear. A transaction removes the
      choice: either both or neither.
    * **Compare-and-set against the state that was read.** Every condition here names a value the
      caller actually observed: the version row's ``owner_sub``/``deployment_id``, and the slot's
      ``owner_sub`` plus each pointer field (``attribute_not_exists`` when it was unset, because
      ``RuntimeSlots.to_item`` omits a None pointer rather than writing NULL). If a deploy or a
      promote moved the slot between the read and this write, the transaction is cancelled and
      ``NameClaimConflict`` is raised instead of the release overwriting it.

    ``delete_version`` is ``(version_id, expected_owner_sub, expected_deployment_id,
    expected_status, expected_created_at)``. The status is part of the identity on purpose: a stale
    ``pending`` row is deletable precisely BECAUSE it is stale, and a slow deploy finishing between
    the read and this write turns it into a live ``succeeded`` row. Without the status in the
    condition that is an ABA race that deletes a version that just came up.

    ``created_at`` is in the condition for the same reason, and it is NOT redundant with the
    status: the caller's liveness rule for a ``pending`` row is *status AND age*
    (``deployment_handler._pending_claim_still_live``), so a retry that re-``put``s the same
    ``version_id`` with a fresh timestamp turns a releasable stale-pending row into a live one
    while leaving ``status`` at exactly the ``"pending"`` the condition pinned. ``None`` means the
    row was read with no timestamp at all and becomes ``attribute_not_exists``.

    ``require_unchanged_rows`` is ``(version_id, observed_status, observed_created_at)`` for every
    OTHER row under the name whose non-liveness the caller relied on. They get ``ConditionCheck``
    entries with the same two terms for backward compatibility.

    ``claim_snapshot`` is the bounded replacement. It comes from
    ``AgentVersionsStore.snapshot_for_name_release`` and represents every row in that stable
    partition snapshot. Every supported version put/status/delete advances the sentinel in the
    same transaction as its row write. The release conditionally advances its exact epoch and
    generation and its current owner while deleting the exact target and settling the exact slot,
    so a new sibling, a completion, a stale-pending refresh, a tenant reacquisition, or an ABA
    sequence all cancel the whole release. When supplied, the per-sibling checks are intentionally
    omitted: retaining them would preserve the 100-action ceiling this fence exists to remove.

    ``release_name`` deletes the sentinel rather than advancing it once the caller has proved no
    live row or slot still holds the namespace. The epoch makes delete/recreate safe: a later
    deployment creates a new epoch, so a stale snapshot can never match merely because its numeric
    generation recurred.

    ``slot_action`` describes both the slot state the caller observed and what the release may do
    to it:

    * ``"assert_absent"`` pins a missing slot row. A finalizer or promote that creates the row
      after the caller's read cancels the whole release.
    * ``"check"`` pins an existing row without mutating it. This is required when the target
      version is not named by any pointer: a concurrent repoint to the target must cancel its
      deletion rather than leave a dangling slot.
    * ``"delete"`` removes the exact observed row.
    * ``"clear"`` removes only the named pointer fields from the exact observed row.
    * ``"none"`` means the caller's decision does not depend on slot state at all.

    A target deletion therefore normally uses one of the first four actions even when no slot
    mutation is needed. The slot check and the bounded claim fence keep the transaction at three
    actions regardless of version-history size.
    """
    slot_actions_with_expected_row = ("check", "delete", "clear")
    if slot_action not in ("none", "assert_absent", *slot_actions_with_expected_row):
        raise ValueError(f"unknown slot_action {slot_action!r}")
    if slot_action in slot_actions_with_expected_row and slot_expected is None:
        raise ValueError("slot_action requires the slot row that was read")
    if slot_action == "assert_absent" and slot_expected is not None:
        raise ValueError("slot_action='assert_absent' requires an absent slot read")
    if slot_action == "clear" and not slot_clear_fields:
        raise ValueError("slot_action='clear' requires at least one field to clear")
    if slot_action != "clear" and slot_clear_fields:
        raise ValueError("slot_clear_fields are valid only with slot_action='clear'")
    if claim_snapshot is not None:
        if claim_snapshot.runtime_name != runtime_name:
            raise ValueError("the name-claim snapshot belongs to a different runtime name")
        if (
            not claim_snapshot.epoch
            or isinstance(claim_snapshot.generation, bool)
            or not isinstance(claim_snapshot.generation, int)
            or claim_snapshot.generation < 0
        ):
            raise ValueError("the name-claim snapshot has an invalid fence")
        if claim_snapshot.claim_owner_sub is not None and not claim_snapshot.claim_owner_sub.strip():
            raise ValueError("the name-claim snapshot has an invalid owner fence")
    if release_name and claim_snapshot is None:
        raise ValueError("release_name requires a bounded name-claim snapshot")
    for field in slot_clear_fields:
        if field not in SLOT_POINTER_FIELDS:
            raise ValueError(f"refusing to clear unknown slot field {field!r}")

    delete_owner = str(delete_version[1]) if delete_version is not None else None
    slot_owner = (
        str(slot_expected.owner_sub)
        if slot_action in slot_actions_with_expected_row and slot_expected is not None
        else None
    )
    if delete_owner and slot_owner and delete_owner != slot_owner:
        raise NameClaimConflict(
            f"runtime name {runtime_name} was not released; its version and slot rows have different owners"
        )
    operation_owner = delete_owner or slot_owner
    if claim_snapshot is not None and claim_snapshot.claim_owner_sub is not None:
        if operation_owner != claim_snapshot.claim_owner_sub:
            raise NameClaimConflict(f"runtime name {runtime_name} was not released; its owner changed before cleanup")

    vstore = get_versions_store()
    sstore = get_slots_store()
    items: list[dict] = []

    def _liveness_terms(status: str, created_at: str | None) -> tuple[str, dict[str, object]]:
        """The two attributes a row's liveness is computed from, as a condition.

        ``created_at`` may legitimately be absent on a legacy row, and ``to_item`` writes nothing
        for a None, so the absent case has to be expressed as ``attribute_not_exists`` -- an
        equality against "" would compare against a missing attribute and never match, which would
        turn every release of such a row into a permanent false conflict.
        """
        terms = ["#st = :status"]
        vals: dict[str, object] = {":status": status}
        if created_at is None:
            terms.append("attribute_not_exists(created_at)")
        else:
            terms.append("created_at = :created")
            vals[":created"] = str(created_at)
        return " AND ".join(terms), vals

    if delete_version is not None:
        version_id, expected_owner, expected_deployment, expected_status, expected_created = delete_version
        if not (version_id and expected_owner and expected_deployment and expected_status):
            # Every one of these is an identity term. An empty one would make the condition
            # trivially true against a row that has the attribute absent, which is the
            # ownerless/legacy shape this whole path refuses to touch. ``created_at`` is
            # deliberately NOT in this list: absent is a real, representable state for it, and
            # refusing it here would make a legacy row unreleasable forever.
            raise ValueError("delete_version needs a version id, an owner sub, a deployment id and a status")
        live_expr, live_vals = _liveness_terms(expected_status, expected_created)
        items.append(
            {
                "Delete": {
                    "TableName": vstore.table_name,
                    "Key": {
                        "runtime_name": runtime_name,
                        "version_id": version_id,
                    },
                    "ConditionExpression": (f"owner_sub = :owner AND deployment_id = :dep AND {live_expr}"),
                    "ExpressionAttributeNames": {"#st": "status"},
                    "ExpressionAttributeValues": {
                        ":owner": expected_owner,
                        ":dep": expected_deployment,
                        **live_vals,
                    },
                }
            }
        )

    if claim_snapshot is None:
        for sibling_id, sibling_status, sibling_created in require_unchanged_rows:
            if not (sibling_id and sibling_status):
                raise ValueError("require_unchanged_rows needs a version id and a status")
            live_expr, live_vals = _liveness_terms(sibling_status, sibling_created)
            items.append(
                {
                    "ConditionCheck": {
                        "TableName": vstore.table_name,
                        "Key": {
                            "runtime_name": runtime_name,
                            "version_id": sibling_id,
                        },
                        "ConditionExpression": live_expr,
                        "ExpressionAttributeNames": {"#st": "status"},
                        "ExpressionAttributeValues": live_vals,
                    }
                }
            )

    if slot_action == "assert_absent":
        items.append(
            {
                "ConditionCheck": {
                    "TableName": sstore.table_name,
                    "Key": {"runtime_name": runtime_name},
                    "ConditionExpression": "attribute_not_exists(runtime_name)",
                }
            }
        )
    elif slot_action != "none":
        assert slot_expected is not None  # noqa: S101 - validated above
        if slot_expected.owner_sub:
            conditions = ["owner_sub = :slot_owner"]
            values: dict[str, object] = {":slot_owner": slot_expected.owner_sub}
        else:
            if slot_action != "check":
                raise ValueError("refusing to touch a slot row with no owner_sub")
            # A legacy ownerless slot may be OBSERVED while an independently
            # owned version row is removed, but it may never be mutated. The
            # dataclass intentionally normalizes both a missing attribute and
            # an empty legacy value to "", so pin that exact ownerless
            # equivalence class along with every pointer below.
            conditions = ["(attribute_not_exists(owner_sub) OR owner_sub = :empty_slot_owner)"]
            values = {":empty_slot_owner": ""}
        # last_promoted_at is in the condition even though it is not a pointer: a promote that
        # lands between the read and this write and happens to leave the same pointer values (an
        # ABA: promote v2, roll back to v1) still moves this field, so including it makes the
        # compare-and-set detect a slot the tenant just touched.
        if slot_expected.last_promoted_at:
            conditions.append("last_promoted_at = :promoted")
            values[":promoted"] = str(slot_expected.last_promoted_at)
        else:
            conditions.append("attribute_not_exists(last_promoted_at)")
        for idx, (field, observed) in enumerate(slot_pointer_pairs(slot_expected)):
            if observed:
                conditions.append(f"{field} = :ptr{idx}")
                values[f":ptr{idx}"] = str(observed)
            else:
                conditions.append(f"attribute_not_exists({field})")
        # F-81f — the trigger fence. The trigger API creates its row in a transaction that also
        # moves this attribute (``TriggerStore.create_trigger``). Pinning it here is what joins the
        # two: a trigger registered after this release read the slot -- even one registered after
        # the release's own enumeration of the triggers table came back empty -- changes this value
        # and cancels the release, so the slot the owner needs to delete that trigger survives.
        # Without it the release's conditions were all satisfiable by a slot that had just been
        # used to authorize a brand-new trigger.
        if slot_expected.trigger_fence:
            conditions.append("trigger_fence = :fence")
            values[":fence"] = str(slot_expected.trigger_fence)
        else:
            conditions.append("attribute_not_exists(trigger_fence)")
        key = {"runtime_name": runtime_name}
        slot_request = {
            "TableName": sstore.table_name,
            "Key": key,
            "ConditionExpression": " AND ".join(conditions),
            "ExpressionAttributeValues": values,
        }
        if slot_action == "check":
            items.append({"ConditionCheck": slot_request})
        elif slot_action == "delete":
            items.append({"Delete": slot_request})
        else:
            slot_request["UpdateExpression"] = "REMOVE " + ", ".join(slot_clear_fields)
            items.append({"Update": slot_request})

    if not items:
        return

    # This is both the bounded snapshot condition and the mutation record for the target delete.
    # The legacy path advances unconditionally so a release performed by an old caller is still
    # visible to every later bounded snapshot. A complete release deletes the sentinel; its epoch
    # prevents a stale snapshot from matching a later delete/recreate cycle.
    if release_name:
        assert claim_snapshot is not None  # noqa: S101 - validated above
        items.append(
            _name_claim_delete(
                vstore.table_name,
                runtime_name,
                expected_epoch=claim_snapshot.epoch,
                expected_generation=claim_snapshot.generation,
                expected_owner_sub=claim_snapshot.claim_owner_sub,
            )
        )
    else:
        items.append(
            _name_claim_generation_update(
                vstore.table_name,
                runtime_name,
                claim_owner_sub=operation_owner,
                expected_epoch=claim_snapshot.epoch if claim_snapshot else None,
                expected_generation=claim_snapshot.generation if claim_snapshot else None,
            )
        )

    if len(items) > MAX_TRANSACT_ITEMS:
        # Refuse HERE, with the outcome the caller already handles. Sending it would return a
        # ValidationException, which is a raw ClientError -- so it escaped ``except
        # NameClaimConflict`` in the teardown, and its botocore message echoes the entire request:
        # every row value and the tenant's sub, into whatever log line caught it.
        #
        # Deliberately NOT truncated to fit. Dropping sibling ConditionChecks is what they are
        # there to prevent: a dropped check is a sibling whose pending->succeeded completion the
        # release can no longer see, so it would release a name another row now holds. A locked
        # name is recoverable; that is not. Current production callers supply ``claim_snapshot``
        # and therefore never build this unbounded list; this is the fail-closed compatibility path.
        logger.error(
            "Name-claim release for %s needs %d transaction actions (max %d); releasing nothing",
            runtime_name,
            len(items),
            MAX_TRANSACT_ITEMS,
        )
        raise NameClaimReleaseTooLarge(
            f"runtime name {runtime_name} was not released; the release needs {len(items)} "
            f"transaction actions and DynamoDB allows {MAX_TRANSACT_ITEMS}"
        )

    client = vstore._table.meta.client  # noqa: SLF001 - low-level client for the transaction
    try:
        client.transact_write_items(TransactItems=items)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            reasons = [r.get("Code", "") for r in exc.response.get("CancellationReasons", [])]
            if "ConditionalCheckFailed" not in reasons:
                # Not a lost race: a throttle, a capacity cancellation, a serialization error, or
                # an empty CancellationReasons (the API does not always populate it). The OUTCOME is
                # identical -- a transaction is all-or-nothing, so nothing was written -- and that
                # is why it converts too: letting a raw ClientError escape here hands the caller a
                # botocore message that echoes the whole request, including every row value and the
                # tenant's sub, into whatever log line catches it. But it is NOT the expected
                # outcome, so it is loud: a serialization bug in this very function cancels with
                # non-condition reasons, and must not read as "the compare-and-set is working".
                logger.error(
                    "Name-claim release for %s was cancelled for a non-condition reason %s; nothing was written",
                    runtime_name,
                    reasons or "<none reported>",
                )
            # Deliberately no botocore message in the raise: it echoes the request, which
            # carries the caller's sub and the row values.
            raise NameClaimConflict(
                f"runtime name {runtime_name} was not released; transaction cancelled ({reasons})"
            ) from None
        raise
    logger.info(
        "Released name claim rows for %s (version=%s, slot=%s%s, claim_generation=%s, released=%s)",
        runtime_name,
        delete_version[0] if delete_version else None,
        slot_action,
        f", cleared {','.join(slot_clear_fields)}" if slot_clear_fields else "",
        claim_snapshot.generation if claim_snapshot else None,
        release_name,
    )


class SlotWriteConflict(RuntimeError):
    """The slot row moved between the caller's read and its write, so nothing was written.

    Distinct from ``NameClaimConflict``: that one means a name could not be RELEASED, which is
    recoverable by leaving the name locked. This one means a pointer move -- a promote, a rollback
    or a deploy finalizer -- did not land, and the caller must report that rather than assume it
    did. Retrying is only correct after a fresh read: the usual cause is that another writer moved
    the row, and the second-most-usual is that a teardown deleted it, where the right answer is not
    to write at all.
    """


#: The two slots a pointer write may fence to. ``previous_production_version_id`` is deliberately
#: absent: it is never a promotion TARGET, only the value production had a moment ago, so a fence
#: naming it would be describing a version nothing is pointing at on purpose.
FENCEABLE_SLOTS = ("production", "staging")

#: The only version status a slot may point at. ``runtime_target_context`` refuses to resolve a
#: pointer to anything else, so writing one produces a slot the API reports as moved and then cannot
#: invoke -- a successful-looking write with no usable outcome.
PROMOTABLE_VERSION_STATUS = "succeeded"


@dataclass(frozen=True)
class VersionFence:
    """The version row a pointer write is fenced to, and WHICH pointer it has to be.

    The ``slot`` field is the whole point. Without it, ``require_version`` was a liveness check on
    an ARBITRARY version row, which a measured probe (peer 8b) exploited: it fenced a live ``v1``
    while writing ``production_version_id="v-does-not-exist"``, and the write succeeded. A caller
    reading that code would reasonably believe the pointer it was writing had been proven to exist
    and be succeeded, and it had not -- some OTHER row had. Naming the slot makes the fence assert
    the thing the caller thinks it asserts, and it is stated by the caller rather than inferred from
    which pointers happen to have changed, because a promote that re-promotes the current version
    changes no pointer at all and would leave nothing to infer from.

    ``deployment_id`` is the immutable identity of the deploy that produced the version, and it is
    REQUIRED. A version row that is deleted and re-created by a retry keeps its id and its
    ``succeeded`` status but not its deploy, so pinning it is what stops the pointer naming a version
    whose content was replaced underneath. An earlier draft made it optional to keep a legacy row
    (``deployment_id`` reads back as ``""``) promotable; peer 2b pointed out that is not a kindness,
    because ``runtime_target_context`` refuses such a version outright -- promoting it would move the
    pointer and then answer 404 on every invoke. Refusing the write is the same outcome, reported at
    the point the caller can act on it.

    ``status`` is required to be ``succeeded`` for the same reason, and measured the same way: a
    probe fenced a ``pending`` version and got a pointer move the resolver then rejected.

    ``runtime_id`` and ``runtime_arn`` are pinned when present and are NOT required, because HARNESS
    mode legitimately produces a version with neither -- ``harness_id``/``harness_arn`` take their
    place. Requiring them would silently stop every harness deploy's slot from moving. Where they
    are present they belong in the condition: a re-put row can carry the same status and
    ``deployment_id`` and still point at a different runtime.
    """

    version_id: str
    owner_sub: str
    status: str
    created_at: str | None
    slot: str
    deployment_id: str
    runtime_id: str | None = None
    runtime_arn: str | None = None


def _slot_mutable_pairs(slot: RuntimeSlots) -> tuple[tuple[str, str | None], ...]:
    """The four attributes a pointer write may touch, as explicit ``(name, value)`` pairs.

    The three pointers plus ``last_promoted_at``. Deliberately NOT ``owner_sub`` (immutable: a slot
    changing hands is a different operation) and deliberately NOT ``trigger_fence`` -- see
    ``set_slot_pointers_atomically``, where never naming the fence in the expression is the fix.
    """
    return (*slot_pointer_pairs(slot), ("last_promoted_at", slot.last_promoted_at))


def set_slot_pointers_atomically(
    runtime_name: str,
    *,
    expected: RuntimeSlots | None,
    new: RuntimeSlots,
    require_version: VersionFence | None = None,
) -> None:
    """Move a slot's pointers as a compare-and-set, optionally fenced to a version row.

    F-83. Replaces ``upsert`` for every caller that is CHANGING an existing row: the deploy
    finalizer, promote and rollback. All three read the slot, mutated the dataclass and re-put the
    whole row, which is wrong in three separate ways that a real-store probe reproduced:

    1. It resurrects. A teardown that deleted the slot and version between the read and the write
       gets the row back, pointing at a version that no longer exists
       (``version_exists=False / slot_resurrected=True / staging_pointer=v1``). The conditional
       ``attribute_exists(runtime_name)`` is what refuses that.
    2. It clobbers concurrent moves. Two promotes, or a promote and a finalizer, both read the same
       row and the second overwrite silently discards the first -- the classic lost update, and here
       the lost value decides where every invocation and trigger resolves.
    3. It DROPS attributes it never read. ``to_item`` omits a None, so a writer holding a slot read
       before ``trigger_fence`` existed re-puts the row without it. That erasure re-satisfies the
       teardown release's fence condition, so the release then deletes the slot and version out from
       under a live trigger. This is why the update expression names exactly the four mutable
       attributes and never mentions ``trigger_fence``: an attribute that is not in the expression
       cannot be erased by a writer that did not know about it, which also means a NEW fence-like
       attribute is safe from these callers by default rather than by remembering to preserve it.

    The fence is deliberately NOT in the CONDITION either. A trigger created between this caller's
    read and its write does not invalidate a pointer move -- the two operations are compatible -- and
    conditioning on it would fail a legitimate promote whenever a trigger was registered
    concurrently, turning a successful deploy into a reported failure. Not writing it is what
    protects it; pinning it would only add false conflicts.

    ``expected=None`` means "create": a Put conditioned on ``attribute_not_exists(runtime_name)``,
    so the first-deploy path cannot silently overwrite a row that appeared in the meantime.

    ``require_version`` is a ``VersionFence`` and adds a ConditionCheck on the AgentVersions row in
    the SAME transaction. Without it, the version status that authorized this promote is a value read
    earlier: a teardown can delete the row, or a retry can re-put it with a fresh ``created_at``, and
    the slot ends up naming a version that is gone or is no longer the one that was approved. It is
    the same liveness pair (``status`` AND ``created_at``) that the release conditions on, for the
    same reason -- a re-put row is a different claim even when every other field matches. The fence
    also names WHICH pointer it authorizes and is rejected unless that pointer in ``new`` is exactly
    the fenced version; see ``VersionFence``.

    Raises ``SlotWriteConflict`` if anything was cancelled, and ``ValueError`` for an argument that
    could only ever produce an unsafe write.
    """
    if not runtime_name:
        raise ValueError("runtime_name is required")
    if not new.owner_sub:
        raise ValueError("refusing to write a slot row with no owner_sub")
    # Checked for BOTH paths, not just the conditional one. ``runtime_name`` is the key every
    # condition, every log line and the caller's own authorization check are about, while the Put
    # writes ``new.to_item()`` -- whose ``runtime_name`` IS the partition key. A measured probe
    # (peer 8b) passed runtime_name="expected-name" with new.runtime_name="different-name" and a
    # version fence under expected-name, and the create succeeded: it authorized against one
    # runtime and wrote the pointer row of another.
    if new.runtime_name != runtime_name:
        raise ValueError("the new row must be the row being written")
    if expected is not None:
        # The ownerless/legacy row shape every other guard refuses. An empty expected value would
        # be compared against an ABSENT attribute, which can match, so it must not be expressible.
        if not expected.owner_sub:
            raise ValueError("refusing to condition on a slot row with no owner_sub")
        if expected.owner_sub != new.owner_sub:
            raise ValueError("a pointer write may not change owner_sub")
        if expected.runtime_name != runtime_name:
            raise ValueError("the expected row must be the row being written")

    sstore = get_slots_store()
    vstore = get_versions_store()
    items: list[dict] = []
    key = {"runtime_name": runtime_name}

    if expected is None:
        item = new.to_item()
        # A row being created carries no fence: the only writer of one is a trigger create, which
        # requires a slot to already exist. Passing one here would be a caller inventing a fence
        # value, and a teardown would then condition on it.
        item.pop("trigger_fence", None)
        items.append(
            {
                "Put": {
                    "TableName": sstore.table_name,
                    "Item": item,
                    "ConditionExpression": "attribute_not_exists(runtime_name)",
                }
            }
        )
    else:
        sets: list[str] = []
        removes: list[str] = []
        values: dict[str, object] = {":owner": expected.owner_sub}
        conditions = ["attribute_exists(runtime_name)", "owner_sub = :owner"]
        for idx, ((field, was), (same_field, now)) in enumerate(
            zip(_slot_mutable_pairs(expected), _slot_mutable_pairs(new), strict=True)
        ):
            if field != same_field:  # pragma: no cover - the pairs are literal in one function
                raise ValueError("slot field order disagreed between the expected and new rows")
            if was:
                conditions.append(f"{field} = :e{idx}")
                values[f":e{idx}"] = str(was)
            else:
                conditions.append(f"attribute_not_exists({field})")
            if now:
                sets.append(f"{field} = :n{idx}")
                values[f":n{idx}"] = str(now)
            else:
                removes.append(field)
        clauses: list[str] = []
        if sets:
            clauses.append("SET " + ", ".join(sets))
        if removes:
            clauses.append("REMOVE " + ", ".join(removes))
        expression = " ".join(clauses)
        items.append(
            {
                "Update": {
                    "TableName": sstore.table_name,
                    "Key": key,
                    "UpdateExpression": expression,
                    "ConditionExpression": " AND ".join(conditions),
                    "ExpressionAttributeValues": values,
                }
            }
        )

    # Which fenceable pointers are ACQUIRING a version -- taking a value the row did not already
    # hold. Those are the only writes that can leave a pointer naming a row that does not exist, so
    # those are the ones that need a fence. A pointer being CLEARED needs none: a REMOVE cannot
    # create a dangling pointer. A pointer being re-written to the value it already has needs none
    # either, though its caller passes one anyway and that proof is still checked below.
    prior_production = expected.production_version_id if expected is not None else None
    prior_staging = expected.staging_version_id if expected is not None else None
    acquiring = []
    if new.production_version_id and new.production_version_id != prior_production:
        acquiring.append("production")
    if new.staging_version_id and new.staging_version_id != prior_staging:
        acquiring.append("staging")
    if acquiring and require_version is None:
        raise ValueError(
            f"this write points {', '.join(acquiring)} at a version but carries no fence; an "
            "unfenced pointer can name a version row a teardown already deleted, which reads back "
            "as a promoted runtime that 404s on every invoke"
        )

    if require_version is not None:
        fence = require_version
        if not fence.version_id or not fence.owner_sub or not fence.status:
            raise ValueError("a version fence needs a version_id, an owner_sub and a status")
        if fence.status != PROMOTABLE_VERSION_STATUS:
            raise ValueError(f"a slot may only point at a {PROMOTABLE_VERSION_STATUS} version, not {fence.status!r}")
        if not fence.deployment_id:
            raise ValueError(
                "a version fence needs the version's deployment_id; a version row without one "
                "cannot be resolved to a runtime, so pointing a slot at it moves the pointer to "
                "something no invoke can use"
            )
        if bool(fence.runtime_id) != bool(fence.runtime_arn):
            # The two are pinned when present and BOTH absent is the legitimate HARNESS case, where
            # ``harness_id``/``harness_arn`` carry the identity instead. Exactly one of them is
            # neither: ``runtime_target_context`` requires a non-empty ``runtime_id`` AND a
            # well-formed ``runtime_arn`` whose tail matches it, so a half-identity row resolves for
            # nobody. Accepting it writes a pointer at a version no caller can act on, and it also
            # makes the harness exemption reachable by a RUNTIME row that merely lost one attribute.
            raise ValueError(
                "a version fence must carry both runtime_id and runtime_arn or neither; exactly one "
                "of them is neither a resolvable runtime nor a harness deploy"
            )
        if fence.owner_sub != new.owner_sub:
            # The slot row and the version it points at belong to the same tenant by construction;
            # a mismatch means the caller mixed up two reads, and the write would point one
            # tenant's slot at another tenant's version.
            raise ValueError("the version fence and the slot row must have the same owner_sub")
        # Spelled out rather than ``getattr(new, f"{fence.slot}_version_id")``. Two reasons, and the
        # second is the load-bearing one: the export bundle is scanned for attribute reads whose name
        # comes from a variable, and an unknown slot name must be an ERROR rather than an
        # AttributeError from a computed read -- ``fence.slot="owner_sub"`` would otherwise be a
        # read of a real attribute that happens to exist.
        if fence.slot == "production":
            written = new.production_version_id
        elif fence.slot == "staging":
            written = new.staging_version_id
        else:
            raise ValueError(f"a version fence must name one of {FENCEABLE_SLOTS}, not {fence.slot!r}")
        if written != fence.version_id:
            raise ValueError(
                f"the version fence names {fence.slot} but the row being written has a different "
                f"{fence.slot} pointer; the fence must be the pointer it authorizes"
            )
        # And the fence must cover every pointer this write is ACQUIRING. Without this, a fence is
        # satisfiable by a slot the write does not touch: production already equals v1, so a fence on
        # production passes for a write whose only real effect is staging=v9. Each slot is resolved by
        # a different caller, so proving one authorizes nothing about the other. All four callers
        # (promote to either slot, rollback, the deploy finalizer) acquire exactly one pointer, so
        # one fence is enough -- a future writer that moves both needs two, and should say so here
        # rather than pass whichever one happens to satisfy the check.
        unfenced = [slot for slot in acquiring if slot != fence.slot]
        if unfenced:
            raise ValueError(
                f"the version fence names {fence.slot} but this write also points "
                f"{', '.join(unfenced)} at a new version; every slot that acquires a version needs "
                "its own proof that the version is live"
            )
        v_conditions = ["attribute_exists(version_id)", "owner_sub = :v_owner", "#st = :v_status"]
        v_values: dict[str, object] = {":v_owner": fence.owner_sub, ":v_status": fence.status}
        v_conditions.append("deployment_id = :v_dep")
        v_values[":v_dep"] = str(fence.deployment_id)
        if fence.runtime_id:
            v_conditions.append("runtime_id = :v_rid")
            v_values[":v_rid"] = str(fence.runtime_id)
            v_conditions.append("runtime_arn = :v_rarn")
            v_values[":v_rarn"] = str(fence.runtime_arn)
        else:
            # The harness case pins ABSENCE. Adding no term at all here would make the harness fence
            # the WEAKEST of the two, satisfied by a row that gained a runtime identity after the read
            # while every other fenced term stayed the same -- which is exactly what a deploy
            # finalizer does to a row a harness caller read a moment earlier. The parity invariant
            # above means one branch covers both fields.
            v_conditions.append("attribute_not_exists(runtime_id)")
            v_conditions.append("attribute_not_exists(runtime_arn)")
        if fence.created_at:
            v_conditions.append("created_at = :v_created")
            v_values[":v_created"] = str(fence.created_at)
        else:
            # A legacy row genuinely has no created_at; an equality against "" would compare with an
            # absent attribute, never match, and make that row permanently un-promotable.
            v_conditions.append("attribute_not_exists(created_at)")
        items.append(
            {
                "ConditionCheck": {
                    "TableName": vstore.table_name,
                    "Key": {"runtime_name": runtime_name, "version_id": fence.version_id},
                    "ConditionExpression": " AND ".join(v_conditions),
                    "ExpressionAttributeNames": {"#st": "status"},
                    "ExpressionAttributeValues": v_values,
                }
            }
        )

    client = sstore._table.meta.client  # noqa: SLF001 - low-level client for the transaction
    try:
        client.transact_write_items(TransactItems=items)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            reasons = [r.get("Code", "") for r in exc.response.get("CancellationReasons", [])]
            if "ConditionalCheckFailed" not in reasons:
                # A throttle, a capacity cancellation, a serialization error, or an unpopulated
                # CancellationReasons. The OUTCOME is the same -- a transaction is all-or-nothing --
                # but it is not the expected one, so it is loud: a serialization bug in this very
                # function cancels with non-condition reasons and must not read as a working CAS.
                logger.error(
                    "Slot pointer write for %s was cancelled for a non-condition reason %s; nothing was written",
                    runtime_name,
                    reasons or "<none reported>",
                )
            # No botocore message in the raise: it echoes the request, which carries the caller's
            # sub and every row value.
            raise SlotWriteConflict(
                f"slot row {runtime_name} was not written; the row or its version moved ({reasons})"
            ) from None
        raise
    logger.info(
        "Set RuntimeSlots pointers for %s (prod=%s, staging=%s, prev=%s, created=%s, fence=%s)",
        runtime_name,
        new.production_version_id,
        new.staging_version_id,
        new.previous_production_version_id,
        expected is None,
        f"{require_version.slot}={require_version.version_id}" if require_version else None,
    )
