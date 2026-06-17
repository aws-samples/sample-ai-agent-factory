---
title: "Create the Registry"
weight: 63
---

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

## CLI Walkthrough

Create an AgentCore Registry with Cognito JWT authentication and manual approval workflow.

### Gather Stack Outputs

Retrieve the values you need **before** assuming the admin role (the persona roles don't have CloudFormation permissions):

::alert[These CLI blocks intentionally avoid `set -eo pipefail` — in an interactive shell (like the Workshop IDE terminal) those options persist across blocks and can terminate the session on the next benign `SIGPIPE` or non-zero exit. Each block fails loudly via explicit `test -n "$X"` guards instead.]{type="info"}

:::code{showCopyAction=true showLineNumbers=false language=bash}
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

REGION=$(aws configure get region)

COGNITO_POOL_ID=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoUserPoolId'].Value" \
  --output text --region $REGION)

M2M_CLIENT_ID=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoM2MClientId'].Value" \
  --output text --region $REGION)

ADMIN_ROLE_ARN=$(aws cloudformation list-exports \
  --query "Exports[?Name=='ac-RegistryAdminRoleArn'].Value" \
  --output text --region $REGION)

DISCOVERY_URL="https://cognito-idp.${REGION}.amazonaws.com/${COGNITO_POOL_ID}/.well-known/openid-configuration"

# Fail loudly but keep the shell alive if any value is missing.
MISSING=""
for V in REGION COGNITO_POOL_ID M2M_CLIENT_ID ADMIN_ROLE_ARN; do
  if [ -z "${!V}" ] || [ "${!V}" = "None" ]; then
    MISSING="$MISSING $V"
  fi
done
if [ -n "$MISSING" ]; then
  echo "ERROR: empty or missing value(s):$MISSING" >&2
  echo "Confirm the AgentCore stack (workshop-agentcore-stack) and Registry stack (workshop-registry-stack) show CREATE_COMPLETE." >&2
else
  echo "Cognito Pool:   $COGNITO_POOL_ID"
  echo "M2M Client:     $M2M_CLIENT_ID"
  echo "Admin Role:     $ADMIN_ROLE_ARN"
  echo "Discovery URL:  $DISCOVERY_URL"
fi
:::

### Assume the Admin Persona

:::code{showCopyAction=true showLineNumbers=false language=bash}
if [ -z "$ADMIN_ROLE_ARN" ] || [ "$ADMIN_ROLE_ARN" = "None" ]; then
  echo "ERROR: ADMIN_ROLE_ARN is empty - re-run the previous block first" >&2
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

You should see an ARN containing `workshop-ac-registry-admin-<region>`.

### Create the Registry

The command below is **idempotent**: it first checks whether `workshop-registry` already exists and only creates a new one if it does not. This keeps the step safe to re-run and avoids hitting the per-account registry quota.

:::code{showCopyAction=true showLineNumbers=false language=bash}
EXISTING_ID=$(aws bedrock-agentcore-control list-registries \
  --query "registries[?name=='workshop-registry'] | [0].registryId" \
  --output text --region $REGION)

if [ -n "$EXISTING_ID" ] && [ "$EXISTING_ID" != "None" ]; then
  echo "Registry already exists: $EXISTING_ID"
  REGISTRY_ID=$EXISTING_ID
else
  CREATE_OUT=$(aws bedrock-agentcore-control create-registry \
    --name workshop-registry \
    --description "Enterprise tool and agent catalog for the workshop platform" \
    --authorizer-type CUSTOM_JWT \
    --authorizer-configuration "{
      \"customJWTAuthorizer\": {
        \"discoveryUrl\": \"${DISCOVERY_URL}\",
        \"allowedClients\": [\"${M2M_CLIENT_ID}\"]
      }
    }" \
    --approval-configuration '{"autoApproval": false}' \
    --region $REGION \
    --output json)
  if [ -n "$CREATE_OUT" ]; then
    REGISTRY_ID=$(echo "$CREATE_OUT" | python3 -c "import sys,json; arn=json.load(sys.stdin)['registryArn']; print(arn.split('/')[-1])")
    echo "Created registry: $REGISTRY_ID"
  else
    echo "ERROR: create-registry returned no output - check AWS credentials and the Admin persona is assumed" >&2
  fi
fi

if [ -n "$REGISTRY_ID" ]; then
  echo "Registry ID: $REGISTRY_ID"
fi
:::

::alert[The registry takes a few seconds to transition from `CREATING` to `READY`. Continue to **Step 4: Wait for the Registry to Become READY** below before registering any tools — attempting to write to a registry that is still `CREATING` returns `ConflictException`.]{type="info"}

Two key design decisions:
- **CUSTOM_JWT** — the registry validates Cognito JWTs. The same pool is used by the Gateway, so agents authenticate once for both.
- **Manual approval** — every tool registration must be approved by an admin before consumers can discover it.

## Step 4: Wait for the Registry to Become READY

The registry takes a few seconds to provision. Poll until the status is `READY`:

### CLI

:::code{showCopyAction=true showLineNumbers=false language=bash}
if [ -z "$REGISTRY_ID" ] || [ "$REGISTRY_ID" = "None" ]; then
  echo "ERROR: REGISTRY_ID is empty - re-run the Create the Registry block first" >&2
else
  echo "Waiting for registry to become READY..."
  for i in $(seq 1 30); do
    STATUS=$(aws bedrock-agentcore-control get-registry \
      --registry-id "$REGISTRY_ID" \
      --query 'status' --output text --region "$REGION" 2>/dev/null)
    echo "  Attempt $i: ${STATUS:-<unreadable>}"
    if [ "$STATUS" = "READY" ]; then
      echo "Registry is READY."
      break
    fi
    sleep 5
  done
fi
:::

::alert[**Why manual approval?** In an enterprise, a new tool should be reviewed before agents can discover and invoke it. The Publisher registers, the Admin approves — separation of duties. You will exercise this workflow in the next step.]{type="info"}

---

## Notebook Walkthrough (Optional alternative)

> This notebook (03-create-registry.ipynb) is an alternative path covering the same material as the CLI section above — follow *either* path, you do not need to do both. The notebook covers the same steps with boto3 and saves state for subsequent notebooks.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-3b-agentcore/notebooks/` and open **`03-create-registry.ipynb`**.
