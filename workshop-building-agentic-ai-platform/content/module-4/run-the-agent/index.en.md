---
title: "Run the Agent"
weight: 76
---

The travel agent is fully connected — model calls flow through the LLM Gateway (Module 2) and tools are served by the AgentCore Gateway (Module 3a (Tools Gateway)). Time to plan some trips.

## Open the frontend

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

## Test 1: plan a trip

Type the following in the chat:

> Plan a trip from SFO to Tokyo for 2026-09-15 to 2026-09-18, 2 guests, budget $2000 for flights

The agent should:

1. Call `search_flights_by_budget` to find flights from SFO to Tokyo under $2000
2. Call `search_hotels` to find hotels in Tokyo for those dates
3. Use the Code Interpreter to calculate total costs
4. Present a structured itinerary with flight options, hotel recommendations, and pricing

You should see tool invocations like `gw_tg-workshop-flights-mcp___search_flights_by_budget` in the chat — this confirms the tools are flowing through the gateway rather than being answered from the model's own knowledge.

The name has two halves, and both are evidence. The prefix is the `prefix=` argument you gave `MCPClient` in `tools/gateway.py`, so it depends on which connect page you followed:

| Path you took | `prefix=` in `gateway.py` | Tool names look like |
|---------------|---------------------------|----------------------|
| **Module 3a** — [Connect to Tools Gateway (MCP)](../connect-gateway-mcp/) | `prefix="gw"` | `gw_tg-workshop-flights-mcp___search_flights` |
| **Module 3b** — [Connect to Tools Gateway (AgentCore)](../connect-gateway-agentcore/) | `prefix="gateway"` | `gateway_tg-workshop-flights-mcp___search_flights` |

The second half, `tg-workshop-flights-mcp`, is the name of the **gateway target** that served the call — so the tool name tells you which Lambda behind which gateway produced the answer.

## Test 2: budget comparison

Try a follow-up that tests multi-turn memory:

> Can you find me a cheaper hotel option? Under $100 per night.

The agent should remember the Tokyo trip context from the previous turn and call `search_hotels_by_budget` with the budget constraint.

## Test 3: different route

Test a different date to verify the mock data coverage:

> Search for flights from SFO to Tokyo for 2026-09-20

The agent should call `search_flights` and return results from the Flights MCP Lambda's mock data.

## Test 4: Code Interpreter

Test the Code Interpreter integration:

> Compare the total cost of the ANA flight + Sakura Inn vs the JAL flight + Tokyo Bay Resort for 2 guests, 3 nights. Show me a breakdown.

The agent should use the Code Interpreter to calculate and format the comparison — demonstrating that it can combine tool results with computation.

## Test 5: prove the governance path (automated)

Tests 1–4 tell you the agent *answers*. They do not tell you the answers were governed. A misconfigured model client falls back to calling Amazon Bedrock directly: the agent still replies correctly, the chat still looks right, and nothing routes through Module 2's LLM Gateway — no budget tracking, no guardrail, no attribution. That failure is invisible from the UI.

This block proves the path instead of trusting it. It reads the virtual key's spend, invokes the runtime over HTTPS, and re-reads the spend. If the number did not move, the model call bypassed the gateway:

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

RUNTIME_ARN=$(aws ssm get-parameter --name /FAST-stack/runtime-arn \
  --query Parameter.Value --output text --region $REGION)
USER_POOL_ID=$(aws ssm get-parameter --name /FAST-stack/cognito-user-pool-id \
  --query Parameter.Value --output text --region $REGION)
CLIENT_ID=$(aws ssm get-parameter --name /FAST-stack/cognito-user-pool-client-id \
  --query Parameter.Value --output text --region $REGION)
LLM_URL=$(aws ssm get-parameter --name /FAST-stack/llm_gateway_url \
  --query Parameter.Value --output text --region $REGION)
LLM_KEY=$(aws ssm get-parameter --name /FAST-stack/llm_gateway_key --with-decryption \
  --query Parameter.Value --output text --region $REGION)

# A throwaway user, so this never touches the password you set for
# workshop@example.com when you logged in to the frontend.
SMOKE_USER="smoketest-$(openssl rand -hex 4)@example.com"
SMOKE_PASS="$(openssl rand -base64 18 | tr -d '/+=')"'!Aa1'
aws cognito-idp admin-create-user --user-pool-id $USER_POOL_ID \
  --username "$SMOKE_USER" --message-action SUPPRESS \
  --user-attributes Name=email,Value="$SMOKE_USER" Name=email_verified,Value=true \
  --region $REGION >/dev/null
aws cognito-idp admin-set-user-password --user-pool-id $USER_POOL_ID \
  --username "$SMOKE_USER" --password "$SMOKE_PASS" --permanent --region $REGION

# Use the AccessToken, NOT the IdToken. The Runtime's JWT authorizer matches the
# token's `client_id` claim against its allowedClients list, and an IdToken has no
# `client_id` claim -- it is rejected with
#   "Claim 'client_id' value mismatch with configuration."
ACCESS_TOKEN=$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
  --client-id $CLIENT_ID \
  --auth-parameters USERNAME="$SMOKE_USER",PASSWORD="$SMOKE_PASS" \
  --query 'AuthenticationResult.AccessToken' --output text --region $REGION)

SPEND_BEFORE=$(curl -s "${LLM_URL%/}/key/info" -H "Authorization: Bearer $LLM_KEY" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["spend"])')
echo "LLM Gateway spend before: $SPEND_BEFORE"

