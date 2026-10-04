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

Bedrock price window (Bug 113): current as of the September-2026 window —
``anthropic.claude-sonnet-5``, ``anthropic.claude-sonnet-4-6``,
``anthropic.claude-opus-4-8``, and ``anthropic.claude-haiku-4-5-...``; the
previous-window ``anthropic.claude-sonnet-4-5-...`` id is kept because
deployed runtimes may still emit it in logs (the table prices LOGGED ids).
Keys are matched after the inference-profile geography prefix is stripped.
Input, output, cache-read, and five-minute cache-write tokens are priced
separately; Bedrock bills all four categories separately. Unknown models fall
back to a conservative default rate and are logged, never crash.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

from app.services.aws_errors import error_code, is_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bedrock price table (USD per 1,000 tokens) — current model window only.
# ---------------------------------------------------------------------------
#
# Rates are keyed by the *normalized* bedrock model id (inference-profile
# region prefix stripped). Both the long anthropic-foundation-model id and the
# bare form are listed so whatever lands in ``gen_ai.request.model`` resolves.
# Source: the AWS Price List API's AmazonBedrockFoundationModels offer,
# us-east-1 Global on-demand SKUs, read 2026-09-22. Cache-write rates are the
# ordinary five-minute TTL rate. The generated runtime does not expose a
# one-hour cache policy; if that changes, the usage record must carry the TTL
# before this ledger can price that more expensive category honestly.
# REVIEW each model-window rotation (Bug 113).


@dataclass(frozen=True)
class _TokenRates:
    input_per_1k: float
    output_per_1k: float
    cache_read_per_1k: float
    cache_write_per_1k: float


_PRICE_PER_1K: dict[str, _TokenRates] = {
    # Claude Sonnet 5
    "anthropic.claude-sonnet-5": _TokenRates(0.002, 0.010, 0.0002, 0.0025),
    # Claude Sonnet 4.6
    "anthropic.claude-sonnet-4-6": _TokenRates(0.003, 0.015, 0.0003, 0.00375),
    # Claude Opus 4.8
    "anthropic.claude-opus-4-8": _TokenRates(0.005, 0.025, 0.0005, 0.00625),
    # Claude Haiku 4.5 (published 2025-10-01)
    "anthropic.claude-haiku-4-5-20251001-v1:0": _TokenRates(0.001, 0.005, 0.0001, 0.00125),
    # -- Previous window: kept because deployed runtimes may still emit this
    #    model id in logs; the table prices LOGGED ids, so removing it would
    #    misprice history.
    # Claude Sonnet 4.5 (published 2025-09-29)
    "anthropic.claude-sonnet-4-5-20250929-v1:0": _TokenRates(0.003, 0.015, 0.0003, 0.00375),
}

# Fallback rate for unknown models so the endpoint never crashes (and is
# never negative). Mirrors the most expensive current tier in the table so we
# under-promise on savings rather than under-report cost.
_DEFAULT_PRICE_PER_1K = _TokenRates(0.005, 0.025, 0.0005, 0.00625)

# Inference-profile region prefixes that AgentCore prepends to a bedrock
# model id. ``apac`` is the current APAC family; ``ap`` is retained for older
# stored values, while ``jp`` and ``au`` are country-scoped APAC profiles.
_INFERENCE_PROFILE_PREFIX = re.compile(r"^(us|us-gov|eu|apac|ap|global|jp|au)\.")

# Default 90-day TTL for explicit usage events (write-path only).
_EVENT_TTL_SECONDS = 90 * 24 * 3600


def normalize_model_id(model_id: str | None) -> str:
    """Strip the inference-profile region prefix from a bedrock model id.

    ``us.anthropic.claude-sonnet-4-5-...`` -> ``anthropic.claude-sonnet-4-5-...``
    Leaves an already-bare id untouched. Returns ``""`` for None/empty.
    """
    if not model_id:
        return ""
    return _INFERENCE_PROFILE_PREFIX.sub("", str(model_id).strip())


