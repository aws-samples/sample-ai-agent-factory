---
title: "Test the Gateway"
weight: 66
---

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

Invoke tools through the AgentCore Gateway with JWT authentication and verify the audit trail.

::alert[If you completed the previous step (Discover & Search) using the Consumer role, make sure you have returned to the base instance role before proceeding. Run the command below to restore it.]{type="warning"}

:::code{showCopyAction=true showLineNumbers=false language=bash}
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
echo "Current role: $(aws sts get-caller-identity --query Arn --output text)"
:::

## CLI Walkthrough

### Step 1: Get a Cognito M2M Token

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

SECRET=$(aws secretsmanager get-secret-value \
  --secret-id workshop-cognito-m2m-secret \
  --query SecretString --output text 2>/dev/null)

TOKEN_URL=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoTokenUrl'].Value" --output text)

if [ -z "$SECRET" ] || [ -z "$TOKEN_URL" ] || [ "$TOKEN_URL" = "None" ]; then
  echo "ERROR: SECRET or TOKEN_URL is empty - confirm workshop-registry-stack is CREATE_COMPLETE" >&2
else
  CLIENT_ID=$(echo "$SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin)['client_id'])")
  CLIENT_SECRET=$(echo "$SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")

  ACCESS_TOKEN=$(curl -s -X POST "$TOKEN_URL" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=client_credentials" \
    -d "client_id=$CLIENT_ID" \
    -d "client_secret=$CLIENT_SECRET" \
    -d "scope=mcp-servers-unrestricted/read mcp-servers-unrestricted/execute" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

  if [ -n "$ACCESS_TOKEN" ]; then
    echo "Token acquired: ${ACCESS_TOKEN:0:20}..."
  else
    echo "ERROR: Failed to acquire Cognito access token" >&2
  fi
fi
:::

### Step 2: Get the Gateway URL

:::code{showCopyAction=true showLineNumbers=false language=bash}
GATEWAY_ID=$(aws cloudformation describe-stacks \
  --stack-name workshop-agentcore-stack \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayId'].OutputValue" \
  --output text --region $REGION)

if [ -z "$GATEWAY_ID" ] || [ "$GATEWAY_ID" = "None" ]; then
  echo "ERROR: GATEWAY_ID is empty - confirm workshop-agentcore-stack is CREATE_COMPLETE" >&2
else
  GATEWAY_URL="https://${GATEWAY_ID}.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp"
  echo "Gateway URL: $GATEWAY_URL"
fi
:::

### Step 3: List Tools

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
tools = data.get('result', {}).get('tools', [])
print(f'Gateway tools: {len(tools)}')
for t in tools:
    print(f'  - {t[\"name\"]}')
"
:::

You should see the flights, hotels, and knowledge base tools from the pre-deployed gateway targets.

### Step 4: Call a Tool — search_flights

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "tg-workshop-flights-mcp___search_flights",
      "arguments": {"origin": "SFO", "destination": "TYO", "date": "2026-09-15"}
    }
  }' | python3 -m json.tool
:::

You should see flight results — SFO to Tokyo with prices and schedules.

### Step 5: Call Another Tool — search_hotels

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "tg-workshop-hotels-mcp___search_hotels",
      "arguments": {"city": "Tokyo", "check_in": "2026-09-15", "check_out": "2026-09-18", "guests": 2}
    }
  }' | python3 -m json.tool
:::

### Step 6: Verify Audit Logging

The request interceptor runs on every `tools/call`. Verify it was invoked:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws logs filter-log-events \
  --log-group-name "/aws/lambda/ac-gateway-request-interceptor" \
  --limit 3 --region $REGION \
  --start-time $(python3 -c "import time; print(int((time.time()-300)*1000))") \
  --query "events[].message" --output text > /tmp/interceptor-events.log
head -10 /tmp/interceptor-events.log
:::

You should see `START` and `END` entries confirming the interceptor executed. In a production setup, the interceptor would log structured audit entries (caller identity, tool name, timestamp) to CloudWatch or DynamoDB.

---

## Notebook Walkthrough (Optional alternative)

> This notebook (06-test-gateway.ipynb) is an alternative path covering the same material as the CLI section above — follow *either* path, you do not need to do both. The notebook covers additional topics including WorkloadIdentity token flow and OAuth2CredentialProvider setup for production M2M auth.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-3b-agentcore/notebooks/` and open **`06-test-gateway.ipynb`**.
