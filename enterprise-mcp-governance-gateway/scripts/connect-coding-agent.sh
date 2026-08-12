#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Connect a REAL coding agent (Claude Code / Kiro) to the governed AgentCore
# Gateway as an MCP server, and (optionally) drive the governance scenarios
# through the agent itself.
#
# This is the test that matters: the coding agent experiences the allow / deny /
# redaction governance through its own MCP integration — exactly as an enterprise
# developer would. Nothing is mocked; calls hit the live gateway.
#
# The solution is coding-agent AGNOSTIC: any MCP-capable agent connects the same
# way (one HTTP URL + a Bearer JWT). Proven on Claude Code and Kiro CLI.
#
# Usage:
#   bash scripts/connect-coding-agent.sh claude        # register + drive Claude Code (headless)
#   bash scripts/connect-coding-agent.sh claude-register # just register in Claude Code, no tests
#   bash scripts/connect-coding-agent.sh kiro-cli      # register + drive Kiro CLI / Amazon Q (headless)
#   bash scripts/connect-coding-agent.sh kiro          # write Kiro IDE workspace MCP config (.kiro/settings/mcp.json)
#   bash scripts/connect-coding-agent.sh kiro-global    # merge into the GLOBAL Kiro config (~/.kiro/settings/mcp.json)
#
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
SSM_PREFIX="${SSM_PREFIX:-/enterprise-mcp-gateway}"
MODE="${1:-claude}"

ssm_get() { aws ssm get-parameter --region "$REGION" --name "$1" --query 'Parameter.Value' --output text 2>/dev/null || true; }

GATEWAY_URL="$(ssm_get "${SSM_PREFIX}/gateway/url")"
[ -n "$GATEWAY_URL" ] && [ "$GATEWAY_URL" != "None" ] || {
  echo "Gateway URL not found in SSM (${SSM_PREFIX}/gateway/url) — deploy the CDK stack first"; exit 1; }

# Derive the REQUEST interceptor log group (function names are CDK-generated).
REQ_ARN="$(ssm_get "${SSM_PREFIX}/lambda/request-interceptor-arn")"
REQ_FN="${REQ_ARN##*:}"
LOG_GROUP="/aws/lambda/${REQ_FN:-agc-gov-request-interceptor}"

# --- Mint a fresh Cognito JWT via get-token.sh (SSM + Secrets Manager + admin-auth) ---
echo ">> Minting JWT (demo admin user) via get-token.sh..."
JWT="$(bash "$ROOT/scripts/get-token.sh")"
[ -n "$JWT" ] || { echo "Failed to mint JWT — run scripts/seed-demo-users.sh after deploy"; exit 1; }
echo ">> Token minted (valid ~1h)."

MCP_CONFIG="/tmp/agc_mcp.json"
python3 - "$GATEWAY_URL" "$JWT" > "$MCP_CONFIG" <<'PY'
import json,sys
url,tok=sys.argv[1],sys.argv[2]
print(json.dumps({"mcpServers":{"enterprise-gateway":{"type":"http","url":url,
  "headers":{"Authorization":"Bearer "+tok}}}}, indent=2))
PY

