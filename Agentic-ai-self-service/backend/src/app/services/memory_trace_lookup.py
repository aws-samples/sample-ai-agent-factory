"""Resolve AgentCore Memory APPLICATION_LOGS records to trace ids.

Why this exists (verified live 2026-09-14, us-west-2):

* Memory application logs (long-term extraction / consolidation, DeleteMemory)
  carry ``session_id``, ``actor_id``, ``requestId`` and the source event ids
  (``body.currentConversations[].eventId``) but NO trace id. The CloudWatch
  vended-log delivery schema lists ``traceId``/``spanId`` for Memory, yet the
  service emits them empty, so they are dropped from every record. This holds
  whether Memory is driven by a Harness, a Runtime agent, or a direct SDK call.
* Memory *spans* (TRACES delivery -> X-Ray -> ``aws/spans``) DO carry
  ``traceId`` and, on ``CreateEvent``, an ``event.id`` attribute in the same
  ``<epoch-ms>#<hash>`` form the logs embed. Every span also carries
  ``session.id`` / ``actor.id`` / ``memory.id`` (and ``harness.id`` when the
  caller is a Harness).

So the join is:

    log record -> body.currentConversations[].eventId
               -> CreateEvent span with attributes.event.id == eventId
               -> traceId                                   (exact, "event")

then widened to the rest of the same ingestion job, because every record an
extraction job writes (all strategies + consolidation) shares one
``body.requestId``:

    log record -> body.requestId -> sibling record resolved by event -> traceId
                                                                    ("request")

with a coarser fallback for anything still unresolved:

    log record -> session_id -> any Memory span in that session -> traceId(s)
                                                                    ("session")

Pure helpers (``parse_record``, ``event_ids_from_record``, ``build_spans_query``,
``join_records_to_spans``) have no AWS in the hot path and are unit-tested;
``resolve_memory_log_traces`` wires them to Logs Insights.
"""

from __future__ import annotations

import json
import logging
import time

import boto3

logger = logging.getLogger(__name__)

SPANS_LOG_GROUP = "aws/spans"
MEMORY_LOG_GROUP_PREFIX = "/aws/vendedlogs/bedrock-agentcore/memory/APPLICATION_LOGS/"

# Memory event ids look like ``0000001789385121339#a124a6bd``: the first field is
# the event timestamp in epoch millis. We use it to bound the span query window.
_EVENT_ID_SEP = "#"


def memory_log_group(memory_id: str) -> str:
    """Default vended log group for a Memory's APPLICATION_LOGS."""
    return f"{MEMORY_LOG_GROUP_PREFIX}{memory_id}"


def parse_record(message: str | dict) -> dict:
    """Parse one Memory application-log line into a dict (``{}`` on garbage)."""
    if isinstance(message, dict):
        return message
    try:
        parsed = json.loads(message)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def event_ids_from_record(record: dict) -> list[str]:
    """Return the source event ids referenced by a Memory log record.

    Extraction logs list the events they analysed under
    ``body.currentConversations[].eventId``; a top-level ``event_id`` is also
    honoured (documented field). Order preserved, duplicates removed.
    """
    ids: list[str] = []
    top = record.get("event_id")
    if isinstance(top, str) and top:
        ids.append(top)
    body = record.get("body")
    if isinstance(body, dict):
        for conv in body.get("currentConversations") or []:
            if isinstance(conv, dict):
                eid = conv.get("eventId")
                if isinstance(eid, str) and eid and eid not in ids:
                    ids.append(eid)
    return ids


def session_id_from_record(record: dict) -> str:
    sid = record.get("session_id")
    if not sid:
        attrs = record.get("attributes")
        if isinstance(attrs, dict):
            sid = attrs.get("session.id")
    return sid if isinstance(sid, str) else ""


def event_id_timestamp_ms(event_id: str) -> int | None:
    """Epoch millis encoded in a Memory event id, or None if not parseable."""
    head = event_id.split(_EVENT_ID_SEP, 1)[0]
    if head.isdigit():
        try:
            return int(head)
        except ValueError:
            return None
    return None


