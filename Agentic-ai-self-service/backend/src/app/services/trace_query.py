"""OTEL trace → waterfall shaping (Phase 5 — Loom-inspired trace viz).

The platform already emits OTEL spans to the runtime's CloudWatch log group
(services/_otel_platform + observability_dashboard). This module reads those
span records via Logs Insights and shapes them into a parent/child WATERFALL
the frontend renders as an interactive timeline.

Design: reuse the same log group + Logs Insights machinery as
cost_tracking.summarize_from_logs (log_group_for_runtime, start_query poll).
Span JSON fields we rely on (OTLP): traceId, spanId, parentSpanId, name,
startTimeUnixNano, endTimeUnixNano. build_waterfall() is a PURE function
(unit-testable) that turns a flat span list into a nested, offset-normalized
tree — no AWS in the hot path.
"""

from __future__ import annotations

import logging
import re

import boto3

from app.services.cost_tracking import (
    RuntimeLogQueryError,
    is_logs_resource_not_found,
    log_group_for_runtime,
    query_runtime_log_groups,
)

logger = logging.getLogger(__name__)

_W3C_TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_XRAY_TRACE_ID = re.compile(r"^1-[0-9a-fA-F]{8}-[0-9a-fA-F]{24}$")


def validate_trace_id_filter(trace_id: str | None) -> str | None:
    """Return a canonical trace id safe to embed in a Logs Insights query.

    AgentCore traces use W3C ids, while AWS/X-Ray integrations may expose the
    ``1-<epoch>-<random>`` form.  Accept only those two wire formats: this is
    both a useful caller error boundary and the injection boundary for the
    query string assembled below.
    """
    if trace_id is None:
        return None
    value = trace_id.strip()
    if not (_W3C_TRACE_ID.fullmatch(value) or _XRAY_TRACE_ID.fullmatch(value)):
        raise ValueError("traceId must be a W3C or X-Ray trace identifier")
    return value.lower()


def _ns_to_ms(ns) -> float:
    try:
        return round(int(ns) / 1_000_000, 3)
    except (TypeError, ValueError):
        return 0.0


def build_waterfall(spans: list[dict]) -> dict:
    """Turn a flat list of OTLP span dicts into a normalized waterfall tree.

    Each input span: {spanId, parentSpanId, name, startTimeUnixNano,
    endTimeUnixNano, (traceId)}. Output:
      {trace_id, start_ms, total_ms, spans:[{span_id, parent_span_id, name,
       offset_ms, duration_ms, depth, children:[...]}]}
    Offsets are relative to the earliest span start (so the UI draws bars from 0).
    Orphan spans (parent not in set) are treated as roots. Pure function.
    """
    norm = []
    for s in spans:
        start = int(s.get("startTimeUnixNano") or 0)
        end = int(s.get("endTimeUnixNano") or 0)
        norm.append(
            {
                "span_id": s.get("spanId") or "",
                "parent_span_id": s.get("parentSpanId") or "",
                "name": s.get("name") or "span",
                "_start": start,
                "_end": end,
            }
        )
    if not norm:
        return {"trace_id": None, "start_ms": 0, "total_ms": 0, "spans": []}

    base = min(n["_start"] for n in norm if n["_start"] > 0) if any(n["_start"] > 0 for n in norm) else 0
    max_end = max((n["_end"] for n in norm if n["_end"] > 0), default=base)
    by_id = {n["span_id"]: n for n in norm if n["span_id"]}

    for n in norm:
        n["offset_ms"] = _ns_to_ms(n["_start"] - base) if n["_start"] else 0.0
        n["duration_ms"] = _ns_to_ms(n["_end"] - n["_start"]) if (n["_end"] and n["_start"]) else 0.0
        n["children"] = []

    roots = []
    for n in norm:
        parent = by_id.get(n["parent_span_id"])
        if parent is not None and parent is not n:
            parent["children"].append(n)
        else:
            roots.append(n)

    def _emit(node, depth):
        out = {
            "span_id": node["span_id"],
            "parent_span_id": node["parent_span_id"],
            "name": node["name"],
            "offset_ms": node["offset_ms"],
            "duration_ms": node["duration_ms"],
            "depth": depth,
        }
        out["children"] = [_emit(c, depth + 1) for c in sorted(node["children"], key=lambda x: x["offset_ms"])]
        return out

    ordered = [_emit(r, 0) for r in sorted(roots, key=lambda x: x["offset_ms"])]
    trace_id = next((s.get("traceId") for s in spans if s.get("traceId")), None)
    return {
        "trace_id": trace_id,
        "start_ms": 0,
        "total_ms": _ns_to_ms(max_end - base) if max_end else 0.0,
        "spans": ordered,
    }


def fetch_trace_waterfall(
    runtime_id: str,
    from_ts: int,
    to_ts: int,
    region: str,
    *,
    logs_client=None,
    trace_id: str | None = None,
    poll_seconds: float = 10.0,
) -> dict:
    """Query the runtime's OTEL spans in [from_ts, to_ts] and build a waterfall.

    Reuses the qualifier-aware Logs Insights machinery from cost_tracking.
    Returns build_waterfall(...) plus {log_group_name, log_group_names,
    query_status}. Empty (not error) when no runtime group exists yet.
    """
    trace_id = validate_trace_id_filter(trace_id)
    # The caller may have resolved a same-account/non-home or cross-account
    # runtime.  Reusing its Logs client is what preserves those credentials;
    # constructing an ambient client here would query the platform account.
    logs_client = logs_client or boto3.client("logs", region_name=region)
    log_group = log_group_for_runtime(runtime_id)
    empty = {
        "trace_id": trace_id,
        "start_ms": 0,
        "total_ms": 0,
        "spans": [],
        "log_group_name": log_group,
        "log_group_names": [],
        "query_status": "Empty",
    }

    # Pull span records: OTLP spans are logged with a spanId + startTimeUnixNano.
    filt = "| filter ispresent(spanId) and ispresent(startTimeUnixNano)"
    if trace_id:
        filt += f' and traceId = "{trace_id}"'
    query_string = (
        "fields @timestamp, traceId, spanId, parentSpanId, name, "
        "startTimeUnixNano, endTimeUnixNano\n"
        f"{filt}\n"
        "| sort startTimeUnixNano asc\n"
        "| limit 200"
    )

    try:
        rows, status, log_groups = query_runtime_log_groups(
            logs_client,
            runtime_id,
            from_ts,
            to_ts,
            query_string,
            poll_seconds=poll_seconds,
        )
    except Exception as exc:
        if is_logs_resource_not_found(exc, logs_client):
            return empty
        raise

    if status not in {"Complete", "Empty"}:
        raise RuntimeLogQueryError(f"runtime trace query ended with status {status}")

    if not log_groups:
        return empty

    result = build_waterfall(rows)
    result["log_group_name"] = log_group
    result["log_group_names"] = log_groups
    result["query_status"] = status
    return result