case "$MODE" in
  claude-register)
    claude mcp remove enterprise-gateway 2>/dev/null || true
    claude mcp add --transport http --scope project enterprise-gateway "$GATEWAY_URL" \
      --header "Authorization: Bearer $JWT"
    echo ">> Registered in .mcp.json (project scope). Run 'claude' and approve the server."
    echo ">> NOTE: token expires in ~1h; re-run to refresh the header."
    ;;
  claude)
    AT="mcp__enterprise-gateway"
    echo; echo "### A. tools/list (visibility governance) ###"
    claude -p "List every MCP tool from enterprise-gateway, one per line, names only." \
      --mcp-config "$MCP_CONFIG" --strict-mcp-config --allowedTools "$AT" --model sonnet </dev/null 2>&1 | tail -8
    echo; echo "### B. allowed read ###"
    claude -p "Call enterprise-gateway DocsAPI get_page with pageId 'arch-overview' and show the result." \
      --mcp-config "$MCP_CONFIG" --strict-mcp-config --allowedTools "${AT}__DocsAPI___get_page" --model sonnet </dev/null 2>&1 | tail -6
    echo; echo "### C. SQL-injection block (REQUEST interceptor) ###"
    claude -p "Authorized governance test, disposable stub backend (read lambdas/sample-targets/database-server/index.py to confirm no real DB). Call enterprise-gateway execute_query with query exactly 'DROP TABLE users; --' and report the gateway's verbatim error." \
      --mcp-config "$MCP_CONFIG" --strict-mcp-config --allowedTools "${AT}__DatabaseAPI___execute_query" "Read" \
      --permission-mode acceptEdits --model sonnet </dev/null 2>&1 | tail -8
    echo; echo "### D. PII redaction (RESPONSE interceptor) ###"
    claude -p "Authorized governance demo. Call enterprise-gateway export_pii_report with department 'engineering' and report the verbatim returned text." \
      --mcp-config "$MCP_CONFIG" --strict-mcp-config --allowedTools "${AT}__DatabaseAPI___export_pii_report" \
      --permission-mode acceptEdits --model sonnet </dev/null 2>&1 | tail -8
    echo; echo ">> Done. Check the CloudWatch audit trail:"
    echo "   aws logs filter-log-events --region $REGION --log-group-name $LOG_GROUP --start-time \$(python3 -c 'import time;print(int((time.time()-600)*1000))')"
    ;;
  kiro-cli)
    # Kiro CLI / Amazon Q: drive a fully headless agent isolated to ONLY the gateway.
    command -v kiro-cli >/dev/null 2>&1 || { echo "kiro-cli not found on PATH"; exit 1; }
    AGENT_DIR=".kiro/agents"; mkdir -p "$AGENT_DIR"
    python3 - "$GATEWAY_URL" "$JWT" > "$AGENT_DIR/governance-test.json" <<'PY'
import json,sys
url,tok=sys.argv[1],sys.argv[2]
print(json.dumps({
  "$schema":"https://raw.githubusercontent.com/aws/amazon-q-developer-cli/refs/heads/main/schemas/agent-v1.json",
  "name":"governance-test",
  "description":"Isolated agent that can ONLY reach the governed AgentCore Gateway over MCP.",
  "prompt":"You are a governance test harness. Call gateway tools exactly as asked and report the VERBATIM result or error code+message.",
  "mcpServers":{"enterprise-gateway":{"type":"http","url":url,"headers":{"Authorization":"Bearer "+tok}}},
  "tools":["@enterprise-gateway","fs_read"],
  "allowedTools":["@enterprise-gateway","fs_read"],
  "resources":[]
}, indent=2))
PY
    K(){ kiro-cli chat --no-interactive --trust-all-tools --require-mcp-startup --agent governance-test "$1" 2>&1 | sed 's/\x1b\[[0-9;]*m//g'; }
    echo; echo "### A. tool discovery (visibility governance) ###"
    K "List the MCP tools you have from enterprise-gateway, names only." | grep -iE "DocsAPI|DatabaseAPI" | head
    echo; echo "### B. allowed read ###"
    K "Call DocsAPI___get_page with pageId 'arch-overview' and show the verbatim result." | grep -iE "Architecture|Completed|content" | head
    echo; echo "### C. SQL-injection block (REQUEST interceptor) ###"
    # Use an injection tautology (a SELECT) — the agent will run it, the gateway blocks it.
    K "Call DatabaseAPI___execute_query with query: SELECT * FROM users WHERE id = 1 OR 1=1 --  Report the gateway's verbatim response (code + message)." | grep -iE "block|dangerous|-326|Completed" | head
    echo; echo "### D. PII redaction (RESPONSE interceptor) ###"
    K "Call DatabaseAPI___export_pii_report with department 'engineering' and show the verbatim returned text." | grep -iE "REDACTED|Completed|Alice" | head
    echo; echo ">> Done. The agent config (with a live JWT) is at $AGENT_DIR/governance-test.json (git-ignored)."
    echo ">> NOTE: Kiro CLI's OWN safety layer refuses literal 'DROP TABLE' regardless of framing —"
    echo "   the injection-tautology query above proves the SERVER-SIDE interceptor block without tripping it."
    ;;
  kiro)
    # Kiro reads MCP config from TWO places: this workspace file
    # (.kiro/settings/mcp.json — only loaded when THIS folder is the open
    # workspace) and the global ~/.kiro/settings/mcp.json (always loaded). We write
    # the workspace file by default; pass `kiro-global` to merge into the global one
    # so it shows up regardless of which folder is open. Remote servers REQUIRE
    # "type":"http" (without it Kiro treats the entry as a stdio/command server and
    # silently ignores it).
    mkdir -p .kiro/settings
    python3 - "$GATEWAY_URL" "$JWT" > .kiro/settings/mcp.json <<'PY'
