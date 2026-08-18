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

## Part A: Bedrock Guardrails on tool output

The response interceptor Lambda (`ac-gateway-response-interceptor`) screens every tool response through Bedrock Guardrails before returning it to the caller. If the guardrail detects PII or harmful content, the output is replaced with a sanitized version.

::alert[**Compliance note.** This part demonstrates PII/PCI screening patterns on tool output. Bedrock Guardrails is one control, not a compliance certification. If your application processes payment card data (PCI-DSS), protected health information (HIPAA/PHI), or EU personal data (GDPR), additional controls and attestations are required. See [AWS Compliance Programs](https://aws.amazon.com/compliance/programs/).]{type="info"}

## CLI walkthrough

### Step 1: Create a Bedrock guardrail

Guardrail names are unique per account, so this block reuses
`workshop-tool-output-guardrail` if it is already there and creates it otherwise.
Either way it leaves `GUARDRAIL_ID` set, so you can re-run the block — or the
whole page — without it failing the second time:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

GUARDRAIL_ID=$(aws bedrock list-guardrails --max-results 100 \
  --query "guardrails[?name=='workshop-tool-output-guardrail'].id | [0]" \
  --output text --region $REGION)

if [ -n "$GUARDRAIL_ID" ] && [ "$GUARDRAIL_ID" != "None" ]; then
  echo "Guardrail already exists: $GUARDRAIL_ID"
else
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
    --content-policy-config '{
      "filtersConfig": [
        {"type": "HATE", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "VIOLENCE", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "SEXUAL", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "INSULTS", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "MISCONDUCT", "inputStrength": "HIGH", "outputStrength": "HIGH"}
      ]
    }' \
    --blocked-input-messaging "Input blocked by guardrail." \
    --blocked-outputs-messaging "Output blocked: sensitive information detected." \
    --region $REGION \
    --output json)
  GUARDRAIL_ID=$(echo "$GUARDRAIL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['guardrailId'])")
  GUARDRAIL_VERSION=$(echo "$GUARDRAIL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])")
  echo "Guardrail created: $GUARDRAIL_ID (version $GUARDRAIL_VERSION)"
fi
:::

::alert[If this block prints nothing but an error, you are missing `bedrock:ListGuardrails` or `bedrock:CreateGuardrail`. Everything after Step 1 reads `$GUARDRAIL_ID`, so do not continue until it prints an ID.]{type="warning"}

This guardrail will:

| PII Type | Action |
|----------|--------|
| Email addresses | **Anonymize** — replaced with `[EMAIL]` |
| Phone numbers | **Anonymize** — replaced with `[PHONE]` |
| Social Security numbers | **Block** — entire response blocked |
| Credit/debit card numbers | **Block** — entire response blocked |

The content policy is the second half: `HATE`, `VIOLENCE`, `SEXUAL`, `INSULTS` and
`MISCONDUCT` at `HIGH` strength on both input and output. PII screening is what the
test in Step 3 exercises, but a tool that returns text from an untrusted upstream can
return more than PII, which is why both policies are attached to the same guardrail.

### Step 2: Attach the guardrail to the response interceptor

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

### Step 3: Test the guardrail

Use the Bedrock Runtime `apply-guardrail` API directly to verify the guardrail catches PII:

The record below is synthetic and uses reserved placeholders only: the reserved `000-00-0000` SSN, an `example.com` address, a `555` phone number, and `4000-0000-0000-0000` as the card number. That card number fails the Luhn checksum, so it can never be a real card. It still has to keep the shape of a card — four groups of four digits next to a card label — because the `CREDIT_DEBIT_CARD_NUMBER` detector matches on format and surrounding context, not on the checksum. Replacing the digits with `XXXX-XXXX-XXXX-XXXX` is not reliable: the detector only picks a fully masked value up when other PII sits beside it.

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

# Synthetic test data only -- reserved/documented placeholders, no real PII.
cat > /tmp/apply-guardrail.json << EOF
{
  "guardrailIdentifier": "$GUARDRAIL_ID",
  "guardrailVersion": "${GUARDRAIL_VERSION:-DRAFT}",
  "source": "OUTPUT",
  "content": [
    {
      "text": {
        "text": "Customer record found. Name: Jane Doe, SSN: 000-00-0000, Credit Card: 4000-0000-0000-0000, Email: jane.doe@example.com, Phone: (555) 123-4567. Account balance: \$12,450.00."
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

### Fail-open vs. fail-closed

The response interceptor uses a **fail-open** design: if the Bedrock Guardrails API call fails (timeout, service error, throttle), the original tool output passes through unchanged. This prevents guardrail outages from blocking all tool calls.

In production, you may prefer **fail-closed** — if the guardrail check fails, return an error instead of unscreened content. The choice depends on your risk tolerance:

| Design | Behavior on Guardrail Failure | Best For |
|--------|-------------------------------|----------|
| **Fail-open** | Tool output passes through unscreened | Availability-first workloads |
| **Fail-closed** | Tool call returns an error | Compliance-first workloads |

::alert[For this workshop, fail-open is the default. To switch to fail-closed, update the interceptor code to raise an exception when the guardrail call fails instead of returning the original output.]{type="warning"}

---

## Part B: Group-based tool access control

The request interceptor Lambda (`ac-gateway-request-interceptor`) enforces group-based access policies. The CloudFormation stack deploys a default `TOOL_ACCESS_POLICY` that maps Cognito groups to tool name patterns:

### Default policy (deployed by CFN)

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

### Step 4: Verify the current policy

The policy is already deployed. Verify it:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

# An empty policy is a legitimate state here — notebook 06 clears the variable so its
# M2M calls cannot be denied — and this block only *displays* the current value. Piping
# an empty string straight into json.tool aborts with a bare
# "Expecting value: line 2 column 1 (char 1)", which says nothing useful.
POLICY=$(aws lambda get-function-configuration \
  --function-name ac-gateway-request-interceptor \
  --query "Environment.Variables.TOOL_ACCESS_POLICY" \
  --output text --region $REGION)

if [ -z "$POLICY" ] || [ "$POLICY" = "None" ]; then
  echo "No TOOL_ACCESS_POLICY is set, so every caller can reach every tool."
  echo "Step 5 below sets one."
else
  echo "$POLICY" | python3 -m json.tool
fi
:::

### Step 5: Customize the policy (optional)

To restrict access further, update the policy using a read-merge-update pattern:

::alert[When copying the code block below, include the complete heredoc — copy the `python3 << 'PYEOF'` header line, all the Python code, AND the closing `PYEOF` line as a single block.]{type="info"}

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)
export REGION

# Build the updated environment payload in a single Python block:
#   1. Fetch each function's current env vars via subprocess (stdin-safe)
#   2. Merge in the new TOOL_ACCESS_POLICY (wildcards match gateway-prefixed
#      tool names, e.g. tg-workshop-flights-mcp___search_flights)
#   3. Write one Lambda --environment payload per function
# Both interceptors are updated: the request interceptor enforces tools/call,
# the response interceptor filters tools/list. Updating only one leaves the
# other enforcing the previous policy.
python3 <<'PYEOF'
import json, os, subprocess

region = os.environ["REGION"]

# Example: restrict developers to read-only travel tools (no budget search)
policy = json.dumps({
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

for fn_name in ("ac-gateway-request-interceptor", "ac-gateway-response-interceptor"):
    result = subprocess.run(
        ["aws", "lambda", "get-function-configuration",
         "--function-name", fn_name,
         "--region", region,
         "--output", "json"],
        capture_output=True, text=True, check=True,
    )
    current = json.loads(result.stdout)
    # Merge into the function's OWN env so its other variables survive --
    # the response interceptor also carries BEDROCK_GUARDRAIL_ID/VERSION.
    env = current.get("Environment", {}).get("Variables", {})
    env["TOOL_ACCESS_POLICY"] = policy

    out_path = f"/tmp/env-{fn_name}.json"
    with open(out_path, "w") as f:
        json.dump({"Variables": env}, f)
    print(f"Wrote {out_path}")
PYEOF

for FN in ac-gateway-request-interceptor ac-gateway-response-interceptor; do
  aws lambda update-function-configuration \
    --function-name "$FN" \
    --environment "file:///tmp/env-${FN}.json" \
    --region $REGION \
    --query "FunctionName" --output text
  echo "Updated $FN with custom access policy"
done
:::

Wait for both updates to propagate:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

for FN in ac-gateway-request-interceptor ac-gateway-response-interceptor; do
  aws lambda wait function-updated-v2 \
    --function-name "$FN" \
    --region $REGION \
    && echo "$FN updated and ready."
done
:::

::alert[Both interceptor Lambdas are deployed with the same `TOOL_ACCESS_POLICY` value — check with `aws lambda get-function-configuration --function-name ac-gateway-response-interceptor --query 'Environment.Variables.TOOL_ACCESS_POLICY'`. The request interceptor uses it to block unauthorized `tools/call` requests; the response interceptor uses it to filter `tools/list` results so callers only see tools they can access. That is why the loop above updates **both**: changing only one leaves the other enforcing the previous policy. The response interceptor also carries `BEDROCK_GUARDRAIL_ID` / `BEDROCK_GUARDRAIL_VERSION` from Part A, which govern output screening and are independent of `TOOL_ACCESS_POLICY`.]{type="info"}

---

## Part C: Policy engine — Cedar policies (optional)

::alert[This section is optional. Cedar policies are an advanced feature for declarative access control. Skip ahead to the Cleanup step if you are short on time.]{type="info"}

The interceptor-based ACL above uses custom Lambda logic. AgentCore also provides a **Policy Engine** — a managed service that evaluates [Cedar](https://www.cedarpolicy.com/) policies. Cedar is a declarative, auditable language purpose-built for authorization.

This gives you two complementary access control layers:
- **Layer 1 (Interceptor ACL):** Fast, custom logic, fail-open — already configured above
- **Layer 2 (Cedar Policy Engine):** Declarative, AWS-managed, auditable — configured here

### Step 6: Create a policy engine

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

# A new Policy Engine starts in CREATING, and the UpdateGateway call further down
# rejects an engine that is not yet ACTIVE ("must be in ACTIVE state to be
# associated to a Gateway"). Wait here so the attach step cannot race it.
import time

engine_status = "UNKNOWN"
for attempt in range(24):  # up to 120s
    engine_status = cp_client.get_policy_engine(policyEngineId=ENGINE_ID).get("status", "UNKNOWN")
    if engine_status == "ACTIVE":
        break
    print(f"  [{(attempt + 1) * 5}s] Policy Engine status: {engine_status}")
    time.sleep(5)
else:
    raise RuntimeError(
        f"Policy Engine {ENGINE_ID} did not reach ACTIVE (last status: {engine_status})."
    )

print(f"Engine ID: {ENGINE_ID} (status: {engine_status})")
:::

### How the Cedar schema maps to your gateway

The Policy Engine automatically discovers the Cedar entity types from your Gateway:

| Cedar Entity | Maps To | Example |
|-------------|---------|---------|
| `AgentCore::Gateway` | The Gateway resource (by ARN) | `AgentCore::Gateway::"arn:aws:bedrock-agentcore:...:gateway/..."` |
| `AgentCore::Action` | Gateway targets (one per target) | `AgentCore::Action::"tg-workshop-flights-mcp"` |
| `AgentCore::OAuthUser` | JWT-authenticated callers | Cognito M2M clients |
| `AgentCore::IamEntity` | IAM-authenticated callers | IAM roles and users |

Cedar actions are **gateway targets**, not generic verbs. Each `GatewayTarget` you created (flights, hotels, search-kb) becomes a distinct action.

::alert[**Target names are per-gateway.** The Cedar policies below name `tg-workshop-flights-mcp`, `tg-workshop-hotels-mcp`, and `tg-workshop-kb-search` — the targets `workshop-agentcore-stack` creates on **this** module's AgentCore Gateway. Module 3a's Tools Gateway is a separate gateway with its own targets (`tg-workshop-flights-mcp`, `tg-workshop-hotels-mcp`, `tg-search-knowledge-base`), so do not copy names between the two. Confirm yours with `aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$GATEWAY_ID" --region $REGION --query "items[].name"`.]{type="warning"}

### Step 7: Create Cedar policies

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

### Step 8: Verify policies

:::code{showCopyAction=true showLineNumbers=false language=python}
policies = cp_client.list_policies(policyEngineId=ENGINE_ID).get("policies", [])
print(f"Policy Engine {ENGINE_ID} has {len(policies)} policies:")
for p in policies:
    print(f"  {p.get('name', '?')} — {p.get('status', '?')}")
:::

::alert[Cedar policies are evaluated by the AgentCore Policy Engine, not by the interceptor Lambda. In a production deployment, the interceptor would call the Policy Engine's authorization API before forwarding each request. The two layers are complementary: interceptor ACL is fast and custom, Cedar policies are declarative and auditable.]{type="info"}

The engine and its policies now exist, but this CLI path deliberately stops short of attaching the engine to the Gateway. If you want to attach it (the notebook walkthrough does), two details matter:

- `UpdateGateway` **replaces** the whole Gateway configuration rather than merging into it. Send back every field you want to keep — omitting `interceptorConfigurations` deletes the Lambda ACL you built in Part B.
- Attach with `"mode": "LOG_ONLY"`, not `"ENFORCE"`. Cedar actions map to gateway *targets*, and nothing authorizes tool discovery itself, so `ENFORCE` makes `tools/list` return an empty list and every later tool call fails with `Unknown tool`. `LOG_ONLY` evaluates each request against your policies and records the decision.

---

## What you configured

| Layer | Component | What It Does |
|-------|-----------|--------------|
| **Output screening** | Response Interceptor + Bedrock Guardrails | Blocks PII and harmful content in tool responses |
| **Access control L1** | Request Interceptor + TOOL_ACCESS_POLICY | Fast, custom tool filtering by Cognito group |
| **Access control L2** | Policy Engine + Cedar policies | Declarative, auditable authorization rules |

Three independent governance layers — all enforced by the platform, transparent to individual tool Lambdas.

---

## Notebook walkthrough (optional alternative)

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
