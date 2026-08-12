#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# atlassian-authorize.sh — one command for the one-time per-user Atlassian OAuth (3LO)
# consent. Run this ONCE per user before using the Atlassian tools in your MCP client
# (Kiro): it mints that user's gateway token, opens the Atlassian login in your browser,
# you click Allow, and it vaults your Atlassian token so the gateway can call Jira/
# Confluence as you from then on.
#
#   bash scripts/atlassian-authorize.sh                      # default user admin@example.com
#   bash scripts/atlassian-authorize.sh you@example.com      # authorize a specific user
#
# The user you authorize MUST match the user your MCP client connects as (the vault is
# keyed per user). Idempotent: if already authorized, it just says so.
#
# NOTE (local dev flow): this uses a localhost:8080 OAuth return URL + your desktop
# browser, so it needs a browser on this machine. PRODUCTION HARDENING: replace the
# localhost return URL with a hosted return endpoint (API Gateway/Lambda that calls
# CompleteResourceTokenAuth) so no local process/browser is needed and it works
# headless / for any user on any device.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AWS_REGION="${AWS_REGION:-us-west-2}"
[ -n "${1:-}" ] && export COGNITO_USERNAME="$1"
USER_SHOWN="${COGNITO_USERNAME:-admin@example.com}"

echo ">> Minting gateway token for ${USER_SHOWN} (region ${AWS_REGION})..."
# shellcheck disable=SC1091
source "$HERE/scripts/get-token.sh"
if [ -z "${AGENTCORE_JWT:-}" ]; then
  echo "ERROR: could not mint a token (is the user seeded? try: bash scripts/seed-demo-users.sh)" >&2
  exit 1
fi

# Free port 8080 in case a stale listener is holding it.
if command -v lsof >/dev/null 2>&1 && lsof -ti:8080 >/dev/null 2>&1; then
  echo ">> Port 8080 in use — freeing it..."
  lsof -ti:8080 | xargs kill -9 2>/dev/null || true
  sleep 1
fi

PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
echo ">> Starting Atlassian consent — a browser will open; log in to Atlassian and click Allow."
echo "   (Use the SAME Atlassian account you want the gateway to act as.)"
exec "$PY" "$HERE/connectors/atlassian/scripts/authorize.py"
