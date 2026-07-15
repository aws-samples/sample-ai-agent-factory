#!/usr/bin/env bash
# Idempotent: create the KB fixture S3 bucket + upload canary docs.
# The matrix KB specs reference s3://agentcore-e2e-kb-fixtures-<acct>/ ; this seeds it.
set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"
ACCT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="agentcore-e2e-kb-fixtures-${ACCT}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "bucket $BUCKET exists"
else
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi
  echo "created bucket $BUCKET"
fi
aws s3 cp "$HERE/fixtures/kb/zephyrine-protocol.md" "s3://$BUCKET/zephyrine-protocol.md" >/dev/null
echo "uploaded fixture doc -> s3://$BUCKET/zephyrine-protocol.md"
aws s3 ls "s3://$BUCKET/"
