#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Deploy the MCP Gateway & Registry stack (Module 3).
#
# This script uploads nested CloudFormation templates to S3 and deploys
# the parent stack. All container images are pulled from Docker Hub
# (mcpgateway/*:v1.0.16) — no ECR build step required.
#
# Usage:
#   bash deploy-registry.sh [deploy|destroy|status]
#
# Prerequisites:
#   - AWS CLI configured with appropriate permissions
#   - An S3 bucket for CloudFormation nested templates (auto-created if missing)
#
# Environment variables (optional):
#   STACK_NAME       — CloudFormation stack name (default: workshop-registry-stack)
#   S3_BUCKET        — existing bucket override; must be owned by the caller
#                      (default: an auto-created account-regional bucket)
#   S3_PREFIX        — S3 prefix for templates (default: cfn/registry/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CFN_DIR="${PROJECT_ROOT}/static/cfn/registry"

STACK_NAME="${STACK_NAME:-workshop-registry-stack}"
REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-$(aws configure get region 2>/dev/null || echo us-west-2)}}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
if [[ ! "${ACCOUNT_ID}" =~ ^[0-9]{12}$ ]]; then
  echo "ERROR: AWS STS returned an invalid account ID: ${ACCOUNT_ID}" >&2
  exit 1
fi

# Account-regional S3 buckets cannot be claimed by another AWS account. An
# explicit S3_BUCKET remains supported for existing deployments, but every S3
# request below independently pins the expected owner.
DEFAULT_S3_BUCKET="cfn-deploy-agentic-platform-${ACCOUNT_ID}-${REGION}-an"
S3_BUCKET="${S3_BUCKET:-${DEFAULT_S3_BUCKET}}"
if [[ "${S3_BUCKET}" == "${DEFAULT_S3_BUCKET}" ]]; then
  S3_BUCKET_IS_DEFAULT=true
else
  S3_BUCKET_IS_DEFAULT=false
fi
S3_PREFIX="${S3_PREFIX:-cfn/registry/}"

ACTION="${1:-deploy}"

REGISTRY_TEMPLATES=(
  network-stack.yaml
  data-stack.yaml
  compute-stack.yaml
  services-stack.yaml
  workshop-tools-stack.yaml
  observability-stack.yaml
)

# ──────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────

banner() {
  echo ""
  echo "============================================="
  echo "  MCP Gateway & Registry — ${1}"
  echo "============================================="
  echo "  Stack:    ${STACK_NAME}"
  echo "  Region:   ${REGION}"
  echo "  S3:       s3://${S3_BUCKET}/${S3_PREFIX}"
  echo "============================================="
  echo ""
}

normalise_s3_region() {
  case "${1}" in
    ""|None|null) echo "us-east-1" ;;
    EU) echo "eu-west-1" ;;
    *) echo "${1}" ;;
  esac
}

# Return 0 only when this account owns the bucket and it is in REGION. Return 1
# only for a confirmed missing bucket. Every other response is a hard failure:
# in particular, a foreign bucket deliberately made writable returns 403 when
# ExpectedBucketOwner is present and must never be treated as usable.
check_s3_bucket() {
  local head_error location

  if ! head_error=$(aws s3api head-bucket \
      --bucket "${S3_BUCKET}" \
      --expected-bucket-owner "${ACCOUNT_ID}" \
      --region "${REGION}" 2>&1); then
    if [[ "${head_error}" == *"(404)"* ||
          "${head_error}" == *"Not Found"* ||
          "${head_error}" == *"NoSuchBucket"* ]]; then
      return 1
    fi

    echo "ERROR: cannot verify that AWS account ${ACCOUNT_ID} owns S3 bucket" >&2
    echo "       '${S3_BUCKET}'. Refusing to upload trusted templates." >&2
    echo "       S3 response: ${head_error}" >&2
    return 2
  fi

  if ! location=$(aws s3api get-bucket-location \
      --bucket "${S3_BUCKET}" \
      --expected-bucket-owner "${ACCOUNT_ID}" \
      --region "${REGION}" \
      --query LocationConstraint \
      --output text 2>&1); then
    echo "ERROR: cannot read the Region of owned bucket '${S3_BUCKET}':" >&2
    echo "       ${location}" >&2
    return 2
  fi

  location=$(normalise_s3_region "${location}")
  if [[ "${location}" != "${REGION}" ]]; then
    echo "ERROR: S3 bucket '${S3_BUCKET}' is in ${location}, not ${REGION}." >&2
    return 2
  fi
}

