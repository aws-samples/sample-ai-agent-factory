#!/usr/bin/env bash
# Deploy script for the AgentCore Visual Workflow Platform (Serverless).
#
# Orchestrates the full deployment:
#   1. Validate prerequisites (Node.js, Python, AWS CLI, CDK)
#   2. Validate AWS credentials
#   3. Install CDK dependencies
#   4. Install backend dependencies
#   5. Bootstrap CDK (if needed)
#   6. Run cdk deploy (creates API Gateway, Lambda, Step Functions, etc.)
#   7. Extract stack outputs (API Gateway URL, CloudFront URL, S3 bucket)
#   8. Build frontend with VITE_API_BASE_URL set to CloudFront URL
#   9. Upload frontend build artifacts to S3
#  10. Invalidate CloudFront cache
#  11. Print output URLs
#
# No Docker required — Lambda code is packaged by CDK from the backend directory.
#
# Requirements: 8.1, 8.3, 8.4

set -euo pipefail

# ── Configuration (override via environment variables) ────────────────
ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_NAME="${PROJECT_NAME:-agentcore-workflow}"
COGNITO_USERS="${COGNITO_USERS:-}"
STACK_NAME="${PROJECT_NAME}-${ENVIRONMENT_NAME}"

# Resolve project root relative to this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Helper functions ──────────────────────────────────────────────────

log_info() {
  echo -e "\n\033[1;34m[INFO]\033[0m $*"
}

log_success() {
  echo -e "\n\033[1;32m[SUCCESS]\033[0m $*"
}

log_error() {
  echo -e "\n\033[1;31m[ERROR]\033[0m $*" >&2
}

get_stack_output() {
  local output_key="$1"
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='${output_key}'].OutputValue" \
    --output text
}

# ── Step 1: Check prerequisites ──────────────────────────────────────

check_prerequisites() {
  log_info "Checking prerequisites..."

  local missing=0

  # Check Node.js
  if ! command -v node &> /dev/null; then
    log_error "Node.js is not installed. Please install Node.js (v18+) and try again."
    missing=1
  else
    log_success "Node.js $(node --version) is available."
  fi

  # Check npm
  if ! command -v npm &> /dev/null; then
    log_error "npm is not installed. Please install Node.js/npm and try again."
    missing=1
  else
    log_success "npm $(npm --version) is available."
  fi

  # Check Python 3
  if ! command -v python3 &> /dev/null; then
    log_error "Python 3 is not installed. Please install Python 3.12+ and try again."
    missing=1
  else
    log_success "Python $(python3 --version 2>&1) is available."
  fi

  # Check AWS CLI
  if ! command -v aws &> /dev/null; then
    log_error "AWS CLI is not installed. Please install the AWS CLI v2 and try again."
    log_error "See: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    missing=1
  else
    log_success "AWS CLI $(aws --version 2>&1 | head -1) is available."
  fi

  # Check CDK CLI
  if ! command -v npx &> /dev/null; then
    log_error "npx is not available. Please install Node.js/npm and try again."
    missing=1
  else
    log_success "npx is available (CDK will be invoked via npx)."
  fi

  if [[ "${missing}" -ne 0 ]]; then
    log_error "One or more prerequisites are missing. Please install them and retry."
    exit 1
  fi

  log_success "All prerequisites satisfied."
}

# ── Step 2: Validate AWS credentials ─────────────────────────────────

check_aws_credentials() {
  log_info "Checking AWS credentials..."
  # The stack creates a CLOUDFRONT-scoped WAFv2 WebACL, which AWS only
  # accepts in us-east-1. Deploying elsewhere also collides with the
  # account-global CloudFront OAC / response-headers-policy / IAM role
  # names of an existing deployment. Fail fast (AWS_REGION env vars from
  # the calling shell silently override the us-east-1 default).
  if [[ "${AWS_REGION}" != "us-east-1" ]]; then
    log_error "AWS_REGION is '${AWS_REGION}', but this stack requires us-east-1"
    log_error "(CLOUDFRONT-scoped WAF WebACL). Unset AWS_REGION or set AWS_REGION=us-east-1."
    exit 1
  fi
  if ! aws sts get-caller-identity --region "${AWS_REGION}" > /dev/null 2>&1; then
    log_error "AWS credentials are not configured or are invalid."
    log_error "Please configure credentials with 'aws configure' or set AWS_PROFILE."
    exit 1
  fi
  local account_id
  account_id=$(aws sts get-caller-identity --region "${AWS_REGION}" --query "Account" --output text)
  log_success "Authenticated to AWS account: ${account_id}"
  log_info "Deployment target: stack '${STACK_NAME}' in region '${AWS_REGION}'"
  log_info "(Override with AWS_REGION=... or ENVIRONMENT_NAME=... before invoking this script.)"
}

# ── Step 3: Install CDK dependencies ─────────────────────────────────

