#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Live test for Module 5 (CloudWatch Omni observability and evaluation).
#
# Runs the EXACT bash blocks published on the Module 5 pages (extracted from
# content/module-5/step-*/index.en.md, so the tested commands are the ones
# participants copy), then executes the notebooks with nbconvert on the
# "workshop" kernel, then checks the account for the signals the module
# promises. Designed to run on the workshop IDE instance (Linux, the
# provisioned AWS CLI and kernel), for example through SSM:
#
#   aws ssm send-command --instance-ids <ide-instance-id> \
#     --document-name AWS-RunShellScript \
#     --parameters 'commands=["sudo -iu participant bash /workshop/scripts/module5-live-test.sh --repo /workshop"]'
#
# Prerequisites: Modules 2 to 4 completed in the account (FAST deployed with
# the Module 4 span patch), the Module 3b gateway test run once, and at least
# one FAST invocation. Evidence is written to $OUT (default
# /tmp/module5-live/evidence.json). Exit status is 0 only if every gate passed.
#
# Flags:
#   --repo DIR        repository root (default: the parent of this script's directory)
#   --skip-cli        do not run the page blocks
#   --skip-notebooks  do not execute the notebooks
#   --regression      also run step 6 / notebook 07 (the off-topic regression, no redeploy)
#   --cleanup         finish with step 7 / notebook 08 (default: keep the space for the dashboards)
#   --out FILE        evidence file
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_CLI=1; RUN_NB=1; RUN_REGRESSION=0; RUN_CLEANUP=0
OUT=/tmp/module5-live/evidence.json
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --skip-cli) RUN_CLI=0; shift ;;
    --skip-notebooks) RUN_NB=0; shift ;;
    --regression) RUN_REGRESSION=1; shift ;;
    --cleanup) RUN_CLEANUP=1; shift ;;
    --out) OUT="$2"; shift 2 ;;
    *) echo "unknown flag $1" >&2; exit 2 ;;
  esac
done
WORK="$(dirname "$OUT")"; mkdir -p "$WORK/cli" "$WORK/nb"
REGION="$(aws configure get region 2>/dev/null || echo "${AWS_REGION:-us-west-2}")"
export AWS_DEFAULT_REGION="$REGION"
PASS=0; FAIL=0; RESULTS=()

record() {  # record <gate> <ok|fail> <detail>
  local gate="$1" status="$2" detail="$3"
  RESULTS+=("{\"gate\": \"$gate\", \"status\": \"$status\", \"detail\": $(printf '%s' "$detail" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()[:800]))')}")
  if [ "$status" = "ok" ]; then PASS=$((PASS+1)); echo "PASS  $gate: $detail"; else FAIL=$((FAIL+1)); echo "FAIL  $gate: $detail" >&2; fi
}

# ---------------------------------------------------------------- preflight
echo "== Module 5 live test, region $REGION, repo $REPO"
aws sts get-caller-identity --query Arn --output text | sed 's/^/identity: /'
CLI_VERSION=$(aws --version 2>&1 | sed -n 's#^aws-cli/\([0-9.]*\).*#\1#p')
if aws cloudwatchomni list-domains --generate-cli-skeleton >/dev/null 2>&1; then
  record "preflight.cli" ok "aws-cli $CLI_VERSION has the cloudwatchomni command"
else
  record "preflight.cli" fail "aws-cli $CLI_VERSION lacks cloudwatchomni (need >= 2.39)"
fi
BOTO=$(python3 -c "import boto3; boto3.client('cloudwatchomni', region_name='$REGION'); print(boto3.__version__)" 2>&1 | tail -1)
case "$BOTO" in 1.4[3-9].*|1.[5-9]*.*|[2-9].*) record "preflight.boto3" ok "boto3 $BOTO builds the cloudwatchomni client" ;;
  *) record "preflight.boto3" fail "boto3 cannot build cloudwatchomni: $BOTO" ;; esac