harden_s3_bucket() {
  aws s3api put-public-access-block \
    --bucket "${S3_BUCKET}" \
    --expected-bucket-owner "${ACCOUNT_ID}" \
    --region "${REGION}" \
    --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true \
    || return $?
  aws s3api put-bucket-encryption \
    --bucket "${S3_BUCKET}" \
    --expected-bucket-owner "${ACCOUNT_ID}" \
    --region "${REGION}" \
    --server-side-encryption-configuration \
      'Rules=[{ApplyServerSideEncryptionByDefault={SSEAlgorithm=AES256},BucketKeyEnabled=true}]' \
    || return $?
}

create_s3_bucket() {
  local -a create_args=(
    --bucket "${S3_BUCKET}"
    --region "${REGION}"
    --object-ownership BucketOwnerEnforced
  )

  if [[ "${S3_BUCKET_IS_DEFAULT}" == "true" ]]; then
    create_args+=(--bucket-namespace account-regional)
  fi
  if [[ "${REGION}" != "us-east-1" ]]; then
    create_args+=(--create-bucket-configuration "LocationConstraint=${REGION}")
  fi

  echo "  Creating S3 bucket: ${S3_BUCKET}"
  aws s3api create-bucket "${create_args[@]}" >/dev/null || return $?
  harden_s3_bucket || return $?
}

ensure_s3_bucket() {
  local status

  if check_s3_bucket; then
    if [[ "${S3_BUCKET_IS_DEFAULT}" == "true" ]]; then
      harden_s3_bucket || return $?
    fi
    echo "  S3 bucket exists and is owned by account ${ACCOUNT_ID}: ${S3_BUCKET}"
    return
  else
    status=$?
  fi

  if [[ "${status}" -ne 1 ]]; then
    return "${status}"
  fi

  create_s3_bucket || return $?
  check_s3_bucket || return $?
  echo "  Created and verified S3 bucket: ${S3_BUCKET}"
}

cleanup_s3_templates() {
  local status tmpl

  if check_s3_bucket; then
    :
  else
    status=$?
    if [[ "${status}" -eq 1 ]]; then
      echo "  S3 bucket is already absent: ${S3_BUCKET}"
      return
    fi
    return "${status}"
  fi

  for tmpl in "${REGISTRY_TEMPLATES[@]}"; do
    aws s3api delete-object \
      --bucket "${S3_BUCKET}" \
      --key "${S3_PREFIX}${tmpl}" \
      --expected-bucket-owner "${ACCOUNT_ID}" \
      --region "${REGION}" >/dev/null \
      || return $?
  done
  echo "  Removed registry templates from ${S3_BUCKET}."

  # The default bucket is dedicated to this script. Delete it when empty; if a
  # concurrent deployment placed anything else in it, S3 returns BucketNotEmpty
  # and preserves the bucket rather than deleting unrelated data.
  if [[ "${S3_BUCKET_IS_DEFAULT}" == "true" ]]; then
    local delete_error
    if delete_error=$(aws s3api delete-bucket \
        --bucket "${S3_BUCKET}" \
        --expected-bucket-owner "${ACCOUNT_ID}" \
        --region "${REGION}" 2>&1); then
      echo "  Removed default deployment bucket ${S3_BUCKET}."
    elif [[ "${delete_error}" == *"BucketNotEmpty"* ]]; then
      echo "  Default deployment bucket is not empty; leaving it in place: ${S3_BUCKET}" >&2
    else
      echo "ERROR: failed to delete default deployment bucket '${S3_BUCKET}':" >&2
      echo "       ${delete_error}" >&2
      return 2
    fi
  fi
}

