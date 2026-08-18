---
title: "Create a Service Account"
weight: 45
---

Agents running on Amazon Bedrock AgentCore authenticate against the registry using **machine-to-machine (M2M) authentication** — an OAuth2 Client Credentials flow where the agent presents a client ID and secret to obtain an access token.

In this step you will:

1. Create a group (`workshop-agents`) that defines what the Travel Agent is allowed to access
2. Create an M2M service account for the AgentCore-hosted agent
3. Store the credentials in AWS Secrets Manager so AgentCore can retrieve them securely

## Create the workshop agents group

Create the group via the Registry API:

:::code{showCopyAction=true showLineNumbers=false language=bash}
# Re-derive the registry variables if this is a fresh terminal (a no-op otherwise).
# Without REGISTRY_TOKEN the Registry answers 401 with an HTML body and the
# `python3 -m json.tool` pipes below fail on "Expecting value: line 1 column 1".
export AWS_REGION=${AWS_REGION:-$(aws configure get region)}
if [ -z "$REGISTRY_URL" ]; then
  export REGISTRY_URL=$(aws cloudformation list-exports \
    --query "Exports[?Name=='workshop-MainCloudFrontUrl'].Value" --output text)
fi
# `sys.stdout.write`, not `print`: the token is captured by the command
# substitution and never echoed, so it stays out of your scrollback. Only the
# field name reaches your shell history, never the value.
if [ -z "$REGISTRY_TOKEN" ]; then
  export REGISTRY_TOKEN=$(aws secretsmanager get-secret-value \
    --secret-id workshop-registry-api-token --query SecretString --output text \
    | python3 -c "import sys,json; sys.stdout.write(json.load(sys.stdin)['api_token'])")
fi

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

## Retrieve M2M credentials

The workshop stack pre-created a Cognito M2M client for agent authentication. Retrieve the credentials:

:::code{showCopyAction=true showLineNumbers=false language=bash}
# Read one field at a time straight out of Secrets Manager. The whole secret
# JSON never lands in a shell variable, so there is nothing to accidentally
# `echo` later, and `sys.stdout.write` keeps the value inside the command
# substitution instead of your scrollback.
get_secret_field() {
  aws secretsmanager get-secret-value --secret-id "$1" \
    --query 'SecretString' --output text \
    | python3 -c "import sys,json; sys.stdout.write(json.load(sys.stdin)['$2'])"
}

M2M_CLIENT_ID=$(get_secret_field workshop-cognito-m2m-secret client_id)
M2M_CLIENT_SECRET=$(get_secret_field workshop-cognito-m2m-secret client_secret)

echo "M2M Client ID: $M2M_CLIENT_ID"
# Confirm the secret was parsed without echoing any of it — the value stays in
# the shell variable for the commands below, and out of your scrollback.
echo "M2M Client Secret: $([ -n "$M2M_CLIENT_SECRET" ] && echo "[retrieved, ${#M2M_CLIENT_SECRET} chars]" || echo "MISSING")"

# Say so here rather than letting an empty credential travel onward. Without this
# the retrieve "succeeds", and the first thing that actually notices is Module 4,
# two modules away, with an opaque "clientId is required" from a service you have
# not opened yet.
if [ -z "$M2M_CLIENT_ID" ] || [ -z "$M2M_CLIENT_SECRET" ]; then
  echo
  echo "ERROR: workshop-cognito-m2m-secret has empty fields."
  echo "       The registry stack populates it at deploy time, so an empty value"
  echo "       means something overwrote it. Do not continue — the gateway and the"
  echo "       Module 4 agent both authenticate with these credentials."
  false
fi
:::

## Where the credentials live

The workshop platform already provisions two secrets during stack deployment:

| Secret name | Contents | Purpose |
|-------------|----------|---------|
| `workshop-registry-api-token` | `{ "api_token": "<token>" }` | Static bearer token for the Registry REST API (Path A) |
| `workshop-cognito-m2m-secret` | `{ "client_id": "...", "client_secret": "...", "token_url": "..." }` | Cognito M2M creds for the AgentCore Gateway (Path B) |

Both are written by the registry stack, so there is nothing for you to store here. Confirm the shape instead:

:::code{showCopyAction=true showLineNumbers=false language=bash}
M2M_SECRET_ARN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoM2MClientSecretArn'].Value" \
  --output text --region $(aws configure get region))

# Confirm the shape, not the contents. Printing the SecretString itself would
# put client_secret on your screen and in your scrollback, which is exactly what
# storing it in Secrets Manager is meant to avoid.
aws secretsmanager get-secret-value \
  --secret-id "$M2M_SECRET_ARN" \
  --query 'SecretString' --output text \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('\n'.join(f'{k}: [{len(str(v))} chars]' for k,v in sorted(d.items())))"
:::

All three fields should report a non-zero length, and `token_url` should end in
`.amazoncognito.com/oauth2/token` — the Cognito endpoint the M2M client authenticates against.

::alert[**Never write this secret back from your shell.** It is the platform's only copy of the M2M client secret, and Cognito will not re-issue it. A `put-secret-value` built from shell variables destroys the credentials the moment one of those variables is empty — which happens as soon as you run the write in a new terminal, or on its own, without the retrieve above. Nothing fails at that point: the damage only surfaces two modules later, when Module 4 reports `clientId is required for CLIENT_SECRET_BASIC`. This is the general rule for platform-owned secrets — the stack that creates a credential is the only thing that should write it.]{type="warning"}

Proceed to the **Verify and Hand Off** step to confirm everything works end-to-end.
