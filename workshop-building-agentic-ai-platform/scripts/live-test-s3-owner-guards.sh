#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Exercise the S3 ownership boundary with real AWS API calls.
# Usage: ./scripts/live-test-s3-owner-guards.sh <expected-account-id> [region]

set -Eeuo pipefail

EXPECTED_ACCOUNT_ID="${1:?Usage: $0 <expected-account-id> [region]}"
TEST_REGION="${2:-us-west-2}"
FOREIGN_BUCKET="${FOREIGN_BUCKET:-aws-publicdatasets}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSHOP_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEPLOY_SCRIPT="${WORKSHOP_ROOT}/source/module-3a-mcp-registry/deploy-registry.sh"
CALLER_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

if [[ ! "${EXPECTED_ACCOUNT_ID}" =~ ^[0-9]{12}$ || "${CALLER_ACCOUNT}" != "${EXPECTED_ACCOUNT_ID}" ]]; then
  echo "ERROR: expected AWS account ${EXPECTED_ACCOUNT_ID}, got ${CALLER_ACCOUNT}." >&2
  exit 1
fi

DEFAULT_BUCKET="cfn-deploy-agentic-platform-${CALLER_ACCOUNT}-${TEST_REGION}-an"
ALT_REGION="us-east-1"
if [[ "${TEST_REGION}" == "us-east-1" ]]; then
  ALT_REGION="us-west-2"
fi
REGISTRY_TEMPLATES=(
  network-stack.yaml
  data-stack.yaml
  compute-stack.yaml
  services-stack.yaml
  workshop-tools-stack.yaml
  observability-stack.yaml
)
DEFAULT_BUCKET_CREATED=false

bucket_exists_owned() {
  local bucket="$1" region="$2"
  aws s3api head-bucket \
    --bucket "${bucket}" \
    --expected-bucket-owner "${CALLER_ACCOUNT}" \
    --region "${region}" >/dev/null 2>&1
}

empty_known_templates() {
  local bucket="$1" region="$2" prefix="$3" tmpl
  for tmpl in "${REGISTRY_TEMPLATES[@]}"; do
    aws s3api delete-object \
      --bucket "${bucket}" \
      --key "${prefix}${tmpl}" \
      --expected-bucket-owner "${CALLER_ACCOUNT}" \
      --region "${region}" >/dev/null 2>&1 || true
  done
}

cleanup() {
  set +e
  if [[ "${DEFAULT_BUCKET_CREATED}" == "true" ]] && \
      bucket_exists_owned "${DEFAULT_BUCKET}" "${TEST_REGION}"; then
    empty_known_templates "${DEFAULT_BUCKET}" "${TEST_REGION}" "cfn/registry/"
    aws s3api delete-bucket \
      --bucket "${DEFAULT_BUCKET}" \
      --expected-bucket-owner "${CALLER_ACCOUNT}" \
      --region "${TEST_REGION}" >/dev/null 2>&1
  fi
}
trap cleanup EXIT

if bucket_exists_owned "${DEFAULT_BUCKET}" "${TEST_REGION}"; then
  echo "ERROR: refusing to reuse or delete pre-existing test bucket ${DEFAULT_BUCKET}." >&2
  exit 1
fi

# The control must distinguish access from ownership. This public AWS-owned
# bucket is readable, but ExpectedBucketOwner must reject it for this account.
aws s3api head-bucket --bucket "${FOREIGN_BUCKET}" --region "${TEST_REGION}" >/dev/null
if foreign_output=$(AWS_DEFAULT_REGION="${TEST_REGION}" S3_BUCKET="${FOREIGN_BUCKET}" \
  bash -c "source \"\$1\"; upload_templates" _ "${DEPLOY_SCRIPT}" 2>&1); then
  echo "ERROR: foreign-owned bucket was accepted." >&2
  exit 1