def _quote(values: list[str]) -> str:
    """Render a Logs Insights string list: ``["a", "b"]`` (json escaping suffices)."""
    return "[" + ", ".join(json.dumps(v) for v in values) + "]"


def build_spans_query(memory_id: str, event_ids: list[str], session_ids: list[str], *, limit: int = 1000) -> str:
    """Logs Insights query over ``aws/spans`` for a Memory's spans.

    Matches spans emitted by *memory_id* whose ``event.id`` is one of
    *event_ids* OR whose ``session.id`` is one of *session_ids*. Both lists may
    be empty (then only the memory filter applies).
    """
    clauses = []
    if event_ids:
        clauses.append(f"attributes.event.id in {_quote(event_ids)}")
    if session_ids:
        clauses.append(f"attributes.session.id in {_quote(session_ids)}")
    scope = f"attributes.memory.id = {json.dumps(memory_id)}"
    where = f"{scope} and ({' or '.join(clauses)})" if clauses else scope
    return (
        "fields @timestamp, traceId, spanId, parentSpanId, name, "
        "attributes.event.id, attributes.session.id, attributes.actor.id, attributes.harness.id\n"
        f"| filter {where}\n"
        "| sort @timestamp asc\n"
        f"| limit {int(limit)}"
    )


def join_records_to_spans(records: list[dict], spans: list[dict]) -> dict:
    """Pure join of parsed log *records* onto span rows.

    *spans* rows are flat dicts as returned by Logs Insights (keys like
    ``traceId``, ``attributes.event.id``, ``attributes.session.id``).

    Returns::

        {"by_event_id": {event_id: trace_id},
         "by_session_id": {session_id: [trace_id, ...]},   # insertion order
         "records": [{"session_id", "actor_id", "request_id", "log",
                      "memory_strategy_id", "event_ids", "trace_id",
                      "trace_ids",
                      "resolution": "event" | "request" | "session" | None}]}
    """
    by_event: dict[str, str] = {}
    by_session: dict[str, list[str]] = {}
    for row in spans:
        tid = row.get("traceId") or ""
        if not tid:
            continue
        eid = row.get("attributes.event.id") or ""
        if eid and eid not in by_event:
            by_event[eid] = tid
        sid = row.get("attributes.session.id") or ""
        if sid:
            bucket = by_session.setdefault(sid, [])
            if tid not in bucket:
                bucket.append(tid)

    # Pass 1: exact event-id resolution, remembered per (session, requestId) so
    # sibling records of the same ingestion job can inherit it in pass 2.
    by_request: dict[tuple[str, str], str] = {}
    for rec in records:
        body = rec.get("body") if isinstance(rec.get("body"), dict) else {}
        rid = body.get("requestId") or ""
        sid = session_id_from_record(rec)
        tid = next((by_event[e] for e in event_ids_from_record(rec) if e in by_event), None)
        if tid and rid and (sid, rid) not in by_request:
            by_request[(sid, rid)] = tid

    out_records = []
    for rec in records:
        eids = event_ids_from_record(rec)
        sid = session_id_from_record(rec)
        body = rec.get("body") if isinstance(rec.get("body"), dict) else {}
        rid = body.get("requestId") or ""
        trace_id = next((by_event[e] for e in eids if e in by_event), None)
        resolution: str | None = "event" if trace_id else None
        if not trace_id and rid and (sid, rid) in by_request:
            trace_id = by_request[(sid, rid)]
            resolution = "request"
        trace_ids = [trace_id] if trace_id else []
        if not trace_id and sid and by_session.get(sid):
            trace_ids = list(by_session[sid])
            # A single candidate is as good as exact; several means "one of".
            trace_id = trace_ids[0] if len(trace_ids) == 1 else None
            resolution = "session"
        out_records.append(
            {
                "session_id": sid,
                "actor_id": rec.get("actor_id") or "",
                "request_id": body.get("requestId") or "",
                "memory_strategy_id": rec.get("memory_strategy_id") or "",
                "log": body.get("log") or "",
                "event_timestamp": rec.get("event_timestamp"),
                "event_ids": eids,
                "trace_id": trace_id,
                "trace_ids": trace_ids,
                "resolution": resolution,
            }
        )
    return {"by_event_id": by_event, "by_session_id": by_session, "records": out_records}


