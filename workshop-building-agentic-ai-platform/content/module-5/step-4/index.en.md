---
title: "Agent traces and sessions"
weight: 84
---

**Tracks 1 and 3.** Requires Module 4: the FAST agent is deployed and the [Keep prompts and responses on the spans](/module-4/deploy#keep-prompts-and-responses-on-the-spans) patch was applied before `cdk deploy`.

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach, both achieve the same result.]{type="info"}

Module 4's observe page showed the agent's runtime logs and a trace in the GenAI Observability dashboard. Here you read a whole conversation the way Omni represents it: a session of traces, each trace a tree of spans for the model calls (through the LLM Gateway), the tool calls (through the AgentCore Gateway), Memory and the Code Interpreter, with the prompt and response text on the spans.

## CLI walkthrough

### Step 1: Drive one conversation with a known session ID

The frontend generates its own session IDs. To find your traces quickly, send a three-turn conversation from the terminal with a session ID you choose. This is the same invocation path as Test 5 on the [Run the agent](/module-4/run-the-agent) page (throwaway Cognito user, access token, HTTPS POST):

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)
RUNTIME_ARN=$(aws ssm get-parameter --name /FAST-stack/runtime-arn \
  --query Parameter.Value --output text --region $REGION)
USER_POOL_ID=$(aws ssm get-parameter --name /FAST-stack/cognito-user-pool-id \
  --query Parameter.Value --output text --region $REGION)
CLIENT_ID=$(aws ssm get-parameter --name /FAST-stack/cognito-user-pool-client-id \
  --query Parameter.Value --output text --region $REGION)

SMOKE_USER="omni-$(openssl rand -hex 4)@example.com"
SMOKE_PASS="$(openssl rand -base64 18 | tr -d '/+=')"'!Aa1'
aws cognito-idp admin-create-user --user-pool-id $USER_POOL_ID \
  --username "$SMOKE_USER" --message-action SUPPRESS \
  --user-attributes Name=email,Value="$SMOKE_USER" Name=email_verified,Value=true \
  --region $REGION >/dev/null
aws cognito-idp admin-set-user-password --user-pool-id $USER_POOL_ID \
  --username "$SMOKE_USER" --password "$SMOKE_PASS" --permanent --region $REGION
ACCESS_TOKEN=$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
  --client-id $CLIENT_ID \
  --auth-parameters USERNAME="$SMOKE_USER",PASSWORD="$SMOKE_PASS" \
  --query 'AuthenticationResult.AccessToken' --output text --region $REGION)

# One session, three turns. The ID must be at least 33 characters.
SESSION_ID="omni-$(openssl rand -hex 16)-session"
ENCODED_ARN=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$RUNTIME_ARN")

