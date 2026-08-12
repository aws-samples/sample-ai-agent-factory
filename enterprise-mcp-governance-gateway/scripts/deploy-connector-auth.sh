#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# deploy-connector-auth.sh — publish the shared connector-authorize SPA.
#
# Reads the ConnectorAuthStack outputs + the gateway's SSM params, writes the
# SPA's runtime config.json (no values hardcoded), uploads the static site to its
# S3 bucket, and invalidates CloudFront. Run AFTER `cdk deploy ConnectorAuthStack`.
#
#   bash scripts/deploy-connector-auth.sh
#   # → prints the authorize URL, e.g. https://<id>.cloudfront.net/?provider=atlassian
set -euo pipefail
cd "$(dirname "$0")/.."
REGION="${AWS_REGION:-us-west-2}"
STACK="${STACK_NAME:-ConnectorAuthStack}"
SPA_DIR="connectors/authorize-spa"

out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null; }
ssm() { aws ssm get-parameter --region "$REGION" --name "$1" \
  --query Parameter.Value --output text 2>/dev/null; }

SITE_URL="$(out SiteUrl)"
BUCKET="$(out SiteBucketName)"
CLIENT_ID="$(out UserPoolClientId)"
IDENTITY_POOL="$(out IdentityPoolId)"
LOGIN_DOMAIN="$(out CognitoLoginDomain)"
GATEWAY_URL="$(ssm /enterprise-mcp-gateway/gateway/url)"
POOL_ID="$(ssm /enterprise-mcp-gateway/cognito/pool-id)"

[ -n "$SITE_URL" ] && [ "$SITE_URL" != "None" ] || {
  echo "ERROR: ConnectorAuthStack outputs not found; deploy it first (cdk deploy ConnectorAuthStack)." >&2; exit 1; }
[ -n "$GATEWAY_URL" ] && [ "$GATEWAY_URL" != "None" ] || {
  echo "ERROR: gateway SSM params not found; deploy the gateway stack first." >&2; exit 1; }

echo ">> Writing $SPA_DIR/config.json (gateway=$GATEWAY_URL)"
cat > "$SPA_DIR/config.json" <<JSON
{
  "region": "${REGION}",
  "gatewayUrl": "${GATEWAY_URL}",
  "cognitoLoginDomain": "${LOGIN_DOMAIN}",
  "userPoolId": "${POOL_ID}",
  "userPoolClientId": "${CLIENT_ID}",
  "identityPoolId": "${IDENTITY_POOL}",
  "redirectUri": "${SITE_URL}",
  "providers": {
    "atlassian": {
      "label": "Atlassian (Jira & Confluence)",
      "probeTool": "Atlassian___getVisibleJiraProjects"
    }
  }
}
JSON

echo ">> Uploading $SPA_DIR to s3://$BUCKET"
aws s3 sync "$SPA_DIR" "s3://$BUCKET" --delete --region "$REGION" --exclude ".*" --exclude "config.example.json"

DIST_ID="$(aws cloudfront list-distributions --region "$REGION" \
  --query "DistributionList.Items[?Comment=='Connector Authorize SPA (per-user 3LO consent)'].Id | [0]" \
  --output text 2>/dev/null || true)"
if [ -n "${DIST_ID:-}" ] && [ "$DIST_ID" != "None" ]; then
  echo ">> Invalidating CloudFront $DIST_ID"
  aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" >/dev/null
fi

echo ">> Done. Authorize a connector by visiting:"
echo "     ${SITE_URL}/?provider=atlassian"
