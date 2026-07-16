"""Cost budgets + FinOps controls (Phase 4 — Loom-inspired).

Upgrades the read-only cost reporting (services/cost_tracking.py) into spend
governance: per-owner / per-agent / per-tag monthly budgets with warn + hard
thresholds, and a breach evaluator that compares a budget to actual spend.

Table (single-table, mirrors tag_policy_store):
  PK ``org_id``, SK ``BUDGET#<scope>#<key>`` where scope ∈ {owner, agent, tag}
  and key is the owner_sub / runtime_name / "tagKey=tagValue".

A budget carries ``limit_usd`` (hard cap), ``warn_pct`` (0-100 soft threshold),
and ``period`` (currently "monthly"). ``evaluate`` returns the spend, the
fraction of the limit used, and a status (ok | warn | over) — the API/poller
uses this to surface badges and (optionally) fire an alarm.

Spend is sourced from the existing cost pipeline (summarize_from_logs), so
budgets need NO new metering — they read the same gen_ai.usage cost rollup the
cost dashboard already shows.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Optional

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

DEFAULT_ORG_ID = "default"
_BUDGET_PREFIX = "BUDGET#"
BudgetScope = Literal["owner", "agent", "tag"]


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

    @property
    def sk(self) -> str:
        return f"{_BUDGET_PREFIX}{self.scope}#{self.key}"

    def to_item(self) -> dict:
        return {
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

    @classmethod
    def from_item(cls, item: dict) -> "Budget":
        return cls(
            org_id=item["org_id"],
            scope=item.get("scope", "owner"),
            key=item.get("key", ""),
            limit_usd=float(item.get("limit_usd", 0)),
            warn_pct=int(item.get("warn_pct", 80)),
            period=item.get("period", "monthly"),
            created_at=item.get("created_at", ""),
            updated_at=item.get("updated_at", ""),
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
    return {"spend": round(spend, 6), "limit": round(limit, 6),
            "used_pct": used_pct, "status": status}


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


class BudgetStore:
    """CRUD for cost budgets."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put(self, budget: Budget) -> Budget:
        now = datetime.now(timezone.utc).isoformat()
        if not budget.created_at:
            budget.created_at = now
        budget.updated_at = now
        self._table.put_item(Item=budget.to_item())
        return budget

    def get(self, org_id: str, scope: BudgetScope, key: str) -> Optional[Budget]:
        resp = self._table.get_item(
            Key={"org_id": org_id, "sk": f"{_BUDGET_PREFIX}{scope}#{key}"}
        )
        item = resp.get("Item")
        return Budget.from_item(item) if item else None

    def delete(self, org_id: str, scope: BudgetScope, key: str) -> bool:
        self._table.delete_item(
            Key={"org_id": org_id, "sk": f"{_BUDGET_PREFIX}{scope}#{key}"}
        )
        return True

    def list_all(self, org_id: str) -> list[Budget]:
        items: list[dict] = []
        kwargs: dict = {
            "KeyConditionExpression": Key("org_id").eq(org_id)
            & Key("sk").begins_with(_BUDGET_PREFIX)
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [Budget.from_item(i) for i in items]


_store: Optional[BudgetStore] = None


def get_budget_store() -> BudgetStore:
    global _store
    if _store is None:
        _store = BudgetStore(
            table_name=os.environ.get("BUDGET_TABLE_NAME", "Budget"),
            region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
        )
    return _store
