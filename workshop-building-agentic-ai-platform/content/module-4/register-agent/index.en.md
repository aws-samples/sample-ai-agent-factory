---
title: "Register Agent in Registry (Optional)"
weight: 78
---

::alert[This step is optional. It demonstrates how to register the deployed agent as a discoverable A2A service in the Module 3b Registry. Skip this if you only want to run the travel agent.]{type="info"}

Now that the agent is deployed and running, the final step is to register it as a discoverable A2A (Agent-to-Agent) service in the Module 3b Registry. This closes the loop — the Registry becomes a complete catalog of both tools AND agents.

## Why Register the Agent?

The MCP and A2A Registry is the platform's single source of truth for all capabilities. By registering the deployed agent:

- Other agents can **discover** it programmatically via the Registry API
- Platform teams can **govern** agent-to-agent communication through the same approval workflows used for tools
- The agent becomes part of the enterprise **service mesh** — visible, versioned, and auditable

::alert[**About the `source /dev/stdin <<'BLOCK'` wrapper.** Each code block on this page is wrapped in a heredoc so it pastes into the terminal as a single command. Some browser terminals strip backslash line-continuations during paste, which turns multi-line commands into unrelated broken commands. Wrapping them in a heredoc and `source`-ing it keeps the multi-line form readable while making the paste robust; `source` runs the content in your current shell so variables like `REGION`, `REGISTRY_ID`, and the assumed-role credentials persist into the next block.]{type="info"}

## Get the Agent Endpoint URL

Retrieve the agent's runtime ARN from the FAST CDK stack outputs:

:::code{showCopyAction=true showLineNumbers=false language=bash}
source /dev/stdin <<'BLOCK'
REGION=$(aws configure get region)

RUNTIME_ARN=$(aws cloudformation describe-stacks --stack-name FAST-stack --query "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue" --output text --region $REGION)

RUNTIME_ID=$(echo "$RUNTIME_ARN" | awk -F'/' '{print $NF}')
AGENT_URL="https://${RUNTIME_ID}.runtime.bedrock-agentcore.${REGION}.amazonaws.com"

echo "Runtime ARN: $RUNTIME_ARN"
echo "Agent URL:   $AGENT_URL"
BLOCK
:::

## Find the Registry

The Registry was created interactively in Module 3b (there is no CloudFormation export for it). Look it up:

:::code{showCopyAction=true showLineNumbers=false language=bash}
source /dev/stdin <<'BLOCK'
REGION=$(aws configure get region)
REGISTRY_ID=$(aws bedrock-agentcore-control list-registries --query "registries[0].registryId" --output text --region $REGION)
echo "Registry ID: $REGISTRY_ID"
BLOCK
:::

::alert[If no registry is returned, you have not completed Step 3 of Module 3b. Go back and create the registry first.]{type="warning"}

## Assume the Publisher Persona

The Registry uses role-based access control. To create a registry record, you need the publisher role:

:::code{showCopyAction=true showLineNumbers=false language=bash}
source /dev/stdin <<'BLOCK'
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

REGION=$(aws configure get region)

PUBLISHER_ROLE_ARN=$(aws cloudformation list-exports --query "Exports[?Name=='ac-RegistryPublisherRoleArn'].Value" --output text --region $REGION)

CREDS=$(aws sts assume-role --role-arn "$PUBLISHER_ROLE_ARN" --role-session-name "agent-registration" --query "Credentials" --output json --region $REGION)

export AWS_ACCESS_KEY_ID=$(echo $CREDS | python3 -c "import sys,json; print(json.load(sys.stdin)['AccessKeyId'])")
export AWS_SECRET_ACCESS_KEY=$(echo $CREDS | python3 -c "import sys,json; print(json.load(sys.stdin)['SecretAccessKey'])")
export AWS_SESSION_TOKEN=$(echo $CREDS | python3 -c "import sys,json; print(json.load(sys.stdin)['SessionToken'])")

echo "Assumed publisher role"
BLOCK
:::

## Create the A2A Registry Record

Register the Travel Planning Agent as an A2A service. Build the full `descriptors` payload with Python (safer than shell string interpolation for nested JSON) and pass it to the AWS CLI via a `file://` reference:

:::code{showCopyAction=true showLineNumbers=false language=bash}
source /dev/stdin <<'BLOCK'
REGION=$(aws configure get region)
REGISTRY_ID=$(aws bedrock-agentcore-control list-registries --query "registries[0].registryId" --output text --region $REGION)

AGENT_URL_EXPORT="$AGENT_URL" python3 <<'PYEOF'
import json, os
agent_card = {
    "protocolVersion": "0.3",
    "name": "Travel Planning Agent",
    "description": "Plans trips by searching flights, hotels, and knowledge bases. Deployed on AgentCore Runtime via FAST.",
    "url": os.environ["AGENT_URL_EXPORT"],
    "version": "1.0.0",
    "capabilities": {"streaming": False, "pushNotifications": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {"id": "plan_trip", "name": "Plan Trip", "description": "Plans complete trips including flights and hotels", "tags": ["travel", "planning"]},
        {"id": "search_flights", "name": "Search Flights", "description": "Searches available flights between cities", "tags": ["travel", "flights"]},
        {"id": "search_hotels", "name": "Search Hotels", "description": "Searches available hotels in a destination", "tags": ["travel", "hotels"]},
    ],
}
descriptors = {"a2a": {"agentCard": {"schemaVersion": "0.3", "inlineContent": json.dumps(agent_card)}}}
with open("/tmp/descriptors.json", "w") as f:
    json.dump(descriptors, f)
print("Wrote /tmp/descriptors.json")
PYEOF

python3 -c "import json; json.load(open('/tmp/descriptors.json')); print('descriptors.json is valid JSON')"

aws bedrock-agentcore-control create-registry-record --registry-id "$REGISTRY_ID" --name "workshop_travel_agent" --descriptor-type A2A --descriptors file:///tmp/descriptors.json --record-version "1.0" --region $REGION
BLOCK
:::

You should see a response with the `recordId`.

## Submit for Approval and Approve

Registry records follow the state machine `DRAFT → PENDING_APPROVAL → APPROVED`. The publisher submits the record, then the admin approves it. This block is self-contained — it re-derives `REGISTRY_ID` and `RECORD_ID` so it works even if you paste it into a new shell. It is also idempotent: reruns on an already-approved record are no-ops.

Submit the record as the publisher (these credentials are still the publisher role from the previous step):

:::code{showCopyAction=true showLineNumbers=false language=bash}
source /dev/stdin <<'BLOCK'
REGION=$(aws configure get region)
REGISTRY_ID=$(aws bedrock-agentcore-control list-registries --query "registries[0].registryId" --output text --region $REGION)
RECORD_ID=$(aws bedrock-agentcore-control list-registry-records --registry-id "$REGISTRY_ID" --query "registryRecords[?name=='workshop_travel_agent'] | [0].recordId" --output text --region $REGION)

if [ -z "$RECORD_ID" ] || [ "$RECORD_ID" = "None" ]; then
  echo "ERROR: no registry record named 'workshop_travel_agent' found in registry $REGISTRY_ID." >&2
  echo "  Run the 'Create the A2A Registry Record' block above first." >&2
  return 1
fi
echo "Registry ID: $REGISTRY_ID"
echo "Record ID:   $RECORD_ID"

for i in $(seq 1 12); do
  STATUS=$(aws bedrock-agentcore-control get-registry-record --registry-id "$REGISTRY_ID" --record-id "$RECORD_ID" --query "status" --output text --region $REGION)
  [ "$STATUS" != "CREATING" ] && break
  echo "  [$((i*5))s] still CREATING..."
  sleep 5
done
echo "Record status: $STATUS"

if [ "$STATUS" = "DRAFT" ]; then
  aws bedrock-agentcore-control submit-registry-record-for-approval --registry-id "$REGISTRY_ID" --record-id "$RECORD_ID" --region $REGION
  echo "Submitted $RECORD_ID -> PENDING_APPROVAL"
fi
BLOCK
:::

Now switch to the admin role and approve the record. Re-derive the IDs here too, since `unset` wipes all env vars and we need fresh admin-side calls:

:::code{showCopyAction=true showLineNumbers=false language=bash}
source /dev/stdin <<'BLOCK'
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

REGION=$(aws configure get region)

