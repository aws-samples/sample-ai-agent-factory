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
#   log-archive       — trust platform-nonprod (may be Management/Governance)
#   audit             — trust platform-nonprod (may be Management/Governance)
#   workload-nonprod  — trust platform-nonprod
#   workload-prod     — trust platform-nonprod
#   sandbox           — trust platform-nonprod
#
# Consolidated deployments can map log-archive and audit to the same account;
# the target list below removes duplicates before bootstrapping.
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
QUALIFIER="hnb659fds"
PARTITION="${AWS_PARTITION:-aws}"

if [[ -z "$REGION" ]]; then
  echo "ERROR: set AWS_REGION or AWS_DEFAULT_REGION to the explicit bootstrap Region" >&2
  exit 1
fi
if [[ ! "$REGION" =~ ^[a-z]{2}(-[a-z0-9]+)+-[0-9]+$ ]]; then
  echo "ERROR: invalid AWS Region '$REGION'" >&2
  exit 1
fi

# SEC (security review) — LEAST PRIVILEGE: the CloudFormation execution policy sets
# the permissions CloudFormation uses to create EVERY resource in every
# CDK-deployed stack. Do NOT use AdministratorAccess in production — it makes
# every deployed stack admin-equivalent. Supply a customer-managed policy ARN
# scoped to exactly the services these stacks provision (IAM, Lambda, DynamoDB,
# S3, KMS, ECS, Bedrock/AgentCore, CloudWatch, EventBridge, Step Functions,
# SNS/SQS, EC2/VPC). Prefer CFN_EXECUTION_POLICY_NAME for multi-account runs;
# use CFN_EXECUTION_POLICY_ARN only for a single-account context.
#
# The policy attached to the Platform account's CloudFormation execution role
# must also allow iam:PassRole on every target account's exact
# cdk-hnb659fds-deploy-role-<account>-<region> with
# iam:PassedToService=codepipeline.amazonaws.com, and on every exact
# cdk-hnb659fds-cfn-exec-role-<account>-<region> with
# iam:PassedToService=cloudformation.amazonaws.com. The pipeline-creation API
# validates both role classes before accepting a cross-account pipeline.
# The Workstream account execution policy must allow iam:PassRole on its exact
# `AgenticAI*`/CDK-generated Provider waiter roles with
# iam:PassedToService=states.amazonaws.com. This is required for the bounded
# Step Functions waiters used by deletion and IAM-propagation barriers; do not
# widen the role resource pattern or omit the passed-to-service condition.
# The optional pipeline-owned Runtime/Memory path additionally requires
# `bedrock-agentcore:Create/Get/DeleteAgentRuntime`,
# `bedrock-agentcore:Create/Get/DeleteMemory`, and resource-tag reads/writes on
# the exact environment-qualified Runtime/Memory families. Allow
# `iam:PassRole` only on the exact
# `AgenticAI-D03-<environment>-<tenant>-<agent>-runtime` role with
# `iam:PassedToService=bedrock-agentcore.amazonaws.com`. The same path creates
# an exact `AgenticAI-D03-<environment>-<tenant>-<agent>-imgscan` role; allow
# `iam:PassRole` on only that role with
# `iam:PassedToService=lambda.amazonaws.com`. Its identity policy is limited to
# `ecr:DescribeImages`, `ecr:StartImageScan`, and
# `ecr:DescribeImageScanFindings` on the exact bootstrap container-assets
# repository and deliberately grants no image or repository deletion. Memory CMK use needs
# `kms:CreateGrant`, `kms:Decrypt`, `kms:GenerateDataKey*`, `kms:ReEncrypt*`, and
# `kms:DescribeKey` on the exact Memory key with
# `kms:ViaService=bedrock-agentcore.<region>.amazonaws.com`; do not grant
# unconstrained KMS administration.
# The Platform inference Gateway creates an exact
# `AgenticAI-InferenceGuardrail-<environment>` role and an exact
# `agenticai-inference-guardrail-<environment>` Lambda (the guardrail REQUEST
# interceptor). Allow IAM and Lambda lifecycle actions on only those names and
# `iam:PassRole` on that role only with
# `iam:PassedToService=lambda.amazonaws.com`; its identity policy is limited to
# `bedrock:ApplyGuardrail` on the stage's exact baseline guardrail ARN.
# The optional Gateway PolicyEngine path also requires kms:CreateGrant,
# kms:Decrypt, kms:GenerateDataKey, and kms:DescribeKey on its exact
# AgenticAI PolicyEngine CMK. Scope them with
# kms:ViaService=bedrock-agentcore.<region>.amazonaws.com and the
# aws:bedrock-agentcore-policy:policy-engine-arn encryption context documented
# by Policy in AgentCore; do not grant unconstrained KMS administration.
# The Management account execution policy additionally needs Kinesis lifecycle
# actions, IAM lifecycle actions on the exact
# AgenticAI-LogArchive-CWLDestinationRole, and iam:PassRole on that role with
# iam:PassedToService=logs.amazonaws.com. Nonproduction bucket cleanup also
# synthesizes resources named Nonprod-LogArchive-CustomS3AutoDeleteObjects*:
# allow iam:CreateRole, iam:DeleteRole, iam:AttachRolePolicy,
# iam:DetachRolePolicy, and related role lifecycle actions on that role-name prefix, iam:PassRole only with
# iam:PassedToService=lambda.amazonaws.com, and Lambda lifecycle actions only on
# the matching function-name prefix.
# The default below is intentionally NOT AdministratorAccess so a copy-paste
# run fails safe and forces an explicit choice. For a multi-account bootstrap,
# prefer CFN_EXECUTION_POLICY_NAME: the runner constructs the same local policy
# name under each target account. CFN_EXECUTION_POLICY_ARN is accepted only for
# a single target because a customer-managed policy ARN is account-scoped.
CFN_EXECUTION_POLICY_NAME="${CFN_EXECUTION_POLICY_NAME:-}"
CFN_EXECUTION_POLICY_ARN="${CFN_EXECUTION_POLICY_ARN:-}"
if [[ -n "$CFN_EXECUTION_POLICY_NAME" && -n "$CFN_EXECUTION_POLICY_ARN" ]]; then
  echo "ERROR: set only one of CFN_EXECUTION_POLICY_NAME or CFN_EXECUTION_POLICY_ARN" >&2
  exit 1
