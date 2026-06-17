#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Workshop environment self-test. Verifies the four platform stacks + the IDE
# are deployed and that key endpoints respond, so a facilitator or self-paced
# user can confirm the environment is functional BEFORE starting the workshop.
# Exits non-zero if any check fails.
#
# Usage: ./scripts/self-test.sh [-r region]
#   Region resolves from: -r flag, then AWS_REGION, then AWS_DEFAULT_REGION,
#   then `aws configure get region`. Fails if none is set.

set -uo pipefail

REGION_FLAG=""
while getopts "r:" opt; do
  case "$opt" in
    r) REGION_FLAG="$OPTARG" ;;
    *) echo "Usage: $0 [-r region]"; exit 2 ;;
  esac
done

# -r wins; otherwise AWS_REGION > AWS_DEFAULT_REGION > aws configure get region.
REGION="$REGION_FLAG"
[[ -n "$REGION" ]] || REGION="${AWS_REGION:-}"
[[ -n "$REGION" ]] || REGION="${AWS_DEFAULT_REGION:-}"
[[ -n "$REGION" ]] || REGION="$(aws configure get region 2>/dev/null || true)"
if [[ -z "$REGION" ]]; then
  echo "ERROR: no AWS region resolved. Pass -r <region>, or set AWS_REGION /"
  echo "       AWS_DEFAULT_REGION, or run 'aws configure set region <region>'."
  exit 2
fi

PASS=0; FAIL=0
ok()  { printf '  [PASS] %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL + 1)); }

stack_status() {
  aws cloudformation describe-stacks --region "$REGION" \
    --stack-name "$1" --query 'Stacks[0].StackStatus' --output text 2>/dev/null
}

output() { # output <stack> <OutputKey>
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null
}

echo "Workshop self-test (region: $REGION)"
echo

echo "1. CloudFormation stacks"
for s in workshop-llm-gateway-stack workshop-registry-stack \
         workshop-tools-gateway-stack workshop-agentcore-stack code-editor; do
  st="$(stack_status "$s")"
  case "$st" in
    CREATE_COMPLETE | UPDATE_COMPLETE) ok "$s ($st)" ;;
    *) bad "$s (${st:-NOT_FOUND})" ;;
  esac
done

echo "2. LLM Gateway health"
proxy="$(output workshop-llm-gateway-stack ProxyUrl)"
if [[ -n "$proxy" && "$proxy" != "None" ]] &&
  curl -fsS --max-time 10 "${proxy%/}/health/liveliness" >/dev/null 2>&1; then
  ok "LiteLLM proxy reachable ($proxy)"
else
  bad "LiteLLM proxy not reachable (${proxy:-no ProxyUrl output})"
fi

echo "3. MCP Registry reachable"
mcp="$(output workshop-registry-stack MCPGatewayUrl)"
[[ -n "$mcp" && "$mcp" == http* ]] || mcp="https://${mcp#None}"
# Probe /health, not the bare root: the registry UI root ("/") can hang while
# rendering, whereas /health is a fast liveness endpoint (matches the LLM
# gateway check above). Falls back to the API auth gate (401 = up) if needed.
if [[ "$mcp" != "https://" ]] && curl -fsS --max-time 15 -o /dev/null "${mcp%/}/health"; then
  ok "Registry endpoint reachable (${mcp%/}/health)"
elif [[ "$mcp" != "https://" ]] && \
     [[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "${mcp%/}/api/servers")" == "401" ]]; then
  ok "Registry API reachable (${mcp%/}/api/servers returns 401 = up, auth-gated)"
else
  bad "Registry endpoint not reachable (${mcp:-no MCPGatewayUrl output})"
fi

echo "4. AgentCore Gateway provisioned"
acgw="$(output workshop-agentcore-stack GatewayId)"
if [[ -n "$acgw" && "$acgw" != "None" ]]; then
  ok "AgentCore Gateway present ($acgw)"
else
  bad "AgentCore Gateway ID missing"
fi

echo "5. Bedrock access"
if aws bedrock list-foundation-models --region "$REGION" \
  --query 'modelSummaries[0].modelId' --output text >/dev/null 2>&1; then
  ok "Bedrock list-foundation-models OK"
else
  bad "Bedrock not accessible in $REGION"
fi

echo
echo "Result: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
