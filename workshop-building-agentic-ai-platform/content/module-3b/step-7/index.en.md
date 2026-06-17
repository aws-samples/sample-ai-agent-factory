---
title: "Add Guardrails"
weight: 67
---

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

The AgentCore Gateway is routing tool calls, but every response passes through without filtering. In this step you add two governance layers that raw Lambda endpoints cannot provide:

- **Bedrock Guardrails** on tool output — screen every response for PII and harmful content
- **Group-based tool access control** — restrict which tools each Cognito group can invoke

These are the safety rails that demonstrate enterprise governance patterns. For production deployment, additional security hardening and review are required.

---

## Part A: Bedrock Guardrails on Tool Output

The response interceptor Lambda (`ac-gateway-response-interceptor`) screens every tool response through Bedrock Guardrails before returning it to the caller. If the guardrail detects PII or harmful content, the output is replaced with a sanitized version.

:::alert[**Compliance note.** This part demonstrates PII/PCI screening patterns on tool output. Bedrock Guardrails is one control, not a compliance certification. If your application processes payment card data (PCI-DSS), protected health information (HIPAA/PHI), or EU personal data (GDPR), additional controls and attestations are required. See [AWS Compliance Programs](https://aws.amazon.com/compliance/programs/).]{type="info"}

## CLI Walkthrough

### Step 1: Create a Bedrock Guardrail

First, set your region and check whether the guardrail already exists:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

EXISTING=$(aws bedrock list-guardrails --max-results 100 \
  --query "guardrails[?name=='workshop-tool-output-guardrail'].id | [0]" \
  --output text --region $REGION)

if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
  echo "Guardrail already exists: $EXISTING"
  GUARDRAIL_ID=$EXISTING
else
  echo "Guardrail not found — create it in the next step."
fi
:::

If the guardrail does not exist, create it:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

GUARDRAIL_RESPONSE=$(aws bedrock create-guardrail \
  --name "workshop-tool-output-guardrail" \
  --description "Screens tool outputs for PII and harmful content" \
  --sensitive-information-policy-config '{
    "piiEntitiesConfig": [
      {"type": "EMAIL", "action": "ANONYMIZE"},
      {"type": "PHONE", "action": "ANONYMIZE"},
      {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCK"},
      {"type": "CREDIT_DEBIT_CARD_NUMBER", "action": "BLOCK"}
    ]
  }' \
  --blocked-input-messaging "Input blocked by guardrail." \
  --blocked-outputs-messaging "Output blocked: sensitive information detected." \
  --region $REGION \
  --output json 2>/dev/null)

if [ -z "$GUARDRAIL_RESPONSE" ]; then
  echo "ERROR: create-guardrail returned no output - it may already exist. Re-run the previous 'list-guardrails' block to fetch its ID." >&2
else
  GUARDRAIL_ID=$(echo "$GUARDRAIL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['guardrailId'])")
  GUARDRAIL_VERSION=$(echo "$GUARDRAIL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])")
  echo "Guardrail ID:      $GUARDRAIL_ID"
  echo "Guardrail Version: $GUARDRAIL_VERSION"
fi
:::

This guardrail will:

| PII Type | Action |
|----------|--------|
| Email addresses | **Anonymize** — replaced with `[EMAIL]` |
| Phone numbers | **Anonymize** — replaced with `[PHONE]` |
| Social Security numbers | **Block** — entire response blocked |
| Credit/debit card numbers | **Block** — entire response blocked |

### Step 2: Attach the Guardrail to the Response Interceptor

Update the response interceptor Lambda's environment variables using a read-merge-update pattern to preserve any existing configuration:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)
RESPONSE_INTERCEPTOR_FN="ac-gateway-response-interceptor"

if [ -z "$GUARDRAIL_ID" ]; then
  echo "ERROR: GUARDRAIL_ID is empty - re-run the Step 1 create-guardrail or list-guardrails block to set it" >&2
else
  CURRENT_ENV=$(aws lambda get-function-configuration \
    --function-name $RESPONSE_INTERCEPTOR_FN \
    --query "Environment.Variables" \
    --output json --region $REGION)

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
fi
:::

Wait for the update to propagate:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

aws lambda wait function-updated-v2 \
  --function-name ac-gateway-response-interceptor \
  --region $REGION \
  && echo "Lambda updated and ready."
:::

### Step 3: Test the Guardrail

Use the Bedrock Runtime `apply-guardrail` API directly to verify the guardrail catches PII:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

cat > /tmp/apply-guardrail.json << EOF
{
  "guardrailIdentifier": "$GUARDRAIL_ID",
  "guardrailVersion": "${GUARDRAIL_VERSION:-DRAFT}",
  "source": "OUTPUT",
  "content": [
    {
      "text": {
        "text": "Customer record found. Name: Jane Doe, SSN: 000-00-0000, Credit Card: 4111-1111-1111-1111, Email: jane.doe@example.com, Phone: (555) 123-4567. Account balance: \$12,450.00."
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

The response should show `"action": "GUARDRAIL_INTERVENED"` with:

- SSN and credit card number **blocked** (entire output replaced)
- Email and phone **anonymized** (replaced with placeholders)

::alert[The `source: OUTPUT` parameter tells the guardrail to evaluate the text as tool output (not user input). This applies the output-side filters you configured.]{type="info"}

### Fail-Open vs. Fail-Closed

The response interceptor uses a **fail-open** design: if the Bedrock Guardrails API call fails (timeout, service error, throttle), the original tool output passes through unchanged. This prevents guardrail outages from blocking all tool calls.

In production, you may prefer **fail-closed** — if the guardrail check fails, return an error instead of unscreened content. The choice depends on your risk tolerance:

| Design | Behavior on Guardrail Failure | Best For |
|--------|-------------------------------|----------|
| **Fail-open** | Tool output passes through unscreened | Availability-first workloads |
| **Fail-closed** | Tool call returns an error | Compliance-first workloads |

::alert[For this workshop, fail-open is the default. To switch to fail-closed, update the interceptor code to raise an exception when the guardrail call fails instead of returning the original output.]{type="warning"}

---

## Part B: Group-Based Tool Access Control

The request interceptor Lambda (`ac-gateway-request-interceptor`) enforces group-based access policies. The CloudFormation stack deploys a default `TOOL_ACCESS_POLICY` that maps Cognito groups to tool name patterns:

### Default Policy (deployed by CFN)

```json
{
  "_default": ["*"],
  "gateway-admins": ["*"],
  "gateway-developers": ["*flights*", "*hotels*", "*knowledge*",
    "search_flights", "get_flight_details", "search_flights_by_budget",
    "search_hotels", "get_hotel_details", "search_hotels_by_budget",
    "search-knowledge-base"]
}
```

| Key | Who | Access |
|-----|-----|--------|
| `_default` | M2M clients (no `cognito:groups` in JWT) | All tools — trusted service accounts |
| `gateway-admins` | Users in the `gateway-admins` Cognito group | All tools |
| `gateway-developers` | Users in the `gateway-developers` Cognito group | Travel tools only (flights, hotels, knowledge base) |

If a caller's groups do not match any rule that permits the requested tool, the interceptor returns an access denied error before the tool is invoked. M2M clients (which have no `cognito:groups` claim) fall back to the `_default` key.

### Step 4: Verify the Current Policy

The policy is already deployed. Verify it:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

aws lambda get-function-configuration \
  --function-name ac-gateway-request-interceptor \
  --query "Environment.Variables.TOOL_ACCESS_POLICY" \
  --output text --region $REGION | python3 -m json.tool
:::

### Step 5: Customize the Policy (Optional)

To restrict access further, update the policy using a read-merge-update pattern:

::alert[When copying the code block below, include the complete heredoc — copy the `python3 << 'PYEOF'` header line, all the Python code, AND the closing `PYEOF` line as a single block.]{type="info"}

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)
export REGION

# Build the updated environment payload in a single Python block:
#   1. Fetch current env vars via subprocess (stdin-safe)
#   2. Merge in the new TOOL_ACCESS_POLICY (wildcards match gateway-prefixed
#      tool names, e.g. tg-workshop-flights-mcp___search_flights)
#   3. Write out the Lambda --environment payload to /tmp/env.json
python3 <<'PYEOF'
import json, os, subprocess, sys

region = os.environ["REGION"]
fn_name = "ac-gateway-request-interceptor"

result = subprocess.run(
    ["aws", "lambda", "get-function-configuration",
     "--function-name", fn_name,
     "--region", region,
     "--output", "json"],
    capture_output=True, text=True, check=True,
)
current = json.loads(result.stdout)
env = current.get("Environment", {}).get("Variables", {})

# Example: restrict developers to read-only travel tools (no budget search)
env["TOOL_ACCESS_POLICY"] = json.dumps({
    "_default": ["*"],
    "gateway-admins": ["*"],
    "gateway-developers": [
        "*search_flights*",
        "*search_hotels*",
        "*get_flight_details*",
        "*get_hotel_details*",
        "*search-knowledge-base*",
    ],
})

with open("/tmp/env.json", "w") as f:
    json.dump({"Variables": env}, f)
print("Wrote /tmp/env.json")
PYEOF

aws lambda update-function-configuration \
  --function-name ac-gateway-request-interceptor \
  --environment file:///tmp/env.json \
  --region $REGION \
  --query "FunctionName" --output text

echo "Updated ac-gateway-request-interceptor with custom access policy"
:::

Wait for the update to propagate:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

aws lambda wait function-updated-v2 \
  --function-name ac-gateway-request-interceptor \
  --region $REGION \
  && echo "Lambda updated and ready."
:::

::alert[Both the request interceptor and the response interceptor use the same `TOOL_ACCESS_POLICY`. The request interceptor blocks unauthorized `tools/call` requests. The response interceptor filters `tools/list` results so users only see tools they can access. Update both if you customize the policy.]{type="info"}

---

## Part C: Policy Engine — Cedar Policies (Optional)

::alert[This section is optional. Cedar policies are an advanced feature for declarative access control. Skip ahead to the Cleanup step if you are short on time.]{type="info"}

The interceptor-based ACL above uses custom Lambda logic. AgentCore also provides a **Policy Engine** — a managed service that evaluates [Cedar](https://www.cedarpolicy.com/) policies. Cedar is a declarative, auditable language purpose-built for authorization.

This gives you two complementary access control layers:
- **Layer 1 (Interceptor ACL):** Fast, custom logic, fail-open — already configured above
- **Layer 2 (Cedar Policy Engine):** Declarative, AWS-managed, auditable — configured here

### Step 6: Create a Policy Engine

:::code{showCopyAction=true showLineNumbers=false language=python}
import boto3

region = boto3.session.Session().region_name
cp_client = boto3.client("bedrock-agentcore-control", region_name=region)

ENGINE_NAME = "workshop_policy_engine"

existing = cp_client.list_policy_engines().get("policyEngines", [])
engine_match = [e for e in existing if e.get("name") == ENGINE_NAME]

if engine_match:
    ENGINE_ID = engine_match[0]["policyEngineId"]
    print(f"Policy Engine already exists: {ENGINE_ID}")
else:
    resp = cp_client.create_policy_engine(name=ENGINE_NAME)
    ENGINE_ID = resp["policyEngineId"]
    print(f"Created Policy Engine: {ENGINE_ID}")
:::

### How the Cedar Schema Maps to Your Gateway

The Policy Engine automatically discovers the Cedar entity types from your Gateway:

| Cedar Entity | Maps To | Example |
|-------------|---------|---------|
| `AgentCore::Gateway` | The Gateway resource (by ARN) | `AgentCore::Gateway::"arn:aws:bedrock-agentcore:...:gateway/..."` |
| `AgentCore::Action` | Gateway targets (one per target) | `AgentCore::Action::"tg-workshop-flights-mcp"` |
| `AgentCore::OAuthUser` | JWT-authenticated callers | Cognito M2M clients |
| `AgentCore::IamEntity` | IAM-authenticated callers | IAM roles and users |

Cedar actions are **gateway targets**, not generic verbs. Each `GatewayTarget` you created (flights, hotels, search-kb) becomes a distinct action.

### Step 7: Create Cedar Policies

Define two policies — developer restricted and admin full access:

:::code{showCopyAction=true showLineNumbers=false language=python}
# Get the Gateway ARN for Cedar resource constraints
cfn = boto3.client("cloudformation", region_name=region)
exports = {e["Name"]: e["Value"] for e in cfn.get_paginator("list_exports").paginate().search("Exports[]")}
GATEWAY_ID = exports.get("ac-GatewayId", "")
ACCOUNT_ID = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
GATEWAY_ARN = f"arn:aws:bedrock-agentcore:{region}:{ACCOUNT_ID}:gateway/{GATEWAY_ID}"

# Developer policy: only flights + knowledge-base targets (read-only tools)
DEVELOPER_POLICY = f"""
permit(
    principal,
    action in [
        AgentCore::Action::"tg-workshop-flights-mcp",
        AgentCore::Action::"tg-workshop-kb-search"
    ],
    resource == AgentCore::Gateway::"{GATEWAY_ARN}"
);
""".strip()

# Admin policy: all 3 targets explicitly listed
ADMIN_POLICY = f"""
permit(
    principal,
    action in [
        AgentCore::Action::"tg-workshop-flights-mcp",
        AgentCore::Action::"tg-workshop-hotels-mcp",
        AgentCore::Action::"tg-workshop-kb-search"
    ],
    resource == AgentCore::Gateway::"{GATEWAY_ARN}"
);
""".strip()

for name, statement in [("developer_tool_access", DEVELOPER_POLICY), ("admin_full_access", ADMIN_POLICY)]:
    try:
        resp = cp_client.create_policy(
            policyEngineId=ENGINE_ID,
            name=name,
            definition={"cedar": {"statement": statement}},
        )
        print(f"Created policy: {name} ({resp['policyId']})")
    except cp_client.exceptions.ConflictException:
        print(f"Policy {name} already exists")
:::

::alert[The Policy Engine rejects wildcard-action policies as "Overly Permissive." You must explicitly list each gateway target in the `action in [...]` clause. This enforces least-privilege by default.]{type="warning"}

### Step 8: Verify Policies

:::code{showCopyAction=true showLineNumbers=false language=python}
policies = cp_client.list_policies(policyEngineId=ENGINE_ID).get("policies", [])
print(f"Policy Engine {ENGINE_ID} has {len(policies)} policies:")
for p in policies:
    print(f"  {p.get('name', '?')} — {p.get('status', '?')}")
:::

::alert[Cedar policies are evaluated by the AgentCore Policy Engine, not by the interceptor Lambda. In a production deployment, the interceptor would call the Policy Engine's authorization API before forwarding each request. The two layers are complementary: interceptor ACL is fast and custom, Cedar policies are declarative and auditable.]{type="info"}

---

## What You Configured

| Layer | Component | What It Does |
|-------|-----------|--------------|
| **Output screening** | Response Interceptor + Bedrock Guardrails | Blocks PII and harmful content in tool responses |
| **Access control L1** | Request Interceptor + TOOL_ACCESS_POLICY | Fast, custom tool filtering by Cognito group |
| **Access control L2** | Policy Engine + Cedar policies | Declarative, auditable authorization rules |

Three independent governance layers — all enforced by the platform, transparent to individual tool Lambdas.

---

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-3b-agentcore/notebooks/` and open the corresponding notebook.

Open **`07-guardrails.ipynb`** to run the same steps interactively. The notebook covers:

1. **Create a Bedrock Guardrail** — configures PII detection with BLOCK and ANONYMIZE actions
2. **Attach to Response Interceptor** — updates the Lambda env vars using read-merge-update
3. **Test the guardrail** — calls `apply-guardrail` with simulated PII content and shows what gets blocked vs. anonymized
4. **Set the tool access policy** — configures group-based filtering on the request interceptor
5. **Verify access control** — compares admin vs. developer tool visibility
6. **Create a Policy Engine** — provisions the AgentCore Cedar policy engine
7. **Create Cedar policies** — defines declarative developer and admin access rules
8. **Verify policies** — lists all policies in the engine
