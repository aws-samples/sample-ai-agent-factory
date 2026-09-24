#!/usr/bin/env bash
# Recover ToolGateway stacks stranded in DELETE_FAILED after their by-name
# execution roles (created by the RegistryRoles stack) were deleted first.
#
# What it does, fail-closed (any error aborts BEFORE the temporary roles are
# removed, so the next attempt has something to inspect):
#   1. Recreates the four Lambda-trusted execution roles the stranded stacks
#      still reference, with delete-path grants only.
#   2. Retries `delete-stack` on both ToolGateway stacks and waits.
#   3. Only when BOTH stacks are gone: removes the temporary roles again.
#
# Run in the WORKSTREAM account (the account that owns the ToolGateway stacks):
#   AGENTICAI_EXPECTED_ACCOUNT=<12 digits> bash scripts/recover-stranded-toolgateway.sh
#
# Optional: AGENTICAI_TENANT_ID (default demo), AGENTICAI_AGENT_ID (default
# primary), AWS_REGION (default us-west-2), AGENTICAI_ENVS (default
# "nonprod prod").
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

export AWS_PAGER=""
REGION="${AWS_REGION:-us-west-2}"
TENANT_ID="${AGENTICAI_TENANT_ID:-demo}"
AGENT_ID="${AGENTICAI_AGENT_ID:-primary}"
ENVS="${AGENTICAI_ENVS:-nonprod prod}"
EXPECTED_ACCOUNT="${AGENTICAI_EXPECTED_ACCOUNT:?set AGENTICAI_EXPECTED_ACCOUNT to the Workstream account id}"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
if [ "$ACCOUNT" != "$EXPECTED_ACCOUNT" ]; then
  printf 'REFUSING: credentials belong to %s, expected %s\n' "$ACCOUNT" "$EXPECTED_ACCOUNT" >&2
  exit 2
fi

cat > "$WORKDIR/lambda-trust.json" <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
# Exactly what the stranded stack's Delete path calls: the DELETE_FAILED
# GatewayTarget resources are retried first (DeleteGatewayTarget), then the
# barrier polls ListGatewayTargets, then the Gateway itself (DeleteGateway).
cat > "$WORKDIR/gw-delete-path.json" <<'EOF'
{"Version":"2012-10-17","Statement":[{"Sid":"DeletePathOnly","Effect":"Allow","Action":["bedrock-agentcore:DeleteGatewayTarget","bedrock-agentcore:DeleteGateway","bedrock-agentcore:GetGateway","bedrock-agentcore:ListGatewayTargets"],"Resource":"*"}]}
EOF

BASIC="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
ROLES=()
ADMIN_ROLES=()
for env in $ENVS; do
  ADMIN_ROLES+=("AgenticAI-D03-${env}-GatewayAdmin")
  ROLES+=("AgenticAI-D03-${env}-GatewayAdmin" "AgenticAI-D03-${env}-${TENANT_ID}-${AGENT_ID}-RegistryValidator")
done

role_exists() {
  aws iam get-role --role-name "$1" --query Role.RoleName --output text >/dev/null 2>&1
}

printf '== 1. Temporary execution roles\n'
for role in "${ROLES[@]}"; do
  if role_exists "$role"; then
    printf '   exists   %s\n' "$role"
  else
    aws iam create-role --role-name "$role" \
      --assume-role-policy-document "file://$WORKDIR/lambda-trust.json" \
      --description "TEMPORARY teardown-recovery role; delete after the ToolGateway stack is gone" \
      --query Role.Arn --output text
  fi
  aws iam attach-role-policy --role-name "$role" --policy-arn "$BASIC"
done
for role in "${ADMIN_ROLES[@]}"; do
  aws iam put-role-policy --role-name "$role" --policy-name DeletePath \
    --policy-document "file://$WORKDIR/gw-delete-path.json"
done
printf '   waiting 30s for IAM propagation\n'
sleep 30

printf '== 2. Retry stack deletion\n'
for env in $ENVS; do
  aws cloudformation delete-stack --region "$REGION" \
    --stack-name "AgenticAI-${TENANT_ID}-${AGENT_ID}-${env}-ToolGateway"
done
for env in $ENVS; do
  stack="AgenticAI-${TENANT_ID}-${AGENT_ID}-${env}-ToolGateway"
  if ! aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$stack"; then
    printf 'FAILED: %s did not reach DELETE_COMPLETE; temporary roles LEFT IN PLACE for diagnosis:\n' "$stack" >&2
    aws cloudformation describe-stack-events --region "$REGION" --stack-name "$stack" --max-items 20 \
      --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason]" --output text >&2
    exit 1
  fi
  printf '   DELETE_COMPLETE %s\n' "$stack"
done

printf '== 3. Remove temporary roles\n'
for role in "${ADMIN_ROLES[@]}"; do
  aws iam delete-role-policy --role-name "$role" --policy-name DeletePath
done
for role in "${ROLES[@]}"; do
  aws iam detach-role-policy --role-name "$role" --policy-arn "$BASIC"
  aws iam delete-role --role-name "$role"
  printf '   deleted  %s\n' "$role"
done

printf '== Remaining AgenticAI-%s-%s stacks (expect none):\n' "$TENANT_ID" "$AGENT_ID"
aws cloudformation list-stacks --region "$REGION" \
  --stack-status-filter DELETE_FAILED DELETE_IN_PROGRESS UPDATE_COMPLETE CREATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
  --query "StackSummaries[?starts_with(StackName,'AgenticAI-${TENANT_ID}-${AGENT_ID}')].[StackName,StackStatus]" --output text