def _run_insights_query(
    logs_client, log_group: str, query: str, start_s: int, end_s: int, poll_seconds: float
) -> tuple[list[dict], str]:
    """Start + poll a Logs Insights query; return (rows, status)."""
    try:
        query_id = logs_client.start_query(
            logGroupName=log_group,
            startTime=int(start_s),
            endTime=int(end_s),
            queryString=query,
        ).get("queryId")
    except logs_client.exceptions.ResourceNotFoundException:
        return [], "Empty"
    if not query_id:
        return [], "Empty"
    deadline = time.time() + poll_seconds
    status = "Running"
    rows: list[dict] = []
    while time.time() < deadline:
        resp = logs_client.get_query_results(queryId=query_id)
        status = resp.get("status", "Running")
        if status in ("Complete", "Failed", "Cancelled"):
            rows = [
                {f["field"]: f["value"] for f in row if f.get("field") != "@ptr"} for row in resp.get("results", [])
            ]
            break
        time.sleep(0.5)
    if status == "Running":
        try:
            logs_client.stop_query(queryId=query_id)
        except Exception:  # noqa: BLE001 — best-effort cancel
            logger.debug("stop_query %s failed", query_id, exc_info=True)
    return rows, status


def fetch_memory_log_records(
    memory_id: str,
    region: str,
    from_ts: int,
    to_ts: int,
    *,
    logs_client=None,
    log_group: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Read parsed Memory APPLICATION_LOGS records in ``[from_ts, to_ts]`` (epoch s)."""
    logs_client = logs_client or boto3.client("logs", region_name=region)
    group = log_group or memory_log_group(memory_id)
    records: list[dict] = []
    kwargs: dict = {"logGroupName": group, "startTime": int(from_ts) * 1000, "endTime": int(to_ts) * 1000}
    try:
        while len(records) < limit:
            resp = logs_client.filter_log_events(**kwargs)
            for ev in resp.get("events", []):
                rec = parse_record(ev.get("message", ""))
                if rec:
                    records.append(rec)
            token = resp.get("nextToken")
            if not token:
                break
            kwargs["nextToken"] = token
    except logs_client.exceptions.ResourceNotFoundException:
        return []
    return records[:limit]


def resolve_memory_log_traces(
    memory_id: str,
    records: list[dict],
    region: str,
    *,
    logs_client=None,
    from_ts: int | None = None,
    to_ts: int | None = None,
    poll_seconds: float = 20.0,
    spans_log_group: str = SPANS_LOG_GROUP,
) -> dict:
    """Attach trace ids to Memory log *records* by joining onto Memory spans.

    The span query window defaults to [earliest event-id timestamp - 1h, now];
    pass ``from_ts``/``to_ts`` (epoch seconds) to override. Returns the
    ``join_records_to_spans`` shape plus ``query_status`` and ``spans_seen``.
    """
    logs_client = logs_client or boto3.client("logs", region_name=region)
    event_ids: list[str] = []
    session_ids: list[str] = []
    for rec in records:
        for eid in event_ids_from_record(rec):
            if eid not in event_ids:
                event_ids.append(eid)
        sid = session_id_from_record(rec)
        if sid and sid not in session_ids:
            session_ids.append(sid)

    now = int(time.time())
    if from_ts is None:
        ts_ms = [t for t in (event_id_timestamp_ms(e) for e in event_ids) if t]
        rec_ts = [int(r["event_timestamp"]) for r in records if isinstance(r.get("event_timestamp"), int)]
        earliest_ms = min(ts_ms + rec_ts) if (ts_ms or rec_ts) else (now - 3600) * 1000
        from_ts = earliest_ms // 1000 - 3600
    if to_ts is None:
        to_ts = now + 60

    result: dict
    if not records:
        result = join_records_to_spans([], [])
        result.update(query_status="Empty", spans_seen=0)
        return result

    query = build_spans_query(memory_id, event_ids, session_ids)
    rows, status = _run_insights_query(logs_client, spans_log_group, query, from_ts, to_ts, poll_seconds)
    result = join_records_to_spans(records, rows)
    result.update(query_status=status, spans_seen=len(rows))
    return result
