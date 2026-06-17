---
title: "Register Tools"
weight: 64
---

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

Register three MCP tools, exercise the Publisher/Admin approval workflow, and verify the governance boundary.

## CLI Walkthrough

### Step 1: Assume the Publisher Persona

The Publisher can register tools but **cannot** approve them. First, unset any assumed role and gather the role ARN:

:::code{showCopyAction=true showLineNumbers=false language=bash}
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

REGION=$(aws configure get region)

PUBLISHER_ROLE_ARN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='ac-RegistryPublisherRoleArn'].Value" \
  --output text --region $REGION)

REGISTRY_ID=$(aws bedrock-agentcore-control list-registries \
  --query "registries[?name=='workshop-registry'].registryId | [0]" \
  --output text --region $REGION)

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Verify all three values were resolved, AND export them so downstream Python
# scripts (which read os.environ) can see them. `exit` would kill an
# interactive shell, so we echo a clear error and keep the session alive.
MISSING=""
for V in PUBLISHER_ROLE_ARN REGISTRY_ID ACCOUNT_ID REGION; do
  if [ -z "${!V}" ] || [ "${!V}" = "None" ]; then
    MISSING="$MISSING $V"
  fi
done
if [ -n "$MISSING" ]; then
  echo "ERROR: empty or missing value(s):$MISSING" >&2
  echo "  PUBLISHER_ROLE_ARN - needs workshop-agentcore-stack CREATE_COMPLETE" >&2
  echo "  REGISTRY_ID        - needs Step 3 (Create the Registry) completed first" >&2
  echo "  ACCOUNT_ID         - AWS credentials not configured" >&2
else
  export PUBLISHER_ROLE_ARN REGISTRY_ID ACCOUNT_ID REGION
  export AWS_DEFAULT_REGION="$REGION"
  echo "Publisher role: $PUBLISHER_ROLE_ARN"
  echo "Registry ID:    $REGISTRY_ID"
  echo "Account ID:     $ACCOUNT_ID"
fi
:::

Now assume the Publisher role:

:::code{showCopyAction=true showLineNumbers=false language=bash}
if [ -z "$PUBLISHER_ROLE_ARN" ] || [ "$PUBLISHER_ROLE_ARN" = "None" ]; then
  echo "ERROR: PUBLISHER_ROLE_ARN is empty - re-run the previous block first" >&2
else
  PUB_CREDS=$(aws sts assume-role \
    --role-arn "$PUBLISHER_ROLE_ARN" \
    --role-session-name registry-publisher-session \
    --query 'Credentials' --output json)

  if [ -n "$PUB_CREDS" ]; then
    export AWS_ACCESS_KEY_ID=$(echo "$PUB_CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['AccessKeyId'])")
    export AWS_SECRET_ACCESS_KEY=$(echo "$PUB_CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['SecretAccessKey'])")
    export AWS_SESSION_TOKEN=$(echo "$PUB_CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['SessionToken'])")
    echo "Assumed role: $(aws sts get-caller-identity --query Arn --output text)"
  else
    echo "ERROR: sts:AssumeRole returned empty credentials" >&2
  fi
fi
:::

### Step 2: Register Three Tools

The script below registers the three travel tools (Flights, Hotels, Knowledge Base) in the Registry, waits for them to reach DRAFT status, then submits them for admin approval:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cat > /tmp/register_tools.py << 'PYEOF'
import boto3, json, time, os, sys

region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION", "us-west-2")
registry_id = os.environ.get("REGISTRY_ID", "").strip()
account_id = os.environ.get("ACCOUNT_ID", "").strip()

missing = [v for v, x in (("REGISTRY_ID", registry_id), ("ACCOUNT_ID", account_id)) if not x or x == "None"]
if missing:
    sys.stderr.write(
        f"ERROR: missing environment variable(s): {', '.join(missing)}\n"
        "Re-run the Step 1 block (Assume the Publisher Persona) — it exports "
        "REGISTRY_ID, ACCOUNT_ID, and PUBLISHER_ROLE_ARN into the shell.\n"
    )
    sys.exit(0)  # Exit the Python subprocess cleanly - does NOT kill the shell.

client = boto3.client("bedrock-agentcore-control", region_name=region)

