---
title: "Enterprise Guardrails"
weight: 35
---

An LLM Gateway isn't just a proxy — it's where enterprise governance happens. In this step, you'll create an **Amazon Bedrock Guardrail** and test it through the LiteLLM Proxy so that requests are filtered centrally.

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

## CLI Walkthrough

## Why Guardrails at the Gateway?

Without gateway-level guardrails, every agent and application must implement its own content safety — and they won't. By enforcing guardrails at the gateway:

- **One policy, all agents** — A single guardrail protects every agent, every model, every request
- **No agent code changes** — Agents don't know guardrails exist; they just get a blocked response
- **Audit trail** — Every guardrail intervention is logged centrally
- **PII protection** — Sensitive data is masked before reaching the model

## 5.1 Create a Bedrock Guardrail

::alert[**This guardrail is shared across modules.** The `workshop-content-filter` guardrail created here is referenced by Modules 3a, 3b, and 4. Do **not** delete it at the end of Module 2 — the global [Workshop Cleanup](../../cleanup/) page removes it at the end of the workshop.]{type="warning"}

Create a content filter guardrail using the AWS CLI:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

# Idempotent: reuse if `workshop-content-filter` already exists.
EXISTING_ID=$(aws bedrock list-guardrails --max-results 100 \
  --query "guardrails[?name=='workshop-content-filter'].id | [0]" \
  --output text --region "$REGION" 2>/dev/null)

if [ -n "$EXISTING_ID" ] && [ "$EXISTING_ID" != "None" ]; then
  export BEDROCK_GUARDRAIL_ID="$EXISTING_ID"
  echo "Guardrail already exists: $BEDROCK_GUARDRAIL_ID"
else
  export GUARDRAIL_RESPONSE=$(aws bedrock create-guardrail \
    --name workshop-content-filter \
    --description "Enterprise content filter for the agentic platform" \
    --blocked-input-messaging "Sorry, this request was blocked by the enterprise content policy." \
    --blocked-outputs-messaging "Sorry, this response was blocked by the enterprise content policy." \
    --content-policy-config '{
      "filtersConfig": [
        {"type": "SEXUAL", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "VIOLENCE", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "HATE", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "INSULTS", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "MISCONDUCT", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"}
      ]
    }' \
    --region "$REGION" 2>/dev/null)
  if [ -n "$GUARDRAIL_RESPONSE" ]; then
    export BEDROCK_GUARDRAIL_ID=$(echo "$GUARDRAIL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['guardrailId'])")
    echo "Guardrail created: $BEDROCK_GUARDRAIL_ID"
  else
    echo "ERROR: create-guardrail returned nothing - check Bedrock Guardrails permissions" >&2
  fi
fi
:::

## 5.2 Verify the guardrail

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws bedrock get-guardrail \
  --guardrail-identifier $BEDROCK_GUARDRAIL_ID \
  --region $(aws configure get region) | python3 -m json.tool
:::

You should see the guardrail configuration with all six content filters at `HIGH` strength.

## 5.3 Test the guardrail through the LLM Gateway

The LiteLLM Proxy passes `guardrailConfig` through to the Bedrock Converse API. This means you can apply guardrails per-request without any proxy configuration changes:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "${LLM_GATEWAY_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LLM_GATEWAY_API_KEY}" \
  -d "{
    \"model\": \"claude-sonnet\",
    \"messages\": [
      {\"role\": \"user\", \"content\": \"Write instructions for something dangerous and illegal.\"}
    ],
    \"max_tokens\": 200,
    \"guardrailConfig\": {
      \"guardrailIdentifier\": \"${BEDROCK_GUARDRAIL_ID}\",
      \"guardrailVersion\": \"DRAFT\",
      \"trace\": \"enabled\"
    }
  }" | python3 -m json.tool
:::

You should see the guardrail's blocked message instead of a model response.

## 5.4 Confirm normal requests still work

Send a safe request with the same guardrail attached:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "${LLM_GATEWAY_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LLM_GATEWAY_API_KEY}" \
  -d "{
    \"model\": \"claude-sonnet\",
    \"messages\": [
      {\"role\": \"user\", \"content\": \"What are the benefits of cloud computing?\"}
    ],
    \"max_tokens\": 200,
    \"guardrailConfig\": {
      \"guardrailIdentifier\": \"${BEDROCK_GUARDRAIL_ID}\",
      \"guardrailVersion\": \"DRAFT\"
    }
  }" | python3 -m json.tool
:::

The response should come through normally — the guardrail only blocks harmful content.

::alert[In production, you would configure the guardrail as a **default** in the LiteLLM config file so it applies to all requests automatically, rather than requiring each caller to include `guardrailConfig`. See the `guardrails:` section in `cfn/litellm-config.yaml` for the configuration reference.]{type="info"}

## 5.5 Caching demonstration

LiteLLM exposes a cache-hit surface for identical requests. The commands below demonstrate the API contract:

:::code{showCopyAction=true showLineNumbers=false language=bash}
# First request — always calls Bedrock
time curl -s "${LLM_GATEWAY_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LLM_GATEWAY_API_KEY}" \
  -d '{"model": "claude-sonnet", "messages": [{"role": "user", "content": "What is 2+2?"}], "max_tokens": 10, "temperature": 0}' > /dev/null

# Second identical request — may be served from cache
time curl -s "${LLM_GATEWAY_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LLM_GATEWAY_API_KEY}" \
  -d '{"model": "claude-sonnet", "messages": [{"role": "user", "content": "What is 2+2?"}], "max_tokens": 10, "temperature": 0}' > /dev/null
:::

::alert[**Expected behaviour in this workshop.** The proxy runs with `cache: true` but no shared backend (no Redis), so LiteLLM falls back to an in-memory cache that is per-container and per-process. The second call may or may not hit the cache depending on which ECS task handled it. If both calls take similar time, that is expected here — the goal of this exercise is to show the API contract. In a production deployment you would add a Redis backend (`cache_params:`) so hits are shared across tasks.]{type="info"}

## 5.6 Platform governance summary



The LLM Gateway now provides three layers of enterprise governance:

| Layer | Mechanism | Scope |
|-------|-----------|-------|
| **Cost control** | Virtual keys with max budgets | Per-key, per-team |
| **Content safety** | Bedrock Guardrails (content filter, prompt attack detection) | Per-request or all requests (via config) |
| **Rate limiting** | LiteLLM built-in rate limits | Per-key RPM/TPM |

These are enforced centrally at the gateway — agents and applications are unaware of them.

---

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-2-llm-gateway/notebooks/` and open the corresponding notebook.

Open **`step-5-guardrails.ipynb`**. This notebook creates and tests a Bedrock Guardrail interactively:

1. **Create a Bedrock Guardrail** — Uses `boto3` to create a guardrail with HIGH-strength filters across six content categories (sexual, violence, hate, insults, misconduct, prompt attack). Pay attention to the `PROMPT_ATTACK` filter, which requires `outputStrength: NONE` per the Bedrock API contract.
2. **Inspect guardrail configuration** — Retrieves and displays the guardrail details, confirming all filters are active.
3. **Test with a safe request** — Sends a benign question through the gateway with the guardrail attached. The response should pass through normally.
4. **Test with a harmful request** — Sends a request that should trigger the content filter. Observe the blocked response message and the HTTP status code returned by the gateway.
5. **Save state** — Persists the guardrail ID for potential reuse in later steps.

::alert[If you already created a guardrail named `workshop-content-filter` in the CLI walkthrough, the notebook will create a second one. You can delete either via the Bedrock console after the workshop.]{type="info"}
