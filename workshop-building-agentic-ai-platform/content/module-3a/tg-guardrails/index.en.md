---
title: "Tools Gateway: Bedrock Guardrails"
weight: 53
---

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

Add Bedrock Guardrails to screen all tool responses for sensitive information before they reach agents.

## Why Guardrails on Tool Output?

Tools return raw data -- database records, API responses, knowledge base passages. This data may contain:

- **PII**: Social Security numbers, credit card numbers, email addresses
- **Harmful content**: Offensive language, dangerous instructions
- **Internal metadata**: Internal IDs, infrastructure details

Path A (NGINX) passes everything through unfiltered. Path B's response interceptor can screen tool output using Bedrock Guardrails before it reaches the agent.

## How It Works in the Response Interceptor

:::code{showCopyAction=true showLineNumbers=false language=python}
# In handlers/interceptors.py (response_interceptor_handler)
if BEDROCK_GUARDRAIL_ID and "content" in result:
    guardrail_resp = bedrock.apply_guardrail(
        guardrailIdentifier=BEDROCK_GUARDRAIL_ID,
        guardrailVersion=BEDROCK_GUARDRAIL_VERSION,
        source="OUTPUT",
        content=[{"text": {"text": combined_text}}],
    )
    if guardrail_resp["action"] == "GUARDRAIL_INTERVENED":
        # Replace tool output with filtered version
        item["text"] = guardrail_resp["outputs"][0]["text"]
:::

## Fail-Open Design

The guardrail check is fail-open: if Bedrock is unavailable or the guardrail call fails, the original tool output passes through unchanged. This prevents guardrail outages from blocking all tool calls.

::alert[This is the **enterprise guardrail** applied to all tool output. Module 4 adds a separate **per-agent guardrail** for domain-specific rules (e.g., "no financial advice"). Both layers apply.]{type="info"}

## CLI Walkthrough

### Step 1: Create a Bedrock Guardrail

First check if the guardrail already exists:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

EXISTING=$(aws bedrock list-guardrails --max-results 100 \
  --query "guardrails[?name=='workshop-tool-guardrail'].id | [0]" \
  --output text --region $REGION)

if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
  echo "Guardrail already exists: $EXISTING"
  GUARDRAIL_ID=$EXISTING
else
  echo "Creating guardrail..."
fi
:::

If the guardrail does not exist, create it:

:::code{showCopyAction=true showLineNumbers=false language=bash}
# Only create if the previous block didn't already find one (idempotent: safe to
# re-run). create-guardrail would fail with ConflictException if it already
# exists, leaving RESULT empty and breaking the parse below.
if [ -z "${GUARDRAIL_ID:-}" ] || [ "${GUARDRAIL_ID:-}" = "None" ]; then
  RESULT=$(aws bedrock create-guardrail \
    --name workshop-tool-guardrail \
    --description "Enterprise guardrail for tool outputs: blocks PII and harmful content" \
    --blocked-input-messaging "This request has been blocked by enterprise guardrails." \
    --blocked-outputs-messaging "This tool response has been blocked because it contains sensitive information." \
    --content-policy-config '{
      "filtersConfig": [
        {"type": "HATE", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "VIOLENCE", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "SEXUAL", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "MISCONDUCT", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "INSULTS", "inputStrength": "HIGH", "outputStrength": "HIGH"}
      ]
    }' \
    --sensitive-information-policy-config '{
      "piiEntitiesConfig": [
        {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCK"},
        {"type": "CREDIT_DEBIT_CARD_NUMBER", "action": "BLOCK"},
        {"type": "EMAIL", "action": "ANONYMIZE"},
        {"type": "PHONE", "action": "ANONYMIZE"},
        {"type": "US_BANK_ACCOUNT_NUMBER", "action": "BLOCK"}
      ]
    }' \
    --region $REGION \
    --output json)
  GUARDRAIL_ID=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['guardrailId'])")
fi

# Resolve the (latest) version whether the guardrail was just created or already
# existed. DRAFT is always valid for apply-guardrail.
GUARDRAIL_VERSION=$(aws bedrock list-guardrails --max-results 100 \
  --query "guardrails[?name=='workshop-tool-guardrail'].version | [0]" \
  --output text --region $REGION)
[ -z "$GUARDRAIL_VERSION" ] || [ "$GUARDRAIL_VERSION" = "None" ] && GUARDRAIL_VERSION=DRAFT

echo "Guardrail ID:      $GUARDRAIL_ID"
echo "Guardrail Version: $GUARDRAIL_VERSION"
:::

This guardrail will block SSNs, credit card numbers, and bank account numbers, anonymize emails and phone numbers, and block harmful content (hate, violence, sexual, misconduct, insults).

