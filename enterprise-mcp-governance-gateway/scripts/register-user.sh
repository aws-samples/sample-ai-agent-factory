#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# register-user.sh — Self-service: register YOUR OWN email as a Cognito user so you
# can mint a real JWT and test the gateway as yourself (not just the demo users).
#
# Creates the user in the gateway's Cognito pool with a permanent password (read
# from Secrets Manager, the same generated demo credential the seed script uses),
# sets the custom:role claim Cedar reads, then prints the exact command to mint a
# token. Idempotent: re-running re-asserts the role and resets the password.
#
# Usage:
#   bash scripts/register-user.sh you@example.com            # role defaults to 'analyst'
#   bash scripts/register-user.sh you@example.com admin      # pick a role
#   EMAIL=you@example.com ROLE=admin bash scripts/register-user.sh
#
# Roles seen by the Cedar policies: 'admin' (can write docs), 'analyst' (read-only),
# 'data-engineer' (bulk export). Any other string is allowed but maps to no extra
# permits, so it behaves like a least-privileged authenticated user.
#
# Env overrides (same as the other scripts):
#   AWS_REGION, SSM_PREFIX, COGNITO_USER_POOL_ID, DEMO_SECRET_ID, PASSWORD
#
# DEMO NOTE: the access token's custom:role lands in the ID token, not the access
# token the gateway validates — so role-gated WRITE permits won't fire for these
# users (writes stay denied, the safe default), exactly as documented for the demo
# users. Read/deny/redaction governance is fully exercised. For production, federate
# the gateway to your enterprise IdP instead of registering local users.
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
SSM_PREFIX="${SSM_PREFIX:-/enterprise-mcp-gateway}"

# --- resolve the email + role (positional args win, then env) ---
EMAIL="${1:-${EMAIL:-}}"
ROLE="${2:-${ROLE:-analyst}}"

if [ -z "${EMAIL}" ]; then
  echo "ERROR: no email given." >&2
  echo "Usage: bash scripts/register-user.sh you@example.com [role]" >&2
  exit 1
fi
# Basic shape check — must look like an email (the pool uses email as the alias).
if ! printf '%s' "$EMAIL" | grep -qE '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'; then
  echo "ERROR: '${EMAIL}' does not look like a valid email address." >&2
  exit 1
fi

command -v aws >/dev/null 2>&1 || { echo "ERROR: aws CLI not found on PATH." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found on PATH." >&2; exit 1; }

ssm_get() {
  aws ssm get-parameter --region "$REGION" --name "$1" \
    --query 'Parameter.Value' --output text 2>/dev/null || true
}

POOL_ID="${COGNITO_USER_POOL_ID:-$(ssm_get "${SSM_PREFIX}/cognito/pool-id")}"
SECRET_ID="${DEMO_SECRET_ID:-$(ssm_get "${SSM_PREFIX}/cognito/demo-secret-arn")}"
SECRET_ID="${SECRET_ID:-enterprise-mcp-gateway/demo-user}"

if [ -z "${POOL_ID:-}" ] || [ "${POOL_ID}" = "None" ]; then
  echo "ERROR: could not resolve the Cognito user pool id." >&2
  echo "       Deploy the CDK stack first (it writes ${SSM_PREFIX}/cognito/pool-id)," >&2
  echo "       or set COGNITO_USER_POOL_ID." >&2
  exit 1
fi

# Password: env override, else the generated demo credential in Secrets Manager.
PASSWORD="${PASSWORD:-}"
if [ -z "$PASSWORD" ]; then
  PASSWORD="$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ID" \
    --query 'SecretString' --output text 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["password"])' 2>/dev/null || true)"
fi
if [ -z "${PASSWORD:-}" ]; then
  echo "ERROR: could not read the password from Secrets Manager (${SECRET_ID})." >&2
  echo "       Provide one with: PASSWORD='...' bash scripts/register-user.sh ${EMAIL}" >&2
  exit 1
fi

echo ">> Registering ${EMAIL} (role=${ROLE}) in pool ${POOL_ID} (region ${REGION})..."

# Create (idempotent): swallow UsernameExistsException, then always re-assert role.
if aws cognito-idp admin-create-user \
      --region "$REGION" --user-pool-id "$POOL_ID" \
      --username "$EMAIL" --message-action SUPPRESS \
      --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
                        "Name=custom:role,Value=${ROLE}" \
      >/dev/null 2>&1; then
  echo "   created ${EMAIL}"
else
  echo "   exists  ${EMAIL} — re-asserting role=${ROLE}"
  aws cognito-idp admin-update-user-attributes \
    --region "$REGION" --user-pool-id "$POOL_ID" --username "$EMAIL" \
    --user-attributes "Name=custom:role,Value=${ROLE}" >/dev/null
fi

# Permanent password so admin_initiate_auth (in get-token.sh) works non-interactively.
aws cognito-idp admin-set-user-password \
  --region "$REGION" --user-pool-id "$POOL_ID" --username "$EMAIL" \
  --password "$PASSWORD" --permanent >/dev/null

echo ">> Done. Mint a token as yourself with:"
echo "     COGNITO_USERNAME='${EMAIL}' source scripts/get-token.sh"
