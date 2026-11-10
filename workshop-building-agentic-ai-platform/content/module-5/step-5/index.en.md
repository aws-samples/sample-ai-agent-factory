---
title: "Evaluate quality"
weight: 85
---

**Tracks 1 and 3.** Requires the session you created in [step 4](../step-4/).

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach, both achieve the same result.]{type="info"}

Error rates say whether the agent answered. They say nothing about whether the answer was good. Amazon Bedrock AgentCore Evaluations scores traces with evaluators, and CloudWatch Omni is where those scores live next to the traces. You run an evaluator once, on demand, against the conversation from step 4, then turn on an online evaluation that scores every new session as it arrives and publishes each score as a CloudWatch metric you can alarm on.

## CLI walkthrough

### Step 1: See what the catalog offers

Built-in evaluators run on service-managed capacity, so they need no model permission in your account. List them:

:::code{showCopyAction=true showLineNumbers=false language=bash}
source ~/.workshop-omni.env
REGION=$(aws configure get region)

aws bedrock-agentcore-control list-evaluators --region $REGION \
  --query "evaluators[?evaluatorType=='Builtin'].[evaluatorId,level]" --output table
:::

The table lists the built-in catalog with the level each one scores at: `TOOL_CALL`, `TRACE` (one turn) or `SESSION` (the whole conversation). You use four of them below: `Builtin.Helpfulness` and `Builtin.ResponseRelevance` on demand in Step 2 (both `TRACE` level, which is what an on-demand call on a trace accepts), then `Builtin.Helpfulness`, `Builtin.Refusal` and `Builtin.ToolSelectionAccuracy` in the online evaluation of Step 3. Third-party evaluators (DeepEval, AutoEval) and your own custom evaluators appear in the same list once added.

### Step 2: Score one turn on demand

`Evaluate` takes the session's span records as input and scores a target trace. The spans come from two log groups: `aws/spans` holds the span tree, and the agent's runtime log group holds the prompt and response text the distro mirrors out as log events. The evaluator needs both.

:::code{showCopyAction=true showLineNumbers=false language=bash}
python3 - "$REGION" "$OMNI_SESSION_ID" <<'PYEOF'
import json, sys, time
import boto3

region, session_id = sys.argv[1], sys.argv[2]
logs = boto3.client("logs", region_name=region)
evaluations = boto3.client("bedrock-agentcore", region_name=region)

runtime_groups = [g["logGroupName"] for g in logs.describe_log_groups(
    logGroupNamePrefix="/aws/bedrock-agentcore/runtimes/FAST_stack_FASTAgent")["logGroups"]]
groups = ["aws/spans"] + runtime_groups

query = (f'fields @message | filter attributes.session.id = "{session_id}"'
         " | filter ispresent(scope.name) and ispresent(traceId) and ispresent(spanId)"
         " | sort @timestamp asc | limit 2000")
qid = logs.start_query(logGroupNames=groups, startTime=int(time.time()) - 3 * 3600,
                       endTime=int(time.time()) + 60, queryString=query)["queryId"]
