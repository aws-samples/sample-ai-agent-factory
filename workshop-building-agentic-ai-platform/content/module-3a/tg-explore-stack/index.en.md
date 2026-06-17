---
title: "Tools Gateway: Explore the Stack"
weight: 49
---

The `workshop-tools-gateway-stack` was pre-deployed alongside the Registry stack. It contains the Lambda functions, interceptors, and IAM roles needed for the AgentCore Gateway. In this step you will verify the stack and create the gateway.

## Verify the Stack

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

aws cloudformation describe-stacks \
  --stack-name workshop-tools-gateway-stack \
  --query "Stacks[0].StackStatus" \
  --output text --region $REGION
:::

You should see `CREATE_COMPLETE`.

## Capture Stack Outputs

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

OUTPUTS=$(aws cloudformation describe-stacks \
  --stack-name workshop-tools-gateway-stack \
  --query "Stacks[0].Outputs" \
  --output json --region $REGION)

export GATEWAY_ROLE_ARN=$(echo $OUTPUTS | python3 -c "import sys,json; print(next(o['OutputValue'] for o in json.load(sys.stdin) if o['OutputKey']=='GatewayRoleArn'))")
export SYNC_LAMBDA_ARN=$(echo $OUTPUTS | python3 -c "import sys,json; print(next(o['OutputValue'] for o in json.load(sys.stdin) if o['OutputKey']=='SyncLambdaArn'))")
export REQUEST_INTERCEPTOR_ARN=$(echo $OUTPUTS | python3 -c "import sys,json; print(next(o['OutputValue'] for o in json.load(sys.stdin) if o['OutputKey']=='RequestInterceptorArn'))")
export RESPONSE_INTERCEPTOR_ARN=$(echo $OUTPUTS | python3 -c "import sys,json; print(next(o['OutputValue'] for o in json.load(sys.stdin) if o['OutputKey']=='ResponseInterceptorArn'))")

echo "Gateway Role:        $GATEWAY_ROLE_ARN"
echo "Sync Lambda:         $SYNC_LAMBDA_ARN"
echo "Request Interceptor: $REQUEST_INTERCEPTOR_ARN"
echo "Response Interceptor:$RESPONSE_INTERCEPTOR_ARN"
:::

## Create the AgentCore Gateway

The Lambda functions are deployed but the AgentCore Gateway API resource must be created via the CLI. This registers a gateway with Cognito JWT authentication and both interceptors:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

COGNITO_POOL_ID=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoUserPoolId'].Value" \
  --output text --region $REGION)

M2M_CLIENT_ID=$(aws cloudformation list-exports \
  --query "Exports[?Name=='workshop-CognitoM2MClientId'].Value" \
  --output text --region $REGION)

CLIENT_ID=$(aws cognito-idp list-user-pool-clients \
  --user-pool-id $COGNITO_POOL_ID --max-results 10 \
  --query "UserPoolClients[?ClientId!='$M2M_CLIENT_ID'].ClientId | [0]" \
  --output text --region $REGION)

export POOL_ID=$COGNITO_POOL_ID
export CLIENT_ID=$CLIENT_ID
export M2M_CLIENT_ID=$M2M_CLIENT_ID
export REGION=$REGION

cd /workshop/source/module-4a-tools-gateway

# Ensure the venv has a boto3 new enough for the AgentCore control-plane
# API (bedrock-agentcore-control). The Module 2 venv ships an older boto3.
# requirements.txt pins exact tested versions (boto3==1.42.87, httpx==0.27.0)
# for supply-chain reproducibility.
pip install -r requirements.txt --quiet

python3 create_gateway.py
:::

::alert[The `create_gateway.py` script creates the gateway, configures the JWT authorizer, attaches interceptors, stores the gateway ID in SSM, and updates the Sync Lambda — all in one step.]{type="info"}

## Update the Sync Lambda (Optional — for reference)

### What is the Sync Lambda?

The **Sync Lambda** is one of the three Lambda functions deployed by `workshop-tools-gateway-stack` (alongside the request and response interceptors). You can see it in the AWS Console as `agentcore-gateway-sync` under **Lambda → Functions**, or in the CloudFormation **Resources** tab of the tools-gateway stack. Its job is to read the MCP Registry's catalog on a schedule (EventBridge every 5 minutes, see [Automated Sync](../tg-automated-sync/)), compare it to the AgentCore Gateway's current targets, and create / update targets for any tool it finds. That's why you only had to register a tool in the Registry UI in the previous step — the Sync Lambda picked it up and wired it into the gateway automatically.

The Lambda needs to know **which** gateway to write targets into. That is passed via its `GATEWAY_ID` environment variable.

### Why you don't normally need to run this section

The `create_gateway.py` script already updated the Sync Lambda with the gateway ID as its final step. This manual section exists for two edge cases:

- You deleted and re-created the gateway outside the script, so the Sync Lambda still holds the old (now-invalid) gateway ID.
- You want to point the Sync Lambda at a **different** gateway (e.g., to test a staging gateway in the same workshop account).

If neither applies, skip to the next step.

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
  --query "items[?name=='tools-gateway'].gatewayId" \
  --output text --region $REGION)

CURRENT_ENV=$(aws lambda get-function-configuration \
  --function-name agentcore-gateway-sync \
  --query "Environment.Variables" \
  --output json --region $REGION)

UPDATED_ENV=$(echo "$CURRENT_ENV" | python3 -c "
import sys, json
env = json.load(sys.stdin)
env['GATEWAY_ID'] = '$GATEWAY_ID'
print(json.dumps({'Variables': env}))
")

aws lambda update-function-configuration \
  --function-name agentcore-gateway-sync \
  --environment "$UPDATED_ENV" \
  --region $REGION \
  --query "FunctionName" --output text

echo "Sync Lambda updated with GATEWAY_ID=$GATEWAY_ID"
:::

The gateway is ready. In the next step you will register tools and sync them as gateway targets.
