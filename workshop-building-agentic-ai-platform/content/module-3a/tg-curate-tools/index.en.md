---
title: "Tools Gateway: Curate Tools"
weight: 50
---

The Registry has several tools registered — but not all need runtime governance. In this step you selectively promote the tools the Travel Agent needs to the AgentCore Gateway.

## Why Selective Curation?

| Tool | Governance Need | Gateway? |
|------|----------------|----------|
| **Flights MCP** | Audit + guardrails (pricing, PII) | **Yes** |
| **Hotels MCP** | Audit + guardrails (pricing, PII) | **Yes** |
| **search-knowledge-base** | Audit (enterprise data) | **Yes** |
| currenttime | None (low-risk utility) | No — Path A only |
| realserverfaketools | None (test fixture) | No — Path A only |

## Set Up Variables

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
  --query "items[?name=='tools-gateway'].gatewayId" \
  --output text --region $REGION)

# Verify the gateway was resolved - missing means the Tools Gateway stack
# did not deploy or its name changed. Stop now rather than fail deeper in
# the target-creation step with a less obvious error.
test -n "$GATEWAY_ID" && test "$GATEWAY_ID" != "None" || { echo "GATEWAY_ID empty - confirm workshop-tools-gateway-stack shows CREATE_COMPLETE" >&2; }
echo "Gateway ID: $GATEWAY_ID"
echo "Account:    $ACCOUNT_ID"
:::

## Create Gateway Targets

Each gateway target connects a Lambda function to the AgentCore Gateway. The process for each tool is:

1. **Discover** — invoke the Lambda with `tools/list` to get its tool schemas (names, descriptions, input parameters)
2. **Sanitize** — strip non-standard fields from the schemas so the gateway accepts them
3. **Register** — call `create-gateway-target` with the Lambda ARN and cleaned schemas

The script below does this for all three tools the Travel Agent needs:

- **Flights MCP** — `search_flights`, `get_flight_details`, `search_flights_by_budget` for route and pricing queries
- **Hotels MCP** — `search_hotels`, `get_hotel_details`, `search_hotels_by_budget` for accommodation search
- **search-knowledge-base** — enterprise knowledge base search (Lambda-only, no Path A route)

:::code{showCopyAction=true showLineNumbers=false language=bash}
cat > /tmp/add_target.py << 'PYEOF'
import json, subprocess, sys, os

REGION = (os.environ.get("REGION") or os.environ.get("AWS_REGION")
          or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID")
GATEWAY_ID = os.environ.get("GATEWAY_ID")

ALLOWED_KEYS = {"type", "properties", "required", "items", "description"}

def clean_schema(s):
    if not isinstance(s, dict):
        return {"type": "object"}
    return {k: ({pk: clean_schema(pv) for pk, pv in v.items()} if k == "properties" else
                clean_schema(v) if k == "items" else v)
            for k, v in s.items() if k in ALLOWED_KEYS}

def add_target(function_name, target_name):
    arn = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:{function_name}"

    # Discover tools
    result = subprocess.run([
        "aws", "lambda", "invoke",
        "--function-name", function_name,
        "--payload", '{"jsonrpc":"2.0","method":"tools/list","id":1}',
        "--cli-binary-format", "raw-in-base64-out",
        "--region", REGION,
        "/tmp/tools_response.json"
    ], capture_output=True, text=True)

    with open("/tmp/tools_response.json") as f:
        tools = json.load(f).get("result", {}).get("tools", [])

    for t in tools:
        t["inputSchema"] = clean_schema(t.get("inputSchema", {"type": "object"}))

    # Create target
    config = json.dumps({"mcp": {"lambda": {"lambdaArn": arn, "toolSchema": {"inlinePayload": tools}}}})
    result = subprocess.run([
        "aws", "bedrock-agentcore-control", "create-gateway-target",
        "--gateway-identifier", GATEWAY_ID,
        "--name", target_name,
        "--target-configuration", config,
        "--credential-provider-configurations", '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]',
        "--region", REGION,
        "--query", "{Name:name,Status:status}",
        "--output", "table"
    ], capture_output=True, text=True)
    print(f"  {target_name}: {len(tools)} tools")
    if result.stdout:
        print(result.stdout)
    if result.returncode != 0:
        # A ConflictException means the target already exists (e.g. you re-ran
        # this step, or the automated Sync Lambda already created it). That is
        # the desired end state, so treat it as success rather than an error.
        if "ConflictException" in result.stderr or "already exists" in result.stderr:
            print(f"  {target_name}: already exists (skipping)")
        else:
            print(f"  ERROR: {result.stderr}")

# Add the three selected tools
for fn, target in [
    ("workshop-flights-mcp", "tg-workshop-flights-mcp"),
    ("workshop-hotels-mcp", "tg-workshop-hotels-mcp"),
    ("workshop-search-knowledge-base", "tg-search-knowledge-base"),
]:
    add_target(fn, target)
PYEOF

export REGION ACCOUNT_ID GATEWAY_ID
python3 /tmp/add_target.py
:::

## Verify Gateway Targets

List all targets to confirm only the selected tools were added:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws bedrock-agentcore-control list-gateway-targets \
  --gateway-identifier $GATEWAY_ID \
  --query "items[].{Name:name, Status:status}" \
  --output table --region $REGION
:::

You should see exactly three targets:
- `tg-workshop-flights-mcp`
- `tg-workshop-hotels-mcp`
- `tg-search-knowledge-base`

::alert[CurrentTime and FakeTools are not in the list — they remain on Path A (direct via NGINX) without audit logging or guardrails. This selective curation is the core value of the Tools Gateway.]{type="info"}
