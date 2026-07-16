"""Cost analytics + FinOps API — Phase 2 Gap 2B.

Surfaces per-runtime cost + token analytics for the deployed AgentCore
production runtime. The PRIMARY data path is query-time: the endpoint reads
``gen_ai.usage.*`` attributes out of the runtime's CloudWatch Logs (the same
source ``observability_dashboard.py`` uses) and prices them with the baked-in
Bedrock price table in ``cost_tracking.py``. No write path, no per-runtime AWS
resource.

Endpoint:

* ``GET /api/runtimes/{runtime_name}/cost?from=&to=`` — returns
  ``{total_cost, total_in, total_out, by_model, from_ts, to_ts, ...}`` for
  the production version's runtime over the requested window.

Ownership is enforced via the ``RuntimeSlots`` + ``AgentVersions`` tables,
mirroring ``evaluations._resolve_owned_runtime_id``. Cross-tenant requests
return 404 (existence-non-disclosure). The endpoint never trusts a
tenant-supplied runtime_id — it resolves it from the owner-checked production
slot, so the Bug-122 tenant-keyed-table collision can't occur here.
"""

from __future__ import annotations

import logging
import os
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from app.services.agent_versions_store import (
    get_slots_store,
    get_versions_store,
)
from app.services.auth import assert_owner, get_caller_sub
from app.services.cost_tracking import summarize_from_logs
from app.services.rbac import require_scopes

logger = logging.getLogger(__name__)


# Window guards: default to last 24h, cap at 90 days to bound the Logs
# Insights query span (matches the eval router's bounded-window philosophy).
_DEFAULT_WINDOW_SECONDS = 24 * 3600
_MAX_WINDOW_SECONDS = 90 * 24 * 3600


def _validate_runtime_name(name: str) -> str:
    if not name or len(name) > 64:
        raise HTTPException(status_code=400, detail="Invalid runtime_name")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", name):
        raise HTTPException(status_code=400, detail="Invalid runtime_name format")
    return name


def _region() -> str:
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))


router = APIRouter(prefix="/api/runtimes", tags=["cost"])


def _resolve_owned_runtime_id(runtime_name: str, caller_sub: str) -> tuple[str, str]:
    """Return (runtime_id, version_id) for the production version owned by
    *caller_sub*, or 404 if either the runtime or the slot is missing.

    Mirrors ``evaluations._resolve_owned_runtime_id`` exactly: assert_owner
    on BOTH the slot row and the version row so a bypass on either can't pass.
    """
    slots = get_slots_store().get(runtime_name)
    if slots is None or not slots.production_version_id:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(slots.owner_sub, caller_sub)
    version = get_versions_store().get(runtime_name, slots.production_version_id)
    if version is None or not version.runtime_id:
        raise HTTPException(status_code=404, detail="Not found")
    assert_owner(version.owner_sub, caller_sub)
    return version.runtime_id, version.version_id


def _resolve_window(from_: int | None, to: int | None) -> tuple[int, int]:
    """Validate + normalize the from/to epoch-second window."""
    now = int(time.time())
    to_ts = int(to) if to is not None else now
    from_ts = int(from_) if from_ is not None else to_ts - _DEFAULT_WINDOW_SECONDS
    if from_ts < 0 or to_ts < 0:
        raise HTTPException(status_code=400, detail="from/to must be non-negative")
    if from_ts >= to_ts:
        raise HTTPException(status_code=400, detail="from must be before to")
    if to_ts - from_ts > _MAX_WINDOW_SECONDS:
        raise HTTPException(
            status_code=400, detail="window must be <= 90 days"
        )
    return from_ts, to_ts


@router.get("/{runtime_name}/traces", dependencies=[Depends(require_scopes("observability:read"))])
async def get_runtime_traces(
    runtime_name: str,
    from_: int | None = Query(default=None, alias="from"),
    to: int | None = Query(default=None),
    trace_id: str | None = Query(default=None, alias="traceId"),
    caller_sub: str = Depends(get_caller_sub),
) -> dict:
    """Phase 5 (Loom) — OTEL span waterfall for *runtime_name*'s production runtime.

    Owner-checked (same resolver as cost). Returns a nested parent/child span
    tree with offsets/durations the frontend renders as a timeline.
    """
    runtime_name = _validate_runtime_name(runtime_name)
    from_ts, to_ts = _resolve_window(from_, to)
    runtime_id, version_id = _resolve_owned_runtime_id(runtime_name, caller_sub)
    from app.services.trace_query import fetch_trace_waterfall

    wf = fetch_trace_waterfall(runtime_id, from_ts, to_ts, _region(), trace_id=trace_id)
    wf.update({"runtime_name": runtime_name, "version_id": version_id})
    return wf


