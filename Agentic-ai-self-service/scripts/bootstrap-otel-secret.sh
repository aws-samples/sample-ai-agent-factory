#!/usr/bin/env bash
# Bootstrap the platform-default OTLP auth secret.
#
# Run ONCE before the first deploy that enables platform OTEL. Takes Langfuse
# (or any OTLP) credentials from env vars, computes the auth header server-side,
# stores it in AWS Secrets Manager, and prints the ARN. Pass that ARN to
# deploy.sh as OTEL_AUTH_SECRET_ARN.
#
# Why a separate script (not deploy.sh): the secret is admin-managed and
# should outlive any individual stack. Putting it in CDK would tie its
# lifecycle to cdk destroy.
#
# Usage (Langfuse):
#   LANGFUSE_PUBLIC_KEY="pk-lf-..." \
#     LANGFUSE_SECRET_KEY="sk-lf-..." \
#     ./scripts/bootstrap-otel-secret.sh
#
# Usage (custom OTLP backend):
#   OTEL_HEADER_VALUE="Authorization=Bearer xyz" ./scripts/bootstrap-otel-secret.sh
#
# Env vars:
#   LANGFUSE_PUBLIC_KEY  Langfuse pk-lf-... (mutually exclusive with OTEL_HEADER_VALUE)
#   LANGFUSE_SECRET_KEY  Langfuse sk-lf-...
#   OTEL_HEADER_VALUE    Raw "Header-Name=Value" string for any other OTLP backend
#   AWS_REGION           Defaults to us-east-1
#   ENVIRONMENT_NAME     Defaults to dev (used in secret name)

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-dev}"

log_info()  { echo -e "\033[1;34m[INFO]\033[0m $*" >&2; }
log_error() { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; }

# Build the auth header value
if [[ -n "${LANGFUSE_PUBLIC_KEY:-}" && -n "${LANGFUSE_SECRET_KEY:-}" ]]; then
  log_info "Building Langfuse Basic-auth header"
  TOKEN=$(printf '%s:%s' "${LANGFUSE_PUBLIC_KEY}" "${LANGFUSE_SECRET_KEY}" | base64 | tr -d '\n')
  HEADER_VALUE="Authorization=Basic ${TOKEN}"
  PROVIDER="langfuse"
elif [[ -n "${OTEL_HEADER_VALUE:-}" ]]; then
  log_info "Using raw OTEL_HEADER_VALUE"
  HEADER_VALUE="${OTEL_HEADER_VALUE}"
  PROVIDER="custom"
else
  log_error "Provide either LANGFUSE_PUBLIC_KEY+LANGFUSE_SECRET_KEY or OTEL_HEADER_VALUE."
  exit 2
fi

# Validate AWS credentials
if ! aws sts get-caller-identity --region "${AWS_REGION}" >/dev/null 2>&1; then
  log_error "AWS credentials not configured. Run 'aws configure' or set AWS_PROFILE."
  exit 2
fi

# Generate a unique secret name; reuse if a stable one exists for this env.
SECRET_NAME="agentcore-otel/platform/${ENVIRONMENT_NAME}"

# If the secret already exists, update; otherwise create.
EXISTING_ARN=$(aws secretsmanager describe-secret \
  --secret-id "${SECRET_NAME}" \
  --region "${AWS_REGION}" \
  --query 'ARN' --output text 2>/dev/null || true)

if [[ -n "${EXISTING_ARN}" && "${EXISTING_ARN}" != "None" ]]; then
  log_info "Secret '${SECRET_NAME}' already exists; rotating value"
  aws secretsmanager put-secret-value \
    --secret-id "${EXISTING_ARN}" \
    --secret-string "${HEADER_VALUE}" \
    --region "${AWS_REGION}" >/dev/null
  ARN="${EXISTING_ARN}"
else
  log_info "Creating secret '${SECRET_NAME}'"
  ARN=$(aws secretsmanager create-secret \
    --name "${SECRET_NAME}" \
    --description "Platform-default OTLP auth header (agentcore-flows, ${ENVIRONMENT_NAME})" \
    --secret-string "${HEADER_VALUE}" \
    --tags "Key=ManagedBy,Value=agentcore-flows" "Key=Purpose,Value=platform-otel-auth" "Key=Provider,Value=${PROVIDER}" "Key=Environment,Value=${ENVIRONMENT_NAME}" \
    --region "${AWS_REGION}" \
    --query 'ARN' --output text)
fi

# Print the ARN to stdout for capture by callers
echo "${ARN}"

log_info "Done. Pass this ARN to deploy.sh as OTEL_AUTH_SECRET_ARN."