while True:
    result = logs.get_query_results(queryId=qid)
    if result["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
        break
    time.sleep(2)
spans = [json.loads(f["value"]) for row in result.get("results", []) for f in row if f["field"] == "@message"]
trace_ids = sorted({s["traceId"] for s in spans if str(s.get("name", "")).startswith("invoke_agent")})
print(f"{len(spans)} spans, {len(trace_ids)} invoke_agent traces")
if not trace_ids:
    sys.exit("No invoke_agent trace found for this session. Run step 4 again and wait a minute.")

target = trace_ids[1] if len(trace_ids) > 1 else trace_ids[0]   # the hotel turn
for evaluator_id in ("Builtin.Helpfulness", "Builtin.ResponseRelevance"):
    response = evaluations.evaluate(
        evaluatorId=evaluator_id,
        evaluationInput={"sessionSpans": spans},
        evaluationTarget={"traceIds": [target]},
    )
    for r in response["evaluationResults"]:
        if r.get("errorCode"):
            print(f"{evaluator_id}: ERROR {r['errorCode']} {r.get('errorMessage', '')}")
            continue
        print(f"{evaluator_id}: label={r.get('label')} value={r.get('value')}")
        print(f"   {str(r.get('explanation', ''))[:300]}")
PYEOF
:::

You can expect output like:

:::code{showCopyAction=false showLineNumbers=false language=text}
41 spans, 3 invoke_agent traces
Builtin.Helpfulness: label=<rating> value=<0.0-1.0>
   <the judge's explanation, quoting the hotel results the agent returned ...>
Builtin.ResponseRelevance: label=<rating> value=<0.0-1.0>
   <the judge's explanation of how the answer stays on the hotel request ...>
:::

On-demand `Evaluate` with `traceIds` accepts `TRACE`-level evaluators only; a `TOOL_CALL` evaluator such as `Builtin.ToolSelectionAccuracy` is rejected with a `ValidationException` here and belongs in the online evaluation of the next step, which scores every level. Each evaluator has its own rating scale; the label names the rating and the value normalizes it. Judge-based scores vary a little between runs; read them as a trend, not a unit test. A `MISSING_OUTPUT_CONTENT` error means the spans carry no response text: the Module 4 span patch was not applied before deployment.

### Step 3: Turn on continuous scoring

An online evaluation samples live traffic for an agent (selected by its service name), runs the evaluators you choose, and writes every score to a results log group and to CloudWatch metrics. `sessionTimeoutMinutes` decides when a conversation counts as finished: a session is scored once it has been idle that long. One minute suits a workshop; production sessions use the real gap between turns.

:::code{showCopyAction=true showLineNumbers=false language=bash}
RUNTIME_LOG_GROUP=$(aws logs describe-log-groups --region $REGION \
  --log-group-name-prefix /aws/bedrock-agentcore/runtimes/FAST_stack_FASTAgent \
  --query 'logGroups[0].logGroupName' --output text)

cat > /tmp/online-eval.json <<EOF
{
  "onlineEvaluationConfigName": "workshop_travel_agent",
  "description": "Module 5: continuous quality scores for the FAST travel agent",
  "rule": {
    "samplingConfig": {"samplingPercentage": 100},
    "sessionConfig": {"sessionTimeoutMinutes": 1}
  },
  "dataSourceConfig": {
    "cloudWatchLogs": {
      "logGroupNames": ["aws/spans", "$RUNTIME_LOG_GROUP"],
      "serviceNames": ["$OMNI_SERVICE_NAME"]
    }
  },
  "evaluators": [
    {"evaluatorId": "Builtin.Helpfulness"},
    {"evaluatorId": "Builtin.Refusal"},
    {"evaluatorId": "Builtin.ToolSelectionAccuracy"}
  ],
  "evaluationExecutionRoleArn": "$OMNI_EVAL_ROLE_ARN",
  "outputConfig": {"cloudWatchConfig": {"resultDestination": "DEDICATED_LOG_GROUP"}},
  "enableOnCreate": true
}
EOF

aws bedrock-agentcore-control create-online-evaluation-config \
  --cli-input-json file:///tmp/online-eval.json --region $REGION \
  --query '{id:onlineEvaluationConfigId,status:status,execution:executionStatus}' --output table
:::

Status `ACTIVE` (or `CREATING` for a few seconds) with execution `ENABLED`. Save the ID for cleanup:

:::code{showCopyAction=true showLineNumbers=false language=bash}
EVAL_CONFIG_ID=$(aws bedrock-agentcore-control list-online-evaluation-configs --region $REGION \
  --query "onlineEvaluationConfigs[?onlineEvaluationConfigName=='workshop_travel_agent'].onlineEvaluationConfigId | [0]" \
  --output text)
echo "export OMNI_EVAL_CONFIG_ID=$EVAL_CONFIG_ID" >> ~/.workshop-omni.env
echo "Online evaluation config: $EVAL_CONFIG_ID"
:::

::alert[The evaluator list of an enabled configuration is locked so that scores stay comparable over time. To change evaluators, disable the configuration first, or create a second one.]{type="info"}

### Step 4: Generate traffic for it to score

The configuration only sees sessions that start after it was enabled. Re-run the three-turn conversation from step 4 with a new session ID (the same block, it generates a fresh `SESSION_ID` each run), then leave the session idle. Scoring starts one minute after the last turn and the results take roughly ten more minutes to appear. Use that time for step 6, or read on.

### Step 5: Read the scores as logs and metrics

Results are JSON records in `/aws/bedrock-agentcore/evaluations/results/<config-id>`, following the OpenTelemetry semantic conventions for generative AI evaluation events. Each record references the trace and session it scored:

:::code{showCopyAction=true showLineNumbers=false language=bash}
source ~/.workshop-omni.env
RESULTS_GROUP="/aws/bedrock-agentcore/evaluations/results/${OMNI_EVAL_CONFIG_ID}"

aws logs filter-log-events --log-group-name "$RESULTS_GROUP" --region $REGION \
  --start-time $(( $(date +%s) * 1000 - 3600000 )) --limit 50 \
  --query 'events[].message' --output json > /tmp/omni-eval-results.json 2>/dev/null \
  || echo "No results group yet: the first scores arrive about ten minutes after a session goes idle."

python3 - <<'PYEOF'
import json, pathlib
path = pathlib.Path("/tmp/omni-eval-results.json")
messages = json.loads(path.read_text()) if path.exists() and path.stat().st_size else []
if not messages:
    print("No scores yet; try again in a few minutes.")
for m in messages:
    a = json.loads(m).get("attributes", {})
    name = a.get("gen_ai.evaluation.name", "?")
    label = a.get("gen_ai.evaluation.score.label")
    value = a.get("gen_ai.evaluation.score.value")
    session = str(a.get("session.id", ""))[-8:]
    error = f"  error={a['error.type']}" if a.get("error.type") else ""
    print(f"{name:32s} label={label}  value={value}  session=...{session}{error}")
PYEOF
:::

Each score is also a metric in the `Bedrock-AgentCore/Evaluations` namespace, which is what turns a quality signal into an operational one:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws cloudwatch list-metrics --namespace Bedrock-AgentCore/Evaluations --region $REGION \
  --query 'Metrics[].[MetricName, join(`,`, Dimensions[].Value)]' --output table
:::

The namespace is empty until the first scores land, about ten minutes after a scored session goes idle; re-run the command then and one row per evaluator, label and agent appears.

A platform team puts a CloudWatch alarm on a score the same way it alarms on an error rate: `Helpfulness` below a threshold for 15 minutes pages the agent's owners, not the platform team.

### Step 6: See the scores in Omni

In your space, open **Evaluation dashboard** (under *Agent observability*) and, in the filter panel on the left, tick `workshop_travel_agent` under **Online evaluations**. The banner says *Results start at …*, the moment the configuration was created: nothing before it is scored. **Evaluators by Score** plots each evaluator's average over time, and **Evaluator statistics** gives the average, the change over the window, the spread, P50/P99 and a count per evaluator, with the direction that counts as better (Refusal: lower is better; the others: higher). Open **Agent sessions**: scored sessions now carry their evaluator results, so a low score is one click away from the exact run that produced it.

::alert[Until the first scores land the dashboard reads *Sessions evaluated: 0 · last evaluated Never*. A session is scored about ten minutes after it goes idle (`sessionTimeoutMinutes` is 1), and only sessions that started after the configuration was created count — so the first numbers appear roughly ten minutes after Step 4's conversation. The `Bedrock-AgentCore/Evaluations` metrics appear at the same moment; the results log group exists from creation, so its presence alone is not the signal.]{type="info"}

![Evaluation dashboard filtered to the workshop_travel_agent online evaluation: evaluators by score and the evaluator statistics table](/static/img/module-5/omni-online-evaluation.png)

## What you observed

| Mode | When it runs | Where results go | Use it for |
|------|--------------|------------------|------------|
| On-demand `Evaluate` | When you call it, on spans you supply | The API response | Debugging one run, testing a new evaluator's contract in seconds |
| Online evaluation | Continuously, on sampled live sessions | Results log group, CloudWatch metrics, Omni trends | Regression detection and alarms in production |

Batch evaluation against a dataset with ground truth (expected responses, expected tool trajectories) is the third mode; the [enterprise blueprint](https://github.com/aws-samples/sample-ai-agent-factory/tree/main/enterprise-agentic-ai-platform-blueprint) uses it as a promotion gate in its delivery pipeline.

---

## Notebook walkthrough (optional alternative)

> This notebook (06-evaluate.ipynb) runs the on-demand evaluation, creates the online evaluation configuration and reads the results from Python. Follow *either* path.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker.
>
> Navigate to `source/module-5-omni/notebooks/` and open **`06-evaluate.ipynb`**.
