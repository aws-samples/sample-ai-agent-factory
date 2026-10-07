"""Phase 4: cost budget store + evaluator (FinOps).

evaluate_budget + month_window are pure (no AWS); the store is moto-backed.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from app.services import budget_store as bs_mod
from app.services.budget_store import (
    Budget,
    BudgetExceeded,
    BudgetReservationError,
    BudgetStore,
    evaluate_budget,
    invocation_reservation_usd,
    month_window,
)

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
            {"AttributeName": "reservation_status", "AttributeType": "S"},
            {"AttributeName": "expires_epoch", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "reservation-status-expires-index",
                "KeySchema": [
                    {"AttributeName": "reservation_status", "KeyType": "HASH"},
                    {"AttributeName": "expires_epoch", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
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


# -- atomic pre-invocation reservations --------------------------------------


def _owned_budget(store: BudgetStore, scope: str, key: str, limit: float) -> None:
    store.put_owned(
        Budget(org_id=ORG, scope=scope, key=key, limit_usd=limit),  # type: ignore[arg-type]
        caller_sub="alice",
    )


def test_no_matching_budget_requires_no_reservation(store: BudgetStore):
    assert (
        store.reserve_invocation(
            ORG,
            owner_sub="alice",
            runtime_name="bot",
            amount_usd=1,
            now_epoch=1_783_000_000,
        )
        is None
    )
    with pytest.raises(BudgetExceeded, match="requires at least one"):
        store.reserve_invocation(
            ORG,
            owner_sub="alice",
            runtime_name="bot",
            amount_usd=1,
            now_epoch=1_783_000_000,
            required=True,
        )


def test_reservation_applies_atomically_to_owner_agent_and_tag_budgets(store: BudgetStore):
    _owned_budget(store, "owner", "alice", 10)
    _owned_budget(store, "agent", "bot", 5)
    _owned_budget(store, "tag", "team=core", 20)

    reservation = store.reserve_invocation(
        ORG,
        owner_sub="alice",
        runtime_name="bot",
        resource_tags={"team": "core"},
        amount_usd=1,
        now_epoch=1_783_000_000,
    )

    assert reservation is not None
    assert len(reservation.counter_sks) == 3
    for scope, key in (("owner", "alice"), ("agent", "bot"), ("tag", "team=core")):
        counter = store.counter_snapshot(ORG, scope, key, reservation.month)
        assert int(counter["reserved_microusd"]) == 1_000_000
        assert int(counter["authorized_microusd"]) == 1_000_000
        assert int(counter["committed_microusd"]) == 0


def test_success_commits_and_failure_releases_authorization_units(store: BudgetStore):
    _owned_budget(store, "owner", "alice", 10)
    first = store.reserve_invocation(
        ORG,
        owner_sub="alice",
        runtime_name="bot",
        amount_usd=1,
        now_epoch=1_783_000_000,
    )
    assert first is not None
    store.settle_reservation(first, success=True, now_epoch=1_783_000_010)
    committed = store.counter_snapshot(ORG, "owner", "alice", first.month)
    assert int(committed["reserved_microusd"]) == 0
    assert int(committed["authorized_microusd"]) == 1_000_000
    assert int(committed["committed_microusd"]) == 1_000_000

    second = store.reserve_invocation(
        ORG,
        owner_sub="alice",
        runtime_name="bot",
        amount_usd=1,
        now_epoch=1_783_000_020,
    )
    assert second is not None
    store.settle_reservation(second, success=False, now_epoch=1_783_000_030)
    released = store.counter_snapshot(ORG, "owner", "alice", first.month)
    assert int(released["reserved_microusd"]) == 0
    assert int(released["authorized_microusd"]) == 1_000_000
    assert int(released["committed_microusd"]) == 1_000_000


def test_hard_limit_blocks_next_reservation_without_partial_budget_debit(store: BudgetStore):
    _owned_budget(store, "owner", "alice", 10)
    _owned_budget(store, "agent", "bot", 0.5)

    with pytest.raises(BudgetExceeded):
        store.reserve_invocation(
            ORG,
            owner_sub="alice",
            runtime_name="bot",
            amount_usd=1,
            now_epoch=1_783_000_000,
        )

    assert int(store.counter_snapshot(ORG, "owner", "alice", "2026-07")["authorized_microusd"]) == 0
    assert int(store.counter_snapshot(ORG, "agent", "bot", "2026-07")["authorized_microusd"]) == 0


def test_mutation_removing_limit_condition_reopens_oversubscription(
    store: BudgetStore,
    monkeypatch,
):
    _owned_budget(store, "owner", "alice", 2)
    monkeypatch.setattr(
        bs_mod,
        "_RESERVATION_LIMIT_CONDITION",
        "owner_sub = :owner",
    )

    reservations = [
        store.reserve_invocation(
            ORG,
            owner_sub="alice",
            runtime_name="bot",
            amount_usd=1,
            now_epoch=1_783_000_000 + index,
        )
        for index in range(3)
    ]

    assert all(reservation is not None for reservation in reservations)
    counter = store.counter_snapshot(ORG, "owner", "alice", "2026-07")
    assert int(counter["authorized_microusd"]) == 3_000_000


def test_limit_counts_committed_and_concurrent_reserved_units(store: BudgetStore):
    _owned_budget(store, "owner", "alice", 2)
    first = store.reserve_invocation(
        ORG,
        owner_sub="alice",
        runtime_name="bot",
        amount_usd=1,
        now_epoch=1_783_000_000,
    )
    second = store.reserve_invocation(
        ORG,
        owner_sub="alice",
        runtime_name="bot",
        amount_usd=1,
        now_epoch=1_783_000_001,
    )
    assert first is not None and second is not None
    with pytest.raises(BudgetExceeded):
        store.reserve_invocation(
            ORG,
            owner_sub="alice",
            runtime_name="bot",
            amount_usd=1,
            now_epoch=1_783_000_002,
        )


def test_expired_interrupted_reservation_is_committed_conservatively(store: BudgetStore):
    _owned_budget(store, "owner", "alice", 1)
    reservation = store.reserve_invocation(
        ORG,
        owner_sub="alice",
        runtime_name="bot",
        amount_usd=1,
        now_epoch=1_783_000_000,
    )
    assert reservation is not None

    assert store.commit_expired_reservations(ORG, now_epoch=1_783_003_601) == 1
    counter = store.counter_snapshot(ORG, "owner", "alice", reservation.month)
    assert int(counter["reserved_microusd"]) == 0
    assert int(counter["authorized_microusd"]) == 1_000_000
    assert int(counter["committed_microusd"]) == 1_000_000


def test_stale_reconciliation_is_not_starved_by_settled_rows(store: BudgetStore):
    _owned_budget(store, "owner", "alice", 10)
    settled = [
        store.reserve_invocation(
            ORG,
            owner_sub="alice",
            runtime_name="bot",
            amount_usd=1,
            now_epoch=1_783_000_000 + index,
        )
        for index in range(4)
    ]
    for index, reservation in enumerate(settled):
        assert reservation is not None
        store.settle_reservation(
            reservation,
            success=True,
            now_epoch=1_783_000_100 + index,
        )
    unknown = store.reserve_invocation(
        ORG,
        owner_sub="alice",
        runtime_name="bot",
        amount_usd=1,
        now_epoch=1_783_000_010,
    )
    assert unknown is not None

    assert (
        store.commit_expired_reservations(
            ORG,
            now_epoch=1_783_003_611,
            limit=1,
        )
        == 1
    )
    counter = store.counter_snapshot(ORG, "owner", "alice", unknown.month)
    assert int(counter["reserved_microusd"]) == 0
    assert int(counter["committed_microusd"]) == 5_000_000


def test_reservation_unit_configuration_is_strict(monkeypatch):
    monkeypatch.setenv("BUDGET_RESERVATION_USD", "0.25")
    assert invocation_reservation_usd() == 0.25
    for invalid in ("", "nan", "0", "-1", "1001", "not-money"):
        monkeypatch.setenv("BUDGET_RESERVATION_USD", invalid)
        with pytest.raises(BudgetReservationError):
            invocation_reservation_usd()