upload_templates() {
  echo "[1/3] Uploading nested templates to S3..."
  ensure_s3_bucket || return $?

  local tmpl
  for tmpl in "${REGISTRY_TEMPLATES[@]}"; do
    if [[ -f "${CFN_DIR}/${tmpl}" ]]; then
      aws s3api put-object \
        --bucket "${S3_BUCKET}" \
        --key "${S3_PREFIX}${tmpl}" \
        --body "${CFN_DIR}/${tmpl}" \
        --expected-bucket-owner "${ACCOUNT_ID}" \
        --checksum-algorithm SHA256 \
        --region "${REGION}" >/dev/null \
        || return $?
      echo "  Uploaded ${tmpl}"
    else
      echo "  WARN: ${tmpl} not found, skipping"
    fi
  done
  echo ""
}

# ──────────────────────────────────────────────────
# Deploy
# ──────────────────────────────────────────────────

do_deploy() {
  banner "Deploy"
  upload_templates || return $?

  echo "[2/3] Validating workshop-registry-stack.yaml..."
  aws cloudformation validate-template \
    --template-body "file://${CFN_DIR}/workshop-registry-stack.yaml" \
    --region "${REGION}" > /dev/null
  echo "  Template valid."
  echo ""

  # Check if stack already exists
  STACK_STATUS=$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || echo "DOES_NOT_EXIST")

  echo "[3/3] Deploying stack..."
  echo "  Current status: ${STACK_STATUS}"

  if [[ "${STACK_STATUS}" == "DOES_NOT_EXIST" ]]; then
    echo "  Creating new stack..."
    aws cloudformation create-stack \
      --stack-name "${STACK_NAME}" \
      --template-body "file://${CFN_DIR}/workshop-registry-stack.yaml" \
      --parameters \
        ParameterKey=TemplateS3Bucket,ParameterValue="${S3_BUCKET}" \
        ParameterKey=TemplateS3Prefix,ParameterValue="${S3_PREFIX}" \
        ParameterKey=EnableObservability,ParameterValue=false \
      --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
      --region "${REGION}" \
      --tags Key=Workshop,Value=agentic-ai-platform Key=Module,Value=3-mcp-registry

    echo ""
    echo "  Stack creation initiated. Waiting for completion..."
    echo "  (This typically takes 20-30 minutes — DocumentDB cluster is the slowest resource)"
    echo ""

    aws cloudformation wait stack-create-complete \
      --stack-name "${STACK_NAME}" \
      --region "${REGION}"

  elif [[ "${STACK_STATUS}" == "ROLLBACK_COMPLETE" || "${STACK_STATUS}" == "DELETE_COMPLETE" ]]; then
    echo "  Stack is in ${STACK_STATUS}. Deleting and recreating..."
    aws cloudformation delete-stack --stack-name "${STACK_NAME}" --region "${REGION}"
    aws cloudformation wait stack-delete-complete --stack-name "${STACK_NAME}" --region "${REGION}"
    echo "  Old stack deleted. Creating fresh stack..."

    aws cloudformation create-stack \
      --stack-name "${STACK_NAME}" \
      --template-body "file://${CFN_DIR}/workshop-registry-stack.yaml" \
      --parameters \
        ParameterKey=TemplateS3Bucket,ParameterValue="${S3_BUCKET}" \
        ParameterKey=TemplateS3Prefix,ParameterValue="${S3_PREFIX}" \
        ParameterKey=EnableObservability,ParameterValue=false \
      --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
      --region "${REGION}" \
      --tags Key=Workshop,Value=agentic-ai-platform Key=Module,Value=3-mcp-registry

    echo ""
    echo "  Stack creation initiated. Waiting for completion..."
    echo "  (This typically takes 20-30 minutes)"
    echo ""

    aws cloudformation wait stack-create-complete \
      --stack-name "${STACK_NAME}" \
      --region "${REGION}"

  else
    echo "  Updating existing stack..."
    aws cloudformation update-stack \
      --stack-name "${STACK_NAME}" \
      --template-body "file://${CFN_DIR}/workshop-registry-stack.yaml" \
      --parameters \
        ParameterKey=TemplateS3Bucket,ParameterValue="${S3_BUCKET}" \
        ParameterKey=TemplateS3Prefix,ParameterValue="${S3_PREFIX}" \
        ParameterKey=EnableObservability,ParameterValue=false \
      --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
      --region "${REGION}" 2>&1 || {
        echo "  No updates to perform (stack is already up to date)."
        do_status
        return
      }

    echo ""
    echo "  Stack update initiated. Waiting for completion..."
    echo ""

    aws cloudformation wait stack-update-complete \
      --stack-name "${STACK_NAME}" \
      --region "${REGION}"
  fi

  echo ""
  echo "  Stack deployed successfully!"
  echo ""
  do_status
}