install_cdk_dependencies() {
  log_info "Installing CDK dependencies..."
  cd "${PROJECT_ROOT}/infra"

  # Install Python CDK dependencies
  if command -v pip3 &> /dev/null; then
    pip3 install -r requirements.txt --quiet
  elif command -v pip &> /dev/null; then
    pip install -r requirements.txt --quiet
  else
    python3 -m pip install -r requirements.txt --quiet
  fi

  # Install Node.js CDK dependencies (for npx cdk)
  npm install --silent 2>/dev/null || true

  cd "${PROJECT_ROOT}"
  log_success "CDK dependencies installed."
}

# ── Step 4: Install backend dependencies ──────────────────────────────

install_backend_dependencies() {
  log_info "Installing backend dependencies..."
  cd "${PROJECT_ROOT}/backend"

  if command -v pip3 &> /dev/null; then
    pip3 install . --quiet
  elif command -v pip &> /dev/null; then
    pip install . --quiet
  else
    python3 -m pip install . --quiet
  fi

  cd "${PROJECT_ROOT}"
  log_success "Backend dependencies installed."
}

# ── Step 4b: Install Lambda dependencies (platform-targeted) ─────────

install_lambda_dependencies() {
  log_info "Installing Lambda dependencies into backend/lib/ (targeting Linux x86_64)..."
  "${SCRIPT_DIR}/install-lambda-deps.sh"
  log_success "Lambda dependencies installed."
}

# ── Step 4c: Install AgentCore dependency bundles (aarch64-targeted) ──

install_agentcore_deps() {
  log_info "Installing AgentCore dependency bundles..."
  bash "${SCRIPT_DIR}/install-agentcore-deps.sh"
  log_success "AgentCore dependency bundles installed."
}

# ── Step 5: Bootstrap CDK (if needed) ────────────────────────────────

bootstrap_cdk() {
  log_info "Checking if CDK bootstrap is needed in ${AWS_REGION}..."
  local account_id
  account_id=$(aws sts get-caller-identity --region "${AWS_REGION}" --query "Account" --output text)

  # Check if bootstrap stack exists
  if ! aws cloudformation describe-stacks \
    --stack-name CDKToolkit \
    --region "${AWS_REGION}" > /dev/null 2>&1; then
    log_info "Bootstrapping CDK in ${AWS_REGION} for account ${account_id}..."
    cd "${PROJECT_ROOT}/infra"
    npx cdk bootstrap "aws://${account_id}/${AWS_REGION}"
    cd "${PROJECT_ROOT}"
    log_success "CDK bootstrap complete."
  else
    log_success "CDK already bootstrapped in ${AWS_REGION}."
  fi
}

# ── Step 5b: Preflight — heal tables deleted out-of-band ─────────────
# If a DynamoDB table in the deployed stack was deleted outside CloudFormation
# (e.g. by an account-level resource reaper), CFN still believes it exists and
# the next update fails with "Unable to retrieve Arn attribute ... Table X
# does not exist". Recreate any such tables empty (exact deployed schema)
# before deploying. No-op on fresh deploys and healthy stacks.

preflight_restore_tables() {
  log_info "Preflight: verifying stack DynamoDB tables exist..."
  python3 "${SCRIPT_DIR}/preflight-ddb-restore.py" \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}"
  log_success "DynamoDB preflight complete."
}

# ── Step 6: Run CDK deploy ────────────────────────────────────────────

run_cdk_deploy() {
  log_info "Deploying CDK stack '${STACK_NAME}' to region '${AWS_REGION}'..."
  log_info "This creates API Gateway, Lambda functions, Step Functions, DynamoDB tables, S3, and CloudFront."
  log_info "Lambda code is packaged automatically by CDK from the backend directory."
  cd "${PROJECT_ROOT}/infra"
  # Optional platform OTEL defaults — feature is enabled iff both
  # OTEL_ENDPOINT and OTEL_AUTH_SECRET_ARN are set. Run scripts/bootstrap-otel-secret.sh
  # first to obtain a secret ARN.
  npx cdk deploy "${STACK_NAME}" \
    --require-approval never \
    -c environment_name="${ENVIRONMENT_NAME}" \
    -c aws_region="${AWS_REGION}" \
    -c project_name="${PROJECT_NAME}" \
    -c cognito_users="${COGNITO_USERS}" \
    -c otel_endpoint="${OTEL_ENDPOINT:-}" \
    -c otel_auth_secret_arn="${OTEL_AUTH_SECRET_ARN:-}" \
    -c otel_sample_rate="${OTEL_SAMPLE_RATE:-1.0}" \
    -c otel_service_name_prefix="${OTEL_SERVICE_NAME_PREFIX:-}"
  cd "${PROJECT_ROOT}"
  log_success "CDK stack deployed."
}

# ── Step 7: Extract stack outputs ─────────────────────────────────────

