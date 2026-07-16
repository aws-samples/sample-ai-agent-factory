"""Phase 4: cost budget store + evaluator (FinOps).

evaluate_budget + month_window are pure (no AWS); the store is moto-backed.
"""

from __future__ import annotations

from typing import Iterator

import boto3
import pytest

from app.services.budget_store import (
    Budget,
    BudgetStore,
    evaluate_budget,
    month_window,
)
from app.services import budget_store as bs_mod

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

TABLE = "Budget"
ORG = "default"


def _create_table() -> None:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "org_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "org_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def store() -> Iterator[BudgetStore]:
    with mock_aws():
        _create_table()
        s = BudgetStore(table_name=TABLE, region="us-east-1")
        bs_mod._store = s
        yield s


# -- evaluate_budget (pure) --------------------------------------------------


def test_status_ok_below_warn():
    r = evaluate_budget(limit_usd=100, warn_pct=80, spend_usd=50)
    assert r["status"] == "ok" and r["used_pct"] == 50.0


def test_status_warn_at_threshold():
    r = evaluate_budget(limit_usd=100, warn_pct=80, spend_usd=85)
    assert r["status"] == "warn"


def test_status_over_at_limit():
    r = evaluate_budget(limit_usd=100, warn_pct=80, spend_usd=100)
    assert r["status"] == "over" and r["used_pct"] == 100.0


def test_status_over_above_limit():
    assert evaluate_budget(100, 80, 250)["status"] == "over"


def test_zero_limit_is_ok():
    # No limit configured → never warn/over (avoids div-by-zero).
    r = evaluate_budget(limit_usd=0, warn_pct=80, spend_usd=999)
    assert r["status"] == "ok" and r["used_pct"] == 0.0


# -- month_window (pure) -----------------------------------------------------


def test_month_window_bounds():
    # 2026-07-15T12:00:00Z → window is [Jul 1, Aug 1)
    import datetime as dt
    now = int(dt.datetime(2026, 7, 15, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp())
    start, end = month_window(now)
    assert dt.datetime.fromtimestamp(start, dt.timezone.utc).day == 1
    assert dt.datetime.fromtimestamp(start, dt.timezone.utc).month == 7
    assert dt.datetime.fromtimestamp(end, dt.timezone.utc).month == 8


def test_month_window_december_rolls_to_january():
    import datetime as dt
    now = int(dt.datetime(2026, 12, 20, tzinfo=dt.timezone.utc).timestamp())
    _, end = month_window(now)
    end_dt = dt.datetime.fromtimestamp(end, dt.timezone.utc)
    assert end_dt.year == 2027 and end_dt.month == 1


# -- store CRUD --------------------------------------------------------------


def test_put_get_delete(store: BudgetStore):
    store.put(Budget(org_id=ORG, scope="owner", key="alice", limit_usd=50, warn_pct=75))
    b = store.get(ORG, "owner", "alice")
    assert b is not None and b.limit_usd == 50.0 and b.warn_pct == 75
    assert store.delete(ORG, "owner", "alice")
    assert store.get(ORG, "owner", "alice") is None


def test_list_all_mixed_scopes(store: BudgetStore):
    store.put(Budget(org_id=ORG, scope="owner", key="alice", limit_usd=50))
    store.put(Budget(org_id=ORG, scope="agent", key="bot1", limit_usd=20))
    store.put(Budget(org_id=ORG, scope="tag", key="team=core", limit_usd=200))
    all_b = store.list_all(ORG)
    assert len(all_b) == 3
    assert {b.scope for b in all_b} == {"owner", "agent", "tag"}