ADMIN_ROLE_ARN=$(aws cloudformation list-exports --query "Exports[?Name=='ac-RegistryAdminRoleArn'].Value" --output text --region $REGION)

CREDS=$(aws sts assume-role --role-arn "$ADMIN_ROLE_ARN" --role-session-name "agent-approval" --query "Credentials" --output json --region $REGION)

export AWS_ACCESS_KEY_ID=$(echo $CREDS | python3 -c "import sys,json; print(json.load(sys.stdin)['AccessKeyId'])")
export AWS_SECRET_ACCESS_KEY=$(echo $CREDS | python3 -c "import sys,json; print(json.load(sys.stdin)['SecretAccessKey'])")
export AWS_SESSION_TOKEN=$(echo $CREDS | python3 -c "import sys,json; print(json.load(sys.stdin)['SessionToken'])")

REGISTRY_ID=$(aws bedrock-agentcore-control list-registries --query "registries[0].registryId" --output text --region $REGION)
RECORD_ID=$(aws bedrock-agentcore-control list-registry-records --registry-id "$REGISTRY_ID" --query "registryRecords[?name=='workshop_travel_agent'] | [0].recordId" --output text --region $REGION)

if [ -z "$RECORD_ID" ] || [ "$RECORD_ID" = "None" ]; then
  echo "ERROR: no registry record named 'workshop_travel_agent' found in registry $REGISTRY_ID." >&2
  return 1
fi

STATUS=$(aws bedrock-agentcore-control get-registry-record --registry-id "$REGISTRY_ID" --record-id "$RECORD_ID" --query "status" --output text --region $REGION)

if [ "$STATUS" = "APPROVED" ]; then
  echo "Record already APPROVED - nothing to do"
elif [ "$STATUS" = "PENDING_APPROVAL" ]; then
  aws bedrock-agentcore-control update-registry-record-status --registry-id "$REGISTRY_ID" --record-id "$RECORD_ID" --status APPROVED --status-reason "Approved for workshop" --region $REGION
  echo "Record approved"
else
  echo "Unexpected status: $STATUS - expected PENDING_APPROVAL or APPROVED" >&2
  return 1
fi
BLOCK
:::

## Verify the Agent in the Registry

Clear the assumed role credentials and list the registry records:

:::code{showCopyAction=true showLineNumbers=false language=bash}
source /dev/stdin <<'BLOCK'
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

REGION=$(aws configure get region)
REGISTRY_ID=$(aws bedrock-agentcore-control list-registries --query "registries[?name=='workshop-registry'].registryId | [0]" --output text --region $REGION)

# Write the listing to a temp file then process in Python. Piping `aws ... | python3 <<'PY'`
# on the same line gives Python its heredoc as stdin instead of the piped JSON.
aws bedrock-agentcore-control list-registry-records \
  --registry-id "$REGISTRY_ID" --region "$REGION" \
  --output json > /tmp/registry-records.json

python3 - <<'PYEOF'
import json
data = json.load(open("/tmp/registry-records.json"))
for record in data.get("registryRecords", []):
    print(f"  Name:   {record['name']}")
    print(f"  Type:   {record['descriptorType']}")
    print(f"  Status: {record.get('status', 'N/A')}")
    print()
PYEOF
BLOCK
:::

You should see the `workshop_travel_agent` record with type `A2A` and status `APPROVED`.

## What This Means

This closes the loop on the platform lifecycle:

| Step | Who | What |
|------|-----|------|
| Register tools | Platform team (Module 3b) | MCP tools cataloged in the Registry |
| Expose tools via gateway | Platform team (Module 3b) | AgentCore Gateway serves tools over MCP |
| Build agent | AI/ML engineer (Module 4) | Agent consumes tools and models |
| Deploy agent | AI/ML engineer (Module 4) | Agent runs on AgentCore Runtime |
| **Register agent** | **AI/ML engineer (Module 4)** | **Agent discoverable as A2A service** |

The Registry is now a complete catalog of both **tools** (MCP) and **agents** (A2A). Other agents can discover the Travel Planning Agent through the same Registry API, enabling agent-to-agent collaboration through the A2A protocol.

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4b-fast/notebooks/` and open **`07-register-agent.ipynb`**.
