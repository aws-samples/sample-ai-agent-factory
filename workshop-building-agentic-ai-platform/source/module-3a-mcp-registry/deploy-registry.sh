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
#   S3_BUCKET        — S3 bucket for templates (default: auto-created)
#   S3_PREFIX        — S3 prefix for templates (default: cfn/registry/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CFN_DIR="${PROJECT_ROOT}/static/cfn/registry"

STACK_NAME="${STACK_NAME:-workshop-registry-stack}"
REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-$(aws configure get region 2>/dev/null || echo us-west-2)}}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="${S3_BUCKET:-cfn-deploy-building-agentic-ai-platform-${ACCOUNT_ID}}"
S3_PREFIX="${S3_PREFIX:-cfn/registry/}"

ACTION="${1:-deploy}"

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

ensure_s3_bucket() {
  if aws s3api head-bucket --bucket "${S3_BUCKET}" 2>/dev/null; then
    echo "  S3 bucket exists: ${S3_BUCKET}"
  else
    echo "  Creating S3 bucket: ${S3_BUCKET}"
    if [[ "${REGION}" == "us-east-1" ]]; then
      aws s3api create-bucket --bucket "${S3_BUCKET}" --region "${REGION}"
    else
      aws s3api create-bucket --bucket "${S3_BUCKET}" --region "${REGION}" \
        --create-bucket-configuration LocationConstraint="${REGION}"
    fi
  fi
}

upload_templates() {
  echo "[1/3] Uploading nested templates to S3..."
  ensure_s3_bucket

  local templates=(
    network-stack.yaml
    data-stack.yaml
    compute-stack.yaml
    services-stack.yaml
    workshop-tools-stack.yaml
    observability-stack.yaml
  )

  for tmpl in "${templates[@]}"; do
    if [[ -f "${CFN_DIR}/${tmpl}" ]]; then
      aws s3 cp "${CFN_DIR}/${tmpl}" "s3://${S3_BUCKET}/${S3_PREFIX}${tmpl}" --quiet
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
  upload_templates

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

  ADMIN_PASSWORD=$(aws cloudformation list-exports \
    --query "Exports[?Name=='workshop-AdminPassword'].Value" \
    --output text 2>/dev/null)

  if [[ -n "${REGISTRY_URL}" && "${REGISTRY_URL}" != "None" ]]; then
    echo "    export REGISTRY_URL=${REGISTRY_URL}"
  else
    echo "    export REGISTRY_URL=<check stack outputs>"
  fi

  if [[ -n "${ADMIN_PASSWORD}" && "${ADMIN_PASSWORD}" != "None" ]]; then
    echo "    export REGISTRY_ADMIN_PASSWORD=${ADMIN_PASSWORD}"
  else
    echo "    export REGISTRY_ADMIN_PASSWORD=<check stack outputs>"
  fi
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
  aws s3 rm "s3://${S3_BUCKET}/${S3_PREFIX}" --recursive --quiet 2>/dev/null || true
  echo "Done."
}

# ──────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────

case "${ACTION}" in
  deploy)  do_deploy  ;;
  destroy) do_destroy ;;
  status)  do_status  ;;
  *)
    echo "Usage: $0 [deploy|destroy|status]"
    exit 1
    ;;
esac
