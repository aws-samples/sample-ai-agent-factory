"""Cost reporting plus atomic invocation-authorization controls.

Per-owner / per-agent / per-tag monthly rows serve two separate views:
actual estimated spend from the existing ``gen_ai.usage`` pipeline, and an atomic
USD-denominated authorization ceiling for platform-mediated Runtime dispatches.
The authorization unit is configured by ``BUDGET_RESERVATION_USD``; it is not an
exact bill estimate or an AWS account spending cap.

Table (single-table, mirrors tag_policy_store):
  * ``BUDGET#<scope>#<key>`` definitions
  * ``SPEND#<scope>#<key>#<month>`` authorization counters
  * ``RESERVATION#<id>`` append/settlement ledger rows

A budget carries ``limit_usd`` (shared display/authorization limit), ``warn_pct``
(0-100 soft actual-spend threshold), and ``period`` (currently ``monthly``).
``evaluate_budget`` compares the separately reconciled spend estimate with that
limit. Atomic transactions condition-check owner and limit, reserve every applicable
counter together, and create one correlated reservation row before Runtime dispatch.
Only calls proven not dispatched release capacity; uncertain or dispatched calls
commit conservatively.

F-12 — a budget belongs to the caller who wrote it. ``agent`` and ``tag`` keys are
caller-supplied (a runtime name, a tag pair), and the row used to carry no owner, so any
``cost:write`` caller could overwrite or delete another tenant's budget and the list
disclosed every tenant's runtime names. The row now records ``owner_sub``; ``put_owned``
and ``delete_owned`` are CONDITIONAL DynamoDB writes (``attribute_not_exists(sk) OR
owner_sub = :caller``), so a read-then-write race between two tenants cannot both win,
and a foreign row raises ``BudgetOwnedByAnother`` for the router to report as 404
(``auth.assert_owner``'s existence-non-disclosure convention). A legacy row with no
``owner_sub`` is nobody's, exactly as ``assert_owner`` treats ``None`` -- only an admin
reaches it. ``list_all`` is unchanged: ``cost_reconcile_step`` is the platform, not a
tenant. ARCC cnt_dwzZ05hLnqhYXQ names caller-identity-only authorization, with no check of
the caller's relationship to the object, as the anti-pattern this closes.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

DEFAULT_ORG_ID = "default"
_BUDGET_PREFIX = "BUDGET#"
_SPEND_PREFIX = "SPEND#"
_RESERVATION_PREFIX = "RESERVATION#"
_MICRO_USD = 1_000_000
_MAX_APPLICABLE_BUDGETS = 20
_RESERVATION_STATUS_INDEX = "reservation-status-expires-index"
_RESERVATION_TTL_SECONDS = 3600
_RESERVATION_LIMIT_CONDITION = "owner_sub = :owner AND authorized_microusd <= :remaining"
BudgetScope = Literal["owner", "agent", "tag"]


class BudgetOwnedByAnother(Exception):
    """The budget row exists and is owned by someone other than the caller (or by nobody)."""


class BudgetExceeded(Exception):
    """The atomic reservation would exceed at least one applicable authorization ceiling."""


class BudgetReservationError(RuntimeError):
    """Budget authorization could not be completed safely."""


@dataclass(frozen=True)
class BudgetReservation:
    org_id: str
    reservation_id: str
    owner_sub: str
    runtime_name: str
    month: str
    amount_microusd: int
    counter_sks: tuple[str, ...]

    @property
    def sk(self) -> str:
        return f"{_RESERVATION_PREFIX}{self.reservation_id}"


@dataclass
class Budget:
    org_id: str
    scope: BudgetScope
    key: str  # owner_sub | runtime_name | "tagKey=tagValue"
    limit_usd: float
    warn_pct: int = 80  # soft threshold (0-100)
    period: str = "monthly"
    created_at: str = ""
    updated_at: str = ""
    #: F-12: the Cognito ``sub`` that wrote the row. ``None`` only on rows written before the
    #: field existed; such a row is nobody's (see module docstring).
    owner_sub: str | None = None

    @property
    def sk(self) -> str:
        return f"{_BUDGET_PREFIX}{self.scope}#{self.key}"

    def to_item(self) -> dict:
        item = {
            "org_id": self.org_id,
            "sk": self.sk,
            "scope": self.scope,
            "key": self.key,
            "limit_usd": Decimal(str(self.limit_usd)),
            "warn_pct": int(self.warn_pct),
            "period": self.period,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.owner_sub:
            item["owner_sub"] = self.owner_sub
        return item

    @classmethod
    def from_item(cls, item: dict) -> Budget:
        return cls(
            org_id=item["org_id"],
            scope=item.get("scope", "owner"),
            key=item.get("key", ""),
            limit_usd=float(item.get("limit_usd", 0)),
            warn_pct=int(item.get("warn_pct", 80)),
            period=item.get("period", "monthly"),
            created_at=item.get("created_at", ""),
            updated_at=item.get("updated_at", ""),
            owner_sub=item.get("owner_sub") or None,
        )


def evaluate_budget(limit_usd: float, warn_pct: int, spend_usd: float) -> dict:
    """Return {spend, limit, used_pct, status} for a budget vs actual spend.

    status: "over" when spend >= limit; "warn" when spend >= warn_pct% of limit;
    else "ok". Pure function — unit-testable without AWS.
    """
    limit = max(float(limit_usd), 0.0)
    spend = max(float(spend_usd), 0.0)
    used_pct = round((spend / limit) * 100, 2) if limit > 0 else 0.0
    if limit > 0 and spend >= limit:
        status = "over"
    elif limit > 0 and used_pct >= max(0, min(warn_pct, 100)):
        status = "warn"
    else:
        status = "ok"
    return {"spend": round(spend, 6), "limit": round(limit, 6), "used_pct": used_pct, "status": status}


def month_window(now_epoch: int) -> tuple[int, int]:
    """Return (start, end) epoch seconds for the calendar month containing now.

    now_epoch is passed in (never call time.time() implicitly) so callers/tests
    control the clock.
    """
    now = datetime.fromtimestamp(now_epoch, tz=timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    if now.month == 12:
        nxt = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        nxt = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(nxt.timestamp())


def invocation_reservation_usd() -> float:
    """Return the platform's conservative per-invocation authorization unit."""

    raw = os.environ.get("BUDGET_RESERVATION_USD", "1.0").strip()
    try:
        amount = Decimal(raw)
    except Exception as exc:  # noqa: BLE001
        raise BudgetReservationError("BUDGET_RESERVATION_USD must be a decimal number") from exc
    if not amount.is_finite() or amount < Decimal("0.000001") or amount > Decimal("1000"):
        raise BudgetReservationError("BUDGET_RESERVATION_USD must be in 0.000001..1000")
    return float(amount)