fi
if [[ -z "$CFN_EXECUTION_POLICY_NAME" && -z "$CFN_EXECUTION_POLICY_ARN" ]]; then
  echo "ERROR: set CFN_EXECUTION_POLICY_NAME (multi-account) or CFN_EXECUTION_POLICY_ARN (single-account)" >&2
  echo "       to a scoped customer-managed policy. Do NOT use AdministratorAccess." >&2
  echo "       Generate role-specific documents with render-cfn-execution-policy.py." >&2
  exit 1
fi
if [[ -n "$CFN_EXECUTION_POLICY_NAME" && ! "$CFN_EXECUTION_POLICY_NAME" =~ ^[A-Za-z0-9+=,.@_/-]+$ ]]; then
  echo "ERROR: invalid CFN_EXECUTION_POLICY_NAME" >&2
  exit 1
fi
if [[ "$CFN_EXECUTION_POLICY_ARN" == "arn:aws:iam::aws:policy/AdministratorAccess" ]]; then
  echo "ERROR: AdministratorAccess is not an accepted CloudFormation execution policy" >&2
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
LOG_ARCHIVE="$(json agenticai/logArchiveAccountId)"
AUDIT="$(json agenticai/auditAccountId)"
SANDBOX="$(json agenticai/sandboxAccountId)"
WORKLOAD_NP="$(json agenticai/workloadNonprodAccountId)"
WORKLOAD_PR="$(json agenticai/workloadProdAccountId)"

if [[ -z "$PLATFORM_NP" ]]; then
  echo "agenticai/platformNonprodAccountId not set in $CONTEXT_FILE" >&2
  exit 1
fi

echo "Bootstrapping every account with --trust ${PLATFORM_NP}:"

TARGET_ACCOUNTS=()
for acct in "$PLATFORM_NP" "$PLATFORM_PR" "$LOG_ARCHIVE" "$AUDIT" "$SANDBOX" "$WORKLOAD_NP" "$WORKLOAD_PR"; do
  [[ -z "$acct" ]] && continue
  if [[ " ${TARGET_ACCOUNTS[*]} " == *" $acct "* ]]; then
    continue
  fi
  TARGET_ACCOUNTS+=("$acct")
done

if [[ -n "$CFN_EXECUTION_POLICY_ARN" ]]; then
  if [[ ${#TARGET_ACCOUNTS[@]} -ne 1 ]]; then
    echo "ERROR: CFN_EXECUTION_POLICY_ARN is account-scoped and can bootstrap only one target; use CFN_EXECUTION_POLICY_NAME for multiple accounts" >&2
    exit 1
  fi
  if [[ ! "$CFN_EXECUTION_POLICY_ARN" =~ ^arn:${PARTITION}:iam::([0-9]{12}):policy/.+ ]]; then
    echo "ERROR: invalid customer-managed CFN_EXECUTION_POLICY_ARN" >&2
    exit 1
  fi
  if [[ "${BASH_REMATCH[1]}" != "${TARGET_ACCOUNTS[0]}" ]]; then
    echo "ERROR: CFN_EXECUTION_POLICY_ARN belongs to ${BASH_REMATCH[1]}, not target ${TARGET_ACCOUNTS[0]}" >&2
    exit 1
  fi
fi

for acct in "${TARGET_ACCOUNTS[@]}"; do
  echo ""
  echo "-> Bootstrap aws://${acct}/${REGION}"
  # Expects the OPERATOR to have previously assumed a sufficiently-privileged
  # role in the target account (via sso / aws sts assume-role) to create the
  # bootstrap roles + KMS key. This is the human's bootstrapping identity and
  # is separate from the CloudFormation EXECUTION policy set below (which must
  # be scoped, not AdministratorAccess — see the header warning).
  execution_policy_arn="$CFN_EXECUTION_POLICY_ARN"
  if [[ -n "$CFN_EXECUTION_POLICY_NAME" ]]; then
    execution_policy_arn="arn:${PARTITION}:iam::${acct}:policy/${CFN_EXECUTION_POLICY_NAME}"
  fi
  npx cdk bootstrap "aws://${acct}/${REGION}" \
    --trust "$PLATFORM_NP" \
    --trust-for-lookup "$PLATFORM_NP" \
    --cloudformation-execution-policies "$execution_policy_arn" \
    --qualifier "$QUALIFIER"
done

echo ""
echo "Bootstrap complete. Next: 'cdk deploy --context stage=pipeline ...'"