@router.get("/{runtime_name}/cost", dependencies=[Depends(require_scopes("cost:read"))])
async def get_runtime_cost(
    runtime_name: str,
    from_: int | None = Query(default=None, alias="from"),
    to: int | None = Query(default=None),
    caller_sub: str = Depends(get_caller_sub),
) -> dict:
    """Return the cost + token rollup for *runtime_name*'s production runtime.

    Query params ``from`` / ``to`` are epoch SECONDS (default: last 24h).
    """
    runtime_name = _validate_runtime_name(runtime_name)
    from_ts, to_ts = _resolve_window(from_, to)
    runtime_id, version_id = _resolve_owned_runtime_id(runtime_name, caller_sub)

    summary = summarize_from_logs(runtime_id, from_ts, to_ts, _region())
    summary.update(
        {
            "runtime_name": runtime_name,
            "version_id": version_id,
            "runtime_id": runtime_id,
        }
    )
    # Phase 4 (Loom) FinOps — annotate the rollup with the caller's owner budget
    # status (if set), so the cost panel can render a spend-vs-budget bar without
    # a second round-trip. Best-effort: never fail the cost read on a budget error.
    try:
        from app.services.budget_store import evaluate_budget, get_budget_store

        b = get_budget_store().get("default", "owner", caller_sub)
        if b is not None:
            ob = evaluate_budget(
                b.limit_usd, b.warn_pct, float(summary.get("total_cost", 0.0))
            )
            summary["owner_budget"] = ob
            # Phase B hardening — emit a CloudWatch metric when a budget is at
            # warn/over so an ops alarm can fire WITHOUT a scheduled poller
            # (metric-on-read; the dashboard already reads cost, so this is free).
            if ob["status"] in ("warn", "over"):
                _emit_budget_breach_metric(ob["status"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner budget annotation skipped: %s", exc)
    return summary


def _emit_budget_breach_metric(status: str) -> None:
    """Best-effort CloudWatch metric for a budget warn/over (Phase B FinOps).

    Namespace <project>/<env>/finops, metric BudgetBreach, dimension Status.
    Never raises — a metric failure must not affect the cost read.
    """
    try:
        import boto3
        proj = os.environ.get("PROJECT_NAME", "agentcore-workflow")
        env = os.environ.get("ENVIRONMENT", "dev")
        boto3.client("cloudwatch", region_name=_region()).put_metric_data(
            Namespace=f"{proj}/{env}/finops",
            MetricData=[{
                "MetricName": "BudgetBreach",
                "Dimensions": [{"Name": "Status", "Value": status}],
                "Value": 1,
                "Unit": "Count",
            }],
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("budget breach metric emit skipped: %s", exc)


# ---------------------------------------------------------------------------
# Phase 4 (Loom) FinOps — cost budgets. Separate /api/cost prefix (org/owner
# scoped, not per-runtime). Budgets read actual spend from the SAME CloudWatch
# cost pipeline as the dashboard, so no new metering is required.
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field  # noqa: E402

budgets_router = APIRouter(prefix="/api/cost", tags=["cost-budgets"])


class BudgetRequest(BaseModel):
    scope: str = Field(pattern=r"^(owner|agent|tag)$")
    key: str = Field(min_length=1, max_length=256)
    limit_usd: float = Field(gt=0)
    warn_pct: int = Field(default=80, ge=0, le=100)


def _budget_key_for_scope(scope: str, key: str, caller_sub: str) -> str:
    """Owner budgets are always keyed to the caller (no cross-tenant budgets)."""
    if scope == "owner":
        return caller_sub
    return key


@budgets_router.get("/budgets", dependencies=[Depends(require_scopes("cost:read"))])
async def list_budgets(caller_sub: str = Depends(get_caller_sub)) -> list[dict]:
    from app.services.budget_store import get_budget_store

    budgets = get_budget_store().list_all("default")
    # Only surface the caller's own owner-budget + shared agent/tag budgets.
    out = []
    for b in budgets:
        if b.scope == "owner" and b.key != caller_sub:
            continue
        out.append({"scope": b.scope, "key": b.key, "limit_usd": b.limit_usd,
                    "warn_pct": b.warn_pct, "period": b.period})
    return out


@budgets_router.post("/budgets", dependencies=[Depends(require_scopes("cost:write"))])
async def upsert_budget(
    body: BudgetRequest, caller_sub: str = Depends(get_caller_sub)
) -> dict:
    from app.services.budget_store import Budget, get_budget_store

    key = _budget_key_for_scope(body.scope, body.key, caller_sub)
    b = get_budget_store().put(
        Budget(org_id="default", scope=body.scope, key=key,  # type: ignore[arg-type]
               limit_usd=body.limit_usd, warn_pct=body.warn_pct)
    )
    return {"scope": b.scope, "key": b.key, "limit_usd": b.limit_usd, "warn_pct": b.warn_pct}


@budgets_router.delete("/budgets/{scope}/{key}",
                       dependencies=[Depends(require_scopes("cost:write"))])
async def delete_budget(scope: str, key: str, caller_sub: str = Depends(get_caller_sub)) -> dict:
    if scope not in ("owner", "agent", "tag"):
        raise HTTPException(status_code=400, detail="Invalid scope")
    from app.services.budget_store import get_budget_store

    resolved_key = _budget_key_for_scope(scope, key, caller_sub)
    get_budget_store().delete("default", scope, resolved_key)  # type: ignore[arg-type]
    return {"deleted": {"scope": scope, "key": resolved_key}}
