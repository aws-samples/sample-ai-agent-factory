---
title: "Cleanup"
weight: 91
---

To avoid ongoing charges, delete the AWS resources created during this workshop.

::alert[If you are running this workshop at an AWS event, you can skip this section. Workshop Studio will automatically clean up your account resources when the event ends.]{type="info"}

::alert[Complete this section when you are finished with the workshop and running in your own AWS account. Skipping cleanup will result in ongoing charges.]{type="warning"}

## Cleanup Order

Delete resources in reverse order of creation. The exact steps depend on which modules you completed.

### Step 1: Module 4 — FAST Agent

If you completed Module 4, destroy the FAST CDK stack and clean up associated resources — this applies to both the MCP path and the AgentCore path. Follow the detailed cleanup instructions in the [Module 4 Cleanup](../module-4/cleanup/) page, which covers:

- OAuth2 Credential Provider deletion
- Cognito user deletion
- CDK stack destruction (`FAST-stack`)
- SSM parameter cleanup (`/FAST-stack/*`)
- LiteLLM virtual key deletion

### Step 2: Module 3a — Tools Gateway

If you completed Module 3a, delete the Bedrock Guardrail (if created) and the AgentCore Gateway API resource. The CloudFormation stack (`workshop-tools-gateway-stack`) is auto-provisioned and will be cleaned up automatically — that also removes the sync/request/response interceptor Lambdas, the EventBridge schedule, the `workshop-agentcore-gateway-role-<region>` IAM role, and the demo tool Lambdas. For self-service deployments, see the stack deletion block at the bottom of this page.

Delete the Module 3a guardrail by looking it up by name:

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

# Delete the Module 3a Bedrock Guardrail (if created)
TOOL_GUARDRAIL_ID=$(aws bedrock list-guardrails \
  --query "guardrails[?name=='workshop-tool-guardrail'].id | [0]" \
  --output text --region $REGION)

if [ -n "$TOOL_GUARDRAIL_ID" ] && [ "$TOOL_GUARDRAIL_ID" != "None" ]; then
  aws bedrock delete-guardrail \
    --guardrail-identifier $TOOL_GUARDRAIL_ID \
    --region $REGION
  echo "Deleted guardrail: workshop-tool-guardrail ($TOOL_GUARDRAIL_ID)"
fi
:::

Delete the Tools Gateway AgentCore Gateway:

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

# Delete the AgentCore Gateway (Module 3a)
GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
  --query 'items[?name==`tools-gateway`].gatewayId | [0]' --output text \
  --region $REGION)

if [ -n "$GATEWAY_ID" ] && [ "$GATEWAY_ID" != "None" ]; then
  aws bedrock-agentcore-control delete-gateway \
    --gateway-identifier $GATEWAY_ID --region $REGION
  echo "Deleted gateway: $GATEWAY_ID"
fi
:::

### Step 3: Module 3b — AgentCore Registry & Gateway

If you completed Module 3b, clean up the AgentCore-specific resources:

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

# Delete the Module 3b Bedrock Guardrail (if created)
AC_GUARDRAIL_ID=$(aws bedrock list-guardrails \
  --query "guardrails[?name=='workshop-tool-output-guardrail'].id | [0]" \
  --output text --region $REGION)

if [ -n "$AC_GUARDRAIL_ID" ] && [ "$AC_GUARDRAIL_ID" != "None" ]; then
  aws bedrock delete-guardrail \
    --guardrail-identifier $AC_GUARDRAIL_ID \
    --region $REGION
  echo "Deleted guardrail: workshop-tool-output-guardrail ($AC_GUARDRAIL_ID)"
fi

# Delete the AgentCore Gateway (Module 3b)
AC_GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
  --query 'items[?name==`ac-tools-gateway`].gatewayId' --output text \
  --region $REGION)

if [ -n "$AC_GATEWAY_ID" ] && [ "$AC_GATEWAY_ID" != "None" ]; then
  # Delete gateway targets first
  for TARGET_ID in $(aws bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier $AC_GATEWAY_ID \
    --query 'items[].targetId' --output text \
    --region $REGION 2>/dev/null); do
    aws bedrock-agentcore-control delete-gateway-target \
      --gateway-identifier $AC_GATEWAY_ID \
      --target-id $TARGET_ID --region $REGION
    echo "Deleted target: $TARGET_ID"
  done

  aws bedrock-agentcore-control delete-gateway \
    --gateway-identifier $AC_GATEWAY_ID --region $REGION
  echo "Deleted AgentCore gateway: $AC_GATEWAY_ID"
fi
:::

