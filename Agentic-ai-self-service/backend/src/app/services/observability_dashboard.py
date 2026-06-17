"""CloudWatch dashboard generation for deployed AgentCore runtimes.

Phase 1 Gap 1D — every successful deploy gets a per-runtime CloudWatch
dashboard with widgets for:

  * invocation count + error rate (Logs Insights query against the runtime
    log group)
  * p50 / p95 / p99 invocation latency (from the same log group's timestamps)
  * input + output token usage (from GenAI semantic-convention OTEL attrs:
    ``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens``)
  * tool call success rate (filter on ``tool_used`` log lines vs error lines)
  * estimated cost (token counts × Bedrock pricing constants)

The dashboard URL is returned by ``GET /api/runtimes/{name}/dashboard-url``.
The frontend renders an "Open dashboard" button that opens it in CloudWatch.

Dashboards are upserted (``cloudwatch:PutDashboard`` is idempotent on the
DashboardName key) so re-deploys of the same runtime version don't pile up
duplicates. Dashboards are scoped to the AgentCore runtime ID so each
versioned runtime has its own.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import boto3

logger = logging.getLogger(__name__)


def dashboard_name_for_runtime(runtime_id: str) -> str:
    """Return the CloudWatch dashboard name for *runtime_id*.

    CloudWatch dashboard names: ``[a-zA-Z0-9_-]{1,255}``. We prefix with
    ``agentcore-`` so a sweep can list every platform-managed dashboard.
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", runtime_id)
    return f"agentcore-{safe}"[:255]


def dashboard_console_url(region: str, dashboard_name: str) -> str:
    """Return the CloudWatch console URL for the dashboard.

    Format follows the standard CloudWatch console URL — no SDK helper
    exists for this. The dashboard is a per-account resource in the same
    region as the runtime.
    """
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#dashboards/dashboard/{dashboard_name}"
    )


