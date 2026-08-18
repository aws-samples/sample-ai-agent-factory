---
title: "Connect to LLM Gateway"
weight: 73
---

The agent is deployed but currently calls Amazon Bedrock directly. In this step you will point it at the LLM Gateway from Module 2 so all model invocations flow through the governed proxy with budget controls and cost tracking.

::alert[**Track 1 participants** — if you skipped Module 2, the LLM Gateway has no models registered and no virtual keys yet. Run the setup script (`setup_keys.py`) below first to register models and create keys. If you completed Module 2, skip to [Retrieve the LLM Gateway credentials](#retrieve-the-llm-gateway-credentials).]{type="warning"}

## Register models and create keys (Track 1 only)

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop
# strands-agents requires Python >= 3.10; the IDE's default `python3` is 3.9 and
# building the venv with it makes the installs below resolve to "No matching
# distribution found", after which setup_keys.py fails on `import boto3`.
PY=""
for CAND in python3.13 python3.12 python3.11 python3.10; do
  command -v "$CAND" >/dev/null 2>&1 && { PY="$CAND"; break; }
done
[ -z "$PY" ] && PY=python3
echo "Building .venv with $PY ($($PY -V 2>&1))"
$PY -m venv --clear .venv
source .venv/bin/activate
python -V

cd /workshop/source/module-2-llm-gateway
pip install --upgrade pip==24.0 --quiet
pip install -r requirements.txt --quiet
python scripts/setup_keys.py --stack-name workshop-llm-gateway-stack
:::

This registers 17 Bedrock models (Claude, Nova, Llama, Mistral, DeepSeek) in the LLM Gateway, creates two teams with budgets, and issues virtual keys. Copy the `export` commands printed at the end and run them in your terminal.

## Retrieve the LLM Gateway credentials

The platform team stores the LLM Gateway URL and virtual key in SSM Parameter Store (Module 2). Check if they exist:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

LLM_GATEWAY_URL=$(aws ssm get-parameter \
  --name "/workshop/llm-gateway-url" \
  --query "Parameter.Value" --output text --region $REGION 2>/dev/null)

LLM_GATEWAY_KEY=$(aws ssm get-parameter \
  --name "/workshop/llm-gateway-key" \
  --query "Parameter.Value" --output text --region $REGION 2>/dev/null)

# Presence is not enough. A virtual key left in SSM by an earlier deployment is no
# longer in this gateway's database, and the agent then fails at runtime with
# HTTP 401 "Unable to find token". Probe the key before trusting it.
KEY_OK=""
if [ -n "$LLM_GATEWAY_URL" ] && [ -n "$LLM_GATEWAY_KEY" ]; then
  HTTP=$(curl -s -o /dev/null -w '%{http_code}' "${LLM_GATEWAY_URL}/key/info" \
    -H "Authorization: Bearer ${LLM_GATEWAY_KEY}")
  if [ "$HTTP" = "200" ]; then
    KEY_OK=1
  else
    echo "Stored virtual key is no longer valid (HTTP $HTTP) — issuing a new one..."
  fi
fi

if [ -n "$KEY_OK" ]; then
  echo "LLM Gateway URL: $LLM_GATEWAY_URL"
  echo "Virtual Key: sk-****${LLM_GATEWAY_KEY: -4}"
else
  echo "Creating LLM Gateway parameters..."

  LLM_GATEWAY_URL=$(aws cloudformation describe-stacks \
    --stack-name workshop-llm-gateway-stack \
    --query "Stacks[0].Outputs[?OutputKey=='ProxyUrl'].OutputValue" \
    --output text --region $REGION)

  # Note: workshop-llm-gateway-stack-admin-key is the secret name for the admin key
  ADMIN_KEY=$(aws secretsmanager get-secret-value \
    --secret-id workshop-llm-gateway-stack-admin-key \
    --query SecretString --output text --region $REGION)

  RESPONSE=$(curl -s -X POST "${LLM_GATEWAY_URL}/key/generate" \
    -H "Authorization: Bearer sk-${ADMIN_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"key_alias\":\"fast-agent-key-$(date +%s)\",\"max_budget\":10.0}")

  LLM_GATEWAY_KEY=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('key',''))")

  aws ssm put-parameter --name "/workshop/llm-gateway-url" \
    --value "$LLM_GATEWAY_URL" --type String --overwrite --region $REGION
  aws ssm put-parameter --name "/workshop/llm-gateway-key" \
    --value "$LLM_GATEWAY_KEY" --type String --overwrite --region $REGION

  echo "Created and stored:"
  echo "  LLM Gateway URL: $LLM_GATEWAY_URL"
  echo "  Virtual Key: sk-****${LLM_GATEWAY_KEY: -4}"
fi
:::

::alert[If you completed Module 2, the parameters already exist. If you're on Track 1 (fast path), the script creates them automatically.]{type="info"}

## Store in FAST SSM parameters

The travel agent reads the LLM Gateway URL and key from SSM at startup (under the FAST stack name). Store them:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws ssm put-parameter \
  --name "/FAST-stack/llm_gateway_url" \
  --value "$LLM_GATEWAY_URL" \
  --type String --overwrite --region $REGION

aws ssm put-parameter \
  --name "/FAST-stack/llm_gateway_key" \
  --value "$LLM_GATEWAY_KEY" \
  --type String --overwrite --region $REGION

echo "SSM parameters stored for FAST agent"
:::

## Redeploy to pick up the new configuration

The agent reads SSM parameters when the container starts. Touch the agent file to force CDK to rebuild the container:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent

echo "# LLM Gateway integration" >> patterns/strands-travel-agent/travel_agent.py

cd /workshop/fast-agent/infra-cdk
npx cdk deploy --require-approval never
:::

::alert[This redeployment takes approximately 1–2 minutes. The agent container is rebuilt and picks up the new SSM values at startup.]{type="info"}

## Verify

Test the LLM Gateway connection directly:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "${LLM_GATEWAY_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LLM_GATEWAY_KEY}" \
  -d '{"model": "claude-sonnet", "messages": [{"role": "user", "content": "Say hello in one word"}], "max_tokens": 10}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['choices'][0]['message']['content'])"
:::

If you see a response, the virtual key and gateway are working. The agent will use the same credentials at runtime after the redeploy.

Open the Amplify URL and send a message (e.g. "hello") to confirm the agent responds. Then verify it went through the LLM Gateway by checking spend on the virtual key:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "${LLM_GATEWAY_URL}/key/info" \
  -H "Authorization: Bearer ${LLM_GATEWAY_KEY}" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
info = d.get('info', d)
print(f'Spend: \${info.get(\"spend\", 0):.4f} / \${info.get(\"max_budget\", 0):.2f} budget')
"
:::

If spend is greater than $0, the agent is routing through the LLM Gateway.

## Notebook walkthrough (optional alternative)

> Prefer an interactive notebook experience? The notebook below covers the same material as the CLI commands above, with additional inline explanations and visualizations.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4b-fast/notebooks/` and open **`03-connect-llm-gateway.ipynb`**.
