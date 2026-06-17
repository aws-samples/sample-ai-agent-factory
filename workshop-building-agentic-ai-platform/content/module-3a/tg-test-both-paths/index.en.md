---
title: "Tools Gateway: Test Both Paths"
weight: 52
---

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

Run real requests through both paths to compare performance, audit, and governance capabilities.

## What You'll Compare

| Capability | Path A (NGINX) | Path B (Gateway) |
|-----------|----------------|-------------------|
| Latency | ~50ms | ~200-400ms |
| Authentication | Static token | Cognito JWT |
| Audit trail | NGINX logs | CloudWatch structured audit log |
| Content screening | None | Bedrock Guardrails |
| Access control | None | Group-based filtering |
| Lambda tools | Not supported | Native invocation |
| MCP protocol | Standard | Standard |

## Group-Based Access Control

When `TOOL_ACCESS_POLICY` is configured on the interceptor Lambdas, access is filtered by Cognito groups:

```json
{
  "gateway-admins": ["*"],
  "gateway-developers": ["flights-*", "hotels-*", "search-*"]
}
```

| Caller Group | `tools/list` Response | `tools/call` Behavior |
|-------------|----------------------|----------------------|
| `gateway-admins` | Sees all tools | Can call any tool |
| `gateway-developers` | Sees only matching tools | Blocked on unauthorized tools |
| No group | Sees nothing (if policy set) | Blocked on all tools |

The request interceptor blocks unauthorized `tools/call` with an MCP error (`-32600`). The response interceptor filters `tools/list` results.

## Key Insight

Path A is faster. Path B is governed. Both coexist -- the agent (or platform policy) chooses which path to use based on the use case. Production agents in workstream accounts should use Path B; internal tooling can use Path A.

## CLI Walkthrough

### Step 1: Set up URLs and authentication

Path A uses a static API token (accepted by the Registry auth layer). Path B uses a Cognito JWT (required by the AgentCore Gateway's CUSTOM_JWT authorizer). Both tokens are retrieved below.

::alert[**Do not use `set -eo pipefail` in interactive terminal blocks** on pages like this one that leak variables into later blocks. `pipefail` is a shell option that persists across blocks in the same terminal session, so a later benign `SIGPIPE` (e.g. from `json.tool | head`) would terminate the participant's shell. The block below uses explicit `test -n ...` guards instead — a missing export or empty token fails loudly without polluting the shell.]{type="info"}

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

# --- URLs ---
REGISTRY_URL=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-RegistryUrl'].Value" \
  --output text --region $REGION)

GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
  --query "items[?name=='tools-gateway'].gatewayId" \
  --output text --region $REGION)

GATEWAY_URL="https://${GATEWAY_ID}.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp"

# --- Path A token (static API token for Registry/NGINX) ---
STATIC_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id workshop-registry-api-token \
  --query SecretString --output text --region $REGION \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['api_token'])")

# --- Path B token (Cognito JWT for AgentCore Gateway) ---
M2M_SECRET_ARN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoM2MClientSecretArn'].Value" \
  --output text --region $REGION)

M2M_JSON=$(aws secretsmanager get-secret-value \
  --secret-id "$M2M_SECRET_ARN" \
  --query SecretString --output text --region $REGION)

M2M_CLIENT_ID=$(echo "$M2M_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['client_id'])")
M2M_CLIENT_SECRET=$(echo "$M2M_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")

COGNITO_DOMAIN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoDomain'].Value" \
  --output text --region $REGION)

GATEWAY_TOKEN=$(curl -s -X POST \
  "https://${COGNITO_DOMAIN}.auth.${REGION}.amazoncognito.com/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -u "${M2M_CLIENT_ID}:${M2M_CLIENT_SECRET}" \
  -d "grant_type=client_credentials&scope=mcp-servers-unrestricted/read mcp-servers-unrestricted/execute" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Fail loudly if any required value is empty — without exiting the shell.
for V in REGISTRY_URL GATEWAY_ID GATEWAY_URL STATIC_TOKEN GATEWAY_TOKEN; do
  if [ -z "${!V}" ] || [ "${!V}" = "None" ]; then
    echo "ERROR: $V is empty - confirm all four workshop stacks show CREATE_COMPLETE" >&2
  fi
done

echo "Registry URL (Path A):   $REGISTRY_URL"
echo "Gateway URL (Path B):    $GATEWAY_URL"
echo "Gateway ID:              $GATEWAY_ID"
echo "Cognito JWT:             ${GATEWAY_TOKEN:0:20}..."
:::

### Step 2: Path A -- List tools via the Registry API through NGINX

:::code{showCopyAction=true showLineNumbers=false language=bash}
echo "=== PATH A: Registry API via NGINX ==="
curl -s -o /tmp/pathA.json -w "Latency: %{time_total}s\n" \
  -H "Authorization: Bearer $STATIC_TOKEN" \
  "$REGISTRY_URL/api/servers"
python3 -c "import json,sys; print('\n'.join(json.dumps(json.load(open('/tmp/pathA.json')), indent=2).splitlines()[:40]))"
:::

::alert[This command shows the first 40 lines of registered tools for illustration. The full listing of all tools available through this Registry endpoint is visible in the next step via the AgentCore Gateway.]{type="info"}

### Step 3: Path B -- List tools via the AgentCore Gateway

First, list available gateway targets:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws bedrock-agentcore-control list-gateway-targets \
  --gateway-identifier $GATEWAY_ID \
  --query "items[].name" \
  --output table --region $REGION
:::

Then call `tools/list` on a target through the gateway:

:::code{showCopyAction=true showLineNumbers=false language=bash}
echo "=== PATH B: tools/list via AgentCore Gateway ==="
curl -s -o /tmp/pathB-list.json -w "Latency: %{time_total}s\n" \
  -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -H "x-amz-agentcore-target: tg-workshop-flights-mcp" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {}
  }'
