---
title: "Tools Gateway: Automated Sync (Optional)"
weight: 51
---

::alert[This step is optional. You already created gateway targets manually in the previous step. The Sync Lambda automates this for production — skip ahead to **Test Both Paths** if you're short on time.]{type="info"}

In the previous step you manually curated tools into the gateway — browsing the catalog, selecting tools, resolving ARNs, and creating targets one by one. That is the right approach for understanding the flow. In production, you automate it with the **Sync Lambda**.

## How the Sync Lambda Works

The Sync Lambda is a scheduled bridge between the Registry and the AgentCore Gateway:

```
Registry API ──(list servers)──→ Sync Lambda ──(create targets)──→ AgentCore Gateway
```

1. **Authenticate** to the Registry API using M2M credentials from Secrets Manager
2. **Fetch** all registered servers (`GET /api/servers`)
3. **List** existing gateway targets to avoid duplicates
4. **Filter** by tags (if `SYNC_FILTER_TAGS` is set — only tagged tools sync)
5. **Build** target configs based on `proxy_pass_url` scheme:
   - `lambda://` -> Lambda target (native invocation)
   - `http://` or `https://` -> Skipped (these use Path A via NGINX/CloudFront or require manual curation as in step 3; the `http://` scheme here refers to **in-cluster** service-to-service URLs inside the Registry's VPC — intentional, not internet-facing traffic)
6. **Create** new targets, skip existing ones

## Tag-Based Selection

The `SYNC_FILTER_TAGS` environment variable controls which Registry tools the Sync Lambda processes:

| Setting | Behavior |
|---------|----------|
| Empty (default) | All `lambda://` tools synced |
| `agentcore-target` | Only tools tagged `agentcore-target` are synced |
| `agentcore-target,priority` | Tools with either tag are synced |

## What Happens When You Run It Now

Since you already created gateway targets manually in step 3, the Sync Lambda will detect them as existing and skip them:

| Registry Server | Gateway Target? | Why? |
|---|---|---|
| `workshop-flights-mcp` | Already exists | Created manually in step 3 |
| `workshop-hotels-mcp` | Already exists | Created manually in step 3 |
| `search-knowledge-base` | Already exists | Created manually in step 3 |
| `currenttime-server` | No | Docker server (in-cluster `http://` — intentional for internal service-to-service traffic), skipped by Sync Lambda |
| `realserverfaketools` | No | Docker server, skipped |

## CLI Walkthrough

### Step 1: Invoke the Sync Lambda

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

aws lambda invoke \
  --function-name agentcore-gateway-sync \
  --invocation-type RequestResponse \
  --payload '{"source": "cli", "manual": true}' \
  --cli-binary-format raw-in-base64-out \
  --region $REGION \
  /tmp/sync-response.json

cat /tmp/sync-response.json | python3 -m json.tool
:::

The response contains count fields for synced servers, targets created (should be 0 — they already exist), skipped, filtered, and errors. You should see a JSON object similar to:

```json
{
  "synced": 5,
  "created": 0,
  "skipped": 5,
  "filtered": 0,
  "errors": 0
}
```

If `errors` is non-zero or `synced` is 0, check the Sync Lambda CloudWatch log group `/aws/lambda/agentcore-gateway-sync` for the latest invocation.

### Step 2: List gateway targets

Confirm the targets from step 3 are still in place:

:::code{showCopyAction=true showLineNumbers=false language=bash}
GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
  --query "items[?name=='tools-gateway'].gatewayId" \
  --output text --region $REGION)

echo "Gateway ID: $GATEWAY_ID"

aws bedrock-agentcore-control list-gateway-targets \
  --gateway-identifier $GATEWAY_ID \
  --query "items[].{Name:name, Status:status, TargetId:targetId}" \
  --output table --region $REGION
:::

### Step 3: Verify expected targets

Check that the three targets from your curation step are present:

:::code{showCopyAction=true showLineNumbers=false language=bash}
for TARGET in tg-workshop-flights-mcp tg-workshop-hotels-mcp tg-search-knowledge-base; do
  RESULT=$(aws bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier $GATEWAY_ID \
    --query "items[?name=='$TARGET'].name" \
    --output text --region $REGION)
  if [ -n "$RESULT" ]; then
    echo "PASS: $TARGET"
  else
    echo "FAIL: $TARGET not found"
  fi
done
:::

### Step 4: Try tag-based filtering (optional)

Enable tag filtering to see how it works in production. Set `SYNC_FILTER_TAGS=agentcore-target` so only explicitly tagged tools would be synced:

:::code{showCopyAction=true showLineNumbers=false language=bash}
# Read current env vars, add filter tag, and update
CURRENT_ENV=$(aws lambda get-function-configuration \
  --function-name agentcore-gateway-sync \
  --query "Environment.Variables" \
  --output json --region $REGION)

UPDATED_ENV=$(echo "$CURRENT_ENV" | python3 -c "
import sys, json
env = json.load(sys.stdin)
env['SYNC_FILTER_TAGS'] = 'agentcore-target'
print(json.dumps({'Variables': env}))
")

aws lambda update-function-configuration \
  --function-name agentcore-gateway-sync \
  --environment "$UPDATED_ENV" \
  --region $REGION \
  --query "FunctionName" --output text

aws lambda wait function-updated-v2 \
  --function-name agentcore-gateway-sync \
  --region $REGION

echo "Tag filter enabled: agentcore-target"
:::

Re-run sync to see filtering in action:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws lambda invoke \
  --function-name agentcore-gateway-sync \
  --invocation-type RequestResponse \
  --payload '{"source": "cli", "manual": true}' \
  --cli-binary-format raw-in-base64-out \
  --region $REGION \
  /tmp/sync-filtered.json

cat /tmp/sync-filtered.json | python3 -m json.tool
:::

Reset the filter for subsequent steps:

:::code{showCopyAction=true showLineNumbers=false language=bash}
CURRENT_ENV=$(aws lambda get-function-configuration \
  --function-name agentcore-gateway-sync \
  --query "Environment.Variables" \
  --output json --region $REGION)

UPDATED_ENV=$(echo "$CURRENT_ENV" | python3 -c "
import sys, json
env = json.load(sys.stdin)
env['SYNC_FILTER_TAGS'] = ''
print(json.dumps({'Variables': env}))
")

aws lambda update-function-configuration \
  --function-name agentcore-gateway-sync \
  --environment "$UPDATED_ENV" \
  --region $REGION \
  --query "FunctionName" --output text

aws lambda wait function-updated-v2 \
  --function-name agentcore-gateway-sync \
  --region $REGION

echo "Tag filter reset (all tools sync)"
:::

::alert[In production, the Sync Lambda runs on an EventBridge schedule (every 5 minutes) and automatically creates targets for new tools registered in the Registry. Combined with tag-based filtering, this gives you a hands-off pipeline: register a tool in Module 3a, tag it `agentcore-target`, and it appears in the gateway within 5 minutes.]{type="info"}

---

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4a-tools-gateway/notebooks/` and open the corresponding notebook.

Open **Notebook 04 -- Automated Sync (Production Pattern)** (`04-sync-catalog.ipynb`).

This notebook covers:

1. **Invoke the Sync Lambda** synchronously and display the result counts (created, skipped, filtered, errors)
2. **List gateway targets** by querying the AgentCore Gateway control plane
3. **Verify expected targets** — confirms `tg-workshop-flights-mcp`, `tg-workshop-hotels-mcp`, and `tg-search-knowledge-base` appear
4. **Tag-based selection** — enables `SYNC_FILTER_TAGS=agentcore-target`, re-runs sync to demonstrate filtering, then resets the filter for later notebooks
