#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Destroy the LLM Gateway CloudFormation stack.
#
# Usage:
#   ./scripts/destroy.sh [STACK_NAME]

set -euo pipefail

STACK_NAME="${1:-workshop-llm-gateway-stack}"
REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-$(aws configure get region 2>/dev/null || echo us-west-2)}}"

echo "============================================"
echo "  LLM Gateway (LiteLLM Proxy) — Destroy"
echo "============================================"
echo "  Stack:  ${STACK_NAME}"
echo "  Region: ${REGION}"
echo "============================================"
echo ""
echo "WARNING: This will delete ALL resources including:"
echo "  - VPC, subnets, NAT Gateway"
echo "  - ECS cluster and service (LiteLLM + PostgreSQL)"
echo "  - EFS file system (all stored data)"
echo "  - ALB and target group"
echo "  - Secrets Manager secrets"
echo "  - IAM roles"
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
aws cloudformation wait stack-delete-complete \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}"

echo ""
echo "Stack deleted successfully."
