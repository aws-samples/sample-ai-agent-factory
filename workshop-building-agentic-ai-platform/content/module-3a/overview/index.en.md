---
title: "Registry Overview"
weight: 42
---

Before registering anything, orient yourself — understand what's running, how to access it, and what's already there.

## Retrieve the Registry URL

Retrieve the registry URL and admin password from your CloudFormation stack outputs:

:::code{showCopyAction=true showLineNumbers=false language=bash}
export REGISTRY_URL=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-MainCloudFrontUrl'].Value" \
  --output text)

export REGISTRY_ADMIN_PASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id "workshop-admin-password" \
  --query 'SecretString' --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['admin_password'])")

echo "Registry URL:    $REGISTRY_URL"
echo "Admin password:  $REGISTRY_ADMIN_PASSWORD"
:::

You can also find these values in the CloudFormation console under the `workshop-registry-stack` **Outputs** tab — `MCPGatewayUrl` for the URL and `MCPGatewayAdminPassword` for a direct link to the password in Secrets Manager:

::alert[Confirm the console **region selector** (top-right) matches the region you deployed into before looking for the stack.]{type="info"}

:button[Open CloudFormation Console]{href="https://console.aws.amazon.com/cloudformation/home#/stacks" target="_blank" variant="primary"}

Verify the registry is healthy:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s $REGISTRY_URL/health | python3 -m json.tool
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

## Log In to the Registry UI

Open the registry UI in your browser:

:::code{showCopyAction=true showLineNumbers=false language=bash}
echo "Registry UI: $REGISTRY_URL"
:::

You will see the sign-in page. Select **Continue with AWS Cognito**:

![Registry sign-in page showing Continue with AWS Cognito and Admin Login options](/static/img/module-3/registry-login-options.png)

Copy the admin password to your clipboard:

:::code{showCopyAction=true showLineNumbers=false language=bash}
echo "$REGISTRY_ADMIN_PASSWORD"
:::

Enter the admin credentials:

- **Username:** `admin`
- **Password:** paste the value from above

![Admin login form with username and password fields](/static/img/module-3/registry-login-credentials.png)

After logging in, you will see the registry dashboard showing the pre-registered demo servers and agents:

![Registry dashboard showing pre-registered MCP servers and A2A agents](/static/img/module-3/registry-dashboard.png)

## Explore What's Already Registered

Take a moment to explore the pre-registered content:

1. Select the **MCP Servers** tab — you will see pre-registered servers including `Current Time API` and `Real Server Fake Tools`
2. Select the **A2A Agents** tab — you will see a pre-registered `workshop-order-processing-agent` stub
3. Try the semantic search bar — type "what time is it" and observe how the registry finds the CurrentTime server

::alert[The demo servers and agents are pre-registered to give you a working baseline. In the next steps, you will register the Flights and Hotels MCP servers that the Travel Agent needs.]{type="info"}

## Get an API Token

Retrieve the static API token from Secrets Manager for API-based operations:

:::code{showCopyAction=true showLineNumbers=false language=bash}
export REGISTRY_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id "workshop-registry-api-token" \
  --query 'SecretString' --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['api_token'])")

echo "Token retrieved: ${REGISTRY_TOKEN:0:20}..."
:::

::alert[This uses the pre-provisioned static API token. For production, M2M service accounts use OAuth2 client credentials via `/auth/token` — you will set that up in the Service Account step.]{type="info"}

You are now ready to register the Flights and Hotels MCP servers.