DEST=$(aws xray get-trace-segment-destination --query 'Destination' --output text 2>&1)
[ "$DEST" = "CloudWatchLogs" ] && record "preflight.transaction_search" ok "trace destination CloudWatchLogs" || record "preflight.transaction_search" fail "trace destination is $DEST"
for key in OmniSpaceAccessRoleArn OmniDatasetIntegrationRoleArn AgentCoreEvaluationRoleArn; do
  v=$(aws cloudformation describe-stacks --stack-name workshop-agentcore-stack --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue" --output text 2>/dev/null)
  [ -n "$v" ] && [ "$v" != "None" ] && record "preflight.output.$key" ok "$v" || record "preflight.output.$key" fail "missing from workshop-agentcore-stack"
done
RUNTIME_GROUPS=$(aws logs describe-log-groups --log-group-name-prefix /aws/bedrock-agentcore/runtimes/FAST_stack_FASTAgent --query 'length(logGroups)' --output text 2>/dev/null || echo 0)
[ "${RUNTIME_GROUPS:-0}" -gt 0 ] && record "preflight.fast_deployed" ok "$RUNTIME_GROUPS FAST runtime log group(s)" || record "preflight.fast_deployed" fail "no FAST runtime log group; Part B needs Module 4"

# ---------------------------------------------------------------- CLI path
# Extract every ':::code{... language=bash}' block of a page into one script,
# preserving order, so variables flow between blocks exactly as they do for a
# participant working down the page in one terminal.
extract_bash() {  # extract_bash <page.md> <out.sh>
  python3 - "$1" "$2" <<'PYEOF'
import re, sys
text = open(sys.argv[1]).read()
blocks = re.findall(r'^:::code\{[^}]*language=bash[^}]*\}\n(.*?)\n:::\s*$', text, re.S | re.M)
with open(sys.argv[2], 'w') as out:
    out.write('#!/usr/bin/env bash\nset -o pipefail\n')
    for i, b in enumerate(blocks, 1):
        out.write(f'\necho "--- block {i} ---"\n{b}\n')
print(len(blocks))
PYEOF
}
run_page() {  # run_page <step-dir> <label>
  local page="$REPO/content/module-5/$1/index.en.md" script="$WORK/cli/$1.sh" log="$WORK/cli/$1.log" n
  n=$(extract_bash "$page" "$script")
  if bash "$script" >"$log" 2>&1; then
    record "cli.$1" ok "$n block(s) ran clean; log $log"
  else
    record "cli.$1" fail "$(grep -iE 'error|denied|exception|invalid|not found|fail' "$log" | head -3 | tr '\n' ' ' | cut -c1-600) (log $log)"
  fi
}
if [ "$RUN_CLI" = 1 ]; then
  run_page step-1 "domain, space, dataset integration"
  run_page step-2 "deep link"
  run_page step-3 "platform signals"
  if [ "${RUNTIME_GROUPS:-0}" -gt 0 ]; then
    run_page step-4 "agent traces"
    run_page step-5 "evaluate"
    [ "$RUN_REGRESSION" = 1 ] && run_page step-6 "regression"
  fi
fi

# ---------------------------------------------------------------- notebooks
if [ "$RUN_NB" = 1 ]; then
  if python3 -m jupyter nbconvert --version >/dev/null 2>&1; then
    NBS="01-architecture 02-create-space 03-sign-in 04-platform-signals"
    [ "${RUNTIME_GROUPS:-0}" -gt 0 ] && NBS="$NBS 05-agent-traces 06-evaluate"
    [ "$RUN_REGRESSION" = 1 ] && NBS="$NBS 07-regression"
    for nb in $NBS; do
      src="$REPO/source/module-5-omni/notebooks/$nb.ipynb"
      if python3 -m jupyter nbconvert --to notebook --execute --ExecutePreprocessor.kernel_name=workshop \
          --ExecutePreprocessor.timeout=1200 --output-dir "$WORK/nb" --output "$nb.executed.ipynb" "$src" >"$WORK/nb/$nb.log" 2>&1; then
        record "notebook.$nb" ok "executed; $WORK/nb/$nb.executed.ipynb"
      else
        record "notebook.$nb" fail "$(grep -iE 'error|exception|denied' "$WORK/nb/$nb.log" | head -3 | tr '\n' ' ' | cut -c1-600)"
      fi
    done
  else
    record "notebook.nbconvert" fail "jupyter nbconvert not available on this host"
  fi
fi

# ---------------------------------------------------------------- signals in Omni
# Does the space actually hold the traffic the module says it does?
python3 - "$REGION" "$WORK/signals.json" <<'PYEOF' > "$WORK/signals.log" 2>&1
import json, sys, time
from datetime import datetime, timedelta, timezone
import boto3
region, out = sys.argv[1], sys.argv[2]
omni = boto3.client("cloudwatchomni", region_name=region)
logs = boto3.client("logs", region_name=region)
control = boto3.client("bedrock-agentcore-control", region_name=region)
sig = {}
domains = omni.list_domains().get("items", [])
sig["domain"] = domains[0]["name"] if domains else None
spaces = omni.list_spaces(domainId=domains[0]["domainId"]).get("items", []) if domains else []
sig["space_status"] = spaces[0]["status"] if spaces else None
sid = omni.start_telemetry_query_session(sessionName=f"m5-live-{int(time.time())}")["sessionId"]
now = datetime.now(timezone.utc)
rng = (f"`@timestamp` BETWEEN to_timestamp('{(now - timedelta(hours=6)):%Y-%m-%dT%H:%M:%SZ}') "
       f"AND to_timestamp('{(now + timedelta(minutes=5)):%Y-%m-%dT%H:%M:%SZ}')")
qid = omni.start_telemetry_query(sessionId=sid, queryString=f"SELECT `@logGroupName` AS lg, COUNT(*) AS n FROM default WHERE {rng} GROUP BY 1 ORDER BY n DESC LIMIT 50")["queryId"]
deadline = time.time() + 120
while True:
    r = omni.get_telemetry_query_results(queryId=qid)
    if r["status"] != "Running" or time.time() > deadline:
        break
    time.sleep(3)
rows = {row.get("lg"): row.get("n") for row in r.get("rows", [])}
sig["dataset_query_status"] = r["status"]
sig["dataset_rows_per_log_group"] = rows
sig["dataset_has_spans"] = any(k and k.endswith("aws/spans") for k in rows)
sig["dataset_has_interceptor_logs"] = any(k and "interceptor" in k for k in rows)
sig["dataset_has_runtime_logs"] = any(k and "/aws/bedrock-agentcore/runtimes/" in k for k in rows)
try:
    omni.stop_telemetry_query_session(sessionId=sid)
except Exception:
    pass
configs = [c for c in control.list_online_evaluation_configs().get("onlineEvaluationConfigs", []) if c["onlineEvaluationConfigName"] == "workshop_travel_agent"]
sig["online_evaluation"] = {"status": configs[0]["status"], "execution": configs[0]["executionStatus"], "id": configs[0]["onlineEvaluationConfigId"]} if configs else None
sig["evaluation_results"] = 0
if configs:
    try:
        ev = logs.filter_log_events(logGroupName=f"/aws/bedrock-agentcore/evaluations/results/{configs[0]['onlineEvaluationConfigId']}", limit=50)["events"]
        sig["evaluation_results"] = len(ev)
        sig["evaluation_result_names"] = sorted({json.loads(e["message"]).get("attributes", {}).get("gen_ai.evaluation.name", "?") for e in ev})
    except logs.exceptions.ResourceNotFoundException:
        sig["evaluation_results_group"] = "absent (arrives ~10 min after a session goes idle)"
json.dump(sig, open(out, "w"), indent=2, default=str)
print(json.dumps(sig, indent=2, default=str))
PYEOF
if [ -s "$WORK/signals.json" ]; then
  # Evaluate the probe's JSON into gate lines, then record them in THIS shell: a
  # `python | while read` pipeline would run `record` in a subshell and the PASS/FAIL
  # counters (and the exit status) would silently miss every signal gate.
  python3 - "$WORK/signals.json" > "$WORK/signal-gates.txt" <<'PYEOF'
import json, sys
s = json.load(open(sys.argv[1]))
def g(name, ok, detail): print(f"{name}|{'ok' if ok else 'fail'}|{detail}")
g("omni.space", s.get("space_status") == "ACTIVE", f"domain={s.get('domain')} space={s.get('space_status')}")
g("omni.dataset_query", s.get("dataset_query_status") == "Complete", f"status={s.get('dataset_query_status')} groups={len(s.get('dataset_rows_per_log_group', {}))}")
g("omni.spans_in_dataset", s.get("dataset_has_spans"), "aws/spans rows present" if s.get("dataset_has_spans") else "no aws/spans rows in the Dataset")
g("omni.interceptor_logs_in_dataset", s.get("dataset_has_interceptor_logs"), "interceptor audit rows present" if s.get("dataset_has_interceptor_logs") else "no interceptor rows")
oe = s.get("online_evaluation") or {}
g("evaluation.online_config", oe.get("execution") == "ENABLED", f"{oe or 'no workshop_travel_agent config'}")
g("evaluation.results", (s.get("evaluation_results") or 0) > 0, f"{s.get('evaluation_results')} result events {s.get('evaluation_result_names', '')} {s.get('evaluation_results_group', '')} (scores arrive ~10 min after a session goes idle; re-run the probe later if this is the only failure)")
PYEOF
  while IFS='|' read -r gate status detail; do record "$gate" "$status" "$detail"; done < "$WORK/signal-gates.txt"
else
  record "omni.signals" fail "signal probe did not run: $(tail -3 "$WORK/signals.log" | tr '\n' ' ')"
fi

# ---------------------------------------------------------------- cleanup (opt-in)
if [ "$RUN_CLEANUP" = 1 ]; then
  [ "$RUN_CLI" = 1 ] && run_page step-7 "cleanup"
fi

# ---------------------------------------------------------------- evidence
{
  echo "{"
  echo "  \"region\": \"$REGION\", \"cli_version\": \"$CLI_VERSION\", \"boto3\": \"$BOTO\","
  echo "  \"pass\": $PASS, \"fail\": $FAIL,"
  echo "  \"gates\": [$(IFS=,; echo "${RESULTS[*]}")]"
  echo "}"
} > "$OUT"
echo "== $PASS passed, $FAIL failed; evidence $OUT"
[ "$FAIL" = 0 ]
