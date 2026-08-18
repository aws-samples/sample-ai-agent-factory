---
title: "Registry Overview"
weight: 42
---

Before registering anything, orient yourself — understand what's running, how to access it, and what's already there.

## Retrieve the registry URL and API token

Retrieve the registry URL from your CloudFormation stack outputs, and print a
console link for the admin password — the password itself stays in Secrets
Manager rather than passing through your shell:

:::code{showCopyAction=true showLineNumbers=false language=bash}
export REGISTRY_URL=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-MainCloudFrontUrl'].Value" \
  --output text)

export AWS_REGION=${AWS_REGION:-$(aws configure get region)}

# Every /api/ call in this module is authenticated. Without the token nginx
# answers 401 with an HTML body, and the `python3 -m json.tool` pipe the later
# steps use then fails with "Expecting value: line 1 column 1 (char 0)".
export REGISTRY_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id workshop-registry-api-token \
  --query SecretString --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['api_token'])")

echo "Registry URL:   $REGISTRY_URL"
echo "Registry token: ${REGISTRY_TOKEN:0:4}... (${#REGISTRY_TOKEN} chars)"
echo "Admin password: copy it from Secrets Manager —"
echo "  https://${AWS_REGION}.console.aws.amazon.com/secretsmanager/secret?name=workshop-admin-password&region=${AWS_REGION}"
:::

::alert[Environment variables live only in the shell you set them in. **If you open a new terminal during this module, re-run the block above** — the following pages all start with a one-line refresher that does it for you.]{type="info"}

You can also find these values in the CloudFormation console under the `workshop-registry-stack` **Outputs** tab — `MCPGatewayUrl` for the URL and `MCPGatewayAdminPassword` for a direct link to the password in Secrets Manager:

::alert[Confirm the console **region selector** (top-right) matches the region you deployed into before looking for the stack.]{type="info"}

:button[Open CloudFormation Console]{href="https://console.aws.amazon.com/cloudformation/home#/stacks" target="_blank" variant="primary"}

Verify the registry is healthy:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -fsS --max-time 10 "$REGISTRY_URL/health" | python3 -m json.tool
:::

Expected response:

```json
{
    "status": "healthy",
    "service": "mcp-gateway-registry",
    "deployment_mode": "with-gateway",
    "registry_mode": "full",
    "nginx_updates_enabled": true
}
```

## Log in to the registry UI

Open the registry UI in your browser:

:::code{showCopyAction=true showLineNumbers=false language=bash}
echo "Registry UI: $REGISTRY_URL"
:::

You will see the sign-in page. Select **Continue with AWS Cognito**:

![Registry sign-in page showing Continue with AWS Cognito and Admin Login options](/static/img/module-3/registry-login-options.png)

You need the admin password in your clipboard to type it into the browser login
form. Open the Secrets Manager link printed above, select **Retrieve secret
value**, then use the copy icon next to `admin_password`. Copying from the
console keeps the value out of your terminal scrollback and shell history.

::alert[The `workshop-registry-stack` **Outputs** tab links straight to this secret via `MCPGatewayAdminPassword` if you no longer have the link in your terminal.]{type="info"}

Enter the admin credentials:

- **Username:** `admin`
- **Password:** paste the value from above

![Admin login form with username and password fields](/static/img/module-3/registry-login-credentials.png)

After logging in, you will see the registry dashboard showing the pre-registered demo servers and agents:

![Registry dashboard showing pre-registered MCP servers and A2A agents](/static/img/module-3/registry-dashboard.png)

## Explore what's already registered

Take a moment to explore the pre-registered content:

1. Select the **MCP Servers** tab — you will see pre-registered servers including `Current Time API` and `Real Server Fake Tools`
2. Select the **A2A Agents** tab — you will see a pre-registered `workshop-order-processing-agent` stub
3. Try the semantic search bar — type "what time is it" and observe how the registry finds the CurrentTime server

::alert[The demo servers and agents are pre-registered to give you a working baseline. In the next steps, you will register the Flights and Hotels MCP servers that the Travel Agent needs.]{type="info"}

## Get an API token

Retrieve the static API token from Secrets Manager for API-based operations:

:::code{showCopyAction=true showLineNumbers=false language=bash}
export REGISTRY_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id "workshop-registry-api-token" \
  --query 'SecretString' --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['api_token'])")

echo "Token retrieved: ${#REGISTRY_TOKEN} chars (value not printed)"
:::

::alert[This uses the pre-provisioned static API token. For production, M2M service accounts use OAuth2 client credentials via `/auth/token` — you will set that up in the Service Account step.]{type="info"}

You are now ready to register the Flights and Hotels MCP servers.
