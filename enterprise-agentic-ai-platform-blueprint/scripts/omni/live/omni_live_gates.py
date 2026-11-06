#!/usr/bin/env python3
"""
omni_live_gates.py -- the live gates for the CloudWatch Omni hub model, run
against real accounts after `cell_validation_app.ts` is deployed and
`run_validation_agent.sh` has produced telemetry (ADR-15, ADR-17).

Gates (each is a real API round-trip; nothing is mocked):

  G1 cell-telemetry        spans for the validation service exist in the cell's
                           `aws/spans`; turn events exist in the agent log group
  G2 hub-centralized-logs  the destination log groups the rule created exist in
                           the hub, hold copied events, are encrypted under the
                           hub CMK, and carry the propagated allocation tags
  G3 rule-health           GetCentralizationRuleForOrganization reports Healthy
  G4 hub-dataset-query     an Omni telemetry query over the hub Dataset returns
                           rows whose `@aws.account` is a SOURCE account (the
                           hub's own account is excluded), proving the fleet
                           view spans accounts
  G5 option-b-results      the AgentCore online evaluation wrote results for the
                           policy evaluator (cell result log group), and the
                           result group is itself centralized into the hub
  G6 platform-source       a second source account's blueprint-prefixed log
                           group is copied too (the `AccountId IN (...)` scope
                           covers both members)
  G7 isolation             a member account can neither read the organization
                           rule nor the hub's centralized copies (AccessDenied
                           is the evidence, with its exact error code)

Evidence is JSON without account ids (last four digits only), prompts, model
text or ARNs of principals. Exit 0 when every gate passes, 2 otherwise; a gate
that cannot be evaluated fails closed.

    python3 scripts/omni/live/omni_live_gates.py --region eu-west-1 \
        --hub-profile <hub> --cell-profile <cell> --platform-profile <platform> \
        --cell-account <12 digits> --platform-account <12 digits> \
        --hub-key-arn <kms arn> --online-config-id <id> --out /tmp/omni-gates.json

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

TERMINAL = ("Complete", "COMPLETE", "Failed", "FAILED", "Cancelled", "CANCELLED", "Timeout", "TIMEOUT")
# The blueprint's default destination pattern (OmniCentralizationRuleConstruct). Note that a source
# group without a leading slash, such as `aws/spans`, lands as `/centralized/<acct>/<region>aws/spans`.
DESTINATION_PATTERN = "/centralized/${source.accountId}/${source.region}${source.logGroup}"


def destination_name(account: str, region: str, source_group: str, pattern: str = DESTINATION_PATTERN) -> str:
    return (pattern.replace("${source.accountId}", account).replace("${source.region}", region)
            .replace("${source.logGroup}", source_group))


def suffix(account: str) -> str:
    return f"...{account[-4:]}"


def client(profile: str | None, region: str, service: str):
    return boto3.session.Session(profile_name=profile, region_name=region).client(service)


# ------------------------------------------------------------------ wrappers


def log_group(logs, name: str) -> dict[str, Any] | None:
    groups = logs.describe_log_groups(logGroupNamePrefix=name).get("logGroups", [])
    return next((g for g in groups if g["logGroupName"] == name), None)


def count_events(logs, name: str, since_ms: int, pattern: str | None = None, limit: int = 200) -> int:
    kwargs: dict[str, Any] = {"logGroupName": name, "startTime": since_ms, "limit": limit}
    if pattern:
        kwargs["filterPattern"] = pattern
    try:
        return len(logs.filter_log_events(**kwargs).get("events", []))
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return 0
        raise


def log_group_tags(logs, arn: str) -> dict[str, str]:
    return logs.list_tags_for_resource(resourceArn=arn.rstrip("*").rstrip(":")).get("tags", {})


def rule_status(obs, rule: str) -> dict[str, Any]:
    r = obs.get_centralization_rule_for_organization(RuleIdentifier=rule)
    return {k: r.get(k) for k in ("RuleHealth", "FailureReason", "TagPropagationStatus", "TagPropagationFailureReason")}


def omni_query(omni, sql: str, session_name: str, timeout_s: int = 180) -> dict[str, Any]:
    sessions = omni.list_telemetry_query_sessions().get("sessions", [])
    sid = next((s["sessionId"] for s in sessions if s.get("sessionName") == session_name), None)
    if sid is None:
        sid = omni.start_telemetry_query_session(sessionName=session_name)["sessionId"]
    qid = omni.start_telemetry_query(queryString=sql, sessionId=sid)["queryId"]
    deadline = time.time() + timeout_s
    while True:
        r = omni.get_telemetry_query_results(queryId=qid, maxResults=100)
        if r["status"] in TERMINAL or time.time() > deadline:
            return {"status": r["status"], "rows": r.get("rows", []), "statistics": r.get("statistics")}
        time.sleep(3)


def eval_results(logs, group: str, since_ms: int) -> list[dict[str, Any]]:
    """Parse OpenTelemetry GenAI evaluation events into {evaluator, label, value}; no content."""
    found: list[dict[str, Any]] = []
    try:
        events = logs.filter_log_events(logGroupName=group, startTime=since_ms, limit=200).get("events", [])
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return found
        raise
    for e in events:
        try:
            body = json.loads(e["message"])
        except json.JSONDecodeError:
            continue
        # Live shape (2026-11): one OTLP log record per evaluated trace, attributes carry
        # gen_ai.evaluation.name, session.id, the evaluator/config ARNs, the rating scale and either
        # a score (gen_ai.evaluation.score.*) or error.type (e.g. MISSING_OUTPUT_CONTENT when the
        # GenAI log events were not available for the trace).
        attrs = body.get("attributes") or {}
        found.append({
            "evaluator": attrs.get("gen_ai.evaluation.name"),
            "label": attrs.get("gen_ai.evaluation.score.label"),
            "value": attrs.get("gen_ai.evaluation.score.value"),
            "errorType": attrs.get("error.type"),
            "level": attrs.get("aws.bedrock_agentcore.evaluation_level"),
            "sessionSuffix": str(attrs.get("session.id", ""))[-6:],
        })
    return found


def denied(fn) -> dict[str, Any]:
    """Run a call expected to be denied; report the exact error code (the evidence) or an unexpected success."""
    try:
        fn()
        return {"denied": False, "code": None}
    except ClientError as exc:
        err = exc.response.get("Error", {})
        return {"denied": True, "code": err.get("Code"), "status": exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")}


# --------------------------------------------------------------------- gates


def run_gates(a: argparse.Namespace) -> dict[str, Any]:
    region = a.region
    since_ms = int((time.time() - a.since_minutes * 60) * 1000)
    hub_logs = client(a.hub_profile, region, "logs")
    hub_obs = client(a.hub_profile, region, "observabilityadmin")
    hub_omni = client(a.hub_profile, region, "cloudwatchomni")
    hub_sts = client(a.hub_profile, region, "sts")
    cell_logs = client(a.cell_profile, region, "logs")
    plat_logs = client(a.platform_profile, region, "logs") if a.platform_profile else None
    hub_account = hub_sts.get_caller_identity()["Account"]
    gates: list[dict[str, Any]] = []

    # G1
    spans = count_events(cell_logs, "aws/spans", since_ms, f'"{a.service_name}"')
    turns = count_events(cell_logs, a.agent_log_group, since_ms, '"agenticai.validation.turn"')
    gates.append({"id": "G1-cell-telemetry", "ok": spans > 0 and turns > 0,
                  "spansMatched": spans, "turnEventsMatched": turns, "account": suffix(a.cell_account)})

    # G2
    dest_spans = destination_name(a.cell_account, region, "aws/spans")
    dest_turns = destination_name(a.cell_account, region, a.agent_log_group)
    g2: dict[str, Any] = {"id": "G2-hub-centralized-logs", "groups": {}}
    ok = True
    for name, pattern, want_tags in ((dest_spans, f'"{a.service_name}"', False), (dest_turns, '"agenticai.validation.turn"', True)):
        g = log_group(hub_logs, name)
        info: dict[str, Any] = {"exists": g is not None}
        if g:
            info["events"] = count_events(hub_logs, name, since_ms, pattern)
            info["encryptedWithHubKey"] = g.get("kmsKeyId") == a.hub_key_arn
            tags = log_group_tags(hub_logs, g["arn"])
            info["propagatedTags"] = sorted(k for k in tags if k in ("application-id", "agent-id", "tenant-id", "cost-centre", "environment"))
            ok = ok and info["events"] > 0 and info["encryptedWithHubKey"] and (not want_tags or len(info["propagatedTags"]) == 5)
        else:
            ok = False
        g2["groups"][name.replace(a.cell_account, suffix(a.cell_account))] = info
    g2["ok"] = ok
    gates.append(g2)

    # G3
    status = rule_status(hub_obs, a.rule)
    gates.append({"id": "G3-rule-health", "ok": status["RuleHealth"] == "Healthy", **status})

    # G4
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(minutes=a.since_minutes)
    rng = f"`@timestamp` BETWEEN to_timestamp('{start:%Y-%m-%dT%H:%M:%SZ}') AND to_timestamp('{end:%Y-%m-%dT%H:%M:%SZ}')"
    sql = (f"SELECT `@aws.account` AS account, `@logGroupName` AS lg, COUNT(*) AS n FROM default "
           f"WHERE {rng} AND `@logGroupName` LIKE '/centralized/%' GROUP BY 1, 2 ORDER BY n DESC LIMIT 50")
    q = omni_query(hub_omni, sql, "agenticai-live-gates")
    source_accounts = sorted({row.get("account") for row in q["rows"] if row.get("account") and row.get("account") != hub_account})
    gates.append({"id": "G4-hub-dataset-query", "ok": q["status"] in ("Complete", "COMPLETE") and a.cell_account in source_accounts,
                  "status": q["status"], "sourceAccountsSeen": [suffix(x) for x in source_accounts],
                  "rows": [{"account": suffix(r["account"]), "logGroup": r["lg"].replace(r["account"], suffix(r["account"])), "n": r["n"]}
                           for r in q["rows"] if r.get("account")][:12],
                  "recordsScanned": (q.get("statistics") or {}).get("recordsScanned")})

    # G5
    result_group = f"/aws/bedrock-agentcore/evaluations/results/{a.online_config_id}"
    results = eval_results(cell_logs, result_group, since_ms)
    hub_results = count_events(hub_logs, destination_name(a.cell_account, region, result_group), since_ms)
    scored = [r for r in results if r.get("errorType") is None and (r.get("label") is not None or r.get("value") is not None)]
    error_types: dict[str, int] = {}
    for r in results:
        if r.get("errorType"):
            error_types[r["errorType"]] = error_types.get(r["errorType"], 0) + 1
    labels = sorted({str(r["label"]) for r in scored if r.get("label") is not None})
    # A pass needs at least one SCORED trace (an error row proves the pipeline ran, not the evaluator).
    gates.append({"id": "G5-option-b-results", "ok": len(scored) > 0 and hub_results > 0,
                  "cellResultEvents": len(results), "scored": len(scored), "labels": labels[:10],
                  "errorTypes": error_types, "evaluators": sorted({str(r["evaluator"]) for r in results}),
                  "hubCentralizedResultEvents": hub_results})

    # G6
    if plat_logs is not None:
        stream = f"probe/{int(time.time())}"
        probe_group = a.platform_log_group
        try:
            plat_logs.create_log_group(logGroupName=probe_group, tags={"application-id": "omni-validation", "agent-id": "platform-probe",
                                                                        "tenant-id": "validation", "cost-centre": "platform", "environment": "validation"})
            plat_logs.put_retention_policy(logGroupName=probe_group, retentionInDays=1)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceAlreadyExistsException":
                raise
        plat_logs.create_log_stream(logGroupName=probe_group, logStreamName=stream)
        plat_logs.put_log_events(logGroupName=probe_group, logStreamName=stream, logEvents=[
            {"timestamp": int(time.time() * 1000), "message": json.dumps({"event": "agenticai.validation.platform-probe", "i": i})} for i in range(3)])
        dest = destination_name(a.platform_account, region, probe_group)
        copied = 0
        deadline = time.time() + a.platform_wait_seconds
        while copied == 0 and time.time() < deadline:
            time.sleep(15)
            copied = count_events(hub_logs, dest, since_ms, '"agenticai.validation.platform-probe"')
        gates.append({"id": "G6-platform-source", "ok": copied > 0, "account": suffix(a.platform_account),
                      "copiedEvents": copied, "waitedSeconds": a.platform_wait_seconds if copied == 0 else None})

    # G7 (adversarial, from the platform account: a plain member)
    if a.platform_profile:
        plat_obs = client(a.platform_profile, region, "observabilityadmin")
        rule_read = denied(lambda: plat_obs.get_centralization_rule_for_organization(RuleIdentifier=a.rule))
        hub_group_arn = f"arn:aws:logs:{region}:{hub_account}:log-group:{dest_spans}"
        copy_read = denied(lambda: plat_logs.filter_log_events(logGroupIdentifier=hub_group_arn, limit=1))
        gates.append({"id": "G7-isolation", "ok": rule_read["denied"] and copy_read["denied"],
                      "memberReadsOrganizationRule": rule_read, "memberReadsHubCopy": copy_read})

    return {"ranAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "region": region,
            "hub": suffix(hub_account), "sinceMinutes": a.since_minutes, "gates": gates, "ok": all(g["ok"] for g in gates)}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", required=True)
    p.add_argument("--hub-profile")
    p.add_argument("--cell-profile")
    p.add_argument("--platform-profile", help="Second source account (a plain member) for G6 and G7; omit to skip them.")
    p.add_argument("--cell-account", required=True)
    p.add_argument("--platform-account")
    p.add_argument("--rule", default="agenticai-fleet-eu-west-1")
    p.add_argument("--service-name", default="omni-validation-agent")
    p.add_argument("--agent-log-group", default="/agenticai/omni-validation/omni-validation-agent")
    p.add_argument("--platform-log-group", default="/agenticai/omni-validation/platform-probe")
    p.add_argument("--platform-wait-seconds", type=int, default=300)
    p.add_argument("--hub-key-arn", required=True)
    p.add_argument("--online-config-id", required=True)
    p.add_argument("--since-minutes", type=int, default=120)
    p.add_argument("--out")
    args = p.parse_args(argv)
    if args.platform_profile and not args.platform_account:
        p.error("--platform-account is required with --platform-profile")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    evidence = run_gates(args)
    text = json.dumps(evidence, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0 if evidence["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