extract_stack_outputs() {
  log_info "Extracting stack outputs..."
  API_GATEWAY_URL=$(get_stack_output "ApiGatewayUrl")
  CLOUDFRONT_URL=$(get_stack_output "CloudFrontUrl")
  S3_BUCKET_NAME=$(get_stack_output "S3BucketName")
  USER_POOL_ID=$(get_stack_output "UserPoolId")
  USER_POOL_CLIENT_ID=$(get_stack_output "UserPoolClientId")

  # Look up CloudFront distribution ID for cache invalidation
  DISTRIBUTION_ID=$(aws cloudfront list-distributions \
    --region "${AWS_REGION}" \
    --query "DistributionList.Items[?Comment=='${PROJECT_NAME}-${ENVIRONMENT_NAME} distribution'].Id" \
    --output text 2>/dev/null || echo "")

  if [[ -z "${CLOUDFRONT_URL}" || -z "${S3_BUCKET_NAME}" ]]; then
    log_error "Failed to extract one or more stack outputs."
    log_error "CloudFront URL: ${CLOUDFRONT_URL:-<empty>}"
    log_error "S3 Bucket:      ${S3_BUCKET_NAME:-<empty>}"
    exit 1
  fi

  log_success "API Gateway URL: ${API_GATEWAY_URL}"
  log_success "CloudFront URL:  ${CLOUDFRONT_URL}"
  log_success "S3 Bucket:       ${S3_BUCKET_NAME}"
}

# ── Step 8: Build frontend ────────────────────────────────────────────

build_frontend() {
  # Use CloudFront URL as the API base — CloudFront routes /api/* to API Gateway
  log_info "Building frontend with VITE_API_BASE_URL=${CLOUDFRONT_URL} ..."
  cd "${PROJECT_ROOT}/frontend"
  # npm ci requires package-lock.json, which is gitignored in this repo —
  # fall back to npm install on a fresh clone.
  if [[ -f package-lock.json ]]; then
    npm ci --silent
  else
    npm install --silent --no-audit --no-fund
  fi
  VITE_API_BASE_URL="${CLOUDFRONT_URL}" VITE_AWS_REGION="${AWS_REGION}" VITE_COGNITO_USER_POOL_ID="${USER_POOL_ID}" VITE_COGNITO_CLIENT_ID="${USER_POOL_CLIENT_ID}" npm run build
  cd "${PROJECT_ROOT}"
  log_success "Frontend build complete."
}

# ── Step 9: Upload frontend to S3 ────────────────────────────────────

upload_frontend_to_s3() {
  log_info "Uploading frontend build to s3://${S3_BUCKET_NAME} ..."
  aws s3 sync "${PROJECT_ROOT}/frontend/dist" "s3://${S3_BUCKET_NAME}" \
    --region "${AWS_REGION}" \
    --delete
  log_success "Frontend uploaded to S3."
}

# ── Step 10: Invalidate CloudFront cache ──────────────────────────────

invalidate_cloudfront_cache() {
  if [[ -z "${DISTRIBUTION_ID}" || "${DISTRIBUTION_ID}" == "None" ]]; then
    log_info "Skipping CloudFront invalidation — distribution ID not found."
    return
  fi
  log_info "Invalidating CloudFront cache for distribution ${DISTRIBUTION_ID} ..."
  aws cloudfront create-invalidation \
    --distribution-id "${DISTRIBUTION_ID}" \
    --paths "/*" \
    --region "${AWS_REGION}" \
    > /dev/null
  log_success "CloudFront cache invalidation started."
}

# ── Step 11: Print summary ───────────────────────────────────────────

print_summary() {
  echo ""
  echo "=============================================="
  echo "  Deployment Complete! (Serverless)"
  echo "=============================================="
  echo ""
  echo "  Frontend (CloudFront): ${CLOUDFRONT_URL}"
  echo "  API      (Gateway):    ${API_GATEWAY_URL}"
  echo ""
  echo "  Stack:   ${STACK_NAME}"
  echo "  Region:  ${AWS_REGION}"
  echo ""
  echo "  Architecture: API Gateway + Lambda + Step Functions"
  echo "  No Docker, no ECS, no ALB, no VPC/NAT required."
  echo ""
  echo "=============================================="
}

# ── Main ──────────────────────────────────────────────────────────────

main() {
  log_info "Starting serverless deployment of ${PROJECT_NAME} (${ENVIRONMENT_NAME}) to ${AWS_REGION}"

  check_prerequisites
  check_aws_credentials
  install_cdk_dependencies
  install_backend_dependencies
  install_lambda_dependencies
  install_agentcore_deps
  bootstrap_cdk
  preflight_restore_tables
  run_cdk_deploy
  extract_stack_outputs
  build_frontend
  upload_frontend_to_s3
  invalidate_cloudfront_cache
  print_summary
}

main "$@"
