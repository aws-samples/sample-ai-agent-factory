---
title: "Create a Service Account"
weight: 45
---

Agents running on Amazon Bedrock AgentCore authenticate against the registry using **machine-to-machine (M2M) authentication** — an OAuth2 Client Credentials flow where the agent presents a client ID and secret to obtain an access token.

In this step you will:

1. Create a group (`workshop-agents`) that defines what the Travel Agent is allowed to access
2. Create an M2M service account for the AgentCore-hosted agent
3. Store the credentials in AWS Secrets Manager so AgentCore can retrieve them securely

## Create the Workshop Agents Group

Create the group via the Registry API:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cat > /tmp/group.json << 'EOF'
{
  "scope_name": "workshop-agents",
  "description": "Service accounts for AgentCore-hosted workshop agents",
  "server_access": [
    {"server": "workshop-flights-mcp", "methods": ["tools/list", "tools/call"], "tools": "*"},
    {"server": "workshop-hotels-mcp", "methods": ["tools/list", "tools/call"], "tools": "*"}
  ],
  "agent_access": ["/workshop-travel-agent"],
  "create_in_idp": false
}
EOF

curl -s -X POST "$REGISTRY_URL/api/servers/groups/import" \
  -H "Authorization: Bearer $REGISTRY_TOKEN" \
  -H "Content-Type: application/json" \
  -d @/tmp/group.json | python3 -m json.tool
:::

## Retrieve M2M Credentials

The workshop stack pre-created a Cognito M2M client for agent authentication. Retrieve the credentials:

:::code{showCopyAction=true showLineNumbers=false language=bash}
M2M_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id "workshop-cognito-m2m-secret" \
  --query 'SecretString' --output text)

M2M_CLIENT_ID=$(echo $M2M_SECRET | python3 -c "import sys,json; print(json.load(sys.stdin)['client_id'])")
M2M_CLIENT_SECRET=$(echo $M2M_SECRET | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")

echo "M2M Client ID: $M2M_CLIENT_ID"
echo "M2M Client Secret: ${M2M_CLIENT_SECRET:0:10}..."
:::

## Store Credentials in AWS Secrets Manager

The workshop platform already provisions two secrets during stack deployment:

| Secret name | Contents | Purpose |
|-------------|----------|---------|
| `workshop-registry-api-token` | `{ "api_token": "<token>" }` | Static bearer token for the Registry REST API (Path A) |
| `workshop-cognito-m2m-secret` | `{ "client_id": "...", "client_secret": "...", "token_url": "..." }` | Cognito M2M creds for the AgentCore Gateway (Path B) |

The secret is automatically populated by the workshop stack during deployment. The commands below retrieve the pre-stored M2M credentials from Secrets Manager so you can use them later:

:::code{showCopyAction=true showLineNumbers=false language=bash}
M2M_SECRET_ARN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoM2MClientSecretArn'].Value" \
  --output text --region $(aws configure get region))

aws secretsmanager put-secret-value \
  --secret-id "$M2M_SECRET_ARN" \
  --secret-string "{
    \"client_id\": \"$M2M_CLIENT_ID\",
    \"client_secret\": \"$M2M_CLIENT_SECRET\",
    \"token_url\": \"$REGISTRY_URL/auth/token\"
  }"
:::

Verify the secret was updated:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws secretsmanager get-secret-value \
  --secret-id "$M2M_SECRET_ARN" \
  --query 'SecretString' --output text | python3 -m json.tool
:::

The credentials are stored. Proceed to the **Verify and Hand Off** step to confirm everything works end-to-end.
