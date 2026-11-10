---
title: "Platform signals"
weight: 83
---

**Tracks 2 and 3.** Track 1 participants skip to [Agent traces and sessions](../step-4/).

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach, both achieve the same result.]{type="info"}

Modules 2, 3a and 3b each left a trail: LiteLLM request logs on ECS, the interceptor Lambdas' audit records, the AgentCore Gateway's own spans. Until now each trail lived in its own log group with its own query. The Dataset integration you created in step 1 forwards all of them into one table, `default`, that you query with SQL, and Omni backfilled up to seven days of history when the space was created, so the gateway tests you ran in Module 3b are already there.

## CLI walkthrough

### Step 1: Start a query session and count records per log group

Telemetry queries run inside a session, and a session name is unique per account, so the command stamps the name with the time. Every query needs an explicit time range; the Dataset scans the whole range you ask for, so keep ranges tight:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)
QUERY_SESSION=$(aws cloudwatchomni start-telemetry-query-session \
  --session-name "workshop-cli-$(date +%s)" --region $REGION \
  --query sessionId --output text)

START=$(date -u -d '-6 hours' +%Y-%m-%dT%H:%M:%SZ)
END=$(date -u -d '+5 minutes' +%Y-%m-%dT%H:%M:%SZ)

run_query() {
  local qid
  qid=$(aws cloudwatchomni start-telemetry-query --session-id $QUERY_SESSION \
    --query-string "$1" --region $REGION --query queryId --output text)
  for _ in $(seq 1 30); do
    STATUS=$(aws cloudwatchomni get-telemetry-query-results --query-id $qid --region $REGION \
      --query status --output text)
    [ "$STATUS" = "Running" ] || break
    sleep 2
  done
  aws cloudwatchomni get-telemetry-query-results --query-id $qid --region $REGION \
    --query 'rows' --output json | python3 -c '
import json, sys
rows = json.load(sys.stdin)
if not rows:
    print("(no rows)")
for r in rows:
    print("  ".join(f"{k}={v}" for k, v in r.items()))'
}

run_query "SELECT \`@logGroupName\` AS log_group, COUNT(*) AS records
  FROM default
  WHERE \`@timestamp\` BETWEEN to_timestamp('$START') AND to_timestamp('$END')
  GROUP BY 1 ORDER BY records DESC LIMIT 25"
:::

You can expect one line per log group that produced records in the window, for example:

:::code{showCopyAction=false showLineNumbers=false language=text}
log_group=/ecs/workshop-llm-gateway-stack  records=412
log_group=aws/spans  records=96
log_group=/aws/lambda/ac-gateway-request-interceptor  records=57
log_group=/aws/lambda/ac-gateway-response-interceptor  records=57
log_group=/aws/lambda/workshop-flights-mcp  records=21
:::

The Dataset backfills for a few minutes after you create the integration in step 1, so a list that stops at the platform's own ECS and Lambda logs (no `aws/spans`, no interceptor groups) is not wrong, only early: wait five minutes and run the query again.

Each line is one platform layer: the LLM Gateway (Module 2), the AgentCore Gateway's spans (Module 3b), the interceptors' audit trail, the tool Lambdas. The exact names and counts depend on which modules you completed. If `aws/spans` is missing, run the gateway test from [Module 3b step 6](/module-3b/step-6) once and query again; the Gateway emits a span per `tools/call` only when Transaction Search is on, which it is.

### Step 2: Follow one tool call across layers

The request interceptor logs the caller identity and tool name for every `tools/call`. Pull the latest audit records, then the Gateway spans from the same minutes:

:::code{showCopyAction=true showLineNumbers=false language=bash}
run_query "SELECT \`@timestamp\` AS at, \`@logGroupName\` AS log_group, \`@message\` AS message
  FROM default
  WHERE \`@timestamp\` BETWEEN to_timestamp('$START') AND to_timestamp('$END')
    AND \`@logGroupName\` LIKE '/aws/lambda/%gateway-request-interceptor'
    AND \`@message\` LIKE '%tools/call%'
  ORDER BY \`@timestamp\` DESC LIMIT 5"
:::

Each row carries the JSON audit record the interceptor wrote in Module 3b: `client_id`, `tool_name`, `method`. The same identity appears on the Gateway spans in `aws/spans`, which is what lets Omni's session and topology views join the two in the UI without a query.

### Step 3: Ask the question in plain language

Open your space (step 2 explained how). In the *Ask anything…* box on the home page, start a thread with:

> Which gateway tools were called most in the last six hours, and by which client?

Omni inspects the raw rows first, writes the SQL, runs it over the same `default` table and answers in the thread, while the **Explore** canvas on the right shows the query it wrote (**Code** view) and the result table. Its SQL goes further than yours in Step 2: it extracts the JSON audit record out of `@message` with `SUBSTRING`/`POSITION`, then reads `tool_name` and `client_id` with `json_get_str` and groups by both — six tool/client combinations, all from the one M2M client. The **Ask AI** button in the canvas edits a query in place the same way. This is the hand-off a platform team wants: operators query, everyone else asks.

![A thread asking which gateway tools were called most, next to the Explore canvas with the generated SQL and its result table](/static/img/module-5/omni-explore-ask.png)

### Step 4: Look at the application map

Open **Applications** in the left navigation. Omni discovers services and their dependencies from the telemetry and shows request rate, errors and duration for each. In the workshop account the map is small: the AgentCore Gateway, the interceptor and tool Lambdas, and (after Part B) the FAST runtime calling the LLM Gateway. The LiteLLM ECS service appears only through its logs, because it does not emit traces; that gap is exactly what OpenTelemetry instrumentation on a platform component closes.

## What you observed

| Layer | Where its records come from | What a platform team reads there |
|-------|-----------------------------|----------------------------------|
| LLM Gateway (Module 2) | ECS task logs, forwarded by the Dataset integration | Which virtual key called which model, guardrail blocks |
| Interceptors (Modules 3a and 3b) | Lambda logs | Caller identity per tool call, access-control decisions |
| AgentCore Gateway (Module 3b) | Service-provided spans in `aws/spans` | Latency and errors per tool, joined to the interceptor audit |
| Tool Lambdas | Lambda logs and spans | Downstream failures behind a slow tool |

One table, one time range, one identity model across all of them. Continue with Part B if you completed Module 4, or go to [Cleanup](../step-7/) if you are on Track 2.

---

## Notebook walkthrough (optional alternative)

> This notebook (04-platform-signals.ipynb) runs the same two SQL queries from Python and prints the rows as a table. Follow *either* path.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker.
>
> Navigate to `source/module-5-omni/notebooks/` and open **`04-platform-signals.ipynb`**.
