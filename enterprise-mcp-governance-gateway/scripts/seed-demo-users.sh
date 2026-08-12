#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# seed-demo-users.sh — Post-deploy seeding of the demo Cognito users.
#
# The CDK stack provisions the user pool, an admin-auth app client, and a
# Secrets Manager secret holding a generated demo password — but CloudFormation
# cannot create a Cognito user WITH a usable (permanent) password, because
# AdminSetUserPassword is an API-only operation. This script closes that gap,
# run once by the deployer after `cdk deploy`: it creates the demo users and sets
# their permanent password from Secrets Manager.
#
# DEMO ONLY. These are throwaway identities on the RFC 2606 reserved example.com
# domain, used solely to mint user JWTs for the governance tests. For production,
# federate the gateway to your own IdP and delete these users.
#
# Uses ONLY the AWS CLI (no third-party dependency). Idempotent: re-running
# re-asserts the role attribute and re-sets the password.
#
# Resolution (env overrides win):
#   AWS_REGION             region for all calls (default us-west-2)
#   SSM_PREFIX             SSM path prefix (default /enterprise-mcp-gateway)
#   COGNITO_USER_POOL_ID   override the pool id (else read from SSM)
#   DEMO_SECRET_ID         override the secret id/arn (else read from SSM)
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
SSM_PREFIX="${SSM_PREFIX:-/enterprise-mcp-gateway}"

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

# Generated demo password lives in Secrets Manager (JSON: {\"password\": \"...\"}).
PASSWORD="$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ID" \
  --query 'SecretString' --output text 2>/dev/null \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["password"])' 2>/dev/null || true)"
if [ -z "${PASSWORD:-}" ]; then
  echo "ERROR: could not read the demo password from Secrets Manager (${SECRET_ID})." >&2
  exit 1
fi

# Demo users: "email:role". admin + security-admin map to role 'admin'; analyst to
# 'analyst'. The role becomes the custom:role claim consumed by Cedar.
USERS=(
  "admin@example.com:admin"
  "analyst@example.com:analyst"
  "security-admin@example.com:admin"
)

echo ">> Seeding ${#USERS[@]} demo users into pool ${POOL_ID} (region ${REGION})..."
for entry in "${USERS[@]}"; do
  username="${entry%%:*}"
  role="${entry##*:}"

  # Create (idempotent): swallow UsernameExistsException, then always re-assert.
  if aws cognito-idp admin-create-user \
        --region "$REGION" --user-pool-id "$POOL_ID" \
        --username "$username" --message-action SUPPRESS \
        --user-attributes Name=email,Value="$username" Name=email_verified,Value=true \
                          "Name=custom:role,Value=${role}" \
        >/dev/null 2>&1; then
    echo "   created ${username} (role=${role})"
  else
    echo "   exists  ${username} — re-asserting role=${role}"
    aws cognito-idp admin-update-user-attributes \
      --region "$REGION" --user-pool-id "$POOL_ID" --username "$username" \
      --user-attributes "Name=custom:role,Value=${role}" >/dev/null
  fi

  # Set the permanent password so admin_initiate_auth works non-interactively.
  aws cognito-idp admin-set-user-password \
    --region "$REGION" --user-pool-id "$POOL_ID" --username "$username" \
    --password "$PASSWORD" --permanent >/dev/null
done

echo ">> Done. Mint a token with:  source scripts/get-token.sh"
echo "   (default user admin@example.com; override with COGNITO_USERNAME=security-admin@example.com)"
