---
title: "Connect to Tools Gateway (AgentCore path)"
weight: 75
---

Connect the travel agent to the pre-deployed AgentCore Gateway (`ac-tools-gateway`). This step wires the agent to the gateway, which has the Flights and Hotels tools as Lambda targets.

::alert[Follow this page if you're on **Track 1 (fast path)** using the AgentCore registry, or you completed **Module 3b** (AgentCore Registry). If you completed Module 3a (OSS MCP Registry), use the [MCP Path](../connect-gateway-mcp/) instead.]{type="info"}

## The Auth Challenge

Same concept as the MCP path — FAST's agent needs to authenticate to Module 3b's `ac-tools-gateway`. The gateway validates JWTs from the shared Cognito pool (created by the Registry stack).

## Retrieve Credentials

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

M2M_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id "workshop-cognito-m2m-secret" \
  --query 'SecretString' --output text)

export M2M_CLIENT_ID=$(echo $M2M_SECRET | python3 -c "import sys,json; print(json.load(sys.stdin)['client_id'])")
export M2M_CLIENT_SECRET=$(echo $M2M_SECRET | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")

export COGNITO_POOL_ID=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoUserPoolId'].Value" \
  --output text --region $REGION)

export DISCOVERY_URL="https://cognito-idp.${REGION}.amazonaws.com/${COGNITO_POOL_ID}/.well-known/openid-configuration"

echo "M2M Client ID: $M2M_CLIENT_ID"
echo "Discovery URL: $DISCOVERY_URL"
:::

## Create the OAuth2 Credential Provider

::alert[If you already created this in Module 3, you'll see "already exists" — that's fine, skip to the next step.]{type="info"}

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

## Store Gateway Configuration in SSM

:::code{showCopyAction=true showLineNumbers=false language=bash}
GATEWAY_ID=$(aws cloudformation describe-stacks \
  --stack-name workshop-agentcore-stack \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayId'].OutputValue" \
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

echo "Gateway URL: $GATEWAY_URL"
:::

## Update the Gateway Client

Replace the agent's `gateway.py` so it reads the credential provider from SSM:

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

_stack_name = os.environ.get("STACK_NAME", "")
try:
    _provider_name = get_ssm_parameter(f"/{_stack_name}/gateway_credential_provider")
    logger.info("[GATEWAY] Using credential provider from SSM: %s", _provider_name)
except Exception:
    _provider_name = os.environ.get("GATEWAY_CREDENTIAL_PROVIDER_NAME", "")
    logger.info("[GATEWAY] Using credential provider from env: %s", _provider_name)


@requires_access_token(provider_name=_provider_name, auth_flow="M2M", scopes=[])
def _fetch_gateway_token(access_token: str) -> str:
    """Fetch OAuth2 token via AgentCore Identity Token Vault."""
    return access_token


def create_gateway_mcp_client() -> MCPClient:
    """Create MCP client for AgentCore Gateway."""
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
        prefix="gateway",
    )
PYEOF
:::

Verify the file:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent
python3 -c "import ast; ast.parse(open('patterns/strands-travel-agent/tools/gateway.py').read()); print('✓ gateway.py syntax OK')"
:::

## Update IAM and Redeploy

Widen the Secrets Manager IAM permission so the agent can read the new OAuth2 credential provider's secret:

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

Force a container rebuild and deploy. FAST's CDK owns `/FAST-stack/gateway_url` and `/FAST-stack/gateway_credential_provider` and resets them on every deploy, so re-apply the overrides afterwards.

:::code{showCopyAction=true showLineNumbers=false language=bash}
# Re-derive REGION and GATEWAY_URL in case you are running this section on its own
REGION=$(aws configure get region)
GATEWAY_ID=$(aws cloudformation describe-stacks \
  --stack-name workshop-agentcore-stack \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayId'].OutputValue" \
  --output text --region $REGION)
GATEWAY_URL="https://${GATEWAY_ID}.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp"

cd /workshop/fast-agent
echo "# Tools Gateway integration" >> patterns/strands-travel-agent/travel_agent.py
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

## Verify

After redeployment, open the Amplify URL and ask:

> Plan a trip from SFO to Tokyo for 2026-09-15 to 2026-09-18, 2 guests

The agent should call `search_flights` and `search_hotels` through the gateway and return real results.

## Notebook Walkthrough (Optional alternative)

> Prefer an interactive notebook experience? The notebook below is the notebook-track equivalent of the **AgentCore path** on this page — if you are instead taking the MCP path, see `04a-connect-gateway-mcp.ipynb` (covered on the [Connect to Tools Gateway — MCP path](../connect-gateway-mcp/) page).
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4b-fast/notebooks/` and open **`04b-connect-gateway-agentcore.ipynb`**.