python3 -m json.tool < /tmp/pathB-list.json
:::

::alert[The `tools/list` response returns namespaced tool names (e.g., `tg-workshop-flights-mcp___search_flights`). Use the full namespaced name when calling `tools/call`.]{type="info"}

### Step 4: Path B -- Call a specific tool

Invoke `search_flights` through the full interceptor chain (request interceptor logs the call, Lambda target executes, response interceptor applies guardrails). Note the namespaced tool name:

:::code{showCopyAction=true showLineNumbers=false language=bash}
echo "=== PATH B: tools/call via AgentCore Gateway ==="
curl -s -o /tmp/pathB-call.json -w "Latency: %{time_total}s\n" \
  -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -H "x-amz-agentcore-target: tg-workshop-flights-mcp" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "tg-workshop-flights-mcp___search_flights",
      "arguments": {"origin": "SFO", "destination": "TYO", "date": "2026-09-15"}
    }
  }'
python3 -m json.tool < /tmp/pathB-call.json
:::

### Step 5: Gateway-only tools (Lambda targets)

Call the `search-knowledge-base` Lambda tool -- this can only be reached through Path B:

:::code{showCopyAction=true showLineNumbers=false language=bash}
echo "=== PATH B: Lambda-only tool (search-knowledge-base) ==="
curl -s -o /tmp/pathB-kb.json -w "Latency: %{time_total}s\n" \
  -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -H "x-amz-agentcore-target: tg-search-knowledge-base" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "tg-search-knowledge-base___search-knowledge-base",
      "arguments": {"query": "shipping policy", "max_results": 3}
    }
  }'
python3 -m json.tool < /tmp/pathB-kb.json
:::

This tool uses a `lambda://` URL, so NGINX has no route to it. Only the AgentCore Gateway can invoke Lambda targets natively.

### Check the audit log (optional)

The request interceptor writes structured audit logs to CloudWatch. To verify:

:::code{showCopyAction=true showLineNumbers=false language=bash}
LOG_GROUP="/aws/lambda/agentcore-gateway-request-interceptor"

# The interceptor may not have written a log stream yet (it only logs once it has
# handled a request). Resolve the latest stream first and only fetch events if one
# exists, so this optional check never errors when there is nothing to show yet.
STREAM=$(aws logs describe-log-streams \
  --log-group-name "$LOG_GROUP" \
  --order-by LastEventTime --descending --limit 1 \
  --query "logStreams[0].logStreamName" \
  --output text --region $REGION 2>/dev/null)

if [ -n "$STREAM" ] && [ "$STREAM" != "None" ]; then
  aws logs get-log-events \
    --log-group-name "$LOG_GROUP" \
    --log-stream-name "$STREAM" \
    --limit 5 --region $REGION \
    --query "events[].message" --output text
else
  echo "No interceptor log stream yet — invoke a tool through the gateway, then re-run."
fi
:::

---

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4a-tools-gateway/notebooks/` and open the corresponding notebook.

Open **Notebook 05 -- Test Both Paths Side-by-Side** (`05-test-both-paths.ipynb`).

This notebook covers five steps:

1. **Setup** -- gathers CloudFront URL, Registry URL, Gateway URL, and authenticates with the static API token
2. **Path A** -- lists tools via the Registry API through NGINX, measuring latency
3. **Path B -- tools/list** -- same request through the AgentCore Gateway with JWT auth, showing the response interceptor stripping internal fields
4. **Path B -- tools/call** -- invokes `search_flights` through the full request/response interceptor chain
5. **Gateway-only tools** -- calls `search-knowledge-base` (Lambda-backed) through Path B, demonstrating that NGINX cannot serve Lambda targets
