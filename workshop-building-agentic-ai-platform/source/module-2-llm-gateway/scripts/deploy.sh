#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Deploy the LLM Gateway CloudFormation stack (LiteLLM Proxy + PostgreSQL).
#
# Usage:
#   ./scripts/deploy.sh [STACK_NAME]
#
# Defaults:
#   STACK_NAME = workshop-llm-gateway-stack

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFN_TEMPLATE="${SCRIPT_DIR}/../../../static/cfn/llm-gateway/workshop-llm-gateway-stack.yaml"
STACK_NAME="${1:-workshop-llm-gateway-stack}"
REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-$(aws configure get region 2>/dev/null || echo us-west-2)}}"

echo "============================================"
echo "  LLM Gateway (LiteLLM Proxy) — Deploy"
echo "============================================"
echo "  Stack:    ${STACK_NAME}"
echo "  Region:   ${REGION}"
echo "  Template: ${CFN_TEMPLATE}"
echo "============================================"
echo ""

# Validate template first
echo "Validating CloudFormation template..."
aws cloudformation validate-template \
  --template-body "file://${CFN_TEMPLATE}" \
  --region "${REGION}" \
  > /dev/null

echo "Template valid. Creating stack..."
# The canonical template's parameters (LiteLLMImageTag, PostgresImageTag,
# AdminKey) all have defaults, so --parameters is unnecessary. Pass
# --parameters ParameterKey=AdminKey,ParameterValue=sk-... here if you want
# to pin a specific admin key.
aws cloudformation create-stack \
  --stack-name "${STACK_NAME}" \
  --template-body "file://${CFN_TEMPLATE}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "${REGION}" \
  --tags Key=Workshop,Value=agentic-ai-platform Key=Module,Value=2-llm-gateway

echo ""
echo "Stack creation initiated. Waiting for completion..."
echo "(This typically takes 5-8 minutes)"
echo ""

aws cloudformation wait stack-create-complete \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}"

echo ""
echo "Stack created successfully!"
echo ""

# Print outputs
echo "============================================"
echo "  Stack Outputs"
echo "============================================"
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table

echo ""
echo "Next steps:"
echo "  1. Run ./scripts/wait_for_ready.sh ${STACK_NAME} to wait for LiteLLM to be healthy."
echo "  2. Run python scripts/setup_keys.py --stack-name ${STACK_NAME} to create virtual keys."
