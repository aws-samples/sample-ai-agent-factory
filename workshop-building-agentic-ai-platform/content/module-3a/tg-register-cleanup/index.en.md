---
title: "Tools Gateway: Register + Cleanup"
weight: 54
---

## Register the Gateway as a Discoverable Service (Optional)

The AgentCore Gateway can be registered in the MCP Registry so agents can discover the governed path dynamically:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
  --query "items[?name=='tools-gateway'].gatewayId" \
  --output text --region $REGION)

GATEWAY_URL="https://${GATEWAY_ID}.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp"

echo "Gateway ID:  $GATEWAY_ID"
echo "Gateway URL: $GATEWAY_URL"
:::

### Step 2: Authenticate with the Registry

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGISTRY_URL=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-RegistryUrl'].Value" \
  --output text --region $REGION)

REGISTRY_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id workshop-registry-api-token \
  --query SecretString --output text --region $REGION \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['api_token'])")
:::

### Step 3: Register the gateway as an MCP server

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s -X POST "$REGISTRY_URL/api/servers/register" \
  -H "Authorization: Bearer $REGISTRY_TOKEN" \
  -d "name=agentcore-gateway" \
  -d "description=Governed MCP endpoint with JWT auth, audit logging, and Bedrock Guardrails" \
  -d "path=/mcp" \
  -d "proxy_pass_url=$GATEWAY_URL" \
  -d "tags=agentcore,gateway,governed" \
  -d "status=active" \
  | python3 -m json.tool
:::

This is composability: the gateway is both infrastructure AND a discoverable service.

## Cleanup

::alert[If you plan to continue to Module 4, **do not clean up** — Module 4 depends on the gateway and tools registered here. Follow the [Workshop Cleanup](../../cleanup/) instructions when you are completely finished.]{type="warning"}

## What You Built

| Component | Purpose |
|-----------|---------|
| **AgentCore Gateway** | Governed MCP endpoint with JWT auth, interceptors |
| **3 Gateway Targets** | Flights MCP, Hotels MCP, search-knowledge-base |
| **Request Interceptor** | Audit logging + group-based access control |
| **Response Interceptor** | Bedrock Guardrails on tool output |
| **Sync Lambda** | Automated Registry → Gateway bridge |

The Tools Gateway transformed the MCP Registry from a tool catalog into a **governed solution** — the foundation for the Travel Agent in Module 4.
