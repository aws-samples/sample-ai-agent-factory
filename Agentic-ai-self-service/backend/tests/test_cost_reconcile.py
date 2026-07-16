"""Tests for the scheduled FinOps cost-reconciliation sweep (Loom-study 5.3)."""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from app.services.budget_store import Budget  # noqa: E402
from app.step_handlers import cost_reconcile_step as crs  # noqa: E402

# 2026-07-16 — inside a calendar month so month_window is deterministic.
NOW = 1_752_624_000


def _patch(monkeypatch, *, budgets, owner_runtimes=None, agent_runtime=None, spends=None):
    """Wire fake stores/summarize so the handler runs without AWS."""
    owner_runtimes = owner_runtimes or {}
    spends = spends or {}
    emitted: list[tuple[str, str]] = []

    class _FakeBudgetStore:
        def list_all(self, org_id):  # noqa: ARG002
            return budgets

    monkeypatch.setattr(crs, "_region", lambda: "us-east-1")
    monkeypatch.setattr("app.services.budget_store.get_budget_store", lambda: _FakeBudgetStore())
    monkeypatch.setattr(crs, "_owner_runtime_ids", lambda sub: owner_runtimes.get(sub, []))
    monkeypatch.setattr(crs, "_agent_runtime_id", lambda name: agent_runtime)
    # sum "spend" by runtime id from the fixture
    monkeypatch.setattr(crs, "_month_spend", lambda rids, f, t, r: sum(spends.get(x, 0.0) for x in rids))
    monkeypatch.setattr(crs, "_emit_breach_metric", lambda status, scope: emitted.append((scope, status)))
    return emitted


def test_owner_budget_over_emits_breach(monkeypatch):
    b = Budget(org_id="default", scope="owner", key="alice", limit_usd=10.0, warn_pct=80)
    emitted = _patch(
        monkeypatch,
        budgets=[b],
        owner_runtimes={"alice": ["rt-1", "rt-2"]},
        spends={"rt-1": 7.0, "rt-2": 5.0},  # $12 > $10 → over
    )
    out = crs.handler({"now_epoch": NOW})
    assert out == {"reconciled": 1, "breached": 1, "skipped": 0, "failed": 0}
    assert emitted == [("owner", "over")]


def test_agent_budget_under_no_breach(monkeypatch):
    b = Budget(org_id="default", scope="agent", key="my-agent", limit_usd=100.0, warn_pct=80)
    emitted = _patch(
        monkeypatch,
        budgets=[b],
        agent_runtime="rt-9",
        spends={"rt-9": 3.0},  # well under
    )
    out = crs.handler({"now_epoch": NOW})
    assert out["reconciled"] == 1 and out["breached"] == 0
    assert emitted == []


def test_tag_budget_is_skipped(monkeypatch):
    b = Budget(org_id="default", scope="tag", key="team=data", limit_usd=5.0, warn_pct=80)
    emitted = _patch(monkeypatch, budgets=[b])
    out = crs.handler({"now_epoch": NOW})
    assert out == {"reconciled": 0, "breached": 0, "skipped": 1, "failed": 0}
    assert emitted == []


def test_list_failure_returns_error_not_raise(monkeypatch):
    class _Boom:
        def list_all(self, org_id):  # noqa: ARG002
            raise RuntimeError("ddb down")

    monkeypatch.setattr(crs, "_region", lambda: "us-east-1")
    monkeypatch.setattr("app.services.budget_store.get_budget_store", lambda: _Boom())
    out = crs.handler({"now_epoch": NOW})
    assert out["error"] == "list_failed" and out["reconciled"] == 0
