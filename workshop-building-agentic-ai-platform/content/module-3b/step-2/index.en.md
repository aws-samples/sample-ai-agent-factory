---
title: "Verify Infrastructure"
weight: 62
---

The `workshop-agentcore-stack` was pre-deployed by the workshop platform. In this step you verify it and capture the outputs you need for the rest of the module.

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

## CLI Walkthrough

### Verify the Stack

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

aws cloudformation describe-stacks \
  --stack-name workshop-agentcore-stack \
  --query "Stacks[0].StackStatus" \
  --output text --region $REGION
:::

You should see `CREATE_COMPLETE`.

### Capture Outputs

Save the key outputs as environment variables for use in subsequent steps:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

export GATEWAY_ID=$(aws cloudformation describe-stacks \
  --stack-name workshop-agentcore-stack \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayId'].OutputValue" \
  --output text --region $REGION)

export COGNITO_USER_POOL_ID=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoUserPoolId'].Value" \
  --output text --region $REGION)

export COGNITO_M2M_CLIENT_ID=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoM2MClientId'].Value" \
  --output text --region $REGION)

export ADMIN_ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name workshop-agentcore-stack \
  --query "Stacks[0].Outputs[?OutputKey=='RegistryAdminRoleArn'].OutputValue" \
  --output text --region $REGION)

export PUBLISHER_ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name workshop-agentcore-stack \
  --query "Stacks[0].Outputs[?OutputKey=='RegistryPublisherRoleArn'].OutputValue" \
  --output text --region $REGION)

export CONSUMER_ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name workshop-agentcore-stack \
  --query "Stacks[0].Outputs[?OutputKey=='RegistryConsumerRoleArn'].OutputValue" \
  --output text --region $REGION)

MISSING=""
for V in GATEWAY_ID COGNITO_USER_POOL_ID COGNITO_M2M_CLIENT_ID ADMIN_ROLE_ARN PUBLISHER_ROLE_ARN CONSUMER_ROLE_ARN; do
  if [ -z "${!V}" ] || [ "${!V}" = "None" ]; then
    MISSING="$MISSING $V"
  fi
done
if [ -n "$MISSING" ]; then
  echo "ERROR: empty or missing value(s):$MISSING" >&2
  echo "Confirm workshop-agentcore-stack and workshop-registry-stack both show CREATE_COMPLETE." >&2
else
  echo "Gateway ID:         $GATEWAY_ID"
  echo "Cognito Pool:       $COGNITO_USER_POOL_ID"
  echo "M2M Client:         $COGNITO_M2M_CLIENT_ID"
  echo "Admin Role:         $ADMIN_ROLE_ARN"
  echo "Publisher Role:     $PUBLISHER_ROLE_ARN"
  echo "Consumer Role:      $CONSUMER_ROLE_ARN"
fi
:::

### Verify the Gateway Has Targets

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

GATEWAY_ID=$(aws cloudformation describe-stacks \
  --stack-name workshop-agentcore-stack \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayId'].OutputValue" \
  --output text --region $REGION)

aws bedrock-agentcore-control list-gateway-targets \
  --gateway-identifier $GATEWAY_ID \
  --query "items[].{Name:name, Status:status}" \
  --output table --region $REGION
:::

You should see 3 targets (flights, hotels, search-kb) all in `READY` status.

::alert[The Gateway and Lambda targets are pre-deployed. The **Registry** does not exist yet — you will create it in the next step.]{type="info"}

---

## Notebook Walkthrough (Optional alternative)

> This notebook (02-deploy.ipynb) is an alternative path covering the same material as the CLI section above — follow *either* path, you do not need to do both. The notebook verifies the stack, captures outputs, and saves state to `.state.json` for subsequent notebooks.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-3b-agentcore/notebooks/` and open **`02-deploy.ipynb`**.
