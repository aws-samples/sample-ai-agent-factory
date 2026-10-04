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

Ownership is enforced through
``runtime_target_context.resolve_owned_runtime_target``: an owner-checked
``RuntimeSlots`` row selects an owner-checked ``AgentVersions`` row, whose
immutable deployment id binds the exact target account, region, and role.
Cross-tenant requests return 404 (existence-non-disclosure). The endpoint never
trusts a tenant-supplied runtime_id, so the Bug-122 tenant-keyed-table collision
can't occur here.
"""

from __future__ import annotations

import logging
import os
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.services.auth import get_caller_sub
from app.services.aws_errors import error_code
from app.services.cost_tracking import summarize_from_logs
from app.services.rbac import SCOPE_ADMIN, has_scopes, require_scopes
from app.services.runtime_target_context import resolve_owned_runtime_target

logger = logging.getLogger(__name__)
_OBSERVABILITY_UNAVAILABLE = "Runtime observability is temporarily unavailable. Try again shortly."


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


def _service_unavailable(operation: str, exc: Exception) -> HTTPException:
    code = error_code(exc)
    logger.warning(
        "%s failed: %s%s",
        operation,
        type(exc).__name__,
        f" {code}" if code else "",
    )
    return HTTPException(status_code=503, detail=_OBSERVABILITY_UNAVAILABLE)


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
        raise HTTPException(status_code=400, detail="window must be <= 90 days")
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
    from app.services.trace_query import (
        fetch_trace_waterfall,
        validate_trace_id_filter,
    )

    try:
        trace_id = validate_trace_id_filter(trace_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    target = resolve_owned_runtime_target(runtime_name, caller_sub)
    runtime_id, version_id = target.runtime_id, target.version_id
    try:
        wf = fetch_trace_waterfall(
            runtime_id,
            from_ts,
            to_ts,
            target.region,
            logs_client=target.client("logs"),
            trace_id=trace_id,
        )
    except Exception as exc:
        raise _service_unavailable("query runtime traces", exc) from exc
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
    target = resolve_owned_runtime_target(runtime_name, caller_sub)
    runtime_id, version_id = target.runtime_id, target.version_id

    try:
        summary = summarize_from_logs(
            runtime_id,
            from_ts,
            to_ts,
            target.region,
            logs_client=target.client("logs"),
        )
    except Exception as exc:
        raise _service_unavailable("query runtime cost", exc) from exc
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
            ob = evaluate_budget(b.limit_usd, b.warn_pct, float(summary.get("total_cost", 0.0)))
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
            MetricData=[
                {
                    "MetricName": "BudgetBreach",
                    "Dimensions": [{"Name": "Status", "Value": status}],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
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


def _caller_is_budget_admin(request: Request) -> bool:
    """F-12: only the org-wide ``admin`` scope sees or changes another tenant's agent/tag budgets.

    Reuses rbac's existing super-scope (``g-admins-super`` / legacy ``org-admin``) rather than
    minting a budget-specific one. ``g-admins-cost`` holds cost:read/write and is a tenant here.
    A FastAPI dependency so tests can override it; the scope check itself is rbac's.
    """
    return has_scopes(request, (SCOPE_ADMIN,))


def _not_found() -> HTTPException:
    # Same wording and status as auth.assert_owner: a 403 would confirm the row exists.
    return HTTPException(status_code=404, detail="Not found")


@budgets_router.get("/budgets", dependencies=[Depends(require_scopes("cost:read"))])
async def list_budgets(
    caller_sub: str = Depends(get_caller_sub),
    admin: bool = Depends(_caller_is_budget_admin),
) -> list[dict]:
    from app.services.budget_store import get_budget_store

    # F-12: the caller's own budgets only. Listing every agent-scope row disclosed other tenants'
    # runtime names; a legacy row with no owner is an admin's to see, nobody else's.
    budgets = get_budget_store().list_visible("default", caller_sub=caller_sub, admin=admin)
    return [
        {"scope": b.scope, "key": b.key, "limit_usd": b.limit_usd, "warn_pct": b.warn_pct, "period": b.period}
        for b in budgets
    ]


@budgets_router.post("/budgets", dependencies=[Depends(require_scopes("cost:write"))])
async def upsert_budget(
    body: BudgetRequest,
    caller_sub: str = Depends(get_caller_sub),
    admin: bool = Depends(_caller_is_budget_admin),
) -> dict:
    from app.services.budget_store import Budget, BudgetOwnedByAnother, get_budget_store

    key = _budget_key_for_scope(body.scope, body.key, caller_sub)
    try:
        b = get_budget_store().put_owned(
            Budget(
                org_id="default",
                scope=body.scope,
                key=key,  # type: ignore[arg-type]
                limit_usd=body.limit_usd,
                warn_pct=body.warn_pct,
            ),
            caller_sub=caller_sub,
            admin=admin,
        )
    except BudgetOwnedByAnother as exc:
        raise _not_found() from exc
    return {"scope": b.scope, "key": b.key, "limit_usd": b.limit_usd, "warn_pct": b.warn_pct}


@budgets_router.delete("/budgets/{scope}/{key}", dependencies=[Depends(require_scopes("cost:write"))])
async def delete_budget(
    scope: str,
    key: str,
    caller_sub: str = Depends(get_caller_sub),
    admin: bool = Depends(_caller_is_budget_admin),
) -> dict:
    if scope not in ("owner", "agent", "tag"):
        raise HTTPException(status_code=400, detail="Invalid scope")
    from app.services.budget_store import BudgetOwnedByAnother, get_budget_store

    resolved_key = _budget_key_for_scope(scope, key, caller_sub)
    try:
        get_budget_store().delete_owned("default", scope, resolved_key, caller_sub=caller_sub, admin=admin)  # type: ignore[arg-type]
    except BudgetOwnedByAnother as exc:
        raise _not_found() from exc
    return {"deleted": {"scope": scope, "key": resolved_key}}
