#!/usr/bin/env bash
# scp-sandbox-soak.sh — live SCP twins in an account under the Sandbox OU.
#
# Every control is tested twice:
#   expect_scp_deny   the call must fail with an EXPLICIT SERVICE CONTROL
#                     POLICY deny (any other failure is a test error, not a
#                     pass — a missing CLI command, a validation error or a
#                     wrong-region endpoint used to count as "denied").
#   expect_scp_allow  the call must NOT be denied by an SCP. It may still fail
#                     for other reasons (e.g. a placeholder role or model
#                     access), which proves it passed SCP authorization.
# An SCP that denies everything (the 2026-09-25 SCP-01 lockout) fails the
# allow twin; an SCP that denies nothing fails the deny twin.
#
# Required environment (the account must be under the Sandbox OU with the
# SCPs attached; credentials for that account):
#   SOAK_APPROVED_GUARDRAIL_ARN   guardrail ARN on SCP-02's allow-list
#   SOAK_SUBNET_ID / SOAK_SECURITY_GROUP_ID   any subnet + SG in the account
#   SOAK_RUNTIME_ROLE_ARN         any role ARN in the account (never assumed:
#                                 the requests are expected to fail after SCP
#                                 evaluation)
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
: "${REGION:?set AWS_REGION or AWS_DEFAULT_REGION to the explicit soak Region}"
: "${SOAK_APPROVED_GUARDRAIL_ARN:?set SOAK_APPROVED_GUARDRAIL_ARN}"
: "${SOAK_SUBNET_ID:?set SOAK_SUBNET_ID}"
: "${SOAK_SECURITY_GROUP_ID:?set SOAK_SECURITY_GROUP_ID}"
: "${SOAK_RUNTIME_ROLE_ARN:?set SOAK_RUNTIME_ROLE_ARN}"

OUT="$(mktemp -d "${TMPDIR:-/tmp}/scp-soak.XXXXXX")"
trap 'rm -rf "$OUT"' EXIT
SCP_DENY_PATTERN='explicit deny in a service control policy'
FAILURES=0

run_capture() {
  # Runs "$@", returns its exit code, leaves stderr in $OUT/err.
  set +e
  "$@" >"$OUT/out" 2>"$OUT/err"
  local rc=$?
  set -e
  return $rc
}

expect_scp_deny() {
  local label="$1"; shift
  if run_capture "$@"; then
    echo "  FAIL [$label]: call succeeded; the SCP did not deny." >&2
    FAILURES=$((FAILURES + 1))
  elif grep -qi "$SCP_DENY_PATTERN" "$OUT/err"; then
    echo "  OK   [$label]: explicit SCP deny."
  else
    echo "  FAIL [$label]: failed, but not with an SCP deny: $(head -c 240 "$OUT/err")" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

expect_scp_allow() {
  local label="$1"; shift
  if run_capture "$@"; then
    echo "  OK   [$label]: allowed (call succeeded)."
  elif grep -qi "$SCP_DENY_PATTERN" "$OUT/err"; then
    echo "  FAIL [$label]: denied by an SCP — the control is over-broad." >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "  OK   [$label]: passed SCP authorization (failed later: $(head -c 120 "$OUT/err"))."
  fi
}

ALLOWED_MODEL="anthropic.claude-haiku-4-5-20251001-v1:0"
BODY='{"anthropic_version":"bedrock-2023-05-31","max_tokens":8,"messages":[{"role":"user","content":"ok"}]}'

echo "SCP-01 model allow-list"
expect_scp_deny "unlisted model" \
  aws bedrock-runtime invoke-model --region "$REGION" --model-id amazon.titan-text-express-v1 \
    --guardrail-identifier "$SOAK_APPROVED_GUARDRAIL_ARN" --guardrail-version DRAFT \
    --body '{"inputText":"ok"}' --cli-binary-format raw-in-base64-out "$OUT/model.out"
expect_scp_allow "allow-listed model" \
  aws bedrock-runtime invoke-model --region "$REGION" --model-id "$ALLOWED_MODEL" \
    --guardrail-identifier "$SOAK_APPROVED_GUARDRAIL_ARN" --guardrail-version DRAFT \
    --body "$BODY" --cli-binary-format raw-in-base64-out "$OUT/model.out"

echo "SCP-02 guardrail on every call"
expect_scp_deny "no guardrail" \
  aws bedrock-runtime invoke-model --region "$REGION" --model-id "$ALLOWED_MODEL" \
    --body "$BODY" --cli-binary-format raw-in-base64-out "$OUT/model.out"

echo "SCP-06 approved regions"
expect_scp_deny "non-approved region" aws ec2 describe-availability-zones --region eu-west-1
expect_scp_allow "approved region" aws ec2 describe-availability-zones --region "$REGION"

echo "SCP-07 AgentCore Runtimes must be VPC-attached"
ARTIFACT='{"containerConfiguration":{"containerUri":"123456789012.dkr.ecr.'"$REGION"'.amazonaws.com/soak@sha256:0000000000000000000000000000000000000000000000000000000000000000"}}'
expect_scp_deny "PUBLIC network Runtime" \
  aws bedrock-agentcore-control create-agent-runtime --region "$REGION" --agent-runtime-name scpsoakpublic \
    --agent-runtime-artifact "$ARTIFACT" --role-arn "$SOAK_RUNTIME_ROLE_ARN" \
    --network-configuration networkMode=PUBLIC
expect_scp_allow "VPC Runtime with subnets + security groups" \
  aws bedrock-agentcore-control create-agent-runtime --region "$REGION" --agent-runtime-name scpsoakvpc \
    --agent-runtime-artifact "$ARTIFACT" --role-arn "$SOAK_RUNTIME_ROLE_ARN" \
    --network-configuration "networkMode=VPC,networkModeConfig={subnets=[$SOAK_SUBNET_ID],securityGroups=[$SOAK_SECURITY_GROUP_ID]}"

echo "SCP-08 no ECR Public"
expect_scp_deny "ecr-public" aws ecr-public describe-registries --region us-east-1

echo ""
if [ "$FAILURES" -ne 0 ]; then
  echo "$FAILURES twin(s) failed. Do NOT promote the SCPs to AgenticAI-Workloads." >&2
  exit 1
fi
echo "All twins passed. The SCPs deny what they must and allow what they must."