# The session ID must be at least 33 characters. The entrypoint reads the prompt
# and the session ID from the JSON body, so both are sent in the payload too.
SESSION_ID="smoke-$(openssl rand -hex 16)-session"
ENCODED_ARN=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$RUNTIME_ARN")

HTTP=$(curl -s -o /tmp/smoke-response.txt -w '%{http_code}' -X POST \
  "https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${ENCODED_ARN}/invocations?qualifier=DEFAULT" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: $SESSION_ID" \
  -d "{\"prompt\": \"Search for flights from SFO to TYO on 2026-09-15\", \"runtimeSessionId\": \"$SESSION_ID\"}")
echo "Runtime invocation: HTTP $HTTP"

# Match both prefixes: `gw_` on the Module 3a path, `gateway_` on the Module 3b path.
echo "Gateway tools the agent loaded:"
grep -oE '"name": *"(gw|gateway)_tg-[A-Za-z0-9_-]+"' /tmp/smoke-response.txt | sort -u

sleep 8
SPEND_AFTER=$(curl -s "${LLM_URL%/}/key/info" -H "Authorization: Bearer $LLM_KEY" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["spend"])')
echo "LLM Gateway spend after:  $SPEND_AFTER"

python3 - "$SPEND_BEFORE" "$SPEND_AFTER" <<'PYEOF'
import sys
before, after = float(sys.argv[1]), float(sys.argv[2])
if after > before:
    print("PASS: spend rose %.6f -> %.6f - model calls are going through the LLM Gateway"
          % (before, after))
else:
    print("FAIL: spend did not move (%.6f). The agent answered but bypassed the gateway."
          % before)
    print("      Check _create_model() in travel_agent.py: api_base/api_key must be")
    print("      inside client_args={...}, not passed as top-level LiteLLMModel kwargs.")
PYEOF

aws cognito-idp admin-delete-user --user-pool-id $USER_POOL_ID \
  --username "$SMOKE_USER" --region $REGION && echo "Removed the throwaway user."
:::

Expected output: `HTTP 200`, the seven `tg-workshop-*` tool names (prefixed `gw_` or `gateway_` depending on the path you took), and a `PASS:` line showing the spend increased.

::alert[`HTTP 403` with `Claim 'client_id' value mismatch with configuration.` means an IdToken was used instead of an AccessToken. `Failed to start MCP client` in the response body means `/FAST-stack/gateway_url` points at a Gateway that no longer exists — re-run the SSM step on the *Connect the Gateway* page.]{type="info"}

## Troubleshooting

If the agent does not respond, returns an error, or hangs, work through the checks below before raising the issue with a workshop instructor. Each row is a real failure mode observed during prior runs — the most likely cause is at the top.

| Symptom | Likely Cause | How to Verify |
|---------|--------------|---------------|
| Agent replies with "Model access denied" or Marketplace error | Bedrock Marketplace subscription gate — Claude Sonnet was not primed in this account | Return to the **Prime Anthropic model access** section in [Architecture and Prerequisites](../architecture/) and complete the priming steps for both Sonnet 4.5 and 4.6. |
| Agent replies but never calls any tool | Tools Gateway not wired — `GATEWAY_URL` SSM parameter points at FAST's built-in gateway instead of Module 3a's | `aws ssm get-parameter --name /FAST-stack/gateway_url --query Parameter.Value --output text` — confirm it matches `https://<your-gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp`. Re-run the SSM re-apply step on the `connect-gateway-mcp` or `connect-gateway-agentcore` page. |
| Agent calls a tool and gets `401 Unauthorized` | OAuth2 credential provider is missing or the agent IAM policy does not include the wildcarded OAuth2 secret ARN | `aws bedrock-agentcore-control list-oauth2-credential-providers --region $REGION` — confirm the provider exists. Then re-run the Python IAM-widening patch from the `connect-gateway-*` page. |
| Agent calls a tool and gets `AccessDeniedException` on `bedrock-agentcore:AuthorizeAction` | Gateway role missing `AuthorizeAction` on the policy engine (only affects the AgentCore path) | Redeploy the AgentCore stack — `workshop-agentcore-stack` now grants `AuthorizeAction` alongside `GetPolicyEngine`. |
| Agent response spins indefinitely | AgentCore Runtime invocation failed silently | Open CloudWatch Logs for `/aws/bedrock-agentcore/runtimes/FAST_stack_FASTAgent-*-DEFAULT` and look for the first stack trace. Stream the latest log group with `aws logs tail /aws/bedrock-agentcore/runtimes/FAST_stack_FASTAgent-DEFAULT --follow`. |

If none of the above resolves the issue, capture the log stream above and the output of the command below when raising the issue. `get-agent-runtime` takes the runtime **ID**, not the ARN, so the ID is sliced off the end of the ARN held in SSM:

:::code{showCopyAction=true showLineNumbers=false language=bash}
RUNTIME_ARN=$(aws ssm get-parameter --name /FAST-stack/runtime-arn \
  --query Parameter.Value --output text --region $REGION)

aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id "${RUNTIME_ARN##*/}" \
  --region $REGION
:::

## What just happened

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

## Notebook walkthrough (optional alternative)

> Prefer an interactive notebook experience? The notebook below covers the same material as this page, with additional inline explanations and an advanced section on calling the runtime programmatically. Note that `boto3.invoke_agent_runtime()` signs with SigV4, which this Runtime rejects because it is configured with a Cognito JWT authorizer — so the programmatic path uses a plain HTTPS `POST` with a Cognito **AccessToken** in the `Authorization` header, exactly as in Test 5 above.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4b-fast/notebooks/` and open **`05-run-the-agent.ipynb`**.
