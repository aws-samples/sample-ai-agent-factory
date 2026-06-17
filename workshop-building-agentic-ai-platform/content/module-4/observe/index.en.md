---
title: "Observe the Platform"
weight: 77
---

The agent is running end to end. In this step you will inspect the observability surfaces that the platform provides.

## View Agent Runtime Logs

AgentCore Runtime streams logs to CloudWatch via OpenTelemetry. Open the log group for your agent:

::alert[Confirm the CloudWatch console **region selector** (top-right) matches the region you deployed into.]{type="info"}

:button[Open CloudWatch Logs]{target="_blank" href="https://console.aws.amazon.com/cloudwatch/home#logsV2:log-groups" variant="primary" iconName="external" iconAlign="right"}

Navigate to the log group `/aws/bedrock-agentcore/runtimes/FAST_stack_FASTAgent-*-DEFAULT`. You should see log streams for each agent invocation.

Key log entries to look for:

| Log Entry | What It Confirms |
|-----------|-----------------|
| `[MODEL] Using LiteLLM Gateway: https://...` | Model calls routed through Module 2 |
| `[GATEWAY] URL: https://tools-gateway-...` | Tools served by Module 3a's Tools Gateway |
| `Getting OAuth2 token...` | M2M auth via Token Vault → Module 3a Cognito |
| `Negotiated protocol version: 2025-03-26` | MCP handshake with gateway succeeded |
| `LiteLLM completion() model=...` | Each model invocation |
| `Created session: ...` | AgentCore Memory session created |
| `Started code interpreter in ...` | Code Interpreter sandbox activated |

## GenAI Observability

Amazon Bedrock provides a GenAI Observability dashboard in CloudWatch. Open it to see traces for your agent:

:button[Open GenAI Observability]{target="_blank" href="https://console.aws.amazon.com/cloudwatch/home#gen-ai-observability" variant="primary" iconName="external" iconAlign="right"}

Select a trace to see the full request flow — from the user's prompt through model calls, tool invocations, and the final response. Each span shows latency, status, and metadata.

## Inspect Gateway Audit Logs

Module 3a's AgentCore Gateway request interceptor logs every tool invocation. Check the interceptor log group:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

aws logs filter-log-events \
  --log-group-name "/aws/lambda/agentcore-gateway-request-interceptor" \
  --filter-pattern '"tools"' \
  --limit 5 --region $REGION \
  --query "events[].message" --output text
:::

Each audit entry includes the caller identity (from the JWT), the tool name, and a timestamp. This is the audit trail that platform teams use to track which agents are calling which tools.

## Explore AgentCore Memory

AgentCore Memory stores conversation history. Query the memory to see what the agent has retained:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

MEMORY_ARN=$(aws cloudformation describe-stacks \
  --stack-name FAST-stack \
  --query "Stacks[0].Outputs[?OutputKey=='MemoryArn'].OutputValue" \
  --output text --region $REGION)

MEMORY_ID=$(echo "$MEMORY_ARN" | awk -F'/' '{print $NF}')

echo "Memory ID: $MEMORY_ID"
:::

`list-events` lives on the `bedrock-agentcore` data plane (not `bedrock-agentcore-control`) and requires `memoryId`, `actorId`, and `sessionId` — so you have to iterate actors → sessions → events.

:::code{showCopyAction=true showLineNumbers=false language=bash}
python3 <<PYEOF
import boto3
REGION = "$REGION"
MEMORY_ID = "$MEMORY_ID"

data = boto3.client("bedrock-agentcore", region_name=REGION)
actors = data.list_actors(memoryId=MEMORY_ID, maxResults=10).get("actorSummaries", [])
print(f"Actors with memory: {len(actors)}")
if not actors:
    print("No memory yet -- send a message through the Amplify UI first.")
for a in actors[:3]:
    actor_id = a["actorId"]
    sessions = data.list_sessions(
        memoryId=MEMORY_ID, actorId=actor_id, maxResults=3,
    ).get("sessionSummaries", [])
    print(f"  actor={actor_id}  sessions={len(sessions)}")
    for s in sessions[:1]:
        session_id = s["sessionId"]
        events = data.list_events(
            memoryId=MEMORY_ID, actorId=actor_id, sessionId=session_id, maxResults=5,
        ).get("events", [])
        print(f"    session={session_id[:12]}  events={len(events)}")
        for ev in events[:3]:
            print(f"      {ev.get('eventTimestamp', '?')}  {ev.get('eventId', '?')[:12]}")
PYEOF
:::

Each event represents a conversation turn stored by the `AgentCoreMemorySessionManager`. The agent loads recent turns at the start of each session to maintain context.

## What You Observed

| Surface | What It Shows | Who Uses It |
|---------|---------------|-------------|
| Runtime Logs | Model invocations, tool calls, errors | AI/ML engineers debugging agent behavior |
| GenAI Observability | End-to-end traces with latency and spans | Platform teams monitoring production |
| Gateway Audit Logs | Every tool invocation with caller identity | Platform teams tracking tool usage |
| AgentCore Memory | Conversation history per user/session | AI/ML engineers tuning memory strategies |

This observability stack demonstrates the patterns needed for an enterprise solution — every layer is instrumented and auditable.

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4b-fast/notebooks/` and open **`06-observe.ipynb`**.