def budget_month_key(now_epoch: int) -> str:
    return datetime.fromtimestamp(now_epoch, tz=timezone.utc).strftime("%Y-%m")


def _to_microusd(amount_usd: float) -> int:
    amount = Decimal(str(amount_usd))
    if not amount.is_finite() or amount <= 0:
        raise BudgetReservationError("reservation amount must be positive")
    value = int((amount * _MICRO_USD).to_integral_value())
    if value <= 0:
        raise BudgetReservationError("reservation amount is below one micro-USD")
    return value


class BudgetStore:
    """CRUD for cost budgets."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self._client = boto3.client("dynamodb", region_name=region)
        self._serializer = TypeSerializer()

    def _serialize(self, values: dict) -> dict:
        return {key: self._serializer.serialize(value) for key, value in values.items()}

    def put(self, budget: Budget) -> Budget:
        now = datetime.now(timezone.utc).isoformat()
        if not budget.created_at:
            budget.created_at = now
        budget.updated_at = now
        self._table.put_item(Item=budget.to_item())
        return budget

    def get(self, org_id: str, scope: BudgetScope, key: str) -> Budget | None:
        resp = self._table.get_item(Key={"org_id": org_id, "sk": f"{_BUDGET_PREFIX}{scope}#{key}"})
        item = resp.get("Item")
        return Budget.from_item(item) if item else None

    def delete(self, org_id: str, scope: BudgetScope, key: str) -> bool:
        self._table.delete_item(Key={"org_id": org_id, "sk": f"{_BUDGET_PREFIX}{scope}#{key}"})
        return True

    # -- F-12: owner-bound writes -------------------------------------------------------

    @staticmethod
    def _owner_condition(caller_sub: str) -> dict:
        """The row is new, or it is the caller's. A legacy row with no ``owner_sub`` fails both."""
        return {
            "ConditionExpression": "attribute_not_exists(sk) OR owner_sub = :caller",
            "ExpressionAttributeValues": {":caller": caller_sub},
        }

    def put_owned(self, budget: Budget, *, caller_sub: str, admin: bool = False) -> Budget:
        """Write *budget* as *caller_sub*'s, refusing to overwrite another tenant's row.

        An admin write is unconditional but does not take the row over: an existing owner is
        kept, and only a row that has none becomes the admin's.
        """
        if not caller_sub:
            raise ValueError("a budget write needs an authenticated caller")
        now = datetime.now(timezone.utc).isoformat()
        if not budget.created_at:
            budget.created_at = now
        budget.updated_at = now
        if admin:
            existing = self.get(budget.org_id, budget.scope, budget.key)
            budget.owner_sub = (existing.owner_sub if existing else None) or budget.owner_sub or caller_sub
            self._table.put_item(Item=budget.to_item())
            return budget
        budget.owner_sub = caller_sub
        try:
            self._table.put_item(Item=budget.to_item(), **self._owner_condition(caller_sub))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise BudgetOwnedByAnother(budget.sk) from exc
            raise
        return budget

    def delete_owned(self, org_id: str, scope: BudgetScope, key: str, *, caller_sub: str, admin: bool = False) -> bool:
        """Delete the caller's row. Absent is idempotent success; another tenant's row raises."""
        if not caller_sub:
            raise ValueError("a budget delete needs an authenticated caller")
        if admin:
            return self.delete(org_id, scope, key)
        try:
            self._table.delete_item(
                Key={"org_id": org_id, "sk": f"{_BUDGET_PREFIX}{scope}#{key}"},
                **self._owner_condition(caller_sub),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise BudgetOwnedByAnother(f"{_BUDGET_PREFIX}{scope}#{key}") from exc
            raise
        return True

    def list_visible(self, org_id: str, *, caller_sub: str, admin: bool = False) -> list[Budget]:
        """The budgets *caller_sub* may see: their own, or every one for an admin.

        An owner-scope row is the caller's when its KEY is the caller (that scope was always
        keyed to the writer); an agent/tag row when ``owner_sub`` is. A legacy row with no owner
        is visible to an admin only.
        """
        budgets = self.list_all(org_id)
        if admin:
            return budgets
        return [b for b in budgets if (b.key == caller_sub if b.scope == "owner" else b.owner_sub == caller_sub)]

    def list_all(self, org_id: str, *, consistent: bool = False) -> list[Budget]:
        items: list[dict] = []
        kwargs: dict = {
            "KeyConditionExpression": Key("org_id").eq(org_id) & Key("sk").begins_with(_BUDGET_PREFIX),
            "ConsistentRead": consistent,
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [Budget.from_item(i) for i in items]

    @staticmethod
    def _counter_sk(budget: Budget, month: str) -> str:
        return f"{_SPEND_PREFIX}{budget.scope}#{budget.key}#{month}"

    def _applicable_budgets(
        self,
        org_id: str,
        *,
        owner_sub: str,
        runtime_name: str,
        resource_tags: dict[str, str] | None,
    ) -> list[Budget]:
        tags = resource_tags or {}
        applicable: list[Budget] = []
        for budget in self.list_all(org_id, consistent=True):
            if budget.owner_sub != owner_sub:
                continue
            if budget.scope == "owner" and budget.key == owner_sub:
                applicable.append(budget)
            elif budget.scope == "agent" and budget.key == runtime_name:
                applicable.append(budget)
            elif budget.scope == "tag" and "=" in budget.key:
                key, value = budget.key.split("=", 1)
                if tags.get(key) == value:
                    applicable.append(budget)
        applicable.sort(key=lambda budget: budget.sk)
        if len(applicable) > _MAX_APPLICABLE_BUDGETS:
            raise BudgetReservationError(
                f"An invocation matches more than {_MAX_APPLICABLE_BUDGETS} budgets; narrow the tag budget set"
            )
        return applicable

    def _ensure_counter(self, budget: Budget, month: str) -> str:
        counter_sk = self._counter_sk(budget, month)
        try:
            self._table.put_item(
                Item={
                    "org_id": budget.org_id,
                    "sk": counter_sk,
                    "scope": budget.scope,
                    "key": budget.key,
                    "month": month,
                    "owner_sub": budget.owner_sub,
                    "authorized_microusd": 0,
                    "reserved_microusd": 0,
                    "committed_microusd": 0,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "ttl": int(time.time()) + 400 * 24 * 3600,
                },
                ConditionExpression="attribute_not_exists(org_id) AND attribute_not_exists(sk)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        return counter_sk

    def reserve_invocation(
        self,
        org_id: str,
        *,
        owner_sub: str,
        runtime_name: str,
        resource_tags: dict[str, str] | None = None,
        amount_usd: float | None = None,
        now_epoch: int | None = None,
        required: bool = False,
    ) -> BudgetReservation | None:
        """Atomically reserve one authorization unit across all matching budgets."""

        if not owner_sub or not runtime_name:
            raise BudgetReservationError("budget authorization requires owner and runtime identity")
        now_epoch = int(time.time()) if now_epoch is None else int(now_epoch)
        self.commit_expired_reservations(org_id, now_epoch=now_epoch, limit=10)
        budgets = self._applicable_budgets(
            org_id,
            owner_sub=owner_sub,
            runtime_name=runtime_name,
            resource_tags=resource_tags,
        )
        if not budgets:
            if required:
                raise BudgetExceeded("Production invocation requires at least one applicable authorization limit")
            return None

        amount_microusd = _to_microusd(invocation_reservation_usd() if amount_usd is None else amount_usd)
        month = budget_month_key(now_epoch)
        reservation = BudgetReservation(
            org_id=org_id,
            reservation_id=uuid.uuid4().hex,
            owner_sub=owner_sub,
            runtime_name=runtime_name,
            month=month,
            amount_microusd=amount_microusd,
            counter_sks=tuple(self._ensure_counter(budget, month) for budget in budgets),
        )
        now_iso = datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()
        transaction: list[dict] = []
        for budget, counter_sk in zip(budgets, reservation.counter_sks, strict=True):
            limit_microusd = _to_microusd(budget.limit_usd)
            transaction.append(
                {
                    "ConditionCheck": {
                        "TableName": self._table.name,
                        "Key": self._serialize({"org_id": org_id, "sk": budget.sk}),
                        "ConditionExpression": "owner_sub = :owner AND limit_usd = :limit",
                        "ExpressionAttributeValues": self._serialize(
                            {
                                ":owner": owner_sub,
                                ":limit": Decimal(str(budget.limit_usd)),
                            }
                        ),
                    }
                }
            )
            transaction.append(
                {
                    "Update": {
                        "TableName": self._table.name,
                        "Key": self._serialize({"org_id": org_id, "sk": counter_sk}),
                        "UpdateExpression": (
                            "SET updated_at = :updated ADD reserved_microusd :amount, authorized_microusd :amount"
                        ),
                        "ConditionExpression": _RESERVATION_LIMIT_CONDITION,
                        "ExpressionAttributeValues": self._serialize(
                            {
                                ":updated": now_iso,
                                ":amount": amount_microusd,
                                ":owner": owner_sub,
                                ":remaining": limit_microusd - amount_microusd,
                            }
                        ),
                    }
                }
            )
        reservation_item = {
            "org_id": org_id,
            "sk": reservation.sk,
            "reservation_id": reservation.reservation_id,
            "owner_sub": owner_sub,
            "runtime_name": runtime_name,
            "month": month,
            "amount_microusd": amount_microusd,
            "counter_sks": list(reservation.counter_sks),
            "state": "RESERVED",
            "reservation_status": f"{org_id}#RESERVED",
            "created_at": now_iso,
            "expires_epoch": now_epoch + _RESERVATION_TTL_SECONDS,
        }
        transaction.append(
            {
                "Put": {
                    "TableName": self._table.name,
                    "Item": self._serialize(reservation_item),
                    "ConditionExpression": "attribute_not_exists(org_id) AND attribute_not_exists(sk)",
                }
            }
        )
        try:
            self._client.transact_write_items(
                TransactItems=transaction,
                ClientRequestToken=reservation.reservation_id,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {
                "TransactionCanceledException",
                "ConditionalCheckFailedException",
            }:
                raise BudgetExceeded("Invocation would exceed an applicable authorization limit") from exc
            raise BudgetReservationError("Budget reservation is temporarily unavailable") from exc
        return reservation

    def settle_reservation(
        self,
        reservation: BudgetReservation,
        *,
        success: bool,
        now_epoch: int | None = None,
    ) -> None:
        """Commit a dispatched unit or release a call proven not dispatched."""

        now_epoch = int(time.time()) if now_epoch is None else int(now_epoch)
        now_iso = datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()
        negative = -reservation.amount_microusd
        transaction: list[dict] = []
        for counter_sk in reservation.counter_sks:
            if success:
                update_expression = (
                    "SET updated_at = :updated ADD reserved_microusd :negative, committed_microusd :amount"
                )
            else:
                update_expression = (
                    "SET updated_at = :updated ADD reserved_microusd :negative, authorized_microusd :negative"
                )
            transaction.append(
                {
                    "Update": {
                        "TableName": self._table.name,
                        "Key": self._serialize({"org_id": reservation.org_id, "sk": counter_sk}),
                        "UpdateExpression": update_expression,
                        "ConditionExpression": "owner_sub = :owner AND reserved_microusd >= :amount",
                        "ExpressionAttributeValues": self._serialize(
                            {
                                ":updated": now_iso,
                                ":negative": negative,
                                ":amount": reservation.amount_microusd,
                                ":owner": reservation.owner_sub,
                            }
                        ),
                    }
                }
            )
        transaction.append(
            {
                "Update": {
                    "TableName": self._table.name,
                    "Key": self._serialize({"org_id": reservation.org_id, "sk": reservation.sk}),
                    "UpdateExpression": (
                        "SET #state = :state, reservation_status = :reservation_status, "
                        "settled_at = :settled, #ttl = :ttl"
                    ),
                    "ConditionExpression": "#state = :reserved",
                    "ExpressionAttributeNames": {"#state": "state", "#ttl": "ttl"},
                    "ExpressionAttributeValues": self._serialize(
                        {
                            ":state": "COMMITTED" if success else "RELEASED",
                            ":reservation_status": (
                                f"{reservation.org_id}#COMMITTED" if success else f"{reservation.org_id}#RELEASED"
                            ),
                            ":settled": now_iso,
                            ":ttl": now_epoch + 90 * 24 * 3600,
                            ":reserved": "RESERVED",
                        }
                    ),
                }
            }
        )
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            raise BudgetReservationError("Budget reservation could not be settled") from exc

    def commit_expired_reservations(
        self,
        org_id: str,
        *,
        now_epoch: int | None = None,
        limit: int = 100,
    ) -> int:
        """Commit bounded stale reservations whose dispatch outcome is unknown."""

        now_epoch = int(time.time()) if now_epoch is None else int(now_epoch)
        response = self._table.query(
            IndexName=_RESERVATION_STATUS_INDEX,
            KeyConditionExpression=(
                Key("reservation_status").eq(f"{org_id}#RESERVED") & Key("expires_epoch").lte(now_epoch)
            ),
            Limit=max(1, min(limit, 100)),
        )
        committed = 0
        for item in response.get("Items", []):
            reservation = BudgetReservation(
                org_id=org_id,
                reservation_id=str(item["reservation_id"]),
                owner_sub=str(item["owner_sub"]),
                runtime_name=str(item["runtime_name"]),
                month=str(item["month"]),
                amount_microusd=int(item["amount_microusd"]),
                counter_sks=tuple(str(value) for value in item.get("counter_sks", [])),
            )
            try:
                self.settle_reservation(reservation, success=True, now_epoch=now_epoch)
                committed += 1
            except BudgetReservationError:
                logger.warning("Could not commit stale budget reservation %s", reservation.reservation_id)
        return committed

    def counter_snapshot(self, org_id: str, scope: BudgetScope, key: str, month: str) -> dict:
        response = self._table.get_item(
            Key={"org_id": org_id, "sk": f"{_SPEND_PREFIX}{scope}#{key}#{month}"},
            ConsistentRead=True,
        )
        return response.get("Item") or {}


_store: BudgetStore | None = None


def get_budget_store() -> BudgetStore:
    global _store
    if _store is None:
        _store = BudgetStore(
            table_name=os.environ.get("BUDGET_TABLE_NAME", "Budget"),
            region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
        )
    return _store
