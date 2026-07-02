"""Cost analytics + FinOps for deployed AgentCore runtimes — Phase 2 Gap 2B.

Per-agent / per-invocation cost + token analytics. The PRIMARY data path is
QUERY-TIME: ``summarize_from_logs()`` reads ``gen_ai.usage.*`` attributes
straight out of the runtime's CloudWatch Logs (the same source the
``observability_dashboard.py`` token widget uses) and prices them with a
baked-in Bedrock price table. There is NO write path and NO per-runtime AWS
resource in the primary flow, so no ``destroy_runtime`` cleanup is required.

The ``UsageEventsStore`` + DDB table below are still designed and shipped for
EXPLICIT events (e.g. a future codegen span processor in the generated agent,
or a batch backfill), but they are optional/dormant in the primary flow.

Storage patterns mirror ``registry_store.py`` / ``agent_versions_store.py``:
Decimal helpers, lazy env-driven singleton, GSI queries, a sortable id.

Tenant model: ``UsageEvent`` PK is ``runtime_id`` (AWS-assigned, never
tenant-supplied) and SK is a random sortable ``event_id``, so cross-tenant
overwrite is structurally impossible (Bug 122). ``owner_sub`` is still stamped
and the ``owner_sub-event_id-index`` GSI is owner-scoped.

Bedrock price window (Bug 113): current as of the July-2026 window —
``anthropic.claude-sonnet-5``, ``anthropic.claude-sonnet-4-6``,
``anthropic.claude-opus-4-8``, and ``anthropic.claude-haiku-4-5-...``; the
previous-window ``anthropic.claude-sonnet-4-5-...`` id is kept because
deployed runtimes may still emit it in logs (the table prices LOGGED ids).
Keys are matched after the ``us.``/``eu.``/``ap.`` inference-profile prefix
is stripped. Unknown models fall back to a default rate and are logged,
never crash.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bedrock price table (USD per 1,000 tokens) — current model window only.
# ---------------------------------------------------------------------------
#
# Rates are keyed by the *normalized* bedrock model id (inference-profile
# region prefix stripped). Both the long anthropic-foundation-model id and the
# bare form are listed so whatever lands in ``gen_ai.request.model`` resolves.
# Source: AWS Bedrock on-demand pricing, current as of the July-2026 window.
# REVIEW each model-window rotation (Bug 113).
_PRICE_PER_1K: dict[str, tuple[float, float]] = {
    # (input_per_1k, output_per_1k)
    # Claude Sonnet 5
    "anthropic.claude-sonnet-5": (0.003, 0.015),
    # Claude Sonnet 4.6
    "anthropic.claude-sonnet-4-6": (0.003, 0.015),
    # Claude Opus 4.8
    "anthropic.claude-opus-4-8": (0.005, 0.025),
    # Claude Haiku 4.5 (published 2025-10-01)
    "anthropic.claude-haiku-4-5-20251001-v1:0": (0.001, 0.005),
    # -- Previous window: kept because deployed runtimes may still emit this
    #    model id in logs; the table prices LOGGED ids, so removing it would
    #    misprice history.
    # Claude Sonnet 4.5 (published 2025-09-29)
    "anthropic.claude-sonnet-4-5-20250929-v1:0": (0.003, 0.015),
}

# Fallback rate for unknown models so the endpoint never crashes (and is
# never negative). Mirrors the more expensive tier so we under-promise on
# savings rather than under-report cost.
_DEFAULT_PRICE_PER_1K: tuple[float, float] = (0.003, 0.015)

# Inference-profile region prefixes that AgentCore prepends to a bedrock
# model id (e.g. ``us.anthropic...`` / ``eu.anthropic...`` / ``ap.anthropic...``).
_INFERENCE_PROFILE_PREFIX = re.compile(r"^(us|eu|ap|us-gov)\.")

# Default 90-day TTL for explicit usage events (write-path only).
_EVENT_TTL_SECONDS = 90 * 24 * 3600


def normalize_model_id(model_id: Optional[str]) -> str:
    """Strip the inference-profile region prefix from a bedrock model id.

    ``us.anthropic.claude-sonnet-4-5-...`` -> ``anthropic.claude-sonnet-4-5-...``
    Leaves an already-bare id untouched. Returns ``""`` for None/empty.
    """
    if not model_id:
        return ""
    return _INFERENCE_PROFILE_PREFIX.sub("", str(model_id).strip())


def compute_cost(
    model_id: Optional[str], input_tokens: int, output_tokens: int
) -> float:
    """Return the USD cost for *input_tokens*/*output_tokens* of *model_id*.

    Normalizes the model id (drops ``us.``/``eu.``/``ap.`` inference-profile
    prefix). Unknown models fall back to ``_DEFAULT_PRICE_PER_1K`` and log a
    warning. Negative token counts are clamped to 0. Always non-negative.
    """
    in_tok = max(int(input_tokens or 0), 0)
    out_tok = max(int(output_tokens or 0), 0)
    if in_tok == 0 and out_tok == 0:
        return 0.0

    normalized = normalize_model_id(model_id)
    rate = _PRICE_PER_1K.get(normalized)
    if rate is None:
        logger.warning(
            "Unknown bedrock model for pricing: %r (normalized=%r); "
            "falling back to default rate",
            model_id,
            normalized,
        )
        rate = _DEFAULT_PRICE_PER_1K

    in_rate, out_rate = rate
    cost = (in_tok / 1000.0) * in_rate + (out_tok / 1000.0) * out_rate
    return round(cost, 8)


def extract_usage_from_otel_span(span_attrs: Optional[dict]) -> dict:
    """Pull token usage + model from a GenAI-semconv span's attributes.

    Reads ``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens`` and
    ``gen_ai.request.model`` (falls back to ``gen_ai.response.model``).
    Tolerates int- and str-typed attribute values. Returns a dict with
    ``input_tokens`` (int), ``output_tokens`` (int), ``model_id`` (str|None).
    Missing/garbage attrs degrade gracefully to zeros / None.
    """
    attrs = span_attrs or {}

    def _to_int(value) -> int:
        if value is None:
            return 0
        try:
            return max(int(float(value)), 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = _to_int(attrs.get("gen_ai.usage.input_tokens"))
    output_tokens = _to_int(attrs.get("gen_ai.usage.output_tokens"))
    model_id = attrs.get("gen_ai.request.model") or attrs.get(
        "gen_ai.response.model"
    )
    if model_id is not None:
        model_id = str(model_id)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model_id": model_id,
    }


# ---------------------------------------------------------------------------
# Sortable event id (ULID-shaped, same shape as agent_versions_store).
# ---------------------------------------------------------------------------


def new_event_id() -> str:
    """Return a 32-char lowercase hex id sortable by creation time.

    12 hex chars of millisecond epoch + 20 hex chars of random. Lexicographic
    order equals chronological order across ms windows, so an SK range query
    on the time-prefix portion bounds events by time.
    """
    ms = int(time.time() * 1000)
    return f"{ms:012x}{secrets.token_hex(10)}"


def _event_id_floor(epoch_ms: int) -> str:
    """Lowest event_id that could have been minted at *epoch_ms* (zero tail)."""
    return f"{max(int(epoch_ms), 0):012x}" + "0" * 20


def _event_id_ceil(epoch_ms: int) -> str:
    """Highest event_id that could have been minted at *epoch_ms* (max tail)."""
    return f"{max(int(epoch_ms), 0):012x}" + "f" * 20


# ---------------------------------------------------------------------------
# Decimal/float helpers (shared shape with the other stores).
# ---------------------------------------------------------------------------


def _floats_to_decimals(obj):
    if isinstance(obj, float):
        if obj != 0.0 and abs(obj) < 1e-130:
            return Decimal("0")
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimals(v) for v in obj]
    return obj


def _decimals_to_floats(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: _decimals_to_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimals_to_floats(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Model (lightweight dataclass; internal, like agent_versions_store).
# ---------------------------------------------------------------------------


@dataclass
class UsageEvent:
    """One priced usage record for a single runtime invocation (write path)."""

    runtime_id: str
    event_id: str
    owner_sub: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    ts: str  # ISO 8601
    version_id: Optional[str] = None
    ttl: Optional[int] = None

    def to_item(self) -> dict:
        item: dict = {
            "runtime_id": self.runtime_id,
            "event_id": self.event_id,
            "owner_sub": self.owner_sub,
            "model_id": self.model_id,
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "cost_usd": float(self.cost_usd),
            "ts": self.ts,
        }
        if self.version_id is not None:
            item["version_id"] = self.version_id
        # TTL defaults to now + 90d so the dormant table self-bounds growth.
        ttl = self.ttl if self.ttl is not None else int(time.time()) + _EVENT_TTL_SECONDS
        item["ttl"] = int(ttl)
        return _floats_to_decimals(item)

    @classmethod
    def from_item(cls, item: dict) -> "UsageEvent":
        item = _decimals_to_floats(dict(item))
        return cls(
            runtime_id=item["runtime_id"],
            event_id=item["event_id"],
            owner_sub=item.get("owner_sub", ""),
            model_id=item.get("model_id", ""),
            input_tokens=int(item.get("input_tokens", 0)),
            output_tokens=int(item.get("output_tokens", 0)),
            cost_usd=float(item.get("cost_usd", 0.0)),
            ts=item.get("ts", ""),
            version_id=item.get("version_id"),
            ttl=int(item["ttl"]) if item.get("ttl") is not None else None,
        )


def summarize(events: list[UsageEvent]) -> dict:
    """Aggregate a list of UsageEvents into a cost/token rollup.

    Returns ``{total_cost, total_in, total_out, by_model}`` where ``by_model``
    maps each ``model_id`` to its own ``{cost, input_tokens, output_tokens,
    count}`` bucket.
    """
    total_cost = 0.0
    total_in = 0
    total_out = 0
    by_model: dict[str, dict] = {}
    for ev in events:
        total_cost += float(ev.cost_usd)
        total_in += int(ev.input_tokens)
        total_out += int(ev.output_tokens)
        bucket = by_model.setdefault(
            ev.model_id or "unknown",
            {"cost": 0.0, "input_tokens": 0, "output_tokens": 0, "count": 0},
        )
        bucket["cost"] += float(ev.cost_usd)
        bucket["input_tokens"] += int(ev.input_tokens)
        bucket["output_tokens"] += int(ev.output_tokens)
        bucket["count"] += 1
    for bucket in by_model.values():
        bucket["cost"] = round(bucket["cost"], 8)
    return {
        "total_cost": round(total_cost, 8),
        "total_in": total_in,
        "total_out": total_out,
        "by_model": by_model,
    }


# ---------------------------------------------------------------------------
# Store (write path — optional / dormant in the primary flow).
# ---------------------------------------------------------------------------


class UsageEventsStore:
    """CRUD + queries for the UsageEvents DDB table."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._region = region
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put(self, event: UsageEvent) -> None:
        self._table.put_item(Item=event.to_item())
        logger.info(
            "Wrote UsageEvent %s/%s (model=%s, cost=%s)",
            event.runtime_id,
            event.event_id,
            event.model_id,
            event.cost_usd,
        )

    def get(self, runtime_id: str, event_id: str) -> Optional[UsageEvent]:
        resp = self._table.get_item(
            Key={"runtime_id": runtime_id, "event_id": event_id}
        )
        item = resp.get("Item")
        return UsageEvent.from_item(item) if item else None

    def query_for_runtime(
        self,
        runtime_id: str,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
    ) -> list[UsageEvent]:
        """Return events for *runtime_id*, newest-first.

        ``from_ts``/``to_ts`` are epoch SECONDS. Because the SK ``event_id``
        is time-prefixed (ms-epoch), we translate the window into an SK
        ``between`` range so DynamoDB filters server-side.
        """
        cond = Key("runtime_id").eq(runtime_id)
        if from_ts is not None or to_ts is not None:
            lo = _event_id_floor((from_ts or 0) * 1000)
            hi = _event_id_ceil((to_ts if to_ts is not None else int(time.time())) * 1000)
            cond = cond & Key("event_id").between(lo, hi)
        items: list[dict] = []
        kwargs: dict = {
            "KeyConditionExpression": cond,
            "ScanIndexForward": False,  # newest first
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [UsageEvent.from_item(i) for i in items]

    def query_for_owner(self, owner_sub: str) -> list[UsageEvent]:
        """Return every event owned by *owner_sub* via the owner GSI, newest-first."""
        items: list[dict] = []
        kwargs: dict = {
            "IndexName": "owner_sub-event_id-index",
            "KeyConditionExpression": Key("owner_sub").eq(owner_sub),
            "ScanIndexForward": False,
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [UsageEvent.from_item(i) for i in items]


# ---------------------------------------------------------------------------
# Query-time primary path: summarize cost from CloudWatch Logs.
# ---------------------------------------------------------------------------


def log_group_for_runtime(runtime_id: str) -> str:
    """Return the canonical runtime token-log group for *runtime_id*.

    Reuses the dashboard's ``-DEFAULT`` form (observability_dashboard.py)
    rather than evaluations.py's bare form, because that is where the runtime
    actually emits ``gen_ai.usage`` spans. See the LOG-GROUP-NAMING-drift risk.
    """
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"


def summarize_from_logs(
    runtime_id: str,
    from_ts: int,
    to_ts: int,
    region: str,
    *,
    poll_seconds: float = 10.0,
) -> dict:
    """Compute a cost/token rollup from the runtime's CloudWatch Logs.

    Runs a Logs Insights query over the runtime token-log group that parses
    ``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens`` and
    ``gen_ai.request.model`` from each log message, grouped by model, then
    applies ``compute_cost`` per model.

    Returns ``{total_cost, total_in, total_out, by_model, from_ts, to_ts,
    log_group_name, query_status}``. When the log group does not exist yet
    (runtime hasn't received instrumented traffic), returns an EMPTY summary
    — never raises.
    """
    log_group = log_group_for_runtime(runtime_id)
    empty = {
        "total_cost": 0.0,
        "total_in": 0,
        "total_out": 0,
        "by_model": {},
        "from_ts": from_ts,
        "to_ts": to_ts,
        "log_group_name": log_group,
        "query_status": "Complete",
    }

    logs_client = boto3.client("logs", region_name=region)

    # Group token sums by the request model so we can price each model
    # separately. The dashboard widget proves this @message shape parses.
    query_string = (
        "fields @timestamp, @message"
        "\n| filter @message like /gen_ai.usage/"
        '\n| parse @message /"gen_ai.usage.input_tokens":\\s*(?<in_tok>\\d+)/'
        '\n| parse @message /"gen_ai.usage.output_tokens":\\s*(?<out_tok>\\d+)/'
        '\n| parse @message /"gen_ai.request.model":\\s*"?(?<model>[^",}\\s]+)"?/'
        "\n| stats sum(in_tok) as input_tokens, sum(out_tok) as output_tokens, "
        "count(*) as invocations by model"
        "\n| sort by input_tokens desc"
        "\n| limit 100"
    )

    try:
        start_resp = logs_client.start_query(
            logGroupName=log_group,
            startTime=int(from_ts),
            endTime=int(to_ts),
            queryString=query_string,
        )
        query_id = start_resp.get("queryId")
        if not query_id:
            logger.warning("summarize_from_logs: no queryId returned")
            return empty
    except logs_client.exceptions.ResourceNotFoundException:
        # Log group not created yet — no instrumented traffic. Empty, not error.
        return empty
    except Exception:
        logger.exception("summarize_from_logs: start_query failed")
        return empty

    # Poll until the query finishes (Logs Insights is async). Bound the poll
    # to stay under API GW's 29s ceiling with margin (mirrors evaluations.py).
    deadline = time.time() + poll_seconds
    rows: list[dict] = []
    status = "Running"
    while time.time() < deadline:
        get_resp = logs_client.get_query_results(queryId=query_id)
        status = get_resp.get("status", "Running")
        if status in ("Complete", "Failed", "Cancelled"):
            for row in get_resp.get("results", []):
                rows.append({field["field"]: field["value"] for field in row})
            break
        time.sleep(0.5)

    if status == "Running":
        try:
            logs_client.stop_query(queryId=query_id)
        except Exception:
            pass

    total_cost = 0.0
    total_in = 0
    total_out = 0
    by_model: dict[str, dict] = {}
    for row in rows:
        model_id = row.get("model") or "unknown"
        try:
            in_tok = int(float(row.get("input_tokens", 0) or 0))
            out_tok = int(float(row.get("output_tokens", 0) or 0))
            invocations = int(float(row.get("invocations", 0) or 0))
        except (TypeError, ValueError):
            continue
        cost = compute_cost(model_id, in_tok, out_tok)
        total_cost += cost
        total_in += in_tok
        total_out += out_tok
        bucket = by_model.setdefault(
            model_id,
            {"cost": 0.0, "input_tokens": 0, "output_tokens": 0, "count": 0},
        )
        bucket["cost"] = round(bucket["cost"] + cost, 8)
        bucket["input_tokens"] += in_tok
        bucket["output_tokens"] += out_tok
        bucket["count"] += invocations

    return {
        "total_cost": round(total_cost, 8),
        "total_in": total_in,
        "total_out": total_out,
        "by_model": by_model,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "log_group_name": log_group,
        "query_status": status,
    }


# ---------------------------------------------------------------------------
# Lazy singleton from env.
# ---------------------------------------------------------------------------

_usage_events_store: Optional[UsageEventsStore] = None


def get_usage_events_store() -> UsageEventsStore:
    global _usage_events_store
    if _usage_events_store is None:
        _usage_events_store = UsageEventsStore(
            table_name=os.environ.get("USAGE_EVENTS_TABLE_NAME", "UsageEvents"),
            region=os.environ.get(
                "APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")
            ),
        )
    return _usage_events_store
