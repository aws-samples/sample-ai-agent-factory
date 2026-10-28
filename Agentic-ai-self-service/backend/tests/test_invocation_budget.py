"""Platform-mediated invocation budget adapter tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from app.services import invocation_budget
from app.services.budget_store import BudgetExceeded, BudgetReservationError
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def configured_budget_table(monkeypatch):
    monkeypatch.setenv("BUDGET_TABLE_NAME", "Budget")


def test_production_requires_budget_and_maps_exhaustion_to_429(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    store = MagicMock()
    store.reserve_invocation.side_effect = BudgetExceeded("none")
    monkeypatch.setattr(invocation_budget, "get_budget_store", lambda: store)

    with pytest.raises(HTTPException) as exc:
        invocation_budget.reserve_invocation_budget(
            owner_sub="alice",
            runtime_name="bot",
            resource_tags={"team": "core"},
        )

    assert exc.value.status_code == 429
    assert exc.value.headers == {"Retry-After": "3600"}
    store.reserve_invocation.assert_called_once_with(
        "default",
        owner_sub="alice",
        runtime_name="bot",
        resource_tags={"team": "core"},
        required=True,
    )


def test_development_budget_is_optional(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    store = MagicMock()
    store.reserve_invocation.return_value = None
    monkeypatch.setattr(invocation_budget, "get_budget_store", lambda: store)

    assert (
        invocation_budget.reserve_invocation_budget(
            owner_sub="alice",
            runtime_name="bot",
        )
        is None
    )
    assert store.reserve_invocation.call_args.kwargs["required"] is False


def test_budget_store_failure_is_503_before_runtime(monkeypatch):
    store = MagicMock()
    store.reserve_invocation.side_effect = BudgetReservationError("ddb unavailable")
    monkeypatch.setattr(invocation_budget, "get_budget_store", lambda: store)

    with pytest.raises(HTTPException) as exc:
        invocation_budget.reserve_invocation_budget(owner_sub="alice", runtime_name="bot")
    assert exc.value.status_code == 503


def test_settlement_uses_dispatch_and_keeps_reservation_on_error(monkeypatch):
    store = MagicMock()
    store.settle_reservation.side_effect = RuntimeError("unknown outcome")
    monkeypatch.setattr(invocation_budget, "get_budget_store", lambda: store)
    reservation = object()

    invocation_budget.settle_invocation_budget(reservation, dispatched=True)  # type: ignore[arg-type]

    store.settle_reservation.assert_called_once_with(reservation, success=True)
