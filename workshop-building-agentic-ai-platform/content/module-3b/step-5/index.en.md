---
title: "Discover & Search"
weight: 65
---

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

Switch personas — you are now an **agent developer** (Consumer) who discovers tools to wire into an agent.

::alert[Only **APPROVED** records appear in search results. Records in DRAFT or PENDING_APPROVAL status are invisible to the Consumer. The platform team controls what is published.]{type="info"}

::alert[**What you just built.** In Step 4, you registered 3 travel tools and exercised the Publisher/Admin approval workflow. Those approved records — flights, hotels, and knowledge base — are now visible in your Registry. In this step, you'll switch to the **Consumer** persona (agent developer) and discover these tools by searching the Registry. The approval gate you just saw ensures only trusted tools appear in consumer search results.]{type="info"}

## CLI Walkthrough

### Step 1: Gather Variables and Assume Consumer Role

:::code{showCopyAction=true showLineNumbers=false language=bash}
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

REGION=$(aws configure get region)

REGISTRY_ID=$(aws bedrock-agentcore-control list-registries \
  --query "registries[?name=='workshop-registry'].registryId | [0]" \
  --output text --region $REGION)

CONSUMER_ROLE_ARN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='ac-RegistryConsumerRoleArn'].Value" \
  --output text --region $REGION)

if [ -z "$REGISTRY_ID" ] || [ "$REGISTRY_ID" = "None" ] || [ -z "$CONSUMER_ROLE_ARN" ] || [ "$CONSUMER_ROLE_ARN" = "None" ]; then
  echo "ERROR: REGISTRY_ID or CONSUMER_ROLE_ARN is empty - confirm the registry was created in Step 3 and workshop-agentcore-stack is CREATE_COMPLETE" >&2
else
  echo "Registry ID: $REGISTRY_ID"
fi
:::

Now assume the Consumer role:

:::code{showCopyAction=true showLineNumbers=false language=bash}
if [ -z "$CONSUMER_ROLE_ARN" ] || [ "$CONSUMER_ROLE_ARN" = "None" ]; then
  echo "ERROR: CONSUMER_ROLE_ARN is empty - re-run the previous block first" >&2
else
  CONSUMER_CREDS=$(aws sts assume-role \
    --role-arn "$CONSUMER_ROLE_ARN" \
    --role-session-name consumer-session \
    --query 'Credentials' --output json)

  if [ -n "$CONSUMER_CREDS" ]; then
    export AWS_ACCESS_KEY_ID=$(echo "$CONSUMER_CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['AccessKeyId'])")
    export AWS_SECRET_ACCESS_KEY=$(echo "$CONSUMER_CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['SecretAccessKey'])")
    export AWS_SESSION_TOKEN=$(echo "$CONSUMER_CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['SessionToken'])")
    echo "Consumer role: $(aws sts get-caller-identity --query Arn --output text)"
  else
    echo "ERROR: sts:AssumeRole returned empty credentials" >&2
  fi
fi
:::

### Step 2: List Approved Records

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws bedrock-agentcore-control list-registry-records \
  --registry-id $REGISTRY_ID \
  --query "registryRecords[].{Name:name, Status:status, Type:descriptorType}" \
  --output table --region $REGION
:::

You should see all 3 tools with `APPROVED` status.

### Step 3: Search by Keyword

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws bedrock-agentcore-control list-registry-records \
  --registry-id $REGISTRY_ID \
  --query "registryRecords[?contains(name, 'flights')].{Name:name, Description:description}" \
  --output table --region $REGION
:::

### Step 4: Negative Test — Consumer Cannot Create

Verify the Consumer cannot register new tools:

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws bedrock-agentcore-control create-registry-record \
  --registry-id $REGISTRY_ID \
  --name "unauthorized-tool" \
  --description "This should fail" \
  --descriptor-type MCP \
  --descriptors '{"mcp":{"server":{"schemaVersion":"2025-12-11","inlineContent":"{\"name\":\"test\"}"}}}' \
  --record-version "1.0.0" \
  --region $REGION > /tmp/consumer-denied.log 2>&1 || true
head -3 /tmp/consumer-denied.log
:::

You should see `AccessDeniedException` — the Consumer role is read-only.

### Step 5: Clean Up Consumer Credentials

Return to the base role for the next step:

:::code{showCopyAction=true showLineNumbers=false language=bash}
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
echo "Back to base role: $(aws sts get-caller-identity --query Arn --output text)"
:::

---

## Notebook Walkthrough (Optional alternative)

> This notebook (05-discover-search.ipynb) is an alternative path covering the same material as the CLI section above — follow *either* path, you do not need to do both. The notebook covers additional search methods including Cognito JWT-based HTTP search, semantic search, and the Registry MCP endpoint.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-3b-agentcore/notebooks/` and open **`05-discover-search.ipynb`**.