def compute_cost(
    model_id: str | None,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_input_tokens: int = 0,
    cache_write_input_tokens: int = 0,
) -> float:
    """Return the USD cost for all reported token categories of *model_id*.

    Normalizes the model id by dropping a known inference-profile prefix.
    Cache reads and writes are not ordinary input tokens: Bedrock prices each
    separately, and its total input-token usage is the sum of all three input
    categories. Unknown models fall back to ``_DEFAULT_PRICE_PER_1K`` and log a
    warning. Negative token counts are clamped to 0. Always non-negative.
    """
    in_tok = max(int(input_tokens or 0), 0)
    out_tok = max(int(output_tokens or 0), 0)
    cache_read_tok = max(int(cache_read_input_tokens or 0), 0)
    cache_write_tok = max(int(cache_write_input_tokens or 0), 0)
    if in_tok == 0 and out_tok == 0 and cache_read_tok == 0 and cache_write_tok == 0:
        return 0.0

    normalized = normalize_model_id(model_id)
    rate = _PRICE_PER_1K.get(normalized)
    if rate is None:
        logger.warning(
            "Unknown bedrock model for pricing: %r (normalized=%r); falling back to default rate",
            model_id,
            normalized,
        )
        rate = _DEFAULT_PRICE_PER_1K

    cost = (
        (in_tok / 1000.0) * rate.input_per_1k
        + (out_tok / 1000.0) * rate.output_per_1k
        + (cache_read_tok / 1000.0) * rate.cache_read_per_1k
        + (cache_write_tok / 1000.0) * rate.cache_write_per_1k
    )
    return round(cost, 8)


