---
title: "Tools Gateway: Two Paths to Tools"
weight: 48
---

The MCP Registry provides direct access to Docker-based MCP servers via NGINX/CloudFront. That's **Path A** — fast but ungoverned. The Tools Gateway adds **Path B** — the AgentCore Gateway — with JWT auth, audit logging, access control, and guardrails.

## Path A vs Path B

| | Path A (Direct) | Path B (Governed) |
|--|----------------|-------------------|
| **Route** | Agent → CloudFront → NGINX → Docker MCP Server | Agent → AgentCore Gateway → Interceptors → Tool Target |
| **Auth** | Static token | Cognito JWT |
| **Audit** | NGINX access logs | CloudWatch structured logs |
| **Guardrails** | None | Bedrock Guardrails on output |
| **Access control** | None | Group-based (Cognito groups) |
| **Tool types** | Docker MCP servers only | Lambda, HTTP APIs, and Docker servers |
| **Use case** | Dev/test | Production |

## Test Path A

Verify the Registry API is accessible (you should already have `$REGISTRY_TOKEN` and `$REGISTRY_URL` from the previous steps):

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s -H "Authorization: Bearer $REGISTRY_TOKEN" \
  "$REGISTRY_URL/api/servers" \
  | python3 -c "
import sys, json
servers = json.load(sys.stdin).get('servers', [])
print(f'Registered servers: {len(servers)}')
for s in servers:
    print(f'  - {s.get(\"display_name\", s.get(\"path\", \"?\"))}')
"
:::

You should see the Flights MCP, Hotels MCP, and demo servers listed. This is Path A — direct, ungoverned access.

#### Store key values in shell variables

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

REGISTRY_URL=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-RegistryUrl'].Value" \
  --output text --region $REGION)

CLOUDFRONT_URL=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-MainCloudFrontUrl'].Value" \
  --output text --region $REGION)

COGNITO_POOL_ID=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoUserPoolId'].Value" \
  --output text --region $REGION)

M2M_CLIENT_ID=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoM2MClientId'].Value" \
  --output text --region $REGION)

echo "Registry URL:   $REGISTRY_URL"
echo "CloudFront URL: $CLOUDFRONT_URL"
echo "Cognito Pool:   $COGNITO_POOL_ID"
echo "M2M Client:     $M2M_CLIENT_ID"
:::

#### Retrieve the M2M static API token from Secrets Manager

:::code{showCopyAction=true showLineNumbers=false language=bash}
ACCESS_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id workshop-registry-api-token \
  --query SecretString --output text --region $REGION \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['api_token'])")

echo "Token retrieved (first 10 chars): ${ACCESS_TOKEN:0:10}..."
:::

#### Test Path A -- list tools via the Registry API through NGINX

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s -H "Authorization: Bearer $ACCESS_TOKEN" \
  "$REGISTRY_URL/api/servers" | python3 -m json.tool
:::

This call goes through CloudFront to NGINX to the Registry API. You should see the list of Docker-based MCP servers that the MCP Registry deployed.

---

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4a-tools-gateway/notebooks/` and open the corresponding notebook.

Open **Notebook 01 -- Two Paths to Tools** (`01-two-paths.ipynb`).

This notebook:

1. Fetches the MCP Registry's CloudFormation exports (Registry URL, CloudFront URL, Cognito IDs) using boto3
2. Authenticates with M2M credentials from Secrets Manager
3. Lists all registered MCP servers via the Registry API (Path A)
4. Presents a decision framework comparing Path A and Path B