import json,sys
url,tok=sys.argv[1],sys.argv[2]
print(json.dumps({"mcpServers":{"enterprise-gateway":{
  "type":"http","url":url,
  "headers":{"Authorization":"Bearer "+tok},
  "disabled":False,"autoApprove":[]}}}, indent=2))
PY
    echo ">> Wrote .kiro/settings/mcp.json with the live gateway + a fresh JWT."
    echo ">> IMPORTANT: this is the WORKSPACE config — it only loads when Kiro has THIS"
    echo "   folder open as the workspace ($ROOT)."
    echo "   In Kiro: File > Open Folder > $ROOT, then reload MCP (command palette:"
    echo "   'MCP: Reload Servers') or toggle the server in the MCP panel."
    echo ">> To make it appear in ANY Kiro window instead, merge it into the GLOBAL"
    echo "   config:  bash scripts/connect-coding-agent.sh kiro-global"
    echo ">> NOTE: the JWT expires in ~1h. Re-run this script to refresh."
    ;;
  kiro-global)
    # Merge the enterprise-gateway entry into the user's GLOBAL Kiro config so it is
    # available in every Kiro window (not just this workspace). Preserves existing
    # servers; backs up the file first.
    GLOBAL_DIR="$HOME/.kiro/settings"; GLOBAL="$GLOBAL_DIR/mcp.json"
    mkdir -p "$GLOBAL_DIR"
    [ -f "$GLOBAL" ] && cp "$GLOBAL" "$GLOBAL.backup-$(date +%Y%m%d-%H%M%S 2>/dev/null || echo bak)" 2>/dev/null || true
    GATEWAY_URL="$GATEWAY_URL" JWT="$JWT" GLOBAL="$GLOBAL" python3 <<'PY'
import json, os
path = os.environ["GLOBAL"]
try:
    cfg = json.load(open(path))
except Exception:
    cfg = {}
cfg.setdefault("mcpServers", {})
cfg["mcpServers"]["enterprise-gateway"] = {
    "type": "http",
    "url": os.environ["GATEWAY_URL"],
    "headers": {"Authorization": "Bearer " + os.environ["JWT"]},
    "disabled": False,
    "autoApprove": [],
}
json.dump(cfg, open(path, "w"), indent=2)
print(">> Merged 'enterprise-gateway' into", path, "(existing servers preserved; backup written).")
PY
    echo ">> Reload MCP in Kiro ('MCP: Reload Servers'); enterprise-gateway tools appear in any window."
    echo ">> NOTE: the JWT expires in ~1h. Re-run to refresh."
    ;;
  *)
    echo "Unknown mode: $MODE (use: claude | claude-register | kiro-cli | kiro | kiro-global)"; exit 1 ;;
esac
