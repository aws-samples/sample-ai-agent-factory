---
title: "Connect to Tools Gateway (MCP path)"
weight: 74
---

Connect the travel agent to the Tools Gateway from Module 3a. This step wires the agent to the `tools-gateway` which has the Flights and Hotels tools synced from the MCP Registry.

::alert[Follow this page if you completed **Module 3a** (OSS MCP Registry + Tools Gateway), or you're on **Track 1 (fast path)** using the MCP registry. If you're on the AgentCore path (completed Module 3b), use the [AgentCore Path](../connect-gateway-agentcore/) instead.]{type="info"}

## The Auth Challenge

FAST's agent authenticates to FAST's own gateway using FAST's Cognito pool. Module 3a's gateway expects JWTs from Module 3a's Cognito pool — a different identity provider.

The solution: register a new **OAuth2 Credential Provider** in AgentCore Identity's Token Vault that points at Module 3a's Cognito.

## Retrieve Module 3a Credentials

Look up the M2M client credentials from Module 3a (MCP Registry)'s Cognito:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

M2M_CLIENT_ID=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoM2MClientId'].Value" \
  --output text --region $REGION)

M2M_SECRET_ARN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoM2MClientSecretArn'].Value" \
  --output text --region $REGION)

M2M_CLIENT_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id "$M2M_SECRET_ARN" \
  --query SecretString --output text --region $REGION \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")

COGNITO_POOL_ID=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoUserPoolId'].Value" \
  --output text --region $REGION)

DISCOVERY_URL="https://cognito-idp.${REGION}.amazonaws.com/${COGNITO_POOL_ID}/.well-known/openid-configuration"

echo "M2M Client ID:  $M2M_CLIENT_ID"
echo "Discovery URL:  $DISCOVERY_URL"
:::

## Create the OAuth2 Credential Provider

::alert[If you see "already exists", the provider was created in a previous step — skip to **Update the Gateway Client** below.]{type="info"}

Register a credential provider in AgentCore Identity's Token Vault. This tells the Token Vault how to get tokens from Module 3a (MCP Registry)'s Cognito:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws bedrock-agentcore-control create-oauth2-credential-provider \
  --name "workshop-tools-gateway-auth" \
  --credential-provider-vendor "CustomOauth2" \
  --oauth2-provider-config-input "{
    \"customOauth2ProviderConfig\": {
      \"oauthDiscovery\": {
        \"discoveryUrl\": \"${DISCOVERY_URL}\"
      },
      \"clientId\": \"${M2M_CLIENT_ID}\",
      \"clientSecret\": \"${M2M_CLIENT_SECRET}\"
    }
  }" --region $REGION
:::

You should see a response with the provider ARN and a `callbackUrl`.

This provider stores:
- **Where to get tokens** — Module 3a (MCP Registry)'s Cognito OIDC discovery URL
- **Which client** — Module 3a (MCP Registry)'s M2M client ID
- **The secret** — stored securely in Secrets Manager by AgentCore Identity

## Update the Gateway Client

The agent's `tools/gateway.py` needs to use the new credential provider. Replace it with a version that reads the provider name from SSM (so you can switch providers without redeploying):

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent
cat > patterns/strands-travel-agent/tools/gateway.py << 'PYEOF'
"""AgentCore Gateway MCP client with OAuth2 authentication."""

import logging
import os

from bedrock_agentcore.identity.auth import requires_access_token
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient
from utils.ssm import get_ssm_parameter

logger = logging.getLogger(__name__)

# Resolve credential provider: SSM override takes priority over env var.
_stack_name = os.environ.get("STACK_NAME", "")
try:
    _provider_name = get_ssm_parameter(f"/{_stack_name}/gateway_credential_provider")
    logger.info("[GATEWAY] Using credential provider from SSM: %s", _provider_name)
except Exception:
    _provider_name = os.environ.get("GATEWAY_CREDENTIAL_PROVIDER_NAME", "")
    logger.info("[GATEWAY] Using credential provider from env: %s", _provider_name)


@requires_access_token(provider_name=_provider_name, auth_flow="M2M", scopes=[])
def _fetch_gateway_token(access_token: str) -> str:
    """Fetch OAuth2 token for Gateway authentication via AgentCore Identity Token Vault."""
    return access_token


def create_gateway_mcp_client() -> MCPClient:
    """Create MCP client for AgentCore Gateway with OAuth2 authentication."""
    stack_name = os.environ.get("STACK_NAME")
    if not stack_name:
        raise ValueError("STACK_NAME environment variable is required")
    if not stack_name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid STACK_NAME format")
    gateway_url = get_ssm_parameter(f"/{stack_name}/gateway_url")
    logger.info("[GATEWAY] URL: %s", gateway_url)
    return MCPClient(
        lambda: streamablehttp_client(
            url=gateway_url,
            headers={"Authorization": f"Bearer {_fetch_gateway_token()}"},
        ),
        prefix="gw",
    )
