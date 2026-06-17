#!/usr/bin/env bash
# Deploy a workflow JSON definition to the AgentCore Flows platform.
#
# Thin client of the existing POST /api/deploy endpoint (Gap 3D CI/CD + GitOps).
# Submits the workflow body, prints the returned deployment_id, then polls
# GET /api/deploy/{deployment_id} until a terminal status or timeout.
#
# Usage:
#   API_URL="https://abc.execute-api.us-east-1.amazonaws.com" \
#     JWT_TOKEN="<cognito-access-token>" \
#     WORKFLOW_JSON_PATH="./agents/support-bot.json" \
#     ./scripts/agentcore-deploy.sh
#
# Env vars:
#   API_URL              Base URL of the AgentCore Flows API (required)
#   JWT_TOKEN            Cognito access token, sent as Bearer (required, secret)
#   WORKFLOW_JSON_PATH   Path to the workflow JSON to deploy (required)
#   POLL_TIMEOUT         Seconds to poll for a terminal status (default 600)
#
# The JWT is NEVER echoed. set -euo pipefail makes any failure fatal.

set -euo pipefail

log_info()  { echo -e "\033[1;34m[INFO]\033[0m $*" >&2; }
log_error() { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; }

: "${API_URL:?API_URL is required}"
: "${JWT_TOKEN:?JWT_TOKEN is required}"
: "${WORKFLOW_JSON_PATH:?WORKFLOW_JSON_PATH is required}"
POLL_TIMEOUT="${POLL_TIMEOUT:-600}"

# Strip a trailing slash so we don't build //api/deploy.
API_URL="${API_URL%/}"

# --- Validate inputs --------------------------------------------------------
if [[ ! -f "${WORKFLOW_JSON_PATH}" ]]; then
  log_error "Workflow file not found: ${WORKFLOW_JSON_PATH}"
  exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
  log_error "jq is required but not installed."
  exit 2
fi
if ! jq empty "${WORKFLOW_JSON_PATH}" >/dev/null 2>&1; then
  log_error "Workflow file is not valid JSON: ${WORKFLOW_JSON_PATH}"
  exit 2
fi

# --- Submit deploy ----------------------------------------------------------
log_info "Submitting deploy for ${WORKFLOW_JSON_PATH} to ${API_URL}/api/deploy"

# Write the response body and HTTP status separately so a non-2xx fails loudly
# without leaking the Authorization header into logs.
HTTP_BODY_FILE="$(mktemp)"
trap 'rm -f "${HTTP_BODY_FILE}"' EXIT

HTTP_CODE=$(curl -sS -o "${HTTP_BODY_FILE}" -w '%{http_code}' \
  -X POST "${API_URL}/api/deploy" \
  -H "Authorization: Bearer ${JWT_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@${WORKFLOW_JSON_PATH}")

if [[ "${HTTP_CODE}" -lt 200 || "${HTTP_CODE}" -ge 300 ]]; then
  log_error "Deploy request failed with HTTP ${HTTP_CODE}:"
  cat "${HTTP_BODY_FILE}" >&2
  exit 1
fi

DEPLOYMENT_ID=$(jq -r '.deployment_id // empty' "${HTTP_BODY_FILE}")
if [[ -z "${DEPLOYMENT_ID}" ]]; then
  log_error "No deployment_id in response:"
  cat "${HTTP_BODY_FILE}" >&2
  exit 1
fi

log_info "Deployment accepted: ${DEPLOYMENT_ID}"
echo "${DEPLOYMENT_ID}"

# --- Poll for terminal status ----------------------------------------------
log_info "Polling deployment status (timeout ${POLL_TIMEOUT}s)"
DEADLINE=$(( $(date +%s) + POLL_TIMEOUT ))

while true; do
  STATUS_CODE=$(curl -sS -o "${HTTP_BODY_FILE}" -w '%{http_code}' \
    -X GET "${API_URL}/api/deploy/${DEPLOYMENT_ID}" \
    -H "Authorization: Bearer ${JWT_TOKEN}")

  if [[ "${STATUS_CODE}" -lt 200 || "${STATUS_CODE}" -ge 300 ]]; then
    log_error "Status poll failed with HTTP ${STATUS_CODE}:"
    cat "${HTTP_BODY_FILE}" >&2
    exit 1
  fi

  STATUS=$(jq -r '.status // .deployment_status // "unknown"' "${HTTP_BODY_FILE}")
  log_info "Deployment ${DEPLOYMENT_ID} status: ${STATUS}"

  case "${STATUS}" in
    succeeded|success|completed|deployed|DEPLOYED|SUCCEEDED|COMPLETED)
      log_info "Deployment succeeded."
      exit 0
      ;;
    failed|error|FAILED|ERROR)
      log_error "Deployment failed:"
      cat "${HTTP_BODY_FILE}" >&2
      exit 1
      ;;
  esac

  if [[ "$(date +%s)" -ge "${DEADLINE}" ]]; then
    log_error "Timed out after ${POLL_TIMEOUT}s waiting for a terminal status (last: ${STATUS})."
    exit 1
  fi
  sleep 10
done