tools = [
    {
        "name": "workshop-flights-mcp",
        "description": "Flight search tools — search by route, date, or budget",
        "tools": [
            {"name": "search_flights", "description": "Search flights by origin, destination, and date", "inputSchema": {"type": "object", "properties": {"origin": {"type": "string"}, "destination": {"type": "string"}, "date": {"type": "string"}}, "required": ["origin", "destination", "date"]}},
            {"name": "get_flight_details", "description": "Get details for a specific flight", "inputSchema": {"type": "object", "properties": {"flight_id": {"type": "string"}}, "required": ["flight_id"]}},
            {"name": "search_flights_by_budget", "description": "Search flights within a price budget", "inputSchema": {"type": "object", "properties": {"origin": {"type": "string"}, "destination": {"type": "string"}, "date": {"type": "string"}, "max_price": {"type": "number"}}, "required": ["origin", "destination", "date", "max_price"]}},
        ],
    },
    {
        "name": "workshop-hotels-mcp",
        "description": "Hotel search tools — search by city, dates, or budget",
        "tools": [
            {"name": "search_hotels", "description": "Search hotels by city and dates", "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}, "checkin_date": {"type": "string"}, "checkout_date": {"type": "string"}, "guests": {"type": "integer"}}, "required": ["city", "checkin_date", "checkout_date"]}},
            {"name": "get_hotel_details", "description": "Get details for a specific hotel", "inputSchema": {"type": "object", "properties": {"hotel_id": {"type": "string"}}, "required": ["hotel_id"]}},
            {"name": "search_hotels_by_budget", "description": "Search hotels within a nightly price budget", "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}, "checkin_date": {"type": "string"}, "checkout_date": {"type": "string"}, "max_price_per_night": {"type": "number"}}, "required": ["city", "max_price_per_night"]}},
        ],
    },
    {
        "name": "workshop-search-knowledge-base",
        "description": "Enterprise knowledge base search",
        "tools": [
            {"name": "search-knowledge-base", "description": "Search the knowledge base", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]}},
        ],
    },
]

record_ids = {}
for t in tools:
    server_def = {"name": f"workshop/{t['name']}", "description": t["description"], "version": "1.0.0"}
    try:
        resp = client.create_registry_record(
            registryId=registry_id,
            name=t["name"],
            description=t["description"],
            descriptorType="MCP",
            descriptors={
                "mcp": {
                    "server": {"schemaVersion": "2025-12-11", "inlineContent": json.dumps(server_def)},
                    "tools": {"protocolVersion": "2024-11-05", "inlineContent": json.dumps({"tools": t["tools"]})},
                }
            },
            recordVersion="1.0.0",
        )
    except client.exceptions.ConflictException:
        # Idempotent re-run: record already exists; look it up by name.
        existing = next(
            (r for r in client.list_registry_records(registryId=registry_id).get("registryRecords", [])
             if r.get("name") == t["name"]),
            None,
        )
        if existing:
            rid = existing.get("recordId") or existing["recordArn"].split("/")[-1]
            record_ids[t["name"]] = rid
            print(f"  Already registered: {t['name']} -> {rid}")
            continue
        raise
    except Exception as e:
        print(f"  FAILED {t['name']}: {type(e).__name__}: {e}", file=sys.stderr)
        continue
    rid = resp.get("recordId") or resp["recordArn"].split("/")[-1]
    record_ids[t["name"]] = rid
    print(f"  Registered: {t['name']} -> {rid}")

if not record_ids:
    print("\nNo records were registered. Check AWS permissions (Publisher role assumed?) and registry ID.", file=sys.stderr)
    sys.exit(0)

# Wait for DRAFT status
print("\nWaiting for records to become DRAFT...")
for name, rid in record_ids.items():
    r = None
    for _ in range(12):
        try:
            r = client.get_registry_record(registryId=registry_id, recordId=rid)
        except Exception as e:
            print(f"  {name}: get_registry_record failed - {type(e).__name__}: {e}", file=sys.stderr)
            break
        if r.get("status") != "CREATING":
            break
        time.sleep(5)
    if r:
        print(f"  {name}: {r.get('status', '<unknown>')}")

# Submit for approval (skip records already PENDING_APPROVAL or APPROVED)
print("\nSubmitting for approval...")
for name, rid in record_ids.items():
    try:
        r = client.get_registry_record(registryId=registry_id, recordId=rid)
        if r.get("status") in ("PENDING_APPROVAL", "APPROVED"):
            print(f"  {name}: already {r['status']}")
            continue
        client.submit_registry_record_for_approval(registryId=registry_id, recordId=rid)
        print(f"  {name}: PENDING")
    except Exception as e:
        print(f"  {name}: submit failed - {type(e).__name__}: {e}", file=sys.stderr)

print(f"\nAll {len(record_ids)} records processed.")
PYEOF

python3 /tmp/register_tools.py
:::

You should see all 3 tools registered and submitted as `PENDING`.

### Step 3: Verify Publisher Cannot Approve

Try to approve a record as the Publisher — this should fail:

:::code{showCopyAction=true showLineNumbers=false language=bash}
if [ -z "$REGISTRY_ID" ] || [ "$REGISTRY_ID" = "None" ]; then
  echo "ERROR: REGISTRY_ID is empty - re-run Step 1 block first" >&2
else
  FIRST_RECORD=$(aws bedrock-agentcore-control list-registry-records \
    --registry-id "$REGISTRY_ID" \
    --query "registryRecords[0].recordId" \
    --output text --region "$REGION" 2>/dev/null)

  if [ -z "$FIRST_RECORD" ] || [ "$FIRST_RECORD" = "None" ]; then
    echo "No records in the registry yet - run Step 2 (Register Three Tools) first" >&2
  else
    aws bedrock-agentcore-control update-registry-record-status \
      --registry-id "$REGISTRY_ID" \
      --record-id "$FIRST_RECORD" \
      --status APPROVED \
      --status-reason "Publisher attempting to approve" \
      --region "$REGION" > /tmp/publisher-denied.log 2>&1 || true
    head -3 /tmp/publisher-denied.log
  fi
