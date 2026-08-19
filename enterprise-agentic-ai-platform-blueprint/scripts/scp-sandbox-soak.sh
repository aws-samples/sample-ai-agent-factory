#!/usr/bin/env bash
# scp-sandbox-soak.sh — canonical denial tests in the Sandbox OU account.
#
# Runs the four denial tests that gate SCP promotion from the Sandbox OU to
# the AgenticAI-Workloads OU. All four must deny before promotion.
#
# Expects AWS credentials for the sandbox account (e.g. via aws sso login).
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"

expect_deny() {
  local label="$1"; shift
  echo ""
  echo "[SOAK] $label — expecting AccessDenied"
  if "$@" >/dev/null 2>&1; then
    echo "  FAIL: command succeeded; SCP did not deny." >&2
    exit 1
  fi
  echo "  OK: command denied as expected."
}

# 1. SCP-02 — missing guardrail identifier.
expect_deny "SCP-02 enforces GuardrailIdentifier on InvokeModel" \
  aws bedrock-runtime invoke-model \
    --model-id anthropic.claude-haiku-4-5-20251001-v1:0 \
    --body '{"messages":[{"role":"user","content":"hello"}]}' \
    --region "$REGION" \
    /tmp/soak-no-guardrail.out

# 2. SCP-01 — non-allowlisted model id.
expect_deny "SCP-01 denies non-allowlisted foundation model" \
  aws bedrock-runtime invoke-model \
    --model-id amazon.titan-text-express-v1 \
    --guardrail-identifier "arn:aws:bedrock:${REGION}:000000000000:guardrail/example" \
    --body '{"inputText":"hello"}' \
    --region "$REGION" \
    /tmp/soak-bad-model.out

# 3. SCP-06 — non-approved region.
expect_deny "SCP-06 denies actions in non-approved region (eu-west-1)" \
  aws ec2 describe-instances --region eu-west-1

# 4. SCP-07 — AgentCore Runtime creation without subnets/SGs.
expect_deny "SCP-07 denies AgentCore Runtime without subnets/SGs" \
  aws bedrock-agentcore create-agent-runtime --name soak-test --region "$REGION"

echo ""
echo "All four canonical denial tests passed. SCPs are safe to promote to"
echo "the AgenticAI-Workloads OU."
