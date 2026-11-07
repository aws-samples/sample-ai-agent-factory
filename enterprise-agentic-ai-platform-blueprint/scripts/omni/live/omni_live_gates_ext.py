"""
omni_live_gates_ext.py -- the second set of live gates (G8-G11), run by
omni_live_gates.py when their flags are given. Each is a real API round-trip
with evidence that carries account suffixes only.

  G8  option-a-results     the online evaluation created in the HUB (option A,
                           CloudWatch Omni) scored a live session: scored records
                           per evaluator in the hub's result log group
  G9  metrics-centralized  the organization centralization rule copies metrics:
                           a metric only the cell emits (the policy evaluator's
                           Lambda) is listed in the hub account
  G10 cmk-fail-closed      CloudWatch Logs accepts the centralized-logs CMK for a
                           log group and REFUSES a key whose policy lacks the
                           Logs service principal (the space key), with the exact
                           error code; the hub key carries LogsManaged=true
  G11 live-fail-verdict    the policy code evaluator returns FAIL on live traces
                           when the output budget is tightened (Lambda env
                           override), and PASS again after the budget is restored

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import json
import time
from typing import Any

from botocore.exceptions import ClientError


def _eval_rows(logs, group: str, since_ms: int) -> list[dict[str, Any]]:
    try:
        events = logs.filter_log_events(logGroupName=group, startTime=since_ms, limit=300).get("events", [])
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return []
        raise
    rows = []
    for e in events:
        try:
            attrs = json.loads(e["message"]).get("attributes") or {}
        except json.JSONDecodeError:
            continue
        rows.append({
            "evaluator": attrs.get("gen_ai.evaluation.name"),
            "label": attrs.get("gen_ai.evaluation.score.label"),
            "value": attrs.get("gen_ai.evaluation.score.value"),
            "errorType": attrs.get("error.type"),
            "sessionSuffix": str(attrs.get("session.id", ""))[-6:],
        })
    return rows


def gate_option_a(hub_logs, config_id: str, since_ms: int) -> dict[str, Any]:
    rows = _eval_rows(hub_logs, f"/aws/bedrock-agentcore/evaluations/results/{config_id}", since_ms)
    scored: dict[str, dict[str, int]] = {}
    errors: dict[str, int] = {}
    for r in rows:
        if r["errorType"]:
            errors[r["errorType"]] = errors.get(r["errorType"], 0) + 1
        elif r["label"] is not None or r["value"] is not None:
            scored.setdefault(str(r["evaluator"]), {})
            key = str(r["label"]) if r["label"] is not None else f"value={r['value']}"
            scored[str(r["evaluator"])][key] = scored[str(r["evaluator"])].get(key, 0) + 1
    return {"id": "G8-option-a-results", "ok": len(scored) > 0, "resultEvents": len(rows),
            "scoredByEvaluator": scored, "errorTypes": errors,
            "sessions": sorted({r["sessionSuffix"] for r in rows})[:10]}


def gate_metrics_centralized(hub_cw, function_name: str, source_account: str) -> dict[str, Any]:
    """A Lambda that exists only in the cell: its AWS/Lambda metrics in the hub prove metrics centralization.

    Observed live (2026-11): centralized copies are owned by the HUB account but are listed only with
    IncludeLinkedAccounts=True, and they carry the source as extra dimensions `:@aws.account` and
    `:@aws.region` (OwningAccount=<source> returns nothing: that filter is for OAM links, not copies)."""
    found = hub_cw.list_metrics(Namespace="AWS/Lambda", Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                                IncludeLinkedAccounts=True).get("Metrics", [])
    from_source = [m for m in found if any(d.get("Name") == ":@aws.account" and d.get("Value") == source_account for d in m.get("Dimensions", []))]
    sample = (from_source or found or [None])[0]
    return {"id": "G9-metrics-centralized", "ok": len(from_source) > 0, "metricsInHub": len(found),
            "metricsTaggedWithSourceAccount": len(from_source),
            "metricNames": sorted({m["MetricName"] for m in from_source})[:10],
            "sampleDimensions": sample.get("Dimensions") if sample else None}


def _denied(fn) -> dict[str, Any]:
    try:
        fn()
        return {"denied": False, "code": None}
    except ClientError as exc:
        return {"denied": True, "code": exc.response.get("Error", {}).get("Code"),
                "message": exc.response.get("Error", {}).get("Message", "")[:160]}


def gate_cmk_fail_closed(hub_logs, hub_kms, logs_key_arn: str, space_key_arn: str, group: str) -> dict[str, Any]:
    """Positive: the centralized-logs key associates with a hub group. Negative twin: the space key
    (no Logs principal in its policy) is refused. Nothing is left behind: the group is deleted."""
    try:
        hub_logs.create_log_group(logGroupName=group)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    try:
        refused = _denied(lambda: hub_logs.associate_kms_key(logGroupName=group, kmsKeyId=space_key_arn))
        accepted = _denied(lambda: hub_logs.associate_kms_key(logGroupName=group, kmsKeyId=logs_key_arn))
        tags = hub_kms.list_resource_tags(KeyId=logs_key_arn).get("Tags", [])
        logs_managed = any(t.get("TagKey") == "LogsManaged" and t.get("TagValue") == "true" for t in tags)
        rotation = hub_kms.get_key_rotation_status(KeyId=logs_key_arn).get("KeyRotationEnabled")
    finally:
        try:
            hub_logs.delete_log_group(logGroupName=group)
        except ClientError:
            pass
    return {"id": "G10-cmk-fail-closed", "ok": refused["denied"] and not accepted["denied"] and logs_managed,
            "spaceKeyRefused": refused, "centralizedLogsKeyAccepted": not accepted["denied"],
            "logsManagedTag": logs_managed, "rotationEnabled": rotation}


def _session_spans(cell_logs, session_id: str, groups: list[str], since_s: int) -> list[dict[str, Any]]:
    """The query the AgentCore SDK collector runs, over aws/spans and the agent log group."""
    query = (f'fields @message | filter attributes.session.id = "{session_id}" | filter ispresent(scope.name)'
             " | filter ispresent(traceId) | filter ispresent(spanId) | sort @timestamp asc | limit 2000")
    qid = cell_logs.start_query(logGroupNames=groups, startTime=since_s, endTime=int(time.time()) + 60, queryString=query)["queryId"]
    for _ in range(60):
        r = cell_logs.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(2)
    spans = []
    for row in r.get("results", []):
        for f in row:
            if f["field"] == "@message":
                spans.append(json.loads(f["value"]))
    return spans


def gate_live_fail_verdict(cell_lambda, cell_logs, cell_eval, function_name: str, evaluator_id: str,
                           session_id: str, groups: list[str], since_s: int, tight_chars: str = "10") -> dict[str, Any]:
    """Tighten POLICY_MAX_OUTPUT_CHARS on the live evaluator, evaluate one trace on demand (FAIL expected),
    restore the budget, evaluate again (PASS expected)."""
    cfg = cell_lambda.get_function_configuration(FunctionName=function_name)
    env = dict((cfg.get("Environment") or {}).get("Variables") or {})
    original = env.get("POLICY_MAX_OUTPUT_CHARS")
    spans = _session_spans(cell_logs, session_id, groups, since_s)
    trace_ids = sorted({s.get("traceId") for s in spans if s.get("traceId") and str(s.get("name", "")).startswith("invoke_agent")})
    if not trace_ids:
        return {"id": "G11-live-fail-verdict", "ok": False, "reason": "no invoke_agent trace for the session", "spans": len(spans)}

    def set_budget(value: str | None) -> None:
        new_env = dict(env)
        if value is None:
            new_env.pop("POLICY_MAX_OUTPUT_CHARS", None)
        else:
            new_env["POLICY_MAX_OUTPUT_CHARS"] = value
        cell_lambda.update_function_configuration(FunctionName=function_name, Environment={"Variables": new_env})
        waiter = cell_lambda.get_waiter("function_updated_v2")
        waiter.wait(FunctionName=function_name)

    def evaluate() -> dict[str, Any]:
        r = cell_eval.evaluate(evaluatorId=evaluator_id, evaluationInput={"sessionSpans": spans},
                               evaluationTarget={"traceIds": [trace_ids[0]]})
        out = (r.get("evaluationResults") or [{}])[0]
        return {"label": out.get("label"), "value": out.get("value"), "errorCode": out.get("errorCode"),
                "explanationMentionsBudget": "bounded_output" in str(out.get("explanation", ""))}

    try:
        set_budget(tight_chars)
        tightened = evaluate()
    finally:
        set_budget(original)
    restored = evaluate()
    return {"id": "G11-live-fail-verdict",
            "ok": tightened["label"] == "FAIL" and tightened["explanationMentionsBudget"] and restored["label"] == "PASS",
            "traceSuffix": trace_ids[0][-8:], "spans": len(spans), "tightened": tightened, "restored": restored,
            "budgetRestoredTo": original}
