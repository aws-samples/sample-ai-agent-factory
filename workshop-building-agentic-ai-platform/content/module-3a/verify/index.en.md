---
title: "Verify and Hand Off"
weight: 46
---

Before moving to the Tools Gateway, run a quick verification to confirm everything is in place.

## Verification checklist

### 1. MCP servers are registered

:::code{showCopyAction=true showLineNumbers=false language=bash}
# Re-derive the registry variables if this is a fresh terminal (a no-op otherwise).
# Without REGISTRY_TOKEN the Registry answers 401 with an HTML body and the
# `python3 -m json.tool` pipes below fail on "Expecting value: line 1 column 1".
export AWS_REGION=${AWS_REGION:-$(aws configure get region)}
if [ -z "$REGISTRY_URL" ]; then
  export REGISTRY_URL=$(aws cloudformation list-exports \
    --query "Exports[?Name=='workshop-MainCloudFrontUrl'].Value" --output text)
fi
if [ -z "$REGISTRY_TOKEN" ]; then
  export REGISTRY_TOKEN=$(aws secretsmanager get-secret-value \
    --secret-id workshop-registry-api-token --query SecretString --output text \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['api_token'])")
fi

curl -s "$REGISTRY_URL/api/servers" \
  -H "Authorization: Bearer $REGISTRY_TOKEN" \
  | python3 -c "
import sys, json
servers = json.load(sys.stdin).get('servers', [])
for name in ['flights-mcp', 'hotels-mcp']:
    found = [s for s in servers if name in s.get('path', '')]
    status = 'PASS' if found else 'FAIL'
    print(f'  {status}: workshop-{name}')
"
:::

Expected: both `PASS`.

### 2. Static token authenticates to registry

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "$REGISTRY_URL/api/auth/me" \
  -H "Authorization: Bearer $REGISTRY_TOKEN" \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'  Auth method: {data.get(\"auth_method\", \"?\")}')
print(f'  Admin: {data.get(\"is_admin\", False)}')
print(f'  PASS' if data.get('is_admin') else '  FAIL')
"
:::

### 3. Cognito M2M token works

Verify that the M2M credentials can obtain a Cognito token (used by the Tools Gateway):

:::code{showCopyAction=true showLineNumbers=false language=bash}
COGNITO_DOMAIN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoDomain'].Value" \
  --output text)

REGION=$(aws configure get region)

# Re-read the M2M credentials from Secrets Manager rather than relying on the
# shell variables set on the previous page: this check has to be able to fail
# for the right reason, and empty credentials look identical to bad ones.
M2M_SECRET_ARN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoM2MClientSecretArn'].Value" \
  --output text)

M2M_JSON=$(aws secretsmanager get-secret-value \
  --secret-id "$M2M_SECRET_ARN" \
  --query SecretString --output text)

M2M_CLIENT_ID=$(echo "$M2M_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['client_id'])")
M2M_CLIENT_SECRET=$(echo "$M2M_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")

M2M_TOKEN=$(curl -s -X POST \
  "https://${COGNITO_DOMAIN}.auth.${REGION}.amazoncognito.com/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&client_id=${M2M_CLIENT_ID}&client_secret=${M2M_CLIENT_SECRET}&scope=mcp-servers-unrestricted/read mcp-servers-unrestricted/execute" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

if [ -n "$M2M_TOKEN" ]; then
  echo "  PASS: M2M Token acquired (${M2M_TOKEN:0:20}...)"
else
  echo "  FAIL: Could not acquire M2M token"
fi
:::

::alert[The static token authenticates to the **Registry API**. The Cognito M2M token authenticates to the **AgentCore Gateway**. Both are stored in the agent credentials secret for use in later modules.]{type="info"}

### 4. Credentials are in Secrets Manager

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws secretsmanager describe-secret \
  --secret-id "workshop-registry-api-token" \
  --query "Name" --output text
:::

Expected: `workshop-registry-api-token`

## What you have built

| What | Where |
|------|-------|
| Flights tool | Registered as `workshop-flights-mcp` MCP server |
| Hotels tool | Registered as `workshop-hotels-mcp` MCP server |
| Travel Agent | Registered as `workshop-travel-agent` A2A agent card |
| Access control | `workshop-agents` group with scoped permissions |
| Agent identity | `workshop-travel-agent-sa` service account |
| Credentials | Registry API token in Secrets Manager at `workshop-registry-api-token`; Cognito M2M creds in `workshop-cognito-m2m-secret` |

## Hand-off

Share the following with the developer team:

:::code{showCopyAction=true showLineNumbers=false language=bash}
echo "=== Module 3a Hand-Off ==="
echo "Registry URL:        $REGISTRY_URL"
echo "Flights MCP:         workshop-flights-mcp"
echo "Hotels MCP:          workshop-hotels-mcp"
echo "Travel Agent:        workshop-travel-agent"
echo "API Token Secret:    workshop-registry-api-token"
echo "M2M Secret Export:   workshop-CognitoM2MClientSecretArn"
echo "========================="
:::

## What's next

The Tools Gateway section adds runtime governance on top of the Registry — Bedrock Guardrails, request interceptors, and audit logging for every tool call. Proceed to **Tools Gateway: Introduction**.
