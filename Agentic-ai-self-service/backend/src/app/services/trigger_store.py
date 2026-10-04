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

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.services.agent_versions_store import (
    AgentVersion,
    RuntimeSlots,
    get_slots_store,
    get_versions_store,
)

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
STATUS_DELETING = "deleting"
# Backward-compatible status for legacy/unfenced rows that were recorded before
# trigger provisioning existed. The request path now creates PROVISIONING rows
# and flips them to ACTIVE only after the EventBridge rule or webhook path is
# attached. Keeping REGISTERED lets cleanup and the UI identify old inert rows
# without falsely treating them as active.
STATUS_REGISTERED = "registered"
TRIGGER_STATUSES = (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_PROVISIONING,
    STATUS_ERROR,
    STATUS_DELETING,
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
    # Immutable authority copied from the AgentVersion row that authorized the
    # trigger. The dispatcher resolves this exact deployment rather than
    # silently following a later production-slot promotion.
    version_id: str = ""
    deployment_id: str = ""
    status: str = STATUS_ACTIVE
    schedule: str | None = None  # cron expr (type=cron)
    pattern: dict | None = None  # event JSON (type=eventbridge/s3)
    webhook_secret_ref: str | None = None  # Secrets Manager ARN (never the secret)
    webhook_out_url: str | None = None  # validated outbound POST target
    eventbridge_rule_arn: str | None = None  # provisioned handle (cleanup)
    scheduler_name: str | None = None  # provisioned handle (cleanup)
    function_url: str | None = None  # webhook Function URL (cleanup)
    function_name: str | None = None  # Lambda function name/ARN (cleanup)
    webhook_path: str | None = None  # public HMAC-authenticated ingress path
    provisioning_token: str | None = None  # create/complete ownership fence
    delete_token: str | None = None  # delete/row-removal ownership fence
    last_error_code: str | None = None  # bounded, non-secret operator hint
    created_at: int = 0  # epoch milliseconds
    updated_at: int = 0  # epoch milliseconds

    def to_item(self) -> dict:
        item = {
            "runtime_name": self.runtime_name,
            "trigger_id": self.trigger_id,
            "owner_sub": self.owner_sub,
            "type": self.type,
            "target_runtime_arn": self.target_runtime_arn,
            "version_id": self.version_id,
            "deployment_id": self.deployment_id,
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
            "function_name",
            "webhook_path",
            "provisioning_token",
            "delete_token",
            "last_error_code",
        ):
            val = getattr(self, fld)
            if val is not None:
                item[fld] = val
        return _floats_to_decimals(item)

    @classmethod
    def from_item(cls, item: dict) -> Trigger:
        item = _decimals_to_floats(dict(item))
        return cls(
            runtime_name=item["runtime_name"],
            trigger_id=item["trigger_id"],
            owner_sub=item.get("owner_sub", ""),
            type=item.get("type", ""),
            target_runtime_arn=item.get("target_runtime_arn", ""),
            version_id=item.get("version_id", ""),
            deployment_id=item.get("deployment_id", ""),
            status=item.get("status", STATUS_ACTIVE),
            schedule=item.get("schedule"),
            pattern=item.get("pattern"),
            webhook_secret_ref=item.get("webhook_secret_ref"),
            webhook_out_url=item.get("webhook_out_url"),
            eventbridge_rule_arn=item.get("eventbridge_rule_arn"),
            scheduler_name=item.get("scheduler_name"),
            function_url=item.get("function_url"),
            function_name=item.get("function_name"),
            webhook_path=item.get("webhook_path"),
            provisioning_token=item.get("provisioning_token"),
            delete_token=item.get("delete_token"),
            last_error_code=item.get("last_error_code"),
            created_at=int(item.get("created_at", 0) or 0),
            updated_at=int(item.get("updated_at", 0) or 0),
        )


# ---------------------------------------------------------------------------
# The claim a trigger is created against (F-81f)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeClaim:
    """The production slot row and the version row it points at, exactly as one caller read them.

    ``routers/triggers`` resolves ownership by reading these two rows and checking their
    ``owner_sub``. That read is the authorization for the trigger create, and F-81f is what happens
    when the rows move between that read and the write: a teardown that deletes the version row and
    the slot leaves the authorization decided against rows that no longer exist, the unconditional
    put then lands a trigger row keyed by a name whose slot is gone, and the owner's own
    ``_resolve_owned_runtime`` answers 404 -- so nobody can ever delete it. Measured with the real
    stores by the Codex audit session.

    So the rows are not just checked, they are CARRIED, and ``TriggerStore.create_trigger`` writes
    the trigger only in a transaction whose conditions name every value here. The ARCC guidance on
    check-then-act races is exactly this: do not decide from a check and act later; make the act
    fail at use time if the checked state moved.
    """

    slot: RuntimeSlots
    version: AgentVersion
    target_runtime_arn: str

    def __post_init__(self) -> None:
        # Every term below becomes an equality condition; an empty one would compare against a
        # missing attribute and either never match (a permanent false conflict) or, for the
        # identity terms, match a row this path must never authorize.
        if not self.slot.owner_sub or not self.version.owner_sub:
            raise ValueError("a runtime claim needs an owner on both rows")
        if self.slot.owner_sub != self.version.owner_sub:
            raise ValueError("a runtime claim needs the slot and the version to agree on the owner")
        if not self.slot.production_version_id or self.slot.production_version_id != self.version.version_id:
            raise ValueError("a runtime claim needs the slot's production pointer to name the version")
        if self.slot.runtime_name != self.version.runtime_name:
            raise ValueError("a runtime claim needs the slot and the version to share a runtime name")
        if not self.version.deployment_id or not self.version.status:
            raise ValueError("a runtime claim needs the version's deployment id and status")
        # The target is DERIVED from the version, never merely carried: the row's own ARN, or its
        # canonical id when no ARN was recorded yet (the same rule ``routers/triggers`` applies).
        # Accepting any non-empty string here would make the store's confused-deputy invariant
        # depend on every caller deriving it correctly; refusing a mismatch makes it self-enforcing.
        expected_target = self.version.runtime_arn or self.version.runtime_id
        if not expected_target:
            raise ValueError("a runtime claim needs a version with a recorded runtime ARN or id")
        if self.target_runtime_arn != expected_target:
            raise ValueError("a runtime claim's target must be the one its version row records")


class TriggerClaimConflict(RuntimeError):
    """The runtime's slot or version row moved between the ownership read and the trigger write.

    A whole-transaction outcome: no trigger row was written and the slot fence did not move. The
    usual cause is a teardown, a promote or a redeploy that landed in between; the caller should
    re-resolve rather than retry blindly, because after a teardown there is nothing to retry
    against.
    """


class TriggerDeleteBusy(RuntimeError):
    """A live delivery lease currently prevents trigger deletion."""


class TriggerDeliveryBusy(RuntimeError):
    """Another worker still owns this trigger or delivery."""


class TriggerDeliveryInactive(RuntimeError):
    """The trigger stopped being active before delivery authority was acquired."""


@dataclass(frozen=True)
class TriggerDeliveryClaim:
    """The exact lease token protecting one durable trigger delivery."""

    runtime_name: str
    trigger_id: str
    delivery_id: str
    token: str


_DELIVERY_PARTITION_PREFIX = "!delivery#"


def _delivery_partition(runtime_name: str, trigger_id: str) -> str:
    return f"{_DELIVERY_PARTITION_PREFIX}{runtime_name}#{trigger_id}"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TriggerStore:
    """CRUD + listing for the Triggers DDB table."""

    GSI_NAME = "owner_sub-trigger_id-index"

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    @property
    def table_name(self) -> str:
        return self._table_name

    def create_trigger(
        self,
        *,
        claim: RuntimeClaim,
        owner_sub: str,
        type: str,
        status: str = STATUS_REGISTERED,
        schedule: str | None = None,
        pattern: dict | None = None,
        webhook_secret_ref: str | None = None,
        webhook_out_url: str | None = None,
        eventbridge_rule_arn: str | None = None,
        scheduler_name: str | None = None,
        function_url: str | None = None,
        function_name: str | None = None,
        webhook_path: str | None = None,
        provisioning_token: str | None = None,
        trigger_id: str | None = None,
    ) -> Trigger:
        """Write a trigger row, conditioned on the claim it was authorized against, and return it.

        F-81f. One ``TransactWriteItems`` with three actions:

        * ``Put`` the trigger row (``attribute_not_exists`` on its key -- a minted id must never
          overwrite);
        * ``ConditionCheck`` the AgentVersions row the caller read: same owner, deployment id,
          status, ``created_at`` and the field the target was derived from;
        * ``Update`` the RuntimeSlots row: same owner, same production pointer, same
          ``last_promoted_at`` and same ``trigger_fence``, and SET a new fence.

        The fence is the join with the teardown. ``release_name_claim_atomically`` pins the fence it
        read, so a create that commits after the release's reads cancels the release, and a release
        that commits first removes the rows this transaction checks, so the create cancels. Neither
        writer reads the other's table; the slot row is what they both condition on.

        ``runtime_name`` and ``target_runtime_arn`` are taken from the claim, never from a caller
        argument, so a row can only ever land under the name whose rows authorized it.

        Raises ``TriggerClaimConflict`` when any condition failed. Nothing is written then.
        """
        if owner_sub != claim.slot.owner_sub:
            # The claim already proved slot.owner == version.owner. The caller asserting a third
            # value is a programming error, not a race, and must not be written.
            raise ValueError("trigger owner must be the owner the claim was resolved for")
        now_ms = int(time.time() * 1000)
        runtime_name = claim.slot.runtime_name
        target_runtime_arn = claim.target_runtime_arn
        trig = Trigger(
            runtime_name=runtime_name,
            trigger_id=trigger_id or new_trigger_id(),
            owner_sub=owner_sub,
            type=type,
            target_runtime_arn=target_runtime_arn,
            version_id=claim.version.version_id,
            deployment_id=claim.version.deployment_id,
            status=status,
            schedule=schedule,
            pattern=pattern,
            webhook_secret_ref=webhook_secret_ref,
            webhook_out_url=webhook_out_url,
            eventbridge_rule_arn=eventbridge_rule_arn,
            scheduler_name=scheduler_name,
            function_url=function_url,
            function_name=function_name,
            webhook_path=webhook_path,
            provisioning_token=provisioning_token,
            created_at=now_ms,
            updated_at=now_ms,
        )

        version = claim.version
        version_conditions = ["owner_sub = :v_owner", "deployment_id = :v_dep", "#st = :v_status"]
        version_values: dict[str, object] = {
            ":v_owner": version.owner_sub,
            ":v_dep": version.deployment_id,
            ":v_status": version.status,
        }
        # The same liveness shape the release pins (status AND created_at): a retry that re-puts
        # the row with a fresh timestamp is a different claim even when every other field matches.
        if version.created_at:
            version_conditions.append("created_at = :v_created")
            version_values[":v_created"] = str(version.created_at)
        else:
            version_conditions.append("attribute_not_exists(created_at)")
        # Pin the field the target was derived from, so the trigger's recorded target is the one
        # the row still names. Whichever is set is what ``_resolve_owned_runtime`` used.
        #
        # Written as explicit (name, value) pairs, not ``getattr`` over a name tuple: the export
        # bundle is scanned for attribute reads whose name comes from a variable, and the scan cannot
        # tell a loop over a literal tuple apart from a loop over a name that came from data. Keeping
        # the finding set empty is what makes a genuinely new dynamic read visible.
        for field, observed in (("runtime_arn", version.runtime_arn), ("runtime_id", version.runtime_id)):
            if observed:
                version_conditions.append(f"{field} = :v_{field}")
                version_values[f":v_{field}"] = str(observed)
            else:
                version_conditions.append(f"attribute_not_exists({field})")

        slot = claim.slot
        slot_conditions = [
            "attribute_exists(runtime_name)",  # an Update would otherwise CREATE the row
            "owner_sub = :s_owner",
            "production_version_id = :s_prod",
        ]
        slot_values: dict[str, object] = {
            ":s_owner": slot.owner_sub,
            ":s_prod": str(slot.production_version_id),
            ":s_new_fence": secrets.token_hex(16),
        }
        if slot.last_promoted_at:
            slot_conditions.append("last_promoted_at = :s_promoted")
            slot_values[":s_promoted"] = str(slot.last_promoted_at)
        else:
            slot_conditions.append("attribute_not_exists(last_promoted_at)")
        if slot.trigger_fence:
            slot_conditions.append("trigger_fence = :s_fence")
            slot_values[":s_fence"] = str(slot.trigger_fence)
        else:
            slot_conditions.append("attribute_not_exists(trigger_fence)")

        items = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": trig.to_item(),
                    "ConditionExpression": "attribute_not_exists(trigger_id)",
                }
            },
            {
                "ConditionCheck": {
                    "TableName": get_versions_store().table_name,
                    "Key": {"runtime_name": runtime_name, "version_id": version.version_id},
                    "ConditionExpression": " AND ".join(version_conditions),
                    "ExpressionAttributeNames": {"#st": "status"},
                    "ExpressionAttributeValues": version_values,
                }
            },
            {
                "Update": {
                    "TableName": get_slots_store().table_name,
                    "Key": {"runtime_name": runtime_name},
                    "UpdateExpression": "SET trigger_fence = :s_new_fence",
                    "ConditionExpression": " AND ".join(slot_conditions),
                    "ExpressionAttributeValues": slot_values,
                }
            },
        ]
        # The resource's client: it applies the document serializer, so plain Python values go in
        # (typed ``{"S": ...}`` values would be wrapped twice and fail every condition with a
        # TypeError cancellation that looks exactly like a lost race).
        client = self._table.meta.client
        try:
            client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                reasons = [r.get("Code", "") for r in exc.response.get("CancellationReasons", [])]
                if "ConditionalCheckFailed" not in reasons:
                    # Same outcome (nothing written) but not the expected cause; a serialization
                    # bug here would otherwise read as "the fence is working".
                    logger.error(
                        "Trigger create for %s was cancelled for a non-condition reason %s; nothing was written",
                        runtime_name,
                        reasons or "<none reported>",
                    )
                # No botocore message in the raise: it echoes the request, including the sub.
                raise TriggerClaimConflict(
                    f"runtime {runtime_name} changed between the ownership read and the trigger write; "
                    f"transaction cancelled ({reasons})"
                ) from None
            raise
        logger.info(
            "Wrote trigger %s/%s (owner=%s, type=%s, status=%s) fenced to version %s",
            trig.runtime_name,
            trig.trigger_id,
            trig.owner_sub,
            trig.type,
            trig.status,
            version.version_id,
        )
        return trig

    def put_trigger_unfenced(
        self,
        *,
        runtime_name: str,
        owner_sub: str,
        type: str,
        target_runtime_arn: str,
        version_id: str = "",
        deployment_id: str = "",
        status: str = STATUS_REGISTERED,
        schedule: str | None = None,
        pattern: dict | None = None,
        webhook_secret_ref: str | None = None,
        webhook_out_url: str | None = None,
        eventbridge_rule_arn: str | None = None,
        scheduler_name: str | None = None,
        function_url: str | None = None,
        function_name: str | None = None,
        webhook_path: str | None = None,
        provisioning_token: str | None = None,
        delete_token: str | None = None,
        last_error_code: str | None = None,
        trigger_id: str | None = None,
    ) -> Trigger:
        """Write a trigger row with NO claim condition. Seeding and repair only.

        This is the write ``create_trigger`` used to be, kept for tests that need a row in a given
        shape (a legacy row with no target, a row under a name with no slot) and for an operator
        restoring metadata. It is not authorization-checked and it does not move the slot fence,
        so no request path may call it: ``tests/test_trigger_create_is_fenced_to_the_claim.py``
        pins that the triggers router does not reference it.
        """
        now_ms = int(time.time() * 1000)
        trig = Trigger(
            runtime_name=runtime_name,
            trigger_id=trigger_id or new_trigger_id(),
            owner_sub=owner_sub,
            type=type,
            target_runtime_arn=target_runtime_arn,
            version_id=version_id,
            deployment_id=deployment_id,
            status=status,
            schedule=schedule,
            pattern=pattern,
            webhook_secret_ref=webhook_secret_ref,
            webhook_out_url=webhook_out_url,
            eventbridge_rule_arn=eventbridge_rule_arn,
            scheduler_name=scheduler_name,
            function_url=function_url,
            function_name=function_name,
            webhook_path=webhook_path,
            provisioning_token=provisioning_token,
            delete_token=delete_token,
            last_error_code=last_error_code,
            created_at=now_ms,
            updated_at=now_ms,
        )
        self._table.put_item(Item=trig.to_item())
        logger.info(
            "Wrote UNFENCED trigger %s/%s (owner=%s, type=%s, status=%s)",
            trig.runtime_name,
            trig.trigger_id,
            trig.owner_sub,
            trig.type,
            trig.status,
        )
        return trig

    def get(
        self,
        runtime_name: str,
        trigger_id: str,
        *,
        consistent: bool = False,
    ) -> Trigger | None:
        kwargs = {
            "Key": {"runtime_name": runtime_name, "trigger_id": trigger_id},
        }
        if consistent:
            kwargs["ConsistentRead"] = True
        resp = self._table.get_item(**kwargs)
        item = resp.get("Item")
        if not item:
            return None
        return Trigger.from_item(item)

    def list_for_runtime(self, runtime_name: str, *, consistent: bool = False) -> list[Trigger]:
        """Return all triggers for ``runtime_name``, newest-first.

        SECURITY: this is NOT owner-scoped — callers MUST gate the runtime by
        ownership (resolve through the production slot) before calling this, and
        the router additionally visibility-filters the result to the caller
        (defense in depth, Bug 126 authz-drift).

        ``consistent`` is for the TEARDOWN caller. An eventually consistent query
        that misses a row reports "no triggers here" and the teardown then
        releases the runtime name, so the missed schedule keeps firing at a
        deleted ARN with no metadata left to find it by. A reader that is merely
        listing for a UI can tolerate a stale page; a reader deciding that
        nothing is left cannot.
        """
        items: list[dict] = []
        kwargs: dict = {
            "KeyConditionExpression": Key("runtime_name").eq(runtime_name),
            "ScanIndexForward": False,  # newest first
        }
        if consistent:
            kwargs["ConsistentRead"] = True
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
        eventbridge_rule_arn: str | None = None,
        scheduler_name: str | None = None,
        function_url: str | None = None,
        function_name: str | None = None,
        webhook_path: str | None = None,
        last_error_code: str | None = None,
    ) -> Trigger | None:
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
            ("function_name", function_name),
            ("webhook_path", webhook_path),
            ("last_error_code", last_error_code),
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
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        return Trigger.from_item(resp["Attributes"])

    def complete_provisioning(
        self,
        *,
        runtime_name: str,
        trigger_id: str,
        provisioning_token: str,
        eventbridge_rule_arn: str | None = None,
        webhook_path: str | None = None,
    ) -> Trigger | None:
        """Publish a provisioned trigger only while this creator still owns it.

        A concurrent delete first changes ``status`` to ``deleting`` and removes
        the provisioning token. This compare-and-set then returns ``None`` and
        the creator must compensate any AWS resource it created.
        """

        now_ms = int(time.time() * 1000)
        sets = ["#s = :active", "updated_at = :updated"]
        values: dict[str, object] = {
            ":active": STATUS_ACTIVE,
            ":provisioning": STATUS_PROVISIONING,
            ":token": provisioning_token,
            ":updated": now_ms,
        }
        if eventbridge_rule_arn is not None:
            sets.append("eventbridge_rule_arn = :rule")
            values[":rule"] = eventbridge_rule_arn
        if webhook_path is not None:
            sets.append("webhook_path = :webhook_path")
            values[":webhook_path"] = webhook_path
        try:
            resp = self._table.update_item(
                Key={"runtime_name": runtime_name, "trigger_id": trigger_id},
                UpdateExpression=(
                    "SET " + ", ".join(sets) + " REMOVE provisioning_token, delete_token, last_error_code"
                ),
                ConditionExpression=(
                    "attribute_exists(trigger_id) AND #s = :provisioning AND provisioning_token = :token"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        return Trigger.from_item(resp["Attributes"])

    def acquire_delivery(
        self,
        *,
        trigger: Trigger,
        delivery_id: str,
        lease_seconds: int,
        retention_seconds: int,
        now: int | None = None,
    ) -> TriggerDeliveryClaim | None:
        """Atomically claim one delivery and one trigger-wide dispatch lease.

        The dedicated delivery item suppresses a completed EventBridge/SQS
        duplicate. The transient lease on the trigger row serializes invokes
        and makes delete wait rather than returning while an agent is still
        running. Both writes share one transaction, conditioned on the trigger
        still being the active deployment authority the dispatcher read.

        Returns ``None`` for an already-completed delivery, raises
        :class:`TriggerDeliveryBusy` while another lease is live, and raises
        :class:`TriggerDeliveryInactive` when delete/disable/metadata movement
        won the race.
        """

        if lease_seconds <= 0 or retention_seconds <= lease_seconds:
            raise ValueError("delivery retention must exceed a positive lease")
        now_epoch = int(time.time()) if now is None else int(now)
        token = secrets.token_hex(16)
        expires = now_epoch + lease_seconds
        ttl = now_epoch + retention_seconds
        delivery_key = {
            "runtime_name": _delivery_partition(
                trigger.runtime_name,
                trigger.trigger_id,
            ),
            "trigger_id": delivery_id,
        }
        client = self._table.meta.client
        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": delivery_key,
                            "UpdateExpression": (
                                "SET item_kind = :kind, source_runtime_name = :runtime, "
                                "source_trigger_id = :trigger, delivery_status = :processing, "
                                "claim_token = :token, claim_expires_at = :expires, "
                                "updated_at = :now, #ttl = :ttl"
                            ),
                            "ConditionExpression": (
                                "attribute_not_exists(trigger_id) OR "
                                "(delivery_status = :processing AND claim_expires_at <= :now)"
                            ),
                            "ExpressionAttributeNames": {"#ttl": "ttl"},
                            "ExpressionAttributeValues": {
                                ":kind": "trigger_delivery",
                                ":runtime": trigger.runtime_name,
                                ":trigger": trigger.trigger_id,
                                ":processing": "processing",
                                ":token": token,
                                ":expires": expires,
                                ":now": now_epoch,
                                ":ttl": ttl,
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": {
                                "runtime_name": trigger.runtime_name,
                                "trigger_id": trigger.trigger_id,
                            },
                            "UpdateExpression": ("SET dispatch_token = :token, dispatch_expires_at = :expires"),
                            "ConditionExpression": (
                                "attribute_exists(trigger_id) AND #status = :active "
                                "AND owner_sub = :owner AND deployment_id = :deployment "
                                "AND version_id = :version AND target_runtime_arn = :target "
                                "AND (attribute_not_exists(dispatch_expires_at) "
                                "OR dispatch_expires_at <= :now)"
                            ),
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {
                                ":active": STATUS_ACTIVE,
                                ":owner": trigger.owner_sub,
                                ":deployment": trigger.deployment_id,
                                ":version": trigger.version_id,
                                ":target": trigger.target_runtime_arn,
                                ":token": token,
                                ":expires": expires,
                                ":now": now_epoch,
                            },
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise

            # Classify from strong reads after the failed transaction. Do not
            # trust CancellationReasons: some IAM/API paths omit them.
            current = self.get(
                trigger.runtime_name,
                trigger.trigger_id,
                consistent=True,
            )
            if (
                current is None
                or current.status != STATUS_ACTIVE
                or current.owner_sub != trigger.owner_sub
                or current.deployment_id != trigger.deployment_id
                or current.version_id != trigger.version_id
                or current.target_runtime_arn != trigger.target_runtime_arn
            ):
                raise TriggerDeliveryInactive("The trigger stopped being active before delivery began") from None
            item = self._table.get_item(
                Key=delivery_key,
                ConsistentRead=True,
            ).get("Item")
            if item and item.get("delivery_status") == "completed":
                return None
            raise TriggerDeliveryBusy("Another worker still owns this trigger or delivery") from None

        return TriggerDeliveryClaim(
            runtime_name=trigger.runtime_name,
            trigger_id=trigger.trigger_id,
            delivery_id=delivery_id,
            token=token,
        )

    def complete_delivery(
        self,
        claim: TriggerDeliveryClaim,
        *,
        retention_seconds: int,
        now: int | None = None,
    ) -> bool:
        """Persist terminal dedupe state, then release the trigger lease.

        The agent invocation has already happened when this method is called.
        Therefore the delivery item's terminal marker is the safety-critical
        write: coupling it transactionally to lease cleanup meant a missing or
        concurrently changed trigger row could roll the marker back and make a
        successful invocation replayable after the lease expired.

        Write the completed marker first, retrying an ambiguous transport
        result only while the same token still owns the processing row. The
        trigger lease is then removed conditionally on that token. Failure to
        release it delays delete/another trigger delivery until its bounded
        expiry, but can no longer erase dedupe evidence.
        """

        if retention_seconds <= 0:
            raise ValueError("delivery retention must be positive")
        now_epoch = int(time.time()) if now is None else int(now)
        delivery_key = {
            "runtime_name": _delivery_partition(
                claim.runtime_name,
                claim.trigger_id,
            ),
            "trigger_id": claim.delivery_id,
        }
        values = {
            ":completed": "completed",
            ":processing": "processing",
            ":token": claim.token,
            ":now": now_epoch,
            ":ttl": now_epoch + retention_seconds,
        }

        last_error: Exception | None = None
        completion_confirmed = False
        for _attempt in range(3):
            try:
                self._table.update_item(
                    Key=delivery_key,
                    UpdateExpression=(
                        "SET delivery_status = :completed, completed_at = :now, "
                        "updated_at = :now, #ttl = :ttl "
                        "REMOVE claim_token, claim_expires_at"
                    ),
                    ConditionExpression=("delivery_status = :processing AND claim_token = :token"),
                    ExpressionAttributeNames={"#ttl": "ttl"},
                    ExpressionAttributeValues=values,
                )
                completion_confirmed = True
                break
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code == "ConditionalCheckFailedException":
                    item = self._table.get_item(
                        Key=delivery_key,
                        ConsistentRead=True,
                    ).get("Item")
                    if item and item.get("delivery_status") == "completed":
                        completion_confirmed = True
                    else:
                        return False
                    break
                last_error = exc
            except Exception as exc:
                # A timeout can be ambiguous: DynamoDB may have committed the
                # write before the response was lost. Read back before retrying.
                last_error = exc

            try:
                item = self._table.get_item(
                    Key=delivery_key,
                    ConsistentRead=True,
                ).get("Item")
            except Exception:
                item = None
            if item and item.get("delivery_status") == "completed":
                completion_confirmed = True
                break
            if item and (item.get("delivery_status") != "processing" or item.get("claim_token") != claim.token):
                return False

        if not completion_confirmed:
            if last_error is None:
                raise RuntimeError("Delivery completion could not be confirmed")
            raise last_error

        try:
            self._table.update_item(
                Key={
                    "runtime_name": claim.runtime_name,
                    "trigger_id": claim.trigger_id,
                },
                UpdateExpression="REMOVE dispatch_token, dispatch_expires_at",
                ConditionExpression="dispatch_token = :token",
                ExpressionAttributeValues={":token": claim.token},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                logger.exception(
                    "Completed delivery %s/%s but could not release its trigger lease",
                    claim.trigger_id,
                    claim.delivery_id,
                )
        except Exception:
            logger.exception(
                "Completed delivery %s/%s but could not release its trigger lease",
                claim.trigger_id,
                claim.delivery_id,
            )
        return True

    def release_delivery(self, claim: TriggerDeliveryClaim) -> bool:
        """Release a failed delivery so SQS can retry it after visibility."""

        try:
            self._table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": {
                                "runtime_name": _delivery_partition(
                                    claim.runtime_name,
                                    claim.trigger_id,
                                ),
                                "trigger_id": claim.delivery_id,
                            },
                            "ConditionExpression": ("delivery_status = :processing AND claim_token = :token"),
                            "ExpressionAttributeValues": {
                                ":processing": "processing",
                                ":token": claim.token,
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": {
                                "runtime_name": claim.runtime_name,
                                "trigger_id": claim.trigger_id,
                            },
                            "UpdateExpression": ("REMOVE dispatch_token, dispatch_expires_at"),
                            "ConditionExpression": "dispatch_token = :token",
                            "ExpressionAttributeValues": {":token": claim.token},
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                return False
            raise
        return True

    def fail_provisioning(
        self,
        *,
        runtime_name: str,
        trigger_id: str,
        provisioning_token: str,
        error_code: str,
    ) -> Trigger | None:
        """Record a bounded failure only while this creator still owns the row."""

        now_ms = int(time.time() * 1000)
        try:
            resp = self._table.update_item(
                Key={"runtime_name": runtime_name, "trigger_id": trigger_id},
                UpdateExpression=(
                    "SET #s = :error, updated_at = :updated, last_error_code = :error_code REMOVE provisioning_token"
                ),
                ConditionExpression=(
                    "attribute_exists(trigger_id) AND #s = :provisioning AND provisioning_token = :token"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":error": STATUS_ERROR,
                    ":provisioning": STATUS_PROVISIONING,
                    ":token": provisioning_token,
                    ":updated": now_ms,
                    ":error_code": error_code[:128],
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        return Trigger.from_item(resp["Attributes"])

    def claim_delete(
        self,
        *,
        runtime_name: str,
        trigger_id: str,
        owner_sub: str,
        delete_token: str,
        now: int | None = None,
    ) -> Trigger | None:
        """Fence provisioning and claim resource cleanup for one owner.

        A second owner can never claim the row. A same-owner retry may replace
        an older delete token; cleanup is idempotent and only the latest claimant
        may remove the metadata row.
        """

        now_epoch = int(time.time()) if now is None else int(now)
        now_ms = now_epoch * 1000
        try:
            resp = self._table.update_item(
                Key={"runtime_name": runtime_name, "trigger_id": trigger_id},
                UpdateExpression=(
                    "SET #s = :deleting, delete_token = :token, "
                    "updated_at = :updated "
                    "REMOVE provisioning_token, dispatch_token, dispatch_expires_at"
                ),
                ConditionExpression=(
                    "attribute_exists(trigger_id) AND owner_sub = :owner "
                    "AND (attribute_not_exists(dispatch_expires_at) "
                    "OR dispatch_expires_at <= :now_epoch)"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":deleting": STATUS_DELETING,
                    ":token": delete_token,
                    ":updated": now_ms,
                    ":owner": owner_sub,
                    ":now_epoch": now_epoch,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                current = self._table.get_item(
                    Key={
                        "runtime_name": runtime_name,
                        "trigger_id": trigger_id,
                    },
                    ConsistentRead=True,
                ).get("Item")
                if current and current.get("owner_sub") == owner_sub:
                    raise TriggerDeleteBusy("A trigger delivery is still in progress") from None
                return None
            raise
        return Trigger.from_item(resp["Attributes"])

    def delete_claimed(
        self,
        *,
        runtime_name: str,
        trigger_id: str,
        delete_token: str,
    ) -> bool:
        """Remove a row only if the caller still owns its delete claim."""

        try:
            self._table.delete_item(
                Key={"runtime_name": runtime_name, "trigger_id": trigger_id},
                ConditionExpression="#s = :deleting AND delete_token = :token",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":deleting": STATUS_DELETING,
                    ":token": delete_token,
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        logger.info("Deleted claimed trigger %s/%s", runtime_name, trigger_id)
        self.purge_deliveries(runtime_name, trigger_id)
        return True

    def delete(self, runtime_name: str, trigger_id: str) -> None:
        """Delete a trigger row and its delivery partition. Idempotent (no-op if already gone)."""
        self._table.delete_item(Key={"runtime_name": runtime_name, "trigger_id": trigger_id})
        logger.info("Deleted trigger %s/%s", runtime_name, trigger_id)
        self.purge_deliveries(runtime_name, trigger_id)

    def purge_deliveries(self, runtime_name: str, trigger_id: str, *, max_rounds: int = 25) -> int:
        """Delete every durable delivery row of one trigger and return how many went.

        Delivery rows live in their own partition (``!delivery#<runtime>#<trigger>``) under the
        trigger table, so deleting the trigger row alone leaves them behind for their TTL, and a
        residue scan reads them as leaked. Drained by consistent re-query until the partition is
        empty; a partition that will not drain raises rather than reporting the trigger deleted.
        """
        partition = _delivery_partition(runtime_name, trigger_id)
        removed = 0
        for _ in range(max_rounds):
            resp = self._table.query(
                KeyConditionExpression=Key("runtime_name").eq(partition),
                ProjectionExpression="runtime_name, trigger_id",
                ConsistentRead=True,
            )
            items = list(resp.get("Items") or [])
            if not items:
                if removed:
                    logger.info("Purged %d delivery row(s) for trigger %s/%s", removed, runtime_name, trigger_id)
                return removed
            with self._table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"runtime_name": item["runtime_name"], "trigger_id": item["trigger_id"]})
                    removed += 1
        raise RuntimeError(f"delivery partition for trigger {runtime_name}/{trigger_id} did not drain")


class TriggerSecretDeletionRefused(RuntimeError):
    """The persisted secret reference could not be proven trigger-owned."""


def delete_owned_webhook_secret(
    trigger: Trigger,
    *,
    secrets_client=None,
) -> bool:
    """Delete a trigger's HMAC secret only after proving exact ownership.

    Trigger rows are mutable platform metadata. Treating a stored ARN as
    sufficient authority would turn a corrupted row into an arbitrary
    Secrets Manager delete. The secret must live in the platform-managed
    namespace and carry all three tags stamped by ``_store_webhook_secret``.

    Returns ``True`` when a secret was deleted and ``False`` when there was no
    secret (or it was already gone). Raises on ownership ambiguity or an AWS
    failure so callers can preserve the trigger row as a retry handle.
    """
    secret_ref = trigger.webhook_secret_ref
    if not secret_ref:
        return False

    sm = secrets_client or boto3.client(
        "secretsmanager",
        region_name=os.environ.get(
            "APP_AWS_REGION",
            os.environ.get("AWS_REGION", "us-east-1"),
        ),
    )
    try:
        metadata = sm.describe_secret(SecretId=secret_ref)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return False
        raise

    tags = {
        str(tag.get("Key")): str(tag.get("Value")) for tag in metadata.get("Tags", []) if tag.get("Key") is not None
    }
    name = str(metadata.get("Name") or "")
    if (
        not name.startswith("agentcore-trigger/")
        or tags.get("ManagedBy") != "agentcore-flows"
        or tags.get("Purpose") != "trigger-webhook-hmac"
        or tags.get("owner_sub") != trigger.owner_sub
    ):
        raise TriggerSecretDeletionRefused(
            f"Refusing to delete webhook secret for trigger "
            f"{trigger.runtime_name}/{trigger.trigger_id}: ownership could not be proven"
        )

    sm.delete_secret(
        SecretId=secret_ref,
        ForceDeleteWithoutRecovery=True,
    )
    return True


# ---------------------------------------------------------------------------
# Convenience singleton (lazy-init from env)
# ---------------------------------------------------------------------------

_trigger_store: TriggerStore | None = None


def get_trigger_store() -> TriggerStore:
    global _trigger_store
    if _trigger_store is None:
        _trigger_store = TriggerStore(
            table_name=os.environ.get("TRIGGERS_TABLE_NAME", "Triggers"),
            region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
        )
    return _trigger_store
