#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Poll the LiteLLM Proxy health endpoint until it responds with 200.
#
# Usage:
#   ./scripts/wait_for_ready.sh [STACK_NAME]

set -euo pipefail

STACK_NAME="${1:-workshop-llm-gateway-stack}"
REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-$(aws configure get region 2>/dev/null || echo us-west-2)}}"
MAX_RETRIES=30
SLEEP_INTERVAL=10

# Get the API Gateway HTTPS endpoint from stack outputs
PROXY_URL=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query 'Stacks[0].Outputs[?OutputKey==`ProxyUrl`].OutputValue' \
  --output text)

if [[ -z "${PROXY_URL}" || "${PROXY_URL}" == "None" ]]; then
  echo "ERROR: Could not find ProxyUrl in stack outputs."
  echo "Make sure the stack '${STACK_NAME}' exists and has completed deployment."
  exit 1
fi

HEALTH_URL="${PROXY_URL}/health/liveliness"

echo "Waiting for LiteLLM Proxy to become healthy..."
echo "Health endpoint: ${HEALTH_URL}"
echo ""

for i in $(seq 1 "${MAX_RETRIES}"); do
  if curl -sf "${HEALTH_URL}" > /dev/null 2>&1; then
    echo ""
    echo "LiteLLM Proxy is healthy!"
    echo ""
    echo "  Proxy URL:   ${PROXY_URL}"
    echo "  Health:      ${PROXY_URL}/health/liveliness"
    echo "  Models:      ${PROXY_URL}/models"
    echo "  Admin UI:    ${PROXY_URL}/ui"
    exit 0
  fi
  echo "  Attempt ${i}/${MAX_RETRIES} — not ready yet, retrying in ${SLEEP_INTERVAL}s..."
  sleep "${SLEEP_INTERVAL}"
done

echo ""
echo "ERROR: LiteLLM Proxy did not become healthy within $((MAX_RETRIES * SLEEP_INTERVAL)) seconds."
echo "Check the ECS task logs:"
echo "  aws logs tail /ecs/${STACK_NAME} --follow --region ${REGION}"
exit 1
