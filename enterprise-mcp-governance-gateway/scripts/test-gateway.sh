#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# test-gateway.sh — curl-based smoke test of the deployed AgentCore Gateway.
#
# Requires:
#   AUTH_TOKEN    a Bearer JWT (run `source scripts/get-token.sh` first). Optional
#                 only if the gateway authorizerType is NONE.
# GATEWAY_URL is read from SSM (published by the CDK stack) if not set in the env.
#
# Exercises: tools/list, an allowed call, a policy-denied call, and an
# interceptor-blocked SQL-injection call.
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
SSM_PREFIX="${SSM_PREFIX:-/enterprise-mcp-gateway}"

# Resolve the gateway URL from the env, else from SSM Parameter Store.
if [ -z "${GATEWAY_URL:-}" ]; then
  GATEWAY_URL="$(aws ssm get-parameter --region "$REGION" \
    --name "${SSM_PREFIX}/gateway/url" --query 'Parameter.Value' --output text 2>/dev/null || true)"
fi
[ -n "${GATEWAY_URL:-}" ] && [ "${GATEWAY_URL}" != "None" ] || {
  echo "ERROR: GATEWAY_URL not set and not found in SSM (${SSM_PREFIX}/gateway/url). Deploy the stack first." >&2
  exit 1
}

# Build curl auth args safely as an array (avoids quoting bugs from embedding
# the header in a string).
AUTH_ARGS=()
if [ -n "${AUTH_TOKEN:-}" ]; then
  AUTH_ARGS=(-H "Authorization: Bearer ${AUTH_TOKEN}")
else
  echo "WARNING: AUTH_TOKEN is not set; sending unauthenticated requests." >&2
fi

pretty() {
  # Pretty-print JSON if possible, otherwise echo raw.
  python3 -m json.tool 2>/dev/null || cat
}

call() {
  # call <json-body>
  curl -sS -X POST "$GATEWAY_URL" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Mcp-Protocol-Version: 2025-11-25" \
    "${AUTH_ARGS[@]}" \
    -d "$1" | pretty
}

echo "=== Test 1: tools/list ==="
call '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

echo ""
echo "=== Test 2: tools/call (allowed - DocsAPI___get_page) ==="
call '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"DocsAPI___get_page","arguments":{"pageId":"arch-overview"}}}'

echo ""
echo "=== Test 3: tools/call (should be denied by policy - DatabaseAPI___drop_table) ==="
call '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"DatabaseAPI___drop_table","arguments":{"tableName":"users"}}}'

echo ""
echo "=== Test 4: tools/call (SQL injection - should be blocked by REQUEST interceptor) ==="
call '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"DatabaseAPI___execute_query","arguments":{"query":"DROP TABLE users; --"}}}'

echo ""
echo "=== Test 5: tools/call (PII redaction - DatabaseAPI___export_pii_report) ==="
call '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"DatabaseAPI___export_pii_report","arguments":{"department":"engineering"}}}'

echo ""
# The Atlassian checks only mean anything if the connector stack is deployed. Without it the
# gateway answers "-32602 Unknown tool", which is neither a pass nor a failure — so detect it
# and skip, the same way tests/integration does.
if curl -s -X POST "$GATEWAY_URL" \
     -H "Authorization: Bearer $AUTH_TOKEN" -H "Content-Type: application/json" \
     -H "Mcp-Protocol-Version: 2025-11-25" \
     -d '{"jsonrpc":"2.0","id":99,"method":"tools/list"}' | grep -q "Atlassian___"; then

  echo "=== Test 6: Atlassian read (allowed) — expect live Jira projects, OR a -32042"
  echo "            consent URL if this user hasn't authorized Atlassian yet (both OK). ==="
  call '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"Atlassian___getVisibleJiraProjects","arguments":{}}}'

  echo ""
  echo "=== Test 7: Atlassian write (should be Cedar-denied — role-gated to atlassian-writer) ==="
  call '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"Atlassian___createJiraIssue","arguments":{"projectKey":"TEST","summary":"smoke-test (should be denied)"}}}'
else
  echo "=== Tests 6-7: SKIPPED — Atlassian connector not deployed (no Atlassian___ tools) ==="
  echo "    Deploy it with connectors/atlassian/README.md to exercise the per-user 3LO path."
fi

echo ""
echo "=== Done ==="