The blocks below delete the remaining AgentCore resources created by Module 3b notebooks 03, 04, 06, and 07. If you completed Module 3b step-8, you have already run these and can skip ahead.

Delete the approved-tool sync rule that the notebooks create. Do NOT delete `ac-registry-auto-review` — that rule is CloudFormation-managed by `workshop-agentcore-stack` and will be removed automatically when the workshop stacks are torn down. Deleting it manually causes drift and makes subsequent stack updates fail.

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

for RULE in ac-registry-sync-gateway; do
  for TARGET_ID in $(aws events list-targets-by-rule \
      --rule $RULE --query 'Targets[].Id' --output text \
      --region $REGION 2>/dev/null); do
    aws events remove-targets --rule $RULE --ids $TARGET_ID --region $REGION
  done
  aws events delete-rule --name $RULE --region $REGION 2>/dev/null \
    && echo "Deleted EventBridge rule: $RULE" \
    || echo "EventBridge rule not found: $RULE"
done
:::

Delete all registry records, then the registry itself:

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

REGISTRY_ID=$(aws bedrock-agentcore-control list-registries \
  --query "registries[?name=='workshop-registry'].registryId | [0]" \
  --output text --region $REGION 2>/dev/null)

if [ -n "$REGISTRY_ID" ] && [ "$REGISTRY_ID" != "None" ]; then
  for RECORD_ID in $(aws bedrock-agentcore-control list-registry-records \
      --registry-id $REGISTRY_ID \
      --query 'registryRecords[].recordId' --output text \
      --region $REGION 2>/dev/null); do
    aws bedrock-agentcore-control delete-registry-record \
      --registry-id $REGISTRY_ID \
      --record-id $RECORD_ID --region $REGION
    echo "Deleted registry record: $RECORD_ID"
  done

  aws bedrock-agentcore-control delete-registry \
    --registry-id $REGISTRY_ID --region $REGION
  echo "Deleted registry: $REGISTRY_ID"
fi
:::

Delete Cedar policies and the policy engine created by notebook 07:

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

ENGINE_ID=$(aws bedrock-agentcore-control list-policy-engines \
  --query "policyEngines[?name=='ac-gateway-policies'].policyEngineId | [0]" \
  --output text --region $REGION 2>/dev/null)

if [ -n "$ENGINE_ID" ] && [ "$ENGINE_ID" != "None" ]; then
  for POLICY_ID in $(aws bedrock-agentcore-control list-policies \
      --policy-engine-id $ENGINE_ID \
      --query 'policies[].policyId' --output text \
      --region $REGION 2>/dev/null); do
    aws bedrock-agentcore-control delete-policy \
      --policy-engine-id $ENGINE_ID \
      --policy-id $POLICY_ID --region $REGION
    echo "Deleted Cedar policy: $POLICY_ID"
  done

  aws bedrock-agentcore-control delete-policy-engine \
    --policy-engine-id $ENGINE_ID --region $REGION
  echo "Deleted policy engine: $ENGINE_ID"
fi
:::

Delete the OAuth2 credential providers and WorkloadIdentity:

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

for PROVIDER in workshop-gateway-oauth workshop-tools-gateway-auth; do
  aws bedrock-agentcore-control delete-oauth2-credential-provider \
    --name $PROVIDER --region $REGION 2>/dev/null \
    && echo "Deleted OAuth2 credential provider: $PROVIDER" \
    || echo "OAuth2 credential provider not found: $PROVIDER"
done

WI_NAME=$(aws cloudformation list-exports \
  --query "Exports[?Name=='ac-WorkloadIdentityName'].Value | [0]" \
  --output text --region $REGION 2>/dev/null)
WI_NAME=${WI_NAME:-ac-agent-identity}

aws bedrock-agentcore-control delete-workload-identity \
  --name $WI_NAME --region $REGION 2>/dev/null \
  && echo "Deleted WorkloadIdentity: $WI_NAME" \
  || echo "WorkloadIdentity not found: $WI_NAME"
:::

### Step 4: Module 2 — LLM Gateway

If you completed Module 2, delete the Bedrock Guardrail (if created). The LLM Gateway CloudFormation stack (`workshop-llm-gateway-stack`) is pre-provisioned and will be cleaned up automatically.

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

# Delete the Module 2 Bedrock Guardrail (if created)
CONTENT_GUARDRAIL_ID=$(aws bedrock list-guardrails \
  --query "guardrails[?name=='workshop-content-filter'].id | [0]" \
  --output text --region $REGION)

if [ -n "$CONTENT_GUARDRAIL_ID" ] && [ "$CONTENT_GUARDRAIL_ID" != "None" ]; then
  aws bedrock delete-guardrail \
    --guardrail-identifier $CONTENT_GUARDRAIL_ID \
    --region $REGION
  echo "Deleted guardrail: workshop-content-filter ($CONTENT_GUARDRAIL_ID)"