# ──────────────────────────────────────────────────
# Status
# ──────────────────────────────────────────────────

do_status() {
  echo "============================================="
  echo "  Stack Outputs"
  echo "============================================="
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
    --output table 2>/dev/null || echo "  Stack not found."

  echo ""
  echo "  Export these for use in Module 3 steps:"
  echo ""

  REGISTRY_URL=$(aws cloudformation list-exports \
    --query "Exports[?Name=='workshop-RegistryUrl'].Value" \
    --output text 2>/dev/null)

  if [[ -n "${REGISTRY_URL}" && "${REGISTRY_URL}" != "None" ]]; then
    echo "    export REGISTRY_URL=${REGISTRY_URL}"
  else
    echo "    export REGISTRY_URL=<check stack outputs>"
  fi
  echo ""

  # The admin password is never printed here. It is a browser-login credential
  # with no CLI consumer, so it stays in Secrets Manager and is copied from the
  # console; echoing it would put it in terminal scrollback and shell history.
  REGION=${AWS_REGION:-$(aws configure get region 2>/dev/null)}
  echo "  Admin password — copy it from Secrets Manager:"
  echo "    https://${REGION}.console.aws.amazon.com/secretsmanager/secret?name=workshop-admin-password&region=${REGION}"
  echo ""
}

# ──────────────────────────────────────────────────
# Destroy
# ──────────────────────────────────────────────────

do_destroy() {
  banner "Destroy"

  echo "WARNING: This will delete ALL registry resources including:"
  echo "  - VPC, subnets, NAT Gateways"
  echo "  - DocumentDB cluster (all data)"
  echo "  - ECS cluster and all services"
  echo "  - CloudFront distribution"
  echo "  - Cognito User Pool"
  echo "  - All registered MCP servers and agents"
  echo ""
  read -rp "Are you sure? (y/N): " confirm
  if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
    echo "Aborted."
    exit 0
  fi

  echo "Deleting stack..."
  aws cloudformation delete-stack \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}"

  echo "Waiting for stack deletion to complete..."
  echo "(This can take 15-20 minutes)"
  aws cloudformation wait stack-delete-complete \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}"

  echo ""
  echo "Stack deleted successfully."

  echo ""
  echo "Cleaning up S3 templates..."
  cleanup_s3_templates || return $?
  echo "Done."
}

# ──────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${ACTION}" in
    deploy)  do_deploy  ;;
    destroy) do_destroy ;;
    status)  do_status  ;;
    *)
      echo "Usage: $0 [deploy|destroy|status]"
      exit 1
      ;;
  esac
fi
