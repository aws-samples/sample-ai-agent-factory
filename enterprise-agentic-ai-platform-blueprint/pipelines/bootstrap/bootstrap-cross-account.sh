#!/usr/bin/env bash
# bootstrap-cross-account.sh — bootstrap every target account with trust to
# the platform-nonprod CodePipeline account.
#
# Reads target accounts from the reference-deployment cdk.context.json.
# Run from the management / admin workstation with credentials that can
# assume AdministratorAccess in each target account (e.g. via SSO).
#
# Bootstrap matrix (see README section 13, Multi-account topology):
#   platform-nonprod  — self-bootstrap
#   platform-prod     — trust platform-nonprod
#   workload-nonprod  — trust platform-nonprod
#   workload-prod     — trust platform-nonprod
#   audit             — trust platform-nonprod
#   sandbox           — trust platform-nonprod
#
# Log Archive is owned by Control Tower and does NOT need CDK bootstrap.
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
QUALIFIER="hnb659fds"

# SEC (security review) — LEAST PRIVILEGE: the CloudFormation execution policy sets
# the permissions CloudFormation uses to create EVERY resource in every
# CDK-deployed stack. Do NOT use AdministratorAccess in production — it makes
# every deployed stack admin-equivalent. Supply a customer-managed policy ARN
# scoped to exactly the services these stacks provision (IAM, Lambda, DynamoDB,
# S3, KMS, ECS, Bedrock/AgentCore, CloudWatch, EventBridge, Step Functions,
# SNS/SQS, EC2/VPC) via CFN_EXECUTION_POLICY_ARN.
# The default below is intentionally NOT AdministratorAccess so a copy-paste
# run fails safe and forces an explicit choice.
CFN_EXECUTION_POLICY_ARN="${CFN_EXECUTION_POLICY_ARN:-}"
if [[ -z "$CFN_EXECUTION_POLICY_ARN" ]]; then
  echo "ERROR: set CFN_EXECUTION_POLICY_ARN to a scoped customer-managed policy ARN" >&2
  echo "       for the CDK CloudFormation execution role. Do NOT use" >&2
  echo "       arn:aws:iam::aws:policy/AdministratorAccess in production." >&2
  echo "       See README §9 for the scoping guidance." >&2
  exit 1
fi

CONTEXT_FILE="${1:-examples/reference-deployment-us-west-2/cdk.context.json}"
if [[ ! -f "$CONTEXT_FILE" ]]; then
  echo "cdk.context.json not found at $CONTEXT_FILE" >&2
  exit 1
fi

json() {
  python3 -c "import json,sys; print(json.load(sys.stdin).get('$1', ''))" < "$CONTEXT_FILE"
}

PLATFORM_NP="$(json agenticai/platformNonprodAccountId)"
PLATFORM_PR="$(json agenticai/platformProdAccountId)"
AUDIT="$(json agenticai/auditAccountId)"
SANDBOX="$(json agenticai/sandboxAccountId)"
WORKLOAD_NP="$(json agenticai/workloadNonprodAccountId)"
WORKLOAD_PR="$(json agenticai/workloadProdAccountId)"

if [[ -z "$PLATFORM_NP" ]]; then
  echo "agenticai/platformNonprodAccountId not set in $CONTEXT_FILE" >&2
  exit 1
fi

echo "Bootstrapping every account with --trust ${PLATFORM_NP}:"

for acct in "$PLATFORM_NP" "$PLATFORM_PR" "$AUDIT" "$SANDBOX" "$WORKLOAD_NP" "$WORKLOAD_PR"; do
  [[ -z "$acct" ]] && continue
  echo ""
  echo "-> Bootstrap aws://${acct}/${REGION}"
  # Expects the OPERATOR to have previously assumed a sufficiently-privileged
  # role in the target account (via sso / aws sts assume-role) to create the
  # bootstrap roles + KMS key. This is the human's bootstrapping identity and
  # is separate from the CloudFormation EXECUTION policy set below (which must
  # be scoped, not AdministratorAccess — see the header warning).
  npx cdk bootstrap "aws://${acct}/${REGION}" \
    --trust "$PLATFORM_NP" \
    --trust-for-lookup "$PLATFORM_NP" \
    --cloudformation-execution-policies "$CFN_EXECUTION_POLICY_ARN" \
    --qualifier "$QUALIFIER"
done

echo ""
echo "Bootstrap complete. Next: 'cdk deploy --context stage=pipeline ...'"