fi
:::

::alert[The LLM Gateway, MCP Gateway & Registry, Tools Gateway, and AgentCore stacks are all pre-provisioned and managed by the workshop platform. Do not delete them manually — they will be cleaned up automatically when the event ends.]{type="warning"}

## Delete Pre-Provisioned Infrastructure (Self-Service Only)

::alert[This step is only required if you deployed the workshop infrastructure yourself using the Self-Paced Setup instructions — those five stacks belong to you and you must remove them. At AWS events, these stacks are cleaned up automatically and you should skip this step.]{type="warning"}

::alert[**Module 4 (FAST) resources** are deployed separately via `cdk deploy` inside the workshop IDE — their cleanup is covered in **Step 1** / the [Module 4 Cleanup page](../module-4/cleanup/). Run that flow **before** deleting the workshop stacks below, so you still have access to the CDK environment when you tear down FAST.]{type="info"}

The repository's deploy engine tears down all five stacks for you, in reverse dependency order. From the repository root in your local terminal:

:::code{showCopyAction=true language=bash}
./deploy-cfn.sh destroy
:::

This removes the AgentCore, Tools Gateway, Registry, LLM Gateway, and Code Editor stacks. It also runs a **GuardDuty pre-cleanup step** (deleting GuardDuty-managed VPC endpoints and security groups) — without this, those managed resources block the Registry stack's VPC from deleting and the teardown hangs. Always prefer this script over deleting stacks by hand.

::::expand{header="Manual fallback: delete stacks individually"}
If you cannot run the script, delete the stacks in this order from the AWS CLI. Note this does **not** perform the GuardDuty pre-cleanup, so the Registry stack delete may stall on lingering network interfaces — if it does, remove the GuardDuty-managed VPC endpoints/security groups in that VPC and retry.

:::code{showCopyAction=true language=bash}
REGION=$(aws configure get region)

# 1. Delete the AgentCore stack first (depends on both Registry and Tools Gateway exports)
aws cloudformation delete-stack --stack-name workshop-agentcore-stack --region $REGION
aws cloudformation wait stack-delete-complete --stack-name workshop-agentcore-stack --region $REGION && echo "AgentCore stack deleted"

# 2. Delete the Tools Gateway stack (depends on Registry exports)
aws cloudformation delete-stack --stack-name workshop-tools-gateway-stack --region $REGION
aws cloudformation wait stack-delete-complete --stack-name workshop-tools-gateway-stack --region $REGION && echo "Tools Gateway stack deleted"

# 3. Delete the Registry stack
aws cloudformation delete-stack --stack-name workshop-registry-stack --region $REGION
aws cloudformation wait stack-delete-complete --stack-name workshop-registry-stack --region $REGION && echo "Registry stack deleted"

# 4. Delete the LLM Gateway stack (no dependencies)
aws cloudformation delete-stack --stack-name workshop-llm-gateway-stack --region $REGION
aws cloudformation wait stack-delete-complete --stack-name workshop-llm-gateway-stack --region $REGION && echo "LLM Gateway stack deleted"

# 5. Delete the Code Editor stack (no dependencies)
aws cloudformation delete-stack --stack-name code-editor --region $REGION
aws cloudformation wait stack-delete-complete --stack-name code-editor --region $REGION && echo "Code Editor stack deleted"
:::
::::

## Verify Cleanup

Confirm participant-deployed stacks have been removed:

:::code{showCopyAction=true language=bash}
aws cloudformation list-stacks \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query "StackSummaries[?contains(StackName, 'workshop-') || StackName == 'code-editor' || contains(StackName, 'FAST-stack')].StackName"
:::

For self-service deployments the output should be an empty list `[]`. At an AWS event the five pre-provisioned stacks will still appear — that is expected, because Workshop Studio removes them automatically when the event ends.

::alert[Double-check that the highest-cost, always-on resources are gone, since these accrue charges by the hour even when idle: the **DocumentDB cluster** and **Aurora PostgreSQL** (Registry stack), the **ECS Fargate** services (LiteLLM and Registry), **CloudFront** distributions, and the **NAT Gateway(s)**. If `./deploy-cfn.sh destroy` reported every stack deleted and the list above is empty, these are gone.]{type="warning"}

You can also check the AWS Cost Explorer to verify no unexpected charges are accruing:

:button[Open Cost Explorer]{target="_blank" href="https://console.aws.amazon.com/cost-management/home" variant="primary" iconName="external" iconAlign="right"}
