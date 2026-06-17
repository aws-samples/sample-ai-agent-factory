---
title: "Run the Agent"
weight: 76
---

The travel agent is fully connected — model calls flow through the LLM Gateway (Module 2) and tools are served by the AgentCore Gateway (Module 3a (Tools Gateway)). Time to plan some trips.

## Open the Frontend

Retrieve the Amplify URL and open it in your browser:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

AMPLIFY_URL=$(aws cloudformation describe-stacks \
  --stack-name FAST-stack \
  --query "Stacks[0].Outputs[?OutputKey=='AmplifyUrl'].OutputValue" \
  --output text --region $REGION)

echo "Open in your browser: $AMPLIFY_URL"
:::

Log in with the credentials you created in the Deploy step (`workshop@example.com`).

## Test 1: Plan a Trip

Type the following in the chat:

> Plan a trip from SFO to Tokyo for 2026-09-15 to 2026-09-18, 2 guests, budget $2000 for flights

The agent should:

1. Call `search_flights_by_budget` to find flights from SFO to Tokyo under $2000
2. Call `search_hotels` to find hotels in Tokyo for those dates
3. Use the Code Interpreter to calculate total costs
4. Present a structured itinerary with flight options, hotel recommendations, and pricing

You should see tool invocations like `gw_tg-workshop-flights-mcp___search_flights_by_budget` in the chat — this confirms the tools are flowing through Module 3a (Tools Gateway)'s gateway.

## Test 2: Budget Comparison

Try a follow-up that tests multi-turn memory:

> Can you find me a cheaper hotel option? Under $100 per night.

The agent should remember the Tokyo trip context from the previous turn and call `search_hotels_by_budget` with the budget constraint.

## Test 3: Different Route

Test a different date to verify the mock data coverage:

> Search for flights from SFO to Tokyo for 2026-09-20

The agent should call `search_flights` and return results from the Flights MCP Lambda's mock data.

## Test 4: Code Interpreter

Test the Code Interpreter integration:

> Compare the total cost of the ANA flight + Sakura Inn vs the JAL flight + Tokyo Bay Resort for 2 guests, 3 nights. Show me a breakdown.

The agent should use the Code Interpreter to calculate and format the comparison — demonstrating that it can combine tool results with computation.

## Troubleshooting

If the agent does not respond, returns an error, or hangs, work through the checks below before raising the issue with a workshop instructor. Each row is a real failure mode observed during prior runs — the most likely cause is at the top.

| Symptom | Likely Cause | How to Verify |
|---------|--------------|---------------|
| Agent replies with "Model access denied" or Marketplace error | Bedrock Marketplace subscription gate — Claude Sonnet was not primed in this account | Return to the **Prime Anthropic Model Access** section in [Architecture and Prerequisites](../architecture/) and complete the priming steps for both Sonnet 4.5 and 4.6. |
| Agent replies but never calls any tool | Tools Gateway not wired — `GATEWAY_URL` SSM parameter points at FAST's built-in gateway instead of Module 3a's | `aws ssm get-parameter --name /FAST-stack/gateway_url --query Parameter.Value --output text` — confirm it matches `https://<your-gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp`. Re-run the SSM re-apply step on the `connect-gateway-mcp` or `connect-gateway-agentcore` page. |
| Agent calls a tool and gets `401 Unauthorized` | OAuth2 credential provider is missing or the agent IAM policy does not include the wildcarded OAuth2 secret ARN | `aws bedrock-agentcore-control list-oauth2-credential-providers --region $REGION` — confirm the provider exists. Then re-run the Python IAM-widening patch from the `connect-gateway-*` page. |
| Agent calls a tool and gets `AccessDeniedException` on `bedrock-agentcore:AuthorizeAction` | Gateway role missing `AuthorizeAction` on the policy engine (only affects the AgentCore path) | Redeploy the AgentCore stack — `workshop-agentcore-stack` now grants `AuthorizeAction` alongside `GetPolicyEngine`. |
| Agent response spins indefinitely | AgentCore Runtime invocation failed silently | Open CloudWatch Logs for `/aws/bedrock-agentcore/runtimes/FAST_stack_FASTAgent-*-DEFAULT` and look for the first stack trace. Stream the latest log group with `aws logs tail /aws/bedrock-agentcore/runtimes/FAST_stack_FASTAgent-DEFAULT --follow`. |

If none of the above resolves the issue, capture the log stream above and the output of `aws bedrock-agentcore-control get-agent-runtime --agent-runtime-arn "$AGENT_RUNTIME_ARN" --region $REGION` when raising the issue.

## What Just Happened

Every interaction exercised the full platform stack:

| Layer | What Happened |
|-------|--------------|
| **Frontend** | React app on Amplify sent the prompt to AgentCore Runtime |
| **User Auth** | Cognito JWT validated by the Runtime |
| **Model Call** | Routed through Module 2's LLM Gateway with virtual key budget tracking |
| **Tool Discovery** | Agent called `tools/list` on Module 3a (Tools Gateway)'s gateway via MCP |
| **Tool Invocation** | Gateway dispatched to Flights/Hotels Lambda targets |
| **M2M Auth** | Token Vault fetched JWT from Module 3a's Cognito for gateway auth |
| **Memory** | AgentCore Memory stored conversation turns for multi-turn context |
| **Code Interpreter** | Python executed in AgentCore's secure sandbox |
| **Observability** | OpenTelemetry traces exported to CloudWatch |

This is the complete lifecycle: platform governance → agent development → production deployment on AgentCore.

## Notebook Walkthrough (Optional alternative)

> Prefer an interactive notebook experience? The notebook below covers the same material as this page, with additional inline explanations and an advanced section on calling the runtime programmatically (the Cognito JWT authorizer means `boto3.invoke_agent_runtime()` does not work directly).
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4b-fast/notebooks/` and open **`05-run-the-agent.ipynb`**.