:::alert[**Compliance note.** This step demonstrates PII/PCI screening patterns on tool output. Bedrock Guardrails is one control, not a compliance certification. If your application processes payment card data (PCI-DSS), protected health information (HIPAA/PHI), or EU personal data (GDPR), additional controls and attestations are required. See [AWS Compliance Programs](https://aws.amazon.com/compliance/programs/).]{type="info"}

### Step 2: Update the Response Interceptor Lambda

Write the guardrail ID and version into the Response Interceptor Lambda's environment variables using a read-merge-update pattern:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)
GUARDRAIL_ID=$(aws bedrock list-guardrails --max-results 100 \
  --query "guardrails[?name=='workshop-tool-guardrail'].id | [0]" \
  --output text --region $REGION)
if [ -z "$GUARDRAIL_ID" ] || [ "$GUARDRAIL_ID" = "None" ]; then
  echo "GUARDRAIL_ID not found - ensure Step 1 completed successfully" >&2
else
  GUARDRAIL_VERSION=$(aws bedrock get-guardrail \
    --guardrail-identifier $GUARDRAIL_ID \
    --query "version" --output text --region $REGION)
fi

RESPONSE_INTERCEPTOR_FN="agentcore-gateway-response-interceptor"

# Read current env vars
CURRENT_ENV=$(aws lambda get-function-configuration \
  --function-name $RESPONSE_INTERCEPTOR_FN \
  --query "Environment.Variables" \
  --output json --region $REGION)

# Merge in guardrail config and update
UPDATED_ENV=$(echo "$CURRENT_ENV" | python3 -c "
import sys, json
env = json.load(sys.stdin)
env['BEDROCK_GUARDRAIL_ID'] = '$GUARDRAIL_ID'
env['BEDROCK_GUARDRAIL_VERSION'] = '${GUARDRAIL_VERSION:-DRAFT}'
print(json.dumps({'Variables': env}))
")

aws lambda update-function-configuration \
  --function-name $RESPONSE_INTERCEPTOR_FN \
  --environment "$UPDATED_ENV" \
  --region $REGION \
  --query "FunctionName" --output text

echo "Updated $RESPONSE_INTERCEPTOR_FN with guardrail config"
:::

Wait for the update to propagate:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws lambda wait function-updated-v2 \
  --function-name $RESPONSE_INTERCEPTOR_FN \
  --region $REGION \
  && echo "Lambda updated and ready."
:::

### Step 3: Test the guardrail with simulated PII content

Use the Bedrock Runtime `apply_guardrail` API directly to verify the guardrail behavior:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cat > /tmp/apply-guardrail.json << EOF
{
  "guardrailIdentifier": "$GUARDRAIL_ID",
  "guardrailVersion": "${GUARDRAIL_VERSION:-DRAFT}",
  "source": "OUTPUT",
  "content": [
    {
      "text": {
        "text": "Customer record found. Name: John Smith, SSN: 000-00-0000, Credit Card: 4111-1111-1111-1111, Email: john.smith@example.com, Phone: (555) 123-4567. Account balance: \$12,450.00."
      }
    }
  ]
}
EOF

aws bedrock-runtime apply-guardrail \
  --cli-input-json file:///tmp/apply-guardrail.json \
  --region $REGION \
  --output json | python3 -m json.tool
:::

The response should show `action: GUARDRAIL_INTERVENED` with SSNs and credit card numbers blocked, and email/phone numbers anonymized.

### Step 4: Test end-to-end through Path B

Now call a tool through the gateway. The response interceptor will apply the guardrail to the tool output:

:::code{showCopyAction=true showLineNumbers=false language=bash}
GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
  --query "items[?name=='tools-gateway'].gatewayId" \
  --output text --region $REGION)
test -n "$GATEWAY_ID" && test "$GATEWAY_ID" != "None" || echo "GATEWAY_ID empty - confirm workshop-tools-gateway-stack shows CREATE_COMPLETE" >&2

GATEWAY_URL="https://${GATEWAY_ID}.gateway.bedrock-agentcore.${REGION}.amazonaws.com/mcp"

# Cognito JWT for the AgentCore Gateway
M2M_SECRET_ARN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoM2MClientSecretArn'].Value" \
  --output text --region $REGION)
M2M_JSON=$(aws secretsmanager get-secret-value \
  --secret-id "$M2M_SECRET_ARN" --query SecretString --output text --region $REGION)
M2M_CLIENT_ID=$(echo "$M2M_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['client_id'])")
M2M_CLIENT_SECRET=$(echo "$M2M_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")
COGNITO_DOMAIN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoDomain'].Value" --output text --region $REGION)
GATEWAY_TOKEN=$(curl -s -X POST \
  "https://${COGNITO_DOMAIN}.auth.${REGION}.amazoncognito.com/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -u "${M2M_CLIENT_ID}:${M2M_CLIENT_SECRET}" \
  -d "grant_type=client_credentials&scope=mcp-servers-unrestricted/read mcp-servers-unrestricted/execute" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "=== PATH B with Bedrock Guardrails active ==="
curl -s -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -H "x-amz-agentcore-target: tg-workshop-flights-mcp" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "tg-workshop-flights-mcp___search_flights",
      "arguments": {"origin": "SFO", "destination": "TYO", "date": "2026-09-15"}
    }
  }' | python3 -m json.tool
:::

If the tool response contains any PII, the guardrail will block or anonymize it before it reaches you. Path A (NGINX) would pass the same content through unfiltered.

---

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4a-tools-gateway/notebooks/` and open the corresponding notebook.

Open **Notebook 06 -- Add Bedrock Guardrails** (`06-bedrock-guardrails.ipynb`).

This notebook covers four steps:

1. **Create a Bedrock Guardrail** -- configures content filters (hate, violence, sexual, misconduct, insults at HIGH strength) and sensitive information filters (SSN/credit card BLOCK, email/phone ANONYMIZE)
2. **Update the Response Interceptor** -- writes `BEDROCK_GUARDRAIL_ID` and `BEDROCK_GUARDRAIL_VERSION` into the Lambda's environment variables using read-merge-update
3. **Test the guardrail** -- calls `bedrock-runtime.apply_guardrail()` with simulated PII content, showing what gets blocked vs. anonymized
4. **Compare paths** -- side-by-side output showing Path A passes PII unfiltered while Path B blocks/anonymizes it