fi
:::

You should see an `AccessDeniedException` — the Publisher role cannot approve records. This is the governance boundary in action.

### Step 4: Switch to Admin and Approve

:::code{showCopyAction=true showLineNumbers=false language=bash}
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

REGION=$(aws configure get region)

ADMIN_ROLE_ARN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='ac-RegistryAdminRoleArn'].Value" \
  --output text --region $REGION)

if [ -z "$ADMIN_ROLE_ARN" ] || [ "$ADMIN_ROLE_ARN" = "None" ]; then
  echo "ERROR: ADMIN_ROLE_ARN is empty - confirm workshop-agentcore-stack shows CREATE_COMPLETE" >&2
else
  ADMIN_CREDS=$(aws sts assume-role \
    --role-arn "$ADMIN_ROLE_ARN" \
    --role-session-name registry-admin-session \
    --query 'Credentials' --output json)

  if [ -n "$ADMIN_CREDS" ]; then
    export AWS_ACCESS_KEY_ID=$(echo "$ADMIN_CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['AccessKeyId'])")
    export AWS_SECRET_ACCESS_KEY=$(echo "$ADMIN_CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['SecretAccessKey'])")
    export AWS_SESSION_TOKEN=$(echo "$ADMIN_CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['SessionToken'])")
    echo "Assumed role: $(aws sts get-caller-identity --query Arn --output text)"
  else
    echo "ERROR: sts:AssumeRole returned empty credentials" >&2
  fi
fi
:::

Approve all pending records:

Review what's waiting for approval:

:::code{showCopyAction=true showLineNumbers=false language=bash}
if [ -z "$REGISTRY_ID" ] || [ "$REGISTRY_ID" = "None" ]; then
  REGION="${REGION:-$(aws configure get region)}"
  REGISTRY_ID=$(aws bedrock-agentcore-control list-registries \
    --query "registries[?name=='workshop-registry'].registryId | [0]" \
    --output text --region "$REGION" 2>/dev/null)
  export REGISTRY_ID
fi
aws bedrock-agentcore-control list-registry-records \
  --registry-id "$REGISTRY_ID" \
  --query "registryRecords[?status=='PENDING_APPROVAL'].{Name:name, Status:status, RecordId:recordId}" \
  --output table --region "$REGION"
:::

Approve them:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

if [ -z "$REGISTRY_ID" ] || [ "$REGISTRY_ID" = "None" ]; then
  # Re-resolve REGISTRY_ID if it was lost between blocks.
  REGISTRY_ID=$(aws bedrock-agentcore-control list-registries \
    --query "registries[?name=='workshop-registry'].registryId | [0]" \
    --output text --region "$REGION" 2>/dev/null)
  export REGISTRY_ID
fi

if [ -z "$REGISTRY_ID" ] || [ "$REGISTRY_ID" = "None" ]; then
  echo "ERROR: REGISTRY_ID is empty - re-run Step 3 (Create the Registry) first" >&2
else
  RECORDS=$(aws bedrock-agentcore-control list-registry-records \
    --registry-id "$REGISTRY_ID" \
    --query "registryRecords[?status=='PENDING_APPROVAL'].recordId" \
    --output text --region "$REGION" 2>/dev/null)

  if [ -z "$RECORDS" ]; then
    echo "No records in PENDING_APPROVAL status. Check the list-registry-records block above."
  else
    for rid in $RECORDS; do
      aws bedrock-agentcore-control update-registry-record-status \
        --registry-id "$REGISTRY_ID" \
        --record-id "$rid" \
        --status APPROVED \
        --status-reason "Approved by workshop admin" \
        --region "$REGION" \
        --query '{RecordId: recordId, Status: status}' \
        --output table 2>&1 || echo "  WARN: approve failed for $rid (check logs)"
    done
  fi
fi
:::

All records should show `APPROVED`.

## What You Built

| Record | Type | Status | Tools |
|--------|------|--------|-------|
| workshop-flights-mcp | MCP | APPROVED | search_flights, get_flight_details, search_flights_by_budget |
| workshop-hotels-mcp | MCP | APPROVED | search_hotels, get_hotel_details, search_hotels_by_budget |
| workshop-search-knowledge-base | MCP | APPROVED | search-knowledge-base |

The governance workflow: **Publisher registers → Admin approves → Consumer discovers** (next step).

---

## Notebook Walkthrough (Optional alternative)

> This notebook (04-register-tools.ipynb) is an alternative path covering the same material as the CLI section above — follow *either* path, you do not need to do both. The notebook covers registration, approval workflow, and additional advanced topics (EventBridge automation, URL-based sync, Registry→Gateway sync).
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-3b-agentcore/notebooks/` and open **`04-register-tools.ipynb`**.