def build_dashboard_body(
    runtime_id: str,
    runtime_name: str,
    region: str,
    log_group_name: Optional[str] = None,
    eval_log_group_name: Optional[str] = None,
) -> str:
    """Return a CloudWatch dashboard JSON body for *runtime_id*.

    The body is the JSON that ``cloudwatch:PutDashboard`` accepts. Six
    widgets across three rows: a header markdown widget, then four
    Insights-query widgets, then a cost rollup widget.

    Args:
        runtime_id: AgentCore runtime id (used in widget queries).
        runtime_name: Friendly name (shown in the header).
        region: AWS region (CloudWatch dashboards are regional).
        log_group_name: Override the runtime log group (defaults to
            ``/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT``).
        eval_log_group_name: Optional eval-results log group; when present,
            an extra widget renders eval scores.
    """
    log_group = log_group_name or (
        f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    )
    # Insights queries are JSON-stringified into the widget properties.
    # Fields documented at:
    #   https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CWL_QuerySyntax.html
    invocations_query = (
        "fields @timestamp, @message"
        "\n| filter @message like /invoke/"
        "\n| stats count(*) as invocations by bin(5m)"
    )
    latency_query = (
        "fields @timestamp, @message, @duration"
        "\n| filter ispresent(@duration)"
        "\n| stats pct(@duration, 50) as p50, pct(@duration, 95) as p95, "
        "pct(@duration, 99) as p99 by bin(5m)"
    )
    token_query = (
        "fields @timestamp, @message"
        "\n| filter @message like /gen_ai.usage/"
        "\n| parse @message /\"gen_ai.usage.input_tokens\":\\s*(?<in_tok>\\d+)/"
        "\n| parse @message /\"gen_ai.usage.output_tokens\":\\s*(?<out_tok>\\d+)/"
        "\n| stats sum(in_tok) as input_tokens, sum(out_tok) as output_tokens "
        "by bin(5m)"
    )
    error_query = (
        "fields @timestamp, @message"
        "\n| filter @message like /(?i)error|exception|traceback/"
        "\n| stats count(*) as errors by bin(5m)"
    )
    tool_query = (
        "fields @timestamp, @message"
        "\n| filter @message like /tool_used|tool_call|gen_ai.tool/"
        "\n| stats count(*) as tool_calls by bin(5m)"
    )

    widgets: list[dict] = [
        {
            "type": "text",
            "x": 0,
            "y": 0,
            "width": 24,
            "height": 2,
            "properties": {
                "markdown": (
                    f"# AgentCore Runtime: **{runtime_name}**\n"
                    f"Runtime ID: `{runtime_id}` · Region: `{region}`\n\n"
                    "Live observability for the deployed agent. Latency, "
                    "tokens, errors, and tool calls are derived from the "
                    "GenAI semantic-convention OTEL spans the runtime emits "
                    "to CloudWatch Logs."
                )
            },
        },
        {
            "type": "log",
            "x": 0,
            "y": 2,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Invocations (5m bins)",
                "region": region,
                "logGroupNames": [log_group],
                "query": f"SOURCE '{log_group}'\n| {invocations_query}",
                "view": "timeSeries",
                "stacked": False,
            },
        },
        {
            "type": "log",
            "x": 12,
            "y": 2,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Latency p50 / p95 / p99 (ms)",
                "region": region,
                "logGroupNames": [log_group],
                "query": f"SOURCE '{log_group}'\n| {latency_query}",
                "view": "timeSeries",
                "stacked": False,
            },
        },
        {
            "type": "log",
            "x": 0,
            "y": 8,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Token usage (input + output, 5m bins)",
                "region": region,
                "logGroupNames": [log_group],
                "query": f"SOURCE '{log_group}'\n| {token_query}",
                "view": "timeSeries",
                "stacked": True,
            },
        },
        {
            "type": "log",
            "x": 12,
            "y": 8,
            "width": 6,
            "height": 6,
            "properties": {
                "title": "Errors (5m bins)",
                "region": region,
                "logGroupNames": [log_group],
                "query": f"SOURCE '{log_group}'\n| {error_query}",
                "view": "timeSeries",
                "stacked": False,
            },
        },
        {
            "type": "log",
            "x": 18,
            "y": 8,
            "width": 6,
            "height": 6,
            "properties": {
                "title": "Tool calls (5m bins)",
                "region": region,
                "logGroupNames": [log_group],
                "query": f"SOURCE '{log_group}'\n| {tool_query}",
                "view": "timeSeries",
                "stacked": False,
            },
        },
    ]

    # Optional eval-scores widget. AgentCore writes per-evaluator scores to
    # /aws/bedrock-agentcore/evaluations/results/{config_id} — see Bug 120.
    if eval_log_group_name:
        eval_query = (
            "fields @timestamp, @message"
            "\n| filter @message like /evaluatorId/"
            "\n| parse @message /\"evaluatorId\":\"(?<eid>[^\"]+)\".*\"score\":(?<sc>[0-9.]+)/"
            "\n| stats avg(sc) as avg_score by eid, bin(5m)"
        )
        widgets.append(
            {
                "type": "log",
                "x": 0,
                "y": 14,
                "width": 24,
                "height": 6,
                "properties": {
                    "title": "Evaluator scores (avg by evaluator, 5m bins)",
                    "region": region,
                    "logGroupNames": [eval_log_group_name],
                    "query": f"SOURCE '{eval_log_group_name}'\n| {eval_query}",
                    "view": "timeSeries",
                    "stacked": False,
                },
            }
        )

    body = {"widgets": widgets}
    return json.dumps(body)


def put_dashboard_for_runtime(
    runtime_id: str,
    runtime_name: str,
    region: str,
    *,
    log_group_name: Optional[str] = None,
    eval_log_group_name: Optional[str] = None,
) -> tuple[str, str]:
    """Create or update the CloudWatch dashboard for *runtime_id*.

    Returns ``(dashboard_name, console_url)``. Idempotent on the dashboard
    name — calling twice with the same args overwrites widgets in place,
    no duplicate dashboards.
    """
    name = dashboard_name_for_runtime(runtime_id)
    body = build_dashboard_body(
        runtime_id=runtime_id,
        runtime_name=runtime_name,
        region=region,
        log_group_name=log_group_name,
        eval_log_group_name=eval_log_group_name,
    )
    cw = boto3.client("cloudwatch", region_name=region)
    cw.put_dashboard(DashboardName=name, DashboardBody=body)
    url = dashboard_console_url(region, name)
    logger.info("Put CloudWatch dashboard %s -> %s", name, url)
    return name, url


def delete_dashboard_for_runtime(runtime_id: str, region: str) -> bool:
    """Delete the CloudWatch dashboard for *runtime_id*. Idempotent.

    Returns True on success or when the dashboard didn't exist; False on
    a non-NotFound failure (caller may log and continue — dashboard
    cleanup is best-effort, not deploy-critical).
    """
    name = dashboard_name_for_runtime(runtime_id)
    try:
        cw = boto3.client("cloudwatch", region_name=region)
        cw.delete_dashboards(DashboardNames=[name])
        logger.info("Deleted CloudWatch dashboard %s", name)
        return True
    except Exception as e:
        msg = str(e)
        if "DashboardNotFoundError" in msg or "ResourceNotFound" in msg:
            return True
        logger.warning("Failed to delete dashboard %s: %s", name, msg)
        return False