fi
if [[ "${foreign_output}" != *"Refusing to upload trusted templates"* ||
      "${foreign_output}" == *"Uploaded network-stack.yaml"* ]]; then
  echo "ERROR: foreign-owner refusal did not fail before upload." >&2
  echo "${foreign_output}" >&2
  exit 1
fi
printf 'PASS foreign owner: denied before upload\n'

# Exercise the positive path: create the default account-regional bucket, apply
# controls, upload all six templates, verify them through real S3 reads, and run
# the product cleanup function. Mark it owned by this run before creation so the
# EXIT trap also cleans a partial create or upload failure.
DEFAULT_BUCKET_CREATED=true
env -u S3_BUCKET AWS_DEFAULT_REGION="${TEST_REGION}" \
  bash -c "source \"\$1\"; upload_templates" _ "${DEPLOY_SCRIPT}"

# Reuse the known-owned positive fixture to prove that selecting another Region
# is rejected before any upload. This avoids a second cross-Region bucket and a
# separate cleanup path.
if wrong_region_output=$(AWS_DEFAULT_REGION="${ALT_REGION}" S3_BUCKET="${DEFAULT_BUCKET}" \
  bash -c "source \"\$1\"; upload_templates" _ "${DEPLOY_SCRIPT}" 2>&1); then
  echo "ERROR: wrong-Region bucket was accepted." >&2
  exit 1
fi
if [[ "${wrong_region_output}" == *"Uploaded network-stack.yaml"* ]]; then
  echo "ERROR: wrong-Region refusal happened after an upload." >&2
  exit 1
fi
printf 'PASS wrong Region: denied before upload\n'

location=$(aws s3api get-bucket-location \
  --bucket "${DEFAULT_BUCKET}" \
  --expected-bucket-owner "${CALLER_ACCOUNT}" \
  --region "${TEST_REGION}" \
  --query LocationConstraint --output text)
if [[ "${TEST_REGION}" == "us-east-1" ]]; then
  [[ "${location}" == "None" || "${location}" == "null" ]]
else
  [[ "${location}" == "${TEST_REGION}" ]]
fi

object_count=$(aws s3api list-objects-v2 \
  --bucket "${DEFAULT_BUCKET}" \
  --prefix cfn/registry/ \
  --expected-bucket-owner "${CALLER_ACCOUNT}" \
  --region "${TEST_REGION}" \
  --query 'length(Contents)' --output text)
[[ "${object_count}" == "6" ]]

aws s3api get-public-access-block \
  --bucket "${DEFAULT_BUCKET}" \
  --expected-bucket-owner "${CALLER_ACCOUNT}" \
  --region "${TEST_REGION}" \
  --query 'PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets]' \
  --output text | grep -q $'True\tTrue\tTrue\tTrue'
aws s3api get-bucket-encryption \
  --bucket "${DEFAULT_BUCKET}" \
  --expected-bucket-owner "${CALLER_ACCOUNT}" \
  --region "${TEST_REGION}" \
  --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' \
  --output text | grep -q '^AES256$'
aws s3api get-bucket-ownership-controls \
  --bucket "${DEFAULT_BUCKET}" \
  --expected-bucket-owner "${CALLER_ACCOUNT}" \
  --region "${TEST_REGION}" \
  --query 'OwnershipControls.Rules[0].ObjectOwnership' \
  --output text | grep -q '^BucketOwnerEnforced$'

env -u S3_BUCKET AWS_DEFAULT_REGION="${TEST_REGION}" \
  bash -c "source \"\$1\"; cleanup_s3_templates" _ "${DEPLOY_SCRIPT}"
DEFAULT_BUCKET_CREATED=false
if bucket_exists_owned "${DEFAULT_BUCKET}" "${TEST_REGION}"; then
  echo "ERROR: product cleanup left the default bucket behind." >&2
  exit 1
fi

trap - EXIT
printf 'PASS positive staging: 6 objects and all bucket controls verified\n'
printf 'PASS cleanup: zero test buckets remain\n'
printf 'S3 owner guard live test: PASS\n'