ask() {
  local http
  http=$(curl -s -o /tmp/omni-turn.txt -w '%{http_code}' -X POST \
    "https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${ENCODED_ARN}/invocations?qualifier=DEFAULT" \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    -H "Content-Type: application/json" \
    -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: $SESSION_ID" \
    -d "{\"prompt\": \"$1\", \"runtimeSessionId\": \"$SESSION_ID\"}")
  echo "HTTP $http  <- $1"
}
ask "Find flights from SFO to TYO on 2026-09-15"
ask "Which of those is cheapest, and find me a hotel in Tokyo for 3 nights from that date"
ask "Compare the total cost of the two cheapest flight and hotel combinations"

aws cognito-idp admin-delete-user --user-pool-id $USER_POOL_ID \
  --username "$SMOKE_USER" --region $REGION
echo "export OMNI_SESSION_ID=$SESSION_ID" >> ~/.workshop-omni.env
echo "Session: $SESSION_ID"
:::

Three `HTTP 200` lines, then the session ID. The third turn usually makes the agent run the Code Interpreter, which gives the trace a span type the first two turns do not have.

### Step 2: Confirm the spans landed, and learn the agent's service name

Spans reach the shared `aws/spans` log group within about a minute. This Logs Insights query lists the spans of your session in order, and extracts the OpenTelemetry service name that identifies the agent everywhere in Omni and in the evaluation configuration you create next:

:::code{showCopyAction=true showLineNumbers=false language=bash}
QUERY_ID=$(aws logs start-query --region $REGION \
  --log-group-names aws/spans \
  --start-time $(date -d '-30 minutes' +%s) --end-time $(date -d '+5 minutes' +%s) \
  --query-string "fields @timestamp, name, traceId, resource.attributes.service.name as service
    | filter attributes.session.id = \"$SESSION_ID\"
    | filter ispresent(scope.name) and ispresent(spanId)
    | sort @timestamp asc | limit 200" \
  --query queryId --output text)

for _ in $(seq 1 20); do
  STATUS=$(aws logs get-query-results --query-id $QUERY_ID --region $REGION --query status --output text)
  [ "$STATUS" = "Complete" ] && break
  sleep 3
done

aws logs get-query-results --query-id $QUERY_ID --region $REGION --output json | python3 -c '
import json, sys, collections
rows = json.load(sys.stdin)["results"]
spans = [{f["field"]: f["value"] for f in r} for r in rows]
print(f"{len(spans)} spans in the session")
print("span names:", dict(collections.Counter(s.get("name", "?") for s in spans)))
traces = sorted({s["traceId"] for s in spans if s.get("traceId")})
print(f"{len(traces)} traces (one per turn):", [t[-8:] for t in traces])
services = {s.get("service") for s in spans if s.get("service")}
print("service name:", services)
open("/tmp/omni-service-name", "w").write(next(iter(services), ""))'

SERVICE_NAME=$(cat /tmp/omni-service-name)
echo "export OMNI_SERVICE_NAME=$SERVICE_NAME" >> ~/.workshop-omni.env
:::

Expect a few dozen spans across three traces. Span names include `invoke_agent`, the model calls (`chat`), the Gateway tool calls (`execute_tool ...`) and, on the third turn, the Code Interpreter. The service name looks like `FAST_stack_FASTAgent.DEFAULT`: the runtime name and its endpoint. If the count is `0`, wait a minute and re-run; if it is still `0` after five minutes, the runtime was deployed without the Module 4 span patch, so spans went to the agent's own log group instead: re-apply the patch and `cdk deploy`.

### Step 3: Read the trace in Omni

Open your space and choose **Agent traces** (under *Agent observability*). Search for your `SESSION_ID` and open one of its traces. The header gives the whole-turn picture — session ID, status (`OK`, or `Fault` when a tool call failed), span count, duration, tokens and the agent's service name — and the **Timeline** tab shows the span tree; **Span view** on the right opens any span's input/output and attributes.

![An agent trace in Omni: the invoke_agent root with event-loop, model, gateway tool, memory and code interpreter spans](/static/img/module-5/omni-agent-trace.png)

Read the tree top to bottom and match it to the platform you built:

| Span | What it proves |
|------|----------------|
| `invoke_agent travel_agent` root | One trace per turn; the session ID and user ID from `trace_attributes` in `travel_agent.py` are on it |
| `execute_event_loop_cycle` | One Strands reasoning cycle; a turn that calls tools has several, nested |
| `chat` (with its `POST`) and `gen_ai.*` attributes, `server.address` = the LLM Gateway URL | Every model call went through Module 2's gateway, with the prompt and response text attached (the Module 4 patch); the token count sits on the span |
| `execute_tool gateway_tg-workshop-…` → `mcp tools/call tg-workshop-…` | The agent's tool call and the MCP request the AgentCore Gateway received for it; latency and status per tool, and a `Fault` marker when the tool returned an error |
| `Bedrock AgentCore.CreateEvent` | The session manager stored a turn in AgentCore Memory |
| `execute_tool execute_python_securely` → `Bedrock AgentCore.StartCodeInterpreterSession` / `InvokeCodeInterpreter` (third turn) | The Code Interpreter sandbox ran; usually the longest tool span in the trace |

Choose **Agent sessions** to see the three traces as one conversation, and **Agent topology** to see the agent, the gateway and the tools as a graph derived from these spans. Open **Agent overview** for the agent's headline numbers — evaluations, sessions, traces, total tokens, error rate and average latency — with charts over time (tokens, failed traces, latency, volume) and an Omni-generated latency-percentile table. Token usage is the number that drives cost.

![Agent overview: evaluations, sessions, traces, tokens, error rate and latency, with the latency-percentile table](/static/img/module-5/omni-agent-dashboard.png)

## What you observed

The GenAI Observability dashboard from Module 4 and Omni read the same spans. The difference is who they serve: the console dashboard gives prebuilt per-service views; the Omni space puts agent telemetry next to the platform telemetry from step 3, in one identity model, and is where evaluation scores attach to traces in the next step.

---

## Notebook walkthrough (optional alternative)

> This notebook (05-agent-traces.ipynb) drives the same three-turn conversation and runs the same Logs Insights query from Python. Follow *either* path.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker.
>
> Navigate to `source/module-5-omni/notebooks/` and open **`05-agent-traces.ipynb`**.
