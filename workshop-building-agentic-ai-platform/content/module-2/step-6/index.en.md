---
title: "Spend Tracking & Admin"
weight: 36
---

LiteLLM Proxy includes built-in spend tracking, an Admin UI, and API endpoints for monitoring usage across all virtual keys and teams.

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

## CLI Walkthrough

## 6.1 View spend in the Admin UI

Open the LiteLLM Admin UI and navigate to the **Usage** tab to see spend per key and per model:

:::code{showCopyAction=true showLineNumbers=false language=bash}
echo "Admin UI: ${LLM_GATEWAY_URL}/ui"
:::

![LiteLLM spend tracking dashboard showing per-key and per-model usage](/static/img/module-2/litellm-spend-tracking.png)

## 6.2 View spend logs via API

Query the spend tracking endpoint to see per-request cost data:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "${LLM_GATEWAY_URL}/spend/logs" \
  -H "Authorization: Bearer ${LLM_GATEWAY_ADMIN_KEY}" \
  | python3 -c "import json,sys; print('\n'.join(json.dumps(json.load(sys.stdin), indent=2).splitlines()[:50]))"
:::

Each log entry includes:
- `model` — Which model was called
- `total_tokens` — Prompt + completion tokens
- `spend` — Cost in USD
- `api_key` — Which virtual key made the request
- `team_id` — Which team the key belongs to

## 6.3 Check per-key spend

View the current budget usage for a specific virtual key:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "${LLM_GATEWAY_URL}/key/info" \
  -H "Authorization: Bearer ${LLM_GATEWAY_ADMIN_KEY}" \
  -G --data-urlencode "key=${LLM_GATEWAY_API_KEY}" \
  | python -m json.tool
:::

Look at the `spend` field vs `max_budget` — this shows how much of the key's budget has been consumed.

## 6.5 Proxy health + model catalog

LiteLLM exposes the deep `/health` endpoint which probes every registered backend — but that endpoint routinely takes 30-90 seconds and returns `503 Service Unavailable` through API Gateway's 30-second integration timeout. For the workshop we use two lightweight endpoints instead: `/health/readiness` (checks the proxy process + database) and `/v1/models` (lists every configured model). Together they answer the same question in under a second:

:::code{showCopyAction=true showLineNumbers=false language=bash}
echo "=== Proxy readiness ==="
curl -s --max-time 10 "${LLM_GATEWAY_URL}/health/readiness" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'Proxy status: {d.get(\"status\")}')
print(f'DB:           {d.get(\"db\")}')
print(f'Version:      {d.get(\"litellm_version\")}')
"

echo ""
echo "=== Registered models ==="
curl -s --max-time 10 "${LLM_GATEWAY_URL}/v1/models" \
  -H "Authorization: Bearer ${LLM_GATEWAY_ADMIN_KEY}" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
models = d.get('data', [])
print(f'{len(models)} models registered')
for m in models[:10]:
    print(f'  - {m.get(\"id\")}')
if len(models) > 10:
    print(f'  ... and {len(models) - 10} more')
"
:::

::alert[If you need a full backend-connectivity probe (which Bedrock models are actually reachable from the proxy, not just configured), open the **Admin UI → Health** tab — the browser can tolerate the long response time even when `curl ... /health` through API Gateway cannot. If a model shows unhealthy there, return to Module 2 Step 2 and prime it in the Bedrock console.]{type="info"}

## 6.6 Observability: What to Monitor

In production, you would create a CloudWatch Dashboard covering:

| Metric | Source | What It Shows |
|--------|--------|---------------|
| **ECS CPU/Memory** | ECS CloudWatch metrics | LiteLLM Proxy resource utilization |
| **ALB Request Count** | ALB CloudWatch metrics | Traffic volume and error rates |
| **API Gateway Latency** | API Gateway metrics | End-to-end response times |
| **5xx Error Rate** | ALB + API Gateway | Service health |

Use the ECS cluster name from the stack outputs (the pre-provisioned stack names it `workshop-llm-gateway-stack-cluster`) and ALB name to build your dashboard. CloudWatch Container Insights can also be enabled on the ECS cluster for deeper visibility.

---

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-2-llm-gateway/notebooks/` and open the corresponding notebook.

Open **`step-6-spend-tracking.ipynb`**. This notebook provides an interactive view into the gateway's cost tracking and administration features:

1. **Spend logs** — Queries the `/spend/logs` endpoint and displays per-request cost data including model, token count, and USD spend. Look at how costs vary across different models.
2. **Virtual key info** — Inspects a specific key's metadata: name, team, budget cap, current spend, and allowed models. Compare the `spend` field against `max_budget` to see budget utilization.
3. **Proxy health** — Checks `/health/readiness` (proxy process + DB) and `/v1/models` (registered model catalog). The deep `/health` endpoint is avoided here because it often exceeds API Gateway's 30-second integration timeout; the Admin UI's Health tab is the right place for a full backend probe.
4. **Admin UI link** — Prints the URL for the LiteLLM Admin UI where you can visually manage keys, teams, and monitor usage in your browser.

::alert[The notebook loads state from `.state.json`, so make sure you have run either the Step 2 and Step 3 notebooks or saved equivalent state before opening this one.]{type="info"}