def extract_usage_from_otel_span(span_attrs: dict | None) -> dict:
    """Pull token usage + model from a GenAI-semconv span's attributes.

    Reads ordinary input, output, cache-read, and cache-write token counts plus
    ``gen_ai.request.model`` (falling back to ``gen_ai.response.model``).
    Tolerates int- and str-typed attribute values. Missing/garbage counts
    degrade gracefully to zero and a missing model to ``None``.
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
    cache_read_input_tokens = _to_int(attrs.get("gen_ai.usage.cache_read_input_tokens"))
    cache_write_input_tokens = _to_int(attrs.get("gen_ai.usage.cache_write_input_tokens"))
    model_id = attrs.get("gen_ai.request.model") or attrs.get("gen_ai.response.model")
    if model_id is not None:
        model_id = str(model_id)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
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
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    version_id: str | None = None
    ttl: int | None = None

    def to_item(self) -> dict:
        item: dict = {
            "runtime_id": self.runtime_id,
            "event_id": self.event_id,
            "owner_sub": self.owner_sub,
            "model_id": self.model_id,
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "cache_read_input_tokens": int(self.cache_read_input_tokens),
            "cache_write_input_tokens": int(self.cache_write_input_tokens),
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
    def from_item(cls, item: dict) -> UsageEvent:
        item = _decimals_to_floats(dict(item))
        return cls(
            runtime_id=item["runtime_id"],
            event_id=item["event_id"],
            owner_sub=item.get("owner_sub", ""),
            model_id=item.get("model_id", ""),
            input_tokens=int(item.get("input_tokens", 0)),
            output_tokens=int(item.get("output_tokens", 0)),
            cache_read_input_tokens=int(item.get("cache_read_input_tokens", 0)),
            cache_write_input_tokens=int(item.get("cache_write_input_tokens", 0)),
            cost_usd=float(item.get("cost_usd", 0.0)),
            ts=item.get("ts", ""),
            version_id=item.get("version_id"),
            ttl=int(item["ttl"]) if item.get("ttl") is not None else None,
        )


def summarize(events: list[UsageEvent]) -> dict:
    """Aggregate a list of UsageEvents into a cost/token rollup.

    Returns ordinary input, cache-read, cache-write, total input, and output
    counts alongside the cost. ``total_in`` remains the ordinary-input field
    for backward compatibility; ``total_input_tokens`` is the complete input
    volume across all three billed input categories.
    """
    total_cost = 0.0
    total_in = 0
    total_out = 0
    total_cache_read = 0
    total_cache_write = 0
    by_model: dict[str, dict] = {}
    for ev in events:
        total_cost += float(ev.cost_usd)
        total_in += int(ev.input_tokens)
        total_out += int(ev.output_tokens)
        total_cache_read += int(ev.cache_read_input_tokens)
        total_cache_write += int(ev.cache_write_input_tokens)
        bucket = by_model.setdefault(
            ev.model_id or "unknown",
            {
                "cost": 0.0,
                "input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "total_input_tokens": 0,
                "output_tokens": 0,
                "count": 0,
            },
        )
        bucket["cost"] += float(ev.cost_usd)
        bucket["input_tokens"] += int(ev.input_tokens)
        bucket["cache_read_input_tokens"] += int(ev.cache_read_input_tokens)
        bucket["cache_write_input_tokens"] += int(ev.cache_write_input_tokens)
        bucket["total_input_tokens"] += (
            int(ev.input_tokens) + int(ev.cache_read_input_tokens) + int(ev.cache_write_input_tokens)
        )
        bucket["output_tokens"] += int(ev.output_tokens)
        bucket["count"] += 1
    for bucket in by_model.values():
        bucket["cost"] = round(bucket["cost"], 8)
    return {
        "total_cost": round(total_cost, 8),
        "total_in": total_in,
        "total_cache_read": total_cache_read,
        "total_cache_write": total_cache_write,
        "total_input_tokens": total_in + total_cache_read + total_cache_write,
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

    def get(self, runtime_id: str, event_id: str) -> UsageEvent | None:
        resp = self._table.get_item(Key={"runtime_id": runtime_id, "event_id": event_id})
        item = resp.get("Item")
        return UsageEvent.from_item(item) if item else None

    def query_for_runtime(
        self,
        runtime_id: str,
        from_ts: int | None = None,
        to_ts: int | None = None,
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


class RuntimeLogQueryError(RuntimeError):
    """A runtime log query could not produce a complete, trustworthy result."""


def is_logs_resource_not_found(exc: Exception, logs_client) -> bool:
    """Match typed CloudWatch Logs not-found errors without message substrings."""

    if is_error(exc, "ResourceNotFound", "ResourceNotFoundException"):
        return True
    exception_type = getattr(getattr(logs_client, "exceptions", None), "ResourceNotFoundException", None)
    return (
        isinstance(exception_type, type)
        and issubclass(exception_type, BaseException)
        and isinstance(exc, exception_type)
    )


def log_group_for_runtime(runtime_id: str) -> str:
    """Return the legacy/default runtime log group for *runtime_id*.

    AgentCore names application-log groups after the invoked endpoint
    qualifier. ``DEFAULT`` is the platform's ordinary live-deploy qualifier,
    but exported stacks can create a named endpoint. Call
    :func:`log_groups_for_runtime` before querying; this helper remains for
    backward-compatible response fields and callers that explicitly need the
    default name.
    """
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"


def log_group_prefix_for_runtime(runtime_id: str) -> str:
    """Return the prefix shared by every endpoint-qualified runtime group."""
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-"


def log_groups_for_runtime(logs_client, runtime_id: str) -> list[str]:
    """Enumerate all CloudWatch groups belonging to one AgentCore runtime.

    Runtime application records follow the endpoint qualifier. A query pinned
    to ``-DEFAULT`` therefore misses invocations made through a named endpoint,
    which is the path the CloudFormation export creates. Pagination and exact
    prefix filtering keep the result complete without sweeping unrelated
    runtimes in the account.
    """
    prefix = log_group_prefix_for_runtime(runtime_id)
    names: set[str] = set()
    kwargs: dict = {"logGroupNamePrefix": prefix, "limit": 50}
    seen_tokens: set[str] = set()
    while True:
        response = logs_client.describe_log_groups(**kwargs)
        for group in response.get("logGroups", []):
            name = group.get("logGroupName")
            if isinstance(name, str) and name.startswith(prefix):
                names.add(name)
        token = response.get("nextToken")
        if not token or token in seen_tokens:
            break
        seen_tokens.add(token)
        kwargs["nextToken"] = token

    default = log_group_for_runtime(runtime_id)
    return sorted(names, key=lambda name: (name != default, name))


def query_runtime_log_groups(
    logs_client,
    runtime_id: str,
    from_ts: int,
    to_ts: int,
    query_string: str,
    *,
    poll_seconds: float,
) -> tuple[list[dict], str, list[str]]:
    """Run one Insights query over every endpoint group for *runtime_id*.

    CloudWatch accepts at most 50 log groups per StartQuery request. Start all
    chunks first, then poll them under one shared deadline so a runtime with
    many historical endpoints does not multiply the API's response latency.
    """
    log_groups = log_groups_for_runtime(logs_client, runtime_id)
    if not log_groups:
        return [], "Empty", []

    query_ids: list[str] = []
    start_failures = 0
    for offset in range(0, len(log_groups), 50):
        try:
            response = logs_client.start_query(
                logGroupNames=log_groups[offset : offset + 50],
                startTime=int(from_ts),
                endTime=int(to_ts),
                queryString=query_string,
            )
        except Exception as exc:  # noqa: BLE001 — preserve other chunks and report a failed query
            start_failures += 1
            logger.warning(
                "Could not start runtime log query chunk (%s)",
                type(exc).__name__,
            )
            continue
        query_id = response.get("queryId")
        if query_id:
            query_ids.append(query_id)
        else:
            start_failures += 1
    if not query_ids:
        return [], "Failed" if start_failures else "Empty", log_groups

    deadline = time.time() + poll_seconds
    rows: list[dict] = []
    statuses = {query_id: "Running" for query_id in query_ids}
    while time.time() < deadline and any(status == "Running" for status in statuses.values()):
        for query_id, current in list(statuses.items()):
            if current != "Running":
                continue
            try:
                response = logs_client.get_query_results(queryId=query_id)
            except Exception as exc:  # noqa: BLE001 — one failed chunk must not discard completed rows
                statuses[query_id] = "Failed"
                logger.warning(
                    "Could not poll runtime log query %s (%s)",
                    query_id,
                    type(exc).__name__,
                )
                continue
            status = response.get("status", "Running")
            statuses[query_id] = status
            if status in ("Complete", "Failed", "Cancelled"):
                for row in response.get("results", []):
                    rows.append({field["field"]: field["value"] for field in row})
        if any(status == "Running" for status in statuses.values()):
            time.sleep(0.5)

    for query_id, status in statuses.items():
        if status != "Running":
            continue
        try:
            logs_client.stop_query(queryId=query_id)
        except Exception:  # noqa: BLE001 — best-effort cancel; partial rows remain useful
            logger.debug("stop_query %s failed", query_id, exc_info=True)

    values = set(statuses.values())
    if start_failures or "Failed" in values:
        overall = "Failed"
    elif "Cancelled" in values:
        overall = "Cancelled"
    elif "Running" in values:
        overall = "Running"
    else:
        overall = "Complete"
    return rows, overall, log_groups


def summarize_from_logs(
    runtime_id: str,
    from_ts: int,
    to_ts: int,
    region: str,
    *,
    logs_client=None,
    poll_seconds: float = 10.0,
) -> dict:
    """Compute a cost/token rollup from the runtime's CloudWatch Logs.

    Runs a Logs Insights query over every endpoint-qualified runtime group and
    parses ordinary input, output, cache-read, and cache-write counts plus
    ``gen_ai.request.model`` from each log message, grouped by model, then
    applies ``compute_cost`` per model.

    Returns ``{total_cost, total_in, total_out, by_model, from_ts, to_ts,
    log_group_name, log_group_names, query_status}``. When no group exists yet
    (runtime hasn't received instrumented traffic), returns an EMPTY summary.
    Permission errors, failed query chunks, cancellation, and timeouts raise
    :class:`RuntimeLogQueryError` so callers cannot present an undercount as a
    measured zero.
    """
    log_group = log_group_for_runtime(runtime_id)
    empty = {
        "total_cost": 0.0,
        "total_in": 0,
        "total_cache_read": 0,
        "total_cache_write": 0,
        "total_input_tokens": 0,
        "total_out": 0,
        "by_model": {},
        "from_ts": from_ts,
        "to_ts": to_ts,
        "log_group_name": log_group,
        "log_group_names": [],
        "cache_reporting": {
            "cache_read_complete": True,
            "cache_write_complete": True,
            "invocations": 0,
            "cache_read_reports": 0,
            "cache_write_reports": 0,
        },
        "query_status": "Empty",
    }

    # Runtime-name API routes resolve the deployment's target session before
    # calling this helper.  Accept that session's Logs client so a
    # cross-account runtime cannot silently fall back to the platform account.
    logs_client = logs_client or boto3.client("logs", region_name=region)

    # Group token sums by the request model so we can price each model
    # separately. Restrict to the explicit marker this bootstrap owns: a future
    # AgentCore native span containing the same semantic-convention fields must
    # not be counted a second time beside our usage record.
    query_string = (
        "fields @timestamp, @message"
        "\n| filter @message like /AGENTCORE_USAGE/"
        '\n| parse @message /"gen_ai.usage.input_tokens":\\s*(?<in_tok>\\d+)/'
        '\n| parse @message /"gen_ai.usage.output_tokens":\\s*(?<out_tok>\\d+)/'
        '\n| parse @message /"gen_ai.usage.cache_read_input_tokens":\\s*(?<cache_read_tok>\\d+)/'
        '\n| parse @message /"gen_ai.usage.cache_write_input_tokens":\\s*(?<cache_write_tok>\\d+)/'
        '\n| parse @message /"gen_ai.request.model":\\s*"?(?<model>[^",}\\s]+)"?/'
        "\n| stats sum(in_tok) as input_tokens, sum(out_tok) as output_tokens, "
        "sum(cache_read_tok) as cache_read_input_tokens, "
        "sum(cache_write_tok) as cache_write_input_tokens, "
        "count(cache_read_tok) as cache_read_reports, "
        "count(cache_write_tok) as cache_write_reports, "
        "count(*) as invocations by model"
        "\n| sort by input_tokens desc"
        "\n| limit 100"
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
            # Runtime groups not created yet — no instrumented traffic.
            return empty
        logger.warning(
            "summarize_from_logs failed: %s%s",
            type(exc).__name__,
            f" {error_code(exc)}" if error_code(exc) else "",
        )
        raise

    if status not in {"Complete", "Empty"}:
        raise RuntimeLogQueryError(f"runtime cost query ended with status {status}")

    if not log_groups:
        return empty

    total_cost = 0.0
    total_in = 0
    total_out = 0
    total_cache_read = 0
    total_cache_write = 0
    total_invocations = 0
    total_cache_read_reports = 0
    total_cache_write_reports = 0
    by_model: dict[str, dict] = {}
    for row in rows:
        model_id = row.get("model") or "unknown"
        try:
            in_tok = int(float(row.get("input_tokens", 0) or 0))
            out_tok = int(float(row.get("output_tokens", 0) or 0))
            cache_read_tok = int(float(row.get("cache_read_input_tokens", 0) or 0))
            cache_write_tok = int(float(row.get("cache_write_input_tokens", 0) or 0))
            invocations = int(float(row.get("invocations", 0) or 0))
            cache_read_reports = int(float(row.get("cache_read_reports", 0) or 0))
            cache_write_reports = int(float(row.get("cache_write_reports", 0) or 0))
        except (TypeError, ValueError):
            continue
        cost = compute_cost(
            model_id,
            in_tok,
            out_tok,
            cache_read_input_tokens=cache_read_tok,
            cache_write_input_tokens=cache_write_tok,
        )
        total_cost += cost
        total_in += in_tok
        total_out += out_tok
        total_cache_read += cache_read_tok
        total_cache_write += cache_write_tok
        total_invocations += invocations
        total_cache_read_reports += cache_read_reports
        total_cache_write_reports += cache_write_reports
        bucket = by_model.setdefault(
            model_id,
            {
                "cost": 0.0,
                "input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "total_input_tokens": 0,
                "output_tokens": 0,
                "count": 0,
                "cache_read_reports": 0,
                "cache_write_reports": 0,
            },
        )
        bucket["cost"] = round(bucket["cost"] + cost, 8)
        bucket["input_tokens"] += in_tok
        bucket["cache_read_input_tokens"] += cache_read_tok
        bucket["cache_write_input_tokens"] += cache_write_tok
        bucket["total_input_tokens"] += in_tok + cache_read_tok + cache_write_tok
        bucket["output_tokens"] += out_tok
        bucket["count"] += invocations
        bucket["cache_read_reports"] += cache_read_reports
        bucket["cache_write_reports"] += cache_write_reports

    for bucket in by_model.values():
        bucket["cache_read_complete"] = bucket["cache_read_reports"] == bucket["count"]
        bucket["cache_write_complete"] = bucket["cache_write_reports"] == bucket["count"]

    return {
        "total_cost": round(total_cost, 8),
        "total_in": total_in,
        "total_cache_read": total_cache_read,
        "total_cache_write": total_cache_write,
        "total_input_tokens": total_in + total_cache_read + total_cache_write,
        "total_out": total_out,
        "by_model": by_model,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "log_group_name": log_group,
        "log_group_names": log_groups,
        "cache_reporting": {
            "cache_read_complete": total_cache_read_reports == total_invocations,
            "cache_write_complete": total_cache_write_reports == total_invocations,
            "invocations": total_invocations,
            "cache_read_reports": total_cache_read_reports,
            "cache_write_reports": total_cache_write_reports,
        },
        "query_status": status,
    }


# ---------------------------------------------------------------------------
# Lazy singleton from env.
# ---------------------------------------------------------------------------

_usage_events_store: UsageEventsStore | None = None


def get_usage_events_store() -> UsageEventsStore:
    global _usage_events_store
    if _usage_events_store is None:
        _usage_events_store = UsageEventsStore(
            table_name=os.environ.get("USAGE_EVENTS_TABLE_NAME", "UsageEvents"),
            region=os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1")),
        )
    return _usage_events_store