PYEOF
:::

Verify the file is valid:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent
python3 -c "import ast; ast.parse(open('patterns/strands-travel-agent/tools/gateway.py').read()); print('✓ gateway.py syntax OK')"
:::

The key change: the credential provider name is read from SSM (`/FAST-stack/gateway_credential_provider`) instead of being hardcoded to FAST's provider. This lets the agent authenticate to Module 3a (Tools Gateway)'s gateway using Module 3a (MCP Registry)'s Cognito.

## Store the Gateway Configuration in SSM

Point the agent at Module 3a (Tools Gateway)'s gateway and the new credential provider:

:::code{showCopyAction=true showLineNumbers=false language=bash}
GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
  --query "items[?name=='tools-gateway'].gatewayId" \
  --output text --region $REGION)

GATEWAY_URL="https://${GATEWAY_ID}.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp"

aws ssm put-parameter \
  --name "/FAST-stack/gateway_url" \
  --value "$GATEWAY_URL" \
  --type String --overwrite --region $REGION

aws ssm put-parameter \
  --name "/FAST-stack/gateway_credential_provider" \
  --value "workshop-tools-gateway-auth" \
  --type String --overwrite --region $REGION

echo "Gateway URL (Module 3a): $GATEWAY_URL"
echo "Credential provider: workshop-tools-gateway-auth"
:::

## Widen IAM Permissions

The agent's IAM role needs permission to read the new credential provider's secret. Update the CDK stack to allow access to all AgentCore Identity OAuth2 secrets:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent
python3 <<'PYEOF'
import pathlib, re
p = pathlib.Path('infra-cdk/lib/backend-stack.ts')
text = p.read_text()
wide = 'bedrock-agentcore-identity!default/oauth2/*'

# Check for the NARROW runtime-gateway-auth pattern specifically.
# FAST v0.4.1 already contains the wide pattern elsewhere in this file (on the
# OAuth2 provider Lambda's role, unrelated to the runtime role), so checking
# "wide in text" would incorrectly short-circuit and skip widening the runtime
# role's inline policy.
narrow_re = re.compile(r'bedrock-agentcore-identity!default/oauth2/[^"\'`]*runtime-gateway-auth\*')
narrow_hits = narrow_re.findall(text)

if not narrow_hits:
    print(f"Already patched: {p} (no narrow runtime-gateway-auth pattern found)")
else:
    new_text, n = narrow_re.subn(wide, text)
    p.write_text(new_text)
    print(f"Patched {p} ({n} narrow pattern(s) replaced)")
    if narrow_re.search(p.read_text()):
        raise SystemExit(f"ERROR: widening verification failed - narrow pattern still in {p}")
    print(f"Verified: {p} no longer contains the narrow runtime pattern")
PYEOF

grep -n 'bedrock-agentcore-identity!default/oauth2/\*' infra-cdk/lib/backend-stack.ts
:::

## Redeploy

FAST's CDK owns the `/FAST-stack/gateway_url` and `/FAST-stack/gateway_credential_provider` SSM parameters and resets them to FAST's built-in gateway on every `cdk deploy`. Run the deploy first, then re-apply the overrides so the runtime keeps pointing at Module 3a's gateway.

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent/infra-cdk
npx cdk deploy --require-approval never

# Re-apply overrides (FAST's CDK resets them on every deploy)
aws ssm put-parameter \
  --name "/FAST-stack/gateway_url" \
  --value "$GATEWAY_URL" \
  --type String --overwrite --region $REGION

aws ssm put-parameter \
  --name "/FAST-stack/gateway_credential_provider" \
  --value "workshop-tools-gateway-auth" \
  --type String --overwrite --region $REGION

echo "Re-applied /FAST-stack/gateway_url and /FAST-stack/gateway_credential_provider"
:::

## Verify Tool Discovery

After the deployment completes, open the Amplify URL and ask:

> Plan a trip from SFO to Tokyo for 2026-09-15 to 2026-09-18, 2 guests

The agent should call `search_flights` and `search_hotels` through the Module 3a gateway and return **real flight and hotel results** — not a generic response. You should see tool invocations like `tg-workshop-flights-mcp___search_flights` in the chat.

::alert[If the agent still falls back to Code Interpreter or says it can't access tools, check the runtime logs for authentication errors. The most common issue is the IAM permission for the new OAuth2 secret.]{type="warning"}

## Notebook Walkthrough (Optional alternative)

> Prefer an interactive notebook experience? The notebook below is the notebook-track equivalent of the **MCP path** on this page — if you are instead taking the AgentCore path, see `04b-connect-gateway-agentcore.ipynb` (covered on the [Connect to Tools Gateway — AgentCore path](../connect-gateway-agentcore/) page).
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4b-fast/notebooks/` and open **`04a-connect-gateway-mcp.ipynb`**.

