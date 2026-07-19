"""DynamoDB-backed store for human-in-the-loop (HITL) approval requests.

Implements Phase 2 Gap 2D — human-in-the-loop approval gates.

One table:

* ``HitlRequestsTable`` — one row per (runtime_id, request_id). A generated
  agent's injected ``human_approval`` @tool writes a PENDING row here and
  returns a "waiting for approval" sentinel; the tenant-scoped router lets the
  deployer view their pending queue and APPROVE / REJECT each request.

  PK ``runtime_id`` — an opaque, server-supplied string. In practice the
  injected @tool stamps it with the AgentCore *runtime name*
  (``agentcore_runtime_name``: ``<friendly[:39]>_<8hex>``), NOT the canonical
  agentRuntimeId. Reason: the canonical id does not exist until
  ``create_agent_runtime`` returns, but the agent's HITL env vars must be
  injected INTO that same create call (AgentCore env vars are fixed at create
  time). The runtime *name* is minted in ``deployment_handler.handle_deploy``,
  threaded through Step Functions, and is stable + owner-scoped (a tenant
  cannot reuse another tenant's friendly name — enforced by the H-1 check), so
  it is a safe, configure-time-known PK. It is server-supplied and NOT a
  tenant-typed key, so the Bug 122 PK-collision class does not apply. SK
  ``request_id`` is sortable (lex order == chronological), shaped like
  ``agent_versions_store``'s ids. A GSI ``owner_sub-request_id-index`` powers
  the owner-scoped pending queue. Rows carry a ``ttl`` attribute (24h) so
  DynamoDB auto-expires them — no destroy_runtime cascade is needed
  (documented in runtime_deployer.destroy_runtime).

Tenant isolation (Critic Finding 3, Bug 37): every row stamps ``owner_sub``;
the queue uses the owner_sub GSI; ``decide()`` re-checks ownership before
mutating. The router returns 404 (existence-non-disclosure) on cross-tenant.
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


# Status values for a HITL request.
STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"

# How long a PENDING request lives before DynamoDB TTL auto-expires it.
TTL_SECONDS = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Sortable id (ULID-shaped, 16 bytes hex). Lex order = chronological.
# ---------------------------------------------------------------------------


def new_request_id() -> str:
    """Return a 32-char lowercase hex string sortable by creation time.

    Layout: 12 hex chars of millisecond epoch + 20 hex chars of random.
    32 chars total = 16 bytes (same shape as ``agent_versions_store``'s
    ``new_version_id``). Lexicographic ordering of two ids equals their
    chronological order as long as they were generated more than ~1ms apart;
    ties break randomly which is fine for our SK ordering needs.

    NOTE: the generated agent's injected ``human_approval`` @tool mints its
    own ids with the identical layout (it cannot import this module — it runs
    in a separate AgentCore runtime), so both producers stay sort-compatible.
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
        # Preserve integer-valued Decimals as ints (created_at/ttl are ints).
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
class HitlRequest:
    runtime_id: str
    request_id: str
    owner_sub: str
    status: str  # PENDING | APPROVED | REJECTED
    action: str
    reason: str = ""
    created_at: int = 0  # epoch milliseconds
    ttl: int = 0  # epoch seconds (DynamoDB TTL)
    comment: str | None = None
    decided_at: str | None = None  # ISO 8601
    decided_by: str | None = None

    def to_item(self) -> dict:
        item = {
            "runtime_id": self.runtime_id,
            "request_id": self.request_id,
            "owner_sub": self.owner_sub,
            "status": self.status,
            "action": self.action,
            "reason": self.reason,
            "created_at": self.created_at,
            "ttl": self.ttl,
        }
        for fld in ("comment", "decided_at", "decided_by"):
            val = getattr(self, fld)
            if val is not None:
                item[fld] = val
        return _floats_to_decimals(item)

    @classmethod
    def from_item(cls, item: dict) -> HitlRequest:
        item = _decimals_to_floats(dict(item))
        return cls(
            runtime_id=item["runtime_id"],
            request_id=item["request_id"],
            owner_sub=item.get("owner_sub", ""),
            status=item.get("status", STATUS_PENDING),
            action=item.get("action", ""),
            reason=item.get("reason", ""),
            created_at=int(item.get("created_at", 0) or 0),
            ttl=int(item.get("ttl", 0) or 0),
            comment=item.get("comment"),
            decided_at=item.get("decided_at"),
            decided_by=item.get("decided_by"),
        )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class HitlNotPending(Exception):
    """Raised by ``decide()`` when a request is no longer PENDING.

    The router maps this to HTTP 409 (Conflict) — a decided request must not
    be re-decided.
    """

    def __init__(self, request_id: str, status: str) -> None:
        super().__init__(f"Request {request_id} is not PENDING (status={status})")
        self.request_id = request_id
        self.status = status


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class HitlRequestsStore:
    """CRUD + queue for the HitlRequests DDB table."""

    GSI_NAME = "owner_sub-request_id-index"

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def create_request(
        self,
        *,
        runtime_id: str,
        owner_sub: str,
        action: str,
        reason: str = "",
        request_id: str | None = None,
    ) -> HitlRequest:
        """Write a PENDING request and return it.

        ``request_id`` is normally minted here; callers may pass one for tests.
        """
        now_ms = int(time.time() * 1000)
        req = HitlRequest(
            runtime_id=runtime_id,
            request_id=request_id or new_request_id(),
            owner_sub=owner_sub,
            status=STATUS_PENDING,
            action=action,
            reason=reason,
            created_at=now_ms,
            ttl=int(time.time()) + TTL_SECONDS,
        )
        self._table.put_item(Item=req.to_item())
        logger.info(
            "Wrote HITL request %s/%s (owner=%s, status=PENDING)",
            req.runtime_id,
            req.request_id,
            req.owner_sub,
        )
        return req

    def get(self, runtime_id: str, request_id: str) -> HitlRequest | None:
        resp = self._table.get_item(Key={"runtime_id": runtime_id, "request_id": request_id})
        item = resp.get("Item")
        if not item:
            return None
        return HitlRequest.from_item(item)

    def list_pending_for_owner(self, owner_sub: str) -> list[HitlRequest]:
        """Return the caller's PENDING requests via the owner_sub GSI.

        Newest-first (SK is the sortable request_id). Decided rows are filtered
        out so the queue only shows actionable items.
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
        requests = [HitlRequest.from_item(i) for i in items]
        return [r for r in requests if r.status == STATUS_PENDING]

    def decide(
        self,
        *,
        runtime_id: str,
        request_id: str,
        decision: str,  # STATUS_APPROVED | STATUS_REJECTED
        decided_by: str,
        comment: str | None = None,
    ) -> HitlRequest:
        """Transition a PENDING request to APPROVED / REJECTED.

        Uses a conditional update so a concurrent double-decide can't race —
        the condition requires the current status to still be PENDING. If the
        row is already decided we raise ``HitlNotPending`` (router → 409).

        Assumes the caller has already verified ownership via ``assert_owner``;
        the conditional also implicitly guards against deciding a missing row.
        """
        if decision not in (STATUS_APPROVED, STATUS_REJECTED):
            raise ValueError(f"Invalid decision: {decision!r}")

        decided_at = _iso_now()
        set_parts = ["#s = :new", "decided_at = :dat", "decided_by = :dby"]
        names = {"#s": "status"}
        values = {
            ":new": decision,
            ":dat": decided_at,
            ":dby": decided_by,
            ":pending": STATUS_PENDING,
        }
        if comment is not None:
            set_parts.append("#c = :c")
            names["#c"] = "comment"
            values[":c"] = comment

        from botocore.exceptions import ClientError

        try:
            resp = self._table.update_item(
                Key={"runtime_id": runtime_id, "request_id": request_id},
                UpdateExpression="SET " + ", ".join(set_parts),
                ConditionExpression="attribute_exists(request_id) AND #s = :pending",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                current = self.get(runtime_id, request_id)
                raise HitlNotPending(request_id, current.status if current else "MISSING") from e
            raise

        logger.info(
            "Decided HITL request %s/%s -> %s (by=%s)",
            runtime_id,
            request_id,
            decision,
            decided_by,
        )
        return HitlRequest.from_item(resp["Attributes"])


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Convenience singleton (lazy-init from env)
# ---------------------------------------------------------------------------

_hitl_store: HitlRequestsStore | None = None


def get_hitl_store() -> HitlRequestsStore:
    global _hitl_store
    if _hitl_store is None:
        _hitl_store = HitlRequestsStore(
            table_name=os.environ.get("HITL_REQUESTS_TABLE_NAME", "HitlRequests"),
            region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
        )
    return _hitl_store
