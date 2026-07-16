"""Scheduled FinOps cost-reconciliation sweep (EventBridge-driven).

Loom-study Phase-5 item 5.3. Cost analytics in this platform are QUERY-TIME
(`cost_tracking.summarize_from_logs` reads gen_ai.usage.* out of CloudWatch when
someone opens the cost panel). Budget breaches therefore only surface — and only
emit the `BudgetBreach` CloudWatch metric (routers/cost.py) — when a human
happens to READ `/cost`. An agent that is deployed, idle-to-the-dashboard, and
quietly overspending never trips an alarm.

This handler is the missing SELF-DRIVE, mirroring the 0.6 Cedar-ENFORCE policy
sweep: an EventBridge schedule invokes it (daily) with a {"cost_reconcile": true}
sentinel; it walks every configured budget, computes the current calendar-month
actual spend from logs, evaluates it, and emits the same BudgetBreach metric for
any budget in warn/over — no user touchpoint required. Idempotent and
best-effort: per-budget failures are logged and retried next tick; a scan/list
failure returns a summary with an error rather than raising.

Scope coverage:
  * owner budgets — sum month spend across every runtime the owner deploys.
  * agent budgets — the single runtime behind that friendly runtime_name.
  * tag  budgets  — SKIPPED here (no tag→runtime index yet); counted + logged
    so the skip is visible (no silent truncation).
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)


def _region() -> str:
    return os.environ.get("APP_AWS_REGION") or os.environ.get("AWS_REGION", "us-east-1")


def _owner_runtime_ids(owner_sub: str) -> list[str]:
    """Distinct runtime_ids the owner currently has deployed (via versions GSI)."""
    from app.services.agent_versions_store import get_versions_store

    seen: set[str] = set()
    for v in get_versions_store().list_for_owner(owner_sub):
        if v.runtime_id:
            seen.add(v.runtime_id)
    return sorted(seen)


def _agent_runtime_id(runtime_name: str) -> str | None:
    """Resolve a friendly runtime_name to its PRODUCTION runtime_id (or None)."""
    from app.services.agent_versions_store import get_slots_store, get_versions_store

    slots = get_slots_store().get(runtime_name)
    if slots is None or not slots.production_version_id:
        return None
    version = get_versions_store().get(runtime_name, slots.production_version_id)
    return version.runtime_id if version and version.runtime_id else None


def _emit_breach_metric(status: str, scope: str) -> None:
    """Emit the BudgetBreach CloudWatch metric for a reconciled warn/over budget.

    Same namespace/metric as the on-read emitter in routers/cost.py so a single
    ops alarm covers both the human-read and the scheduled-sweep paths. The
    scheduled source is tagged via a ``Source=reconcile`` dimension. Never raises.
    """
    try:
        import boto3

        proj = os.environ.get("PROJECT_NAME", "agentcore-workflow")
        env = os.environ.get("ENVIRONMENT", "dev")
        boto3.client("cloudwatch", region_name=_region()).put_metric_data(
            Namespace=f"{proj}/{env}/finops",
            MetricData=[{
                "MetricName": "BudgetBreach",
                "Dimensions": [
                    {"Name": "Status", "Value": status},
                    {"Name": "Source", "Value": "reconcile"},
                    {"Name": "Scope", "Value": scope},
                ],
                "Value": 1,
                "Unit": "Count",
            }],
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("reconcile breach metric emit skipped: %s", exc)


def _month_spend(runtime_ids: list[str], from_ts: int, to_ts: int, region: str) -> float:
    """Sum this-month actual cost across the given runtimes (best-effort)."""
    from app.services.cost_tracking import summarize_from_logs

    total = 0.0
    for rid in runtime_ids:
        try:
            s = summarize_from_logs(rid, from_ts, to_ts, region)
            total += float(s.get("total_cost", 0.0))
        except Exception:  # noqa: BLE001
            logger.warning("reconcile: cost summarize failed for runtime %s", rid)
    return total


def handler(event: dict, context: object = None) -> dict:  # noqa: ARG001
    """EventBridge entrypoint: reconcile every budget's month-to-date spend.

    ``event`` may carry ``now_epoch`` (tests pin the clock); otherwise the wall
    clock is used. Returns a CloudWatch-friendly summary
    ``{reconciled, breached, skipped, failed}``.
    """
    region = _region()
    now_epoch = int(event.get("now_epoch") or time.time()) if isinstance(event, dict) else int(time.time())
    org_id = (event.get("org_id") if isinstance(event, dict) else None) or os.environ.get("DEFAULT_ORG_ID", "default")

    from app.services.budget_store import evaluate_budget, get_budget_store, month_window

    from_ts, to_ts = month_window(now_epoch)

    try:
        budgets = get_budget_store().list_all(org_id)
    except Exception:  # noqa: BLE001
        logger.exception("cost-reconcile: list_all budgets failed")
        return {"reconciled": 0, "breached": 0, "skipped": 0, "failed": 0, "error": "list_failed"}

    reconciled = 0
    breached = 0
    skipped = 0
    failed = 0

    for b in budgets:
        try:
            if b.scope == "owner":
                runtime_ids = _owner_runtime_ids(b.key)
            elif b.scope == "agent":
                rid = _agent_runtime_id(b.key)
                runtime_ids = [rid] if rid else []
            else:
                # tag-scoped budgets need a tag→runtime index we don't have yet.
                skipped += 1
                logger.info("cost-reconcile: skipping tag-scoped budget %s (no tag index)", b.key)
                continue

            spend = _month_spend(runtime_ids, from_ts, to_ts, region)
            verdict = evaluate_budget(b.limit_usd, b.warn_pct, spend)
            reconciled += 1
            if verdict["status"] in ("warn", "over"):
                _emit_breach_metric(verdict["status"], b.scope)
                breached += 1
                logger.info(
                    "cost-reconcile: budget %s/%s at %s ($%.4f of $%.2f)",
                    b.scope, b.key, verdict["status"], spend, b.limit_usd,
                )
        except Exception:  # noqa: BLE001
            failed += 1
            logger.warning("cost-reconcile: reconcile failed for %s/%s (retry next tick)", b.scope, b.key)

    summary = {"reconciled": reconciled, "breached": breached, "skipped": skipped, "failed": failed}
    logger.info("cost-reconcile sweep complete: %s", summary)
    return summary
