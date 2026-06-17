"""DynamoDB-backed store for scheduled / event triggers — Phase 3 Gap 3F.

One table:

* ``TriggersTable`` — one row per (runtime_name, trigger_id). A tenant
  registers a cron / EventBridge / S3 / webhook trigger against the production
  slot of one of their runtimes. The router resolves ownership through the
  production slot (mirroring ``evaluations._resolve_owned_runtime_id``) BEFORE
  any write, so a tenant can never register a trigger on another tenant's
  runtime_name (Bug 122 PK-collision protection comes for free).

  PK ``runtime_name`` — a tenant-supplied friendly name. It is server-validated
  for charset/length at the router boundary and the write is gated on the
  production-slot owner, so the Bug 122 PK-collision class is closed by the
  ownership resolution, NOT by the key shape. SK ``trigger_id`` is sortable
  (lex order == chronological), the identical layout to
  ``hitl_store.new_request_id`` / ``agent_versions_store.new_version_id``. A GSI
  ``owner_sub-trigger_id-index`` powers the owner-scoped list-across-runtimes
  query.

Tenant isolation (Critic Finding 3, Bug 37): every row stamps ``owner_sub``;
list-by-owner uses the owner_sub GSI; the router re-checks ownership on get /
delete. Cross-tenant requests return 404 (existence-non-disclosure).

Secrets (lessons.md rule 5): ``webhook_secret_ref`` is the *ARN* of an
owner-scoped Secrets Manager secret holding the HMAC signing key — never the
raw secret. ``webhook_out_url``, if set, is an outbound POST target that MUST
be SSRF-validated before any server-side fetch (mirror
``gateway_deployer._validate_discovery_url``); the store only persists it.

Cleanup (Bug 124): ``eventbridge_rule_arn`` / ``scheduler_name`` are the
provisioned-resource handles so ``runtime_deployer.destroy_runtime`` can tear
down the live cron/rule/Function-URL + delete the webhook secret + the DDB rows
when the runtime is destroyed (described as the integration hook).
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)


# Trigger types.
TYPE_CRON = "cron"
TYPE_EVENTBRIDGE = "eventbridge"
TYPE_S3 = "s3"
TYPE_WEBHOOK = "webhook"
TRIGGER_TYPES = (TYPE_CRON, TYPE_EVENTBRIDGE, TYPE_S3, TYPE_WEBHOOK)

# Status values.
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUS_PROVISIONING = "provisioning"
STATUS_ERROR = "error"
# Bug 139: a trigger is RECORDED in the store but its AWS resource (EventBridge
# rule / Scheduler / Function URL) is not yet provisioned by the platform — so it
# does NOT fire yet. Stamp new triggers REGISTERED (not ACTIVE) so the UI never
# falsely tells a customer the trigger is live. Flip to ACTIVE only once the AWS
# resource is created and its handle stamped (TriggerStore.update_status).
STATUS_REGISTERED = "registered"
TRIGGER_STATUSES = (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_PROVISIONING,
    STATUS_ERROR,
    STATUS_REGISTERED,
)


# ---------------------------------------------------------------------------
# Sortable id (ULID-shaped, 16 bytes hex). Lex order = chronological.
# ---------------------------------------------------------------------------


def new_trigger_id() -> str:
    """Return a 32-char lowercase hex string sortable by creation time.

    Layout: 12 hex chars of millisecond epoch + 20 hex chars of random — the
    identical shape to ``hitl_store.new_request_id`` so SK ordering is
    chronological. 32 chars total = 16 bytes.
    """
    ms = int(time.time() * 1000)
    return f"{ms:012x}{secrets.token_hex(10)}"


# ---------------------------------------------------------------------------
# Decimal/float helpers shared with the other DDB stores.
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
        # Preserve integer-valued Decimals as ints (created_at/updated_at).
        if obj == obj.to_integral_value():
            return int(obj)
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimals_to_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimals_to_floats(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Model (lightweight dataclass; not Pydantic — internal)
# ---------------------------------------------------------------------------


@dataclass
class Trigger:
    runtime_name: str
    trigger_id: str
    owner_sub: str
    type: str  # cron | eventbridge | s3 | webhook
    target_runtime_arn: str
    status: str = STATUS_ACTIVE
    schedule: Optional[str] = None  # cron expr (type=cron)
    pattern: Optional[dict] = None  # event JSON (type=eventbridge/s3)
    webhook_secret_ref: Optional[str] = None  # Secrets Manager ARN (never the secret)
    webhook_out_url: Optional[str] = None  # validated outbound POST target
    eventbridge_rule_arn: Optional[str] = None  # provisioned handle (cleanup)
    scheduler_name: Optional[str] = None  # provisioned handle (cleanup)
    function_url: Optional[str] = None  # webhook Function URL (cleanup)
    created_at: int = 0  # epoch milliseconds
    updated_at: int = 0  # epoch milliseconds

    def to_item(self) -> dict:
        item = {
            "runtime_name": self.runtime_name,
            "trigger_id": self.trigger_id,
            "owner_sub": self.owner_sub,
            "type": self.type,
            "target_runtime_arn": self.target_runtime_arn,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        for fld in (
            "schedule",
            "pattern",
            "webhook_secret_ref",
            "webhook_out_url",
            "eventbridge_rule_arn",
            "scheduler_name",
            "function_url",
        ):
            val = getattr(self, fld)
            if val is not None:
                item[fld] = val
        return _floats_to_decimals(item)

    @classmethod
    def from_item(cls, item: dict) -> "Trigger":
        item = _decimals_to_floats(dict(item))
        return cls(
            runtime_name=item["runtime_name"],
            trigger_id=item["trigger_id"],
            owner_sub=item.get("owner_sub", ""),
            type=item.get("type", ""),
            target_runtime_arn=item.get("target_runtime_arn", ""),
            status=item.get("status", STATUS_ACTIVE),
            schedule=item.get("schedule"),
            pattern=item.get("pattern"),
            webhook_secret_ref=item.get("webhook_secret_ref"),
            webhook_out_url=item.get("webhook_out_url"),
            eventbridge_rule_arn=item.get("eventbridge_rule_arn"),
            scheduler_name=item.get("scheduler_name"),
            function_url=item.get("function_url"),
            created_at=int(item.get("created_at", 0) or 0),
            updated_at=int(item.get("updated_at", 0) or 0),
        )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TriggerStore:
    """CRUD + listing for the Triggers DDB table."""

    GSI_NAME = "owner_sub-trigger_id-index"

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def create_trigger(
        self,
        *,
        runtime_name: str,
        owner_sub: str,
        type: str,
        target_runtime_arn: str,
        status: str = STATUS_REGISTERED,
        schedule: Optional[str] = None,
        pattern: Optional[dict] = None,
        webhook_secret_ref: Optional[str] = None,
        webhook_out_url: Optional[str] = None,
        eventbridge_rule_arn: Optional[str] = None,
        scheduler_name: Optional[str] = None,
        function_url: Optional[str] = None,
        trigger_id: Optional[str] = None,
    ) -> Trigger:
        """Write a trigger row and return it.

        ``trigger_id`` is normally minted here; callers may pass one for tests.
        ``target_runtime_arn`` MUST be derived server-side from the resolved
        owned version — never from a request body (confused-deputy guard).
        """
        now_ms = int(time.time() * 1000)
        trig = Trigger(
            runtime_name=runtime_name,
            trigger_id=trigger_id or new_trigger_id(),
            owner_sub=owner_sub,
            type=type,
            target_runtime_arn=target_runtime_arn,
            status=status,
            schedule=schedule,
            pattern=pattern,
            webhook_secret_ref=webhook_secret_ref,
            webhook_out_url=webhook_out_url,
            eventbridge_rule_arn=eventbridge_rule_arn,
            scheduler_name=scheduler_name,
            function_url=function_url,
            created_at=now_ms,
            updated_at=now_ms,
        )
        self._table.put_item(Item=trig.to_item())
        logger.info(
            "Wrote trigger %s/%s (owner=%s, type=%s, status=%s)",
            trig.runtime_name,
            trig.trigger_id,
            trig.owner_sub,
            trig.type,
            trig.status,
        )
        return trig

    def get(self, runtime_name: str, trigger_id: str) -> Optional[Trigger]:
        resp = self._table.get_item(
            Key={"runtime_name": runtime_name, "trigger_id": trigger_id}
        )
        item = resp.get("Item")
        if not item:
            return None
        return Trigger.from_item(item)

    def list_for_runtime(self, runtime_name: str) -> list[Trigger]:
        """Return all triggers for ``runtime_name``, newest-first.

        SECURITY: this is NOT owner-scoped — callers MUST gate the runtime by
        ownership (resolve through the production slot) before calling this, and
        the router additionally visibility-filters the result to the caller
        (defense in depth, Bug 126 authz-drift).
        """
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
        return [Trigger.from_item(i) for i in items]

    def list_for_owner(self, owner_sub: str) -> list[Trigger]:
        """Return all of a tenant's triggers across runtimes via the owner GSI.

        Newest-first (SK is the sortable trigger_id). A caller only ever sees
        rows stamped with their own sub — no cross-tenant leakage.
        """
        items: list[dict] = []
        kwargs: dict = {
            "IndexName": self.GSI_NAME,
            "KeyConditionExpression": Key("owner_sub").eq(owner_sub),
            "ScanIndexForward": False,  # newest first
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [Trigger.from_item(i) for i in items]

    def update_status(
        self,
        *,
        runtime_name: str,
        trigger_id: str,
        status: str,
        eventbridge_rule_arn: Optional[str] = None,
        scheduler_name: Optional[str] = None,
        function_url: Optional[str] = None,
    ) -> Optional[Trigger]:
        """Flip a trigger's status (and optionally stamp provisioned handles).

        Returns the updated row, or None if the row doesn't exist (idempotent).
        """
        if status not in TRIGGER_STATUSES:
            raise ValueError(f"Invalid status: {status!r}")

        now_ms = int(time.time() * 1000)
        set_parts = ["#s = :s", "updated_at = :u"]
        names = {"#s": "status"}
        values = {":s": status, ":u": now_ms}
        for attr, val in (
            ("eventbridge_rule_arn", eventbridge_rule_arn),
            ("scheduler_name", scheduler_name),
            ("function_url", function_url),
        ):
            if val is not None:
                set_parts.append(f"{attr} = :{attr}")
                values[f":{attr}"] = val

        from botocore.exceptions import ClientError

        try:
            resp = self._table.update_item(
                Key={"runtime_name": runtime_name, "trigger_id": trigger_id},
                UpdateExpression="SET " + ", ".join(set_parts),
                ConditionExpression="attribute_exists(trigger_id)",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if (
                e.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                return None
            raise
        return Trigger.from_item(resp["Attributes"])

    def delete(self, runtime_name: str, trigger_id: str) -> None:
        """Delete a trigger row. Idempotent (no-op if already gone)."""
        self._table.delete_item(
            Key={"runtime_name": runtime_name, "trigger_id": trigger_id}
        )
        logger.info("Deleted trigger %s/%s", runtime_name, trigger_id)


# ---------------------------------------------------------------------------
# Convenience singleton (lazy-init from env)
# ---------------------------------------------------------------------------

_trigger_store: Optional[TriggerStore] = None


def get_trigger_store() -> TriggerStore:
    global _trigger_store
    if _trigger_store is None:
        _trigger_store = TriggerStore(
            table_name=os.environ.get("TRIGGERS_TABLE_NAME", "Triggers"),
            region=os.environ.get(
                "APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")
            ),
        )
    return _trigger_store
