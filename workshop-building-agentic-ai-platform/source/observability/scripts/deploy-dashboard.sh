#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Deploy the Agentic AI Platform observability dashboard.
#
# Usage:
#   bash scripts/deploy-dashboard.sh [--stack-name llm-gateway] [--region us-west-2]
#
# Prerequisites:
#   - Module 2 (LLM Gateway) deployed
#   - Module 4 (Tools Gateway) deployed (optional but recommended)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/../cfn/platform-dashboard.yaml"

LLM_STACK="${1:-llm-gateway}"
REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-$(aws configure get region 2>/dev/null || echo us-west-2)}}"

echo "============================================="
echo "  Agentic AI Platform — Observability Setup"
echo "============================================="
echo ""

# Validate template
echo "[1/3] Validating CloudFormation template..."
aws cloudformation validate-template \
  --template-body "file://${TEMPLATE}" \
  --region "${REGION}" > /dev/null
echo "  Template valid."
echo ""

# Auto-detect resource names from LLM Gateway stack
echo "[2/3] Reading resource names from ${LLM_STACK} stack..."
ECS_CLUSTER="${LLM_STACK}-cluster"
ECS_SERVICE=$(aws ecs list-services \
  --cluster "${ECS_CLUSTER}" \
  --region "${REGION}" \
  --query 'serviceArns[0]' --output text 2>/dev/null | awk -F/ '{print $NF}' || echo "${LLM_STACK}-service")

ALB_NAME=$(aws elbv2 describe-load-balancers \
  --names "${LLM_STACK}-alb" \
  --region "${REGION}" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null | awk -F: '{print $NF}' | sed 's|loadbalancer/||' || echo "${LLM_STACK}-alb")

echo "  ECS Cluster:  ${ECS_CLUSTER}"
echo "  ECS Service:  ${ECS_SERVICE}"
echo "  ALB:          ${ALB_NAME}"
echo ""

# Deploy dashboard
echo "[3/3] Deploying dashboard..."
aws cloudformation deploy \
  --template-file "${TEMPLATE}" \
  --stack-name agentic-platform-dashboard \
  --parameter-overrides \
    "LLMGatewayStackName=${LLM_STACK}" \
    "LLMGatewayECSClusterName=${ECS_CLUSTER}" \
    "LLMGatewayECSServiceName=${ECS_SERVICE}" \
    "LLMGatewayALBName=${ALB_NAME}" \
  --region "${REGION}" \
  --no-fail-on-empty-changeset

echo ""
echo "============================================="
echo "  Dashboard deployed!"
echo "============================================="
echo ""
echo "  Open in your browser:"
echo "  https://${REGION}.console.aws.amazon.com/cloudwatch/home?region=${REGION}#dashboards/dashboard/agentic-platform-overview"
echo ""
