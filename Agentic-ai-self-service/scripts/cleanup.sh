#!/usr/bin/env bash
# Cleanup script for the AgentCore Visual Workflow Platform (Serverless).
#
# Tears down all AWS resources — both CDK-managed and dynamically created:
#   1. Check prerequisites (AWS CLI)
#   2. Validate AWS credentials
#   3. Check if the stack exists
#   4. Confirm the exact regional stack identity
#   5. Install and verify the repository-pinned CDK toolchain
#   6. Clean up dynamically-created deployment resources (runtimes, gateways, etc.)
#   7. Sweep for orphaned AgentCore-* resources
#   8. Run cdk destroy --force for CloudFormation-owned resources
#   9. Verify the stack is absent
#  10. Delete the exact RETAINed gateway-auth pool + hosted domain
#
# No Docker, ECS, ECR, ALB, or VPC resources to clean up — fully serverless.
#
# Requirements: 8.2

set -euo pipefail

# ── Configuration (override via environment variables) ────────────────
ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_NAME="${PROJECT_NAME:-agentcore-workflow}"
STACK_NAME="${PROJECT_NAME}-${ENVIRONMENT_NAME}"
STACK_EXISTS=false
AWS_ACCOUNT_ID=""
PROJECT_PYTHON=""
STACK_ID=""

# The shared gateway-auth pool and hosted domain deliberately use RETAIN so an
# unrelated stack update or rollback cannot revoke every deployed gateway's
# client credentials.  An intentional, confirmed cleanup still has to remove
# them.  Capture their exact CloudFormation outputs before the stack disappears;
# never rediscover them with the broad AgentCore* pool sweep below.
RETAINED_GATEWAY_AUTH_POOL_ID=""
RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX=""
RETAINED_GATEWAY_AUTH_VALIDATED=false

# Recovery-only inputs for a retry after CloudFormation has already deleted the
# stack.  Both are required together and are accepted only after the referenced
# pool is re-read and its exact Project/Environment/stack-name tags match.
CLEANUP_GATEWAY_AUTH_POOL_ID="${CLEANUP_GATEWAY_AUTH_POOL_ID:-}"
CLEANUP_GATEWAY_AUTH_DOMAIN_PREFIX="${CLEANUP_GATEWAY_AUTH_DOMAIN_PREFIX:-}"

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

log_warn() {
  echo -e "\n\033[1;33m[WARN]\033[0m $*"
}

# ── Step 1: Check prerequisites ──────────────────────────────────────

check_prerequisites() {
  log_info "Checking prerequisites..."

  local missing=0

  # Check AWS CLI
  if ! command -v aws &> /dev/null; then
    log_error "AWS CLI is not installed. Please install the AWS CLI v2 and try again."
    log_error "See: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    missing=1
  else
    log_success "AWS CLI $(aws --version 2>&1 | head -1) is available."
  fi

  # Select one interpreter for both strict JSON parsing and the CDK app.
  local requested_python="${CDK_PYTHON:-python3}"
  if ! PROJECT_PYTHON="$(command -v "${requested_python}")"; then
    log_error "Python '${requested_python}' is not installed. Required for safe AWS response parsing."
    missing=1
  else
    export CDK_PYTHON="${PROJECT_PYTHON}"
    log_success "Python $("${PROJECT_PYTHON}" --version 2>&1) is available at ${PROJECT_PYTHON}."
  fi

  if [[ "${missing}" -ne 0 ]]; then
    log_error "One or more prerequisites are missing. Please install them and retry."
    exit 1
  fi

  log_success "All prerequisites satisfied."
}

# ── CDK toolchain installation and verification ──────────────────────
# Never depend on deploy.sh having run on this checkout. A cleanup may be the
# first command run from a fresh clone, or node_modules may have been deleted
# since deployment. Install from the committed lockfile and verify the exact
# CLI pin before any destructive operation. Tests may override CDK_BIN with a
# stub, but the stub must still report the committed version.
CDK_BIN="${CDK_BIN:-${PROJECT_ROOT}/infra/node_modules/.bin/cdk}"

install_cdk_dependencies_for_cleanup() {
  log_info "Installing the repository-pinned CDK toolchain for cleanup..."

  local infra_dir="${PROJECT_ROOT}/infra"
  local package_json="${infra_dir}/package.json"
  local package_lock="${infra_dir}/package-lock.json"
  local requirements="${infra_dir}/requirements.txt"
  local pinned installed node_major

  for required_file in "${package_json}" "${package_lock}" "${requirements}"; do
    if [[ ! -f "${required_file}" ]]; then
      log_error "Required cleanup toolchain file is missing: ${required_file}"
      log_error "Refusing to delete anything without the repository-pinned CDK toolchain."
      return 1
    fi
  done

  if ! command -v node > /dev/null 2>&1; then
    log_error "Node.js is not installed. Node.js 18 or newer is required for the pinned CDK CLI."
    return 1
  fi
  if ! command -v npm > /dev/null 2>&1; then
    log_error "npm is not installed. It is required to install the pinned CDK CLI from package-lock.json."
    return 1
  fi
  node_major=$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || true)
  if [[ ! "${node_major}" =~ ^[0-9]+$ || "${node_major}" -lt 18 ]]; then
    log_error "Node.js 18 or newer is required; found '$(node --version 2>/dev/null || echo unknown)'."
    return 1
  fi

  if ! pinned=$("${PROJECT_PYTHON}" - "${package_json}" "${package_lock}" <<'PY'
import json
import re
import sys

package_path, lock_path = sys.argv[1:]
with open(package_path, encoding="utf-8") as handle:
    package = json.load(handle)
with open(lock_path, encoding="utf-8") as handle:
    lock = json.load(handle)

pin = package.get("devDependencies", {}).get("aws-cdk")
if not isinstance(pin, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pin) is None:
    raise SystemExit("infra/package.json must contain an exact numeric aws-cdk version")

root_pin = lock.get("packages", {}).get("", {}).get("devDependencies", {}).get("aws-cdk")
resolved_pin = lock.get("packages", {}).get("node_modules/aws-cdk", {}).get("version")
if root_pin != pin or resolved_pin != pin:
    raise SystemExit(
        "infra/package-lock.json does not resolve the exact aws-cdk version from package.json"
    )
print(pin)
PY
  ); then
    log_error "The committed CDK package metadata is invalid or inconsistent."
    log_error "Refusing cleanup before npm or AWS deletion commands run."
    return 1
  fi

  # cdk destroy executes the Python CDK app as well as the Node CLI, so a fresh
  # checkout needs both dependency sets. The package files are committed inputs;
  # npm ci refuses a stale lock and --ignore-scripts prevents lifecycle scripts.
  # an interpreter built by uv carries no pip: install through uv against that interpreter instead
  if "${PROJECT_PYTHON}" -m pip --version >/dev/null 2>&1; then
    installer=("${PROJECT_PYTHON}" -m pip install -r "${requirements}" --quiet)
  elif command -v uv >/dev/null 2>&1; then
    installer=(uv pip install --python "${PROJECT_PYTHON}" -r "${requirements}" --quiet)
  else
    log_error "Neither pip (in ${PROJECT_PYTHON}) nor uv is available to install ${requirements}."
    return 1
  fi
  if ! "${installer[@]}"; then
    log_error "Could not install the Python CDK dependencies from ${requirements}."
    return 1
  fi
  if ! (
    cd "${infra_dir}"
    npm ci --ignore-scripts --no-audit --no-fund --loglevel=error
  ); then
    log_error "Could not install the pinned CDK CLI from ${package_lock}."
    return 1
  fi

  if [[ ! -x "${CDK_BIN}" ]]; then
    log_error "Pinned CDK CLI was not installed at ${CDK_BIN}."
    return 1
  fi
  if ! installed=$("${CDK_BIN}" --version 2>/dev/null | awk 'NR == 1 { print $1 }'); then
    log_error "Could not execute the pinned CDK CLI at ${CDK_BIN}."
    return 1
  fi
  if [[ -z "${installed}" || "${installed}" != "${pinned}" ]]; then
    log_error "CDK CLI at ${CDK_BIN} is '${installed:-missing}', but the repository pins ${pinned}."
    log_error "Refusing cleanup rather than destroying with an unreviewed toolkit version."
    return 1
  fi

  log_success "Cleanup toolchain ready (aws-cdk CLI ${installed}, pinned by infra/package-lock.json)."
}

# ── Step 2: Validate AWS credentials ─────────────────────────────────

check_aws_credentials() {
  log_info "Checking AWS credentials..."
  if ! aws sts get-caller-identity --region "${AWS_REGION}" > /dev/null 2>&1; then
    log_error "AWS credentials are not configured or are invalid."
    log_error "Please configure credentials with 'aws configure' or set AWS_PROFILE."
    exit 1
  fi
  AWS_ACCOUNT_ID=$(aws sts get-caller-identity \
    --region "${AWS_REGION}" \
    --query "Account" \
    --output text)
  log_success "Authenticated to AWS account: ${AWS_ACCOUNT_ID}"
}

# ── Step 3: Check stack exists ────────────────────────────────────────

check_stack_exists() {
  log_info "Checking if stack '${STACK_NAME}' exists in region '${AWS_REGION}'..."
  local describe_result
  if describe_result=$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}" \
    --output json 2>&1); then
    STACK_EXISTS=true
    capture_retained_gateway_auth_outputs "${describe_result}"
    log_success "Stack '${STACK_NAME}' found."
    return
  fi

  if [[ "${describe_result}" == *"does not exist"* ]]; then
    STACK_EXISTS=false
    log_warn "Stack '${STACK_NAME}' has already been deleted."
    log_warn "Only the exact-owner orphan sweep will be available after confirmation."
    return
  fi

  log_error "Could not verify whether stack '${STACK_NAME}' exists."
  log_error "Refusing cleanup because an authorization/network failure is not proof of absence."
  return 1
}

capture_retained_gateway_auth_outputs() {
  local stack_json="$1"
  local parsed pool_id domain_prefix
  if ! parsed=$(printf '%s' "${stack_json}" | python3 -c '
import json
import sys

stacks = json.load(sys.stdin).get("Stacks") or []
if len(stacks) != 1:
    raise ValueError("expected exactly one described stack")
stack = stacks[0]
outputs = {
    item.get("OutputKey"): item.get("OutputValue", "")
    for item in stack.get("Outputs") or []
    if isinstance(item, dict)
}
print(stack.get("StackId", ""))
print(outputs.get("GatewayAuthUserPoolId", ""))
print(outputs.get("GatewayAuthDomainPrefix", ""))
'); then
    log_error "Could not parse the exact stack outputs for retained gateway-auth cleanup."
    return 1
  fi

  STACK_ID=$(printf '%s\n' "${parsed}" | sed -n '1p')
  pool_id=$(printf '%s\n' "${parsed}" | sed -n '2p')
  domain_prefix=$(printf '%s\n' "${parsed}" | sed -n '3p')

  if [[ -z "${STACK_ID}" ]]; then
    log_error "The stack description did not contain StackId; refusing destructive cleanup."
    return 1
  fi
  if [[ -z "${pool_id}" && -z "${domain_prefix}" ]]; then
    # Legacy platform stacks predate the shared gateway-auth resources.
    return
  fi
  if [[ -z "${pool_id}" || -z "${domain_prefix}" ]]; then
    log_error "The stack exposes only one of GatewayAuthUserPoolId/GatewayAuthDomainPrefix."
    log_error "Refusing cleanup because a partial retained-resource identity is unsafe."
    return 1
  fi
  set_retained_gateway_auth_identity "${pool_id}" "${domain_prefix}"
}

set_retained_gateway_auth_identity() {
  local pool_id="$1" domain_prefix="$2"
  if [[ ! "${pool_id}" =~ ^[A-Za-z0-9_-]+$ ]]; then
    log_error "Invalid retained gateway-auth user-pool id; refusing cleanup."
    return 1
  fi
  if [[ -n "${domain_prefix}" ]] \
    && { [[ ! "${domain_prefix}" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || [[ "${domain_prefix}" == *- ]]; }; then
    log_error "Invalid retained gateway-auth domain prefix; refusing cleanup."
    return 1
  fi
  RETAINED_GATEWAY_AUTH_POOL_ID="${pool_id}"
  RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX="${domain_prefix}"
}

# ── Step 4: Clean up dynamically-created deployment resources ────────

cleanup_deployment_resources() {
  local table_name="${PROJECT_NAME}-${ENVIRONMENT_NAME}-deployments"
  local function_name="${PROJECT_NAME}-${ENVIRONMENT_NAME}-deployment"

  log_info "Scanning deployments table '${table_name}' for deployment records..."

  local table_result
  if ! table_result=$(aws dynamodb describe-table \
    --table-name "${table_name}" \
    --region "${AWS_REGION}" \
    --output json 2>&1); then
    if [[ "${table_result}" == *"ResourceNotFoundException"* ]]; then
      log_error "Deployments table '${table_name}' is missing."
    else
      log_error "Could not read deployments table '${table_name}'."
    fi
    log_error "Refusing to destroy the stack without its teardown authority records."
    return 1
  fi

  local scan_result
  if ! scan_result=$(aws dynamodb scan \
    --table-name "${table_name}" \
    --region "${AWS_REGION}" \
    --projection-expression "deployment_id, delete_status" \
    --consistent-read \
    --output json 2>&1); then
    log_error "Could not scan deployment teardown records."
    log_error "Refusing to turn an unreadable table into an empty cleanup plan."
    return 1
  fi

  # The deployments table also stores short-lived test-* and gen-* scratch
  # jobs. Only canonical UUID keys are real DeploymentState records.
  local deployment_rows
  if ! deployment_rows=$(printf '%s' "${scan_result}" | python3 -c '
import json
import sys
import uuid

for item in json.load(sys.stdin).get("Items", []):
    raw = item.get("deployment_id", {}).get("S", "")
    try:
        canonical = str(uuid.UUID(raw))
    except (ValueError, AttributeError):
        continue
    if canonical != raw.lower():
        continue
    status = item.get("delete_status", {}).get("S", "")
    print(f"{canonical}\t{status}")
'); then
    log_error "Could not parse deployment teardown records."
    return 1
  fi

  if [[ -z "${deployment_rows}" ]]; then
    log_info "No deployment records found."
    return
  fi

  local count
  count=$(printf '%s\n' "${deployment_rows}" | wc -l | tr -d '[:space:]')
  log_info "Found ${count} deployment record(s). Delegating to guarded manifest teardown..."

  local dep_id delete_status payload response_file invoke_meta function_error
  local success message
  while IFS=$'\t' read -r dep_id delete_status; do
    if [[ "${delete_status}" == "deleted" ]]; then
      log_info "  Deployment ${dep_id} is already deleted."
      continue
    fi

    payload=$(python3 -c '
import json
import sys
print(json.dumps({
    "_stack_cleanup_delete": True,
    "deployment_id": sys.argv[1],
    "expected_stack_owner": sys.argv[2],
}))
' "${dep_id}" "${STACK_OWNER_ID}")
    response_file=$(mktemp "${TMPDIR:-/tmp}/agentcore-cleanup-response.XXXXXX")

    log_info "  Cleaning deployment ${dep_id} through ${function_name}..."
    if ! invoke_meta=$(aws lambda invoke \
      --function-name "${function_name}" \
      --invocation-type RequestResponse \
      --cli-binary-format raw-in-base64-out \
      --payload "${payload}" \
      --region "${AWS_REGION}" \
      --cli-connect-timeout 30 \
      --cli-read-timeout 650 \
      "${response_file}" \
      --output json 2>&1); then
      rm -f "${response_file}"
      log_error "Deployment ${dep_id} cleanup invocation failed."
      return 1
    fi

    function_error=$(printf '%s' "${invoke_meta}" | python3 -c \
      'import json,sys; print(json.load(sys.stdin).get("FunctionError", ""))')
    if [[ -n "${function_error}" ]]; then
      rm -f "${response_file}"
      log_error "Deployment ${dep_id} cleanup Lambda returned ${function_error}."
      return 1
    fi

    if ! success=$(python3 -c \
      'import json,sys; print("true" if json.load(open(sys.argv[1])).get("success") is True else "false")' \
      "${response_file}"); then
      rm -f "${response_file}"
      log_error "Deployment ${dep_id} cleanup returned an unreadable response."
      return 1
    fi
    message=$(python3 -c \
      'import json,sys; print(" ".join(str(json.load(open(sys.argv[1])).get("message", "")).split()))' \
      "${response_file}")
    rm -f "${response_file}"

    if [[ "${success}" != "true" ]]; then
      log_error "Deployment ${dep_id} was not fully removed: ${message:-no reason returned}"
      log_error "Stopping before CDK destroy so the deployment record and cleanup Lambda remain available."
      return 1
    fi
    log_success "  Deployment ${dep_id}: ${message:-cleanup confirmed}"
  done <<< "${deployment_rows}"

  log_success "Guarded per-deployment cleanup complete."
}

# ── Resource ownership: who is allowed to delete what ────────────────
#
# Why this exists: sweeps 8-12 below match on an account-global NAME PREFIX —
# Cognito pools "AgentCore*", secrets "agentcore-connector/" and
# "agentcore-otel/", IAM roles "AgentCoreMemory-*". None of those names carry
# any deployment identity, so tearing down one deployment deleted a *different*
# live deployment's resources in the same account, including the secrets holding
# raw customer API keys. Customers deploy and delete this platform often, so two
# co-resident deployments (dev + prod, or two teams) is routine — not an edge
# case, and not something an operator gets warned about.
#
# The fix is a tag, not a narrower prefix, because no name is available to
# narrow on: backend/src/app/services/resource_ownership.py stamps
# AgentCoreStack={project}-{env}-{region} on every account-global resource the
# platform creates, and the sweeps delete only what carries THIS stack's value.
# The identity is recomputed here from the same three inputs rather than looked
# up, which is exactly why resource_ownership.stack_id() is defined to match.
#
# It fails CLOSED. An untagged resource counts as foreign and is skipped,
# because a resource created before this tag existed and a resource belonging to
# someone else are indistinguishable — and only one of those two mistakes is
# recoverable. A failed tag read is a third, distinct state: it aborts cleanup
# before CDK destroy rather than being misreported as "untagged".
OWNER_TAG_KEY="AgentCoreStack"
STACK_OWNER_ID="${PROJECT_NAME}-${ENVIRONMENT_NAME}-${AWS_REGION}"
SKIPPED_FOREIGN=0
SKIPPED_UNREADABLE=0
DELETE_FAILURES=0
SCHEDULED_SECRET_DELETIONS=0
OWNER_READ_STATE=""
OWNER_READ_VALUE=""
POOL_DOMAIN=""

# True when a tag value read off a resource proves this stack created it.
# "None"/"" is what the AWS CLI prints for a missing tag with --output text.
is_owned_by_this_stack() {
  local tag_value="${1:-}"
  [[ "${tag_value}" == "${STACK_OWNER_ID}" ]]
}

skip_foreign() {
  SKIPPED_FOREIGN=$((SKIPPED_FOREIGN + 1))
  local tag_value="${2:-}"
  if [[ -z "${tag_value}" || "${tag_value}" == "None" ]]; then
    log_warn "  SKIPPED (untagged, cannot prove ownership): $1"
  else
    log_warn "  SKIPPED (owned by ${tag_value}, not ${STACK_OWNER_ID}): $1"
  fi
}

# Preserve the difference between "tag is missing" and "AWS would not let us
# read the tag". The old `... || echo None` collapsed both into untagged and
# allowed the teardown to continue after losing its ownership evidence.
classify_owner_tag() {
  local raw="${1:-}"
  if [[ -z "${raw}" || "${raw}" == "None" ]]; then
    OWNER_READ_STATE="missing"
    OWNER_READ_VALUE=""
  else
    OWNER_READ_STATE="readable"
    OWNER_READ_VALUE="${raw}"
  fi
}

mark_owner_unreadable() {
  OWNER_READ_STATE="unreadable"
  OWNER_READ_VALUE=""
  SKIPPED_UNREADABLE=$((SKIPPED_UNREADABLE + 1))
  log_error "  SKIPPED (ownership tags unreadable): $1"
}

read_iam_role_owner_tag() {
  local value
  if ! value=$(aws iam list-role-tags --role-name "$1" \
    --query "Tags[?Key=='${OWNER_TAG_KEY}'].Value | [0]" \
    --output text 2>/dev/null); then
    mark_owner_unreadable "IAM role $1"
    return
  fi
  classify_owner_tag "${value}"
}

read_secret_owner_tag() {
  local value
  if ! value=$(aws secretsmanager describe-secret \
    --secret-id "$1" \
    --region "${AWS_REGION}" \
    --query "Tags[?Key=='${OWNER_TAG_KEY}'].Value | [0]" \
    --output text 2>/dev/null); then
    mark_owner_unreadable "secret $1"
    return
  fi
  classify_owner_tag "${value}"
}

read_cognito_pool() {
  local detail
  if ! detail=$(aws cognito-idp describe-user-pool \
    --user-pool-id "$1" \
    --region "${AWS_REGION}" \
    --output json 2>/dev/null); then
    mark_owner_unreadable "Cognito user pool $1"
    POOL_DOMAIN=""
    return
  fi
  OWNER_READ_VALUE=$(printf '%s' "${detail}" | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get("UserPool", {}).get("UserPoolTags", {}).get("AgentCoreStack", ""))')
  POOL_DOMAIN=$(printf '%s' "${detail}" | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get("UserPool", {}).get("Domain", ""))')
  classify_owner_tag "${OWNER_READ_VALUE}"
}

discover_retained_gateway_auth_target() {
  # Recovery path for a prior cleanup that deleted the stack but was interrupted
  # before deleting its RETAINed pool. User-pool names are not unique, so the
  # exact name is only an inventory filter; Project, Environment and the
  # CloudFormation stack-name system tag must all agree before an ID is accepted.
  local expected_name="${PROJECT_NAME}-${ENVIRONMENT_NAME}-gateway-auth"
  local inventory candidate_ids pool_id detail classification
  local matches="" match_count=0 match_domain=""

  log_info "Looking for a retained gateway-auth pool from the already-absent stack..."
  if ! inventory=$(aws cognito-idp list-user-pools \
    --max-results 60 \
    --region "${AWS_REGION}" \
    --output json 2>&1); then
    log_error "Could not inventory Cognito pools for retained-resource recovery."
    return 1
  fi
  if ! candidate_ids=$(printf '%s' "${inventory}" | python3 -c '
import json
import sys

expected = sys.argv[1]
for item in json.load(sys.stdin).get("UserPools") or []:
    if item.get("Name") == expected and item.get("Id"):
        print(item["Id"])
' "${expected_name}"); then
    log_error "Could not parse Cognito inventory for retained-resource recovery."
    return 1
  fi

  for pool_id in ${candidate_ids}; do
    if ! detail=$(aws cognito-idp describe-user-pool \
      --user-pool-id "${pool_id}" \
      --region "${AWS_REGION}" \
      --output json 2>&1); then
      if [[ "${detail}" == *"ResourceNotFoundException"* ]]; then
        continue
      fi
      log_error "Could not verify retained gateway-auth candidate ${pool_id}."
      return 1
    fi
    if ! classification=$(printf '%s' "${detail}" | python3 -c '
import json
import sys

pool = json.load(sys.stdin).get("UserPool") or {}
tags = pool.get("UserPoolTags") or {}
expected_name, project, environment, stack_name = sys.argv[1:]
matches = (
    pool.get("Name") == expected_name
    and tags.get("Project") == project
    and tags.get("Environment") == environment
    and tags.get("aws:cloudformation:stack-name") == stack_name
)
print(("MATCH|" + str(pool.get("Domain") or "")) if matches else "FOREIGN")
' "${expected_name}" "${PROJECT_NAME}" "${ENVIRONMENT_NAME}" "${STACK_NAME}"); then
      log_error "Could not parse retained gateway-auth candidate ${pool_id}."
      return 1
    fi
    if [[ "${classification}" == MATCH\|* ]]; then
      match_count=$((match_count + 1))
      matches="${matches} ${pool_id}"
      match_domain="${classification#MATCH|}"
    fi
  done

  if [[ "${match_count}" -eq 0 ]]; then
    log_info "No retained gateway-auth pool is attributable to '${STACK_NAME}'."
    return
  fi
  if [[ "${match_count}" -ne 1 ]]; then
    log_error "Found ${match_count} retained gateway-auth pools attributable to '${STACK_NAME}'."
    log_error "Refusing an ambiguous delete. Retry with both CLEANUP_GATEWAY_AUTH_POOL_ID"
    log_error "and CLEANUP_GATEWAY_AUTH_DOMAIN_PREFIX set to the exact retained resource."
    return 1
  fi
  pool_id="${matches# }"
  set_retained_gateway_auth_identity "${pool_id}" "${match_domain}"
}

prepare_retained_gateway_auth_target() {
  local supplied_pool="${CLEANUP_GATEWAY_AUTH_POOL_ID}"
  local supplied_domain="${CLEANUP_GATEWAY_AUTH_DOMAIN_PREFIX}"

  if [[ -n "${supplied_pool}" || -n "${supplied_domain}" ]]; then
    if [[ -z "${supplied_pool}" || -z "${supplied_domain}" ]]; then
      log_error "CLEANUP_GATEWAY_AUTH_POOL_ID and CLEANUP_GATEWAY_AUTH_DOMAIN_PREFIX"
      log_error "must be supplied together; a partial retained-resource identity is unsafe."
      return 1
    fi
    if [[ "${STACK_EXISTS}" == "true" ]]; then
      log_error "Do not override gateway-auth IDs while '${STACK_NAME}' still exists."
      log_error "The exact CloudFormation outputs are authoritative for this cleanup."
      return 1
    fi
    set_retained_gateway_auth_identity "${supplied_pool}" "${supplied_domain}"
  elif [[ "${STACK_EXISTS}" != "true" ]]; then
    discover_retained_gateway_auth_target
  fi

  validate_retained_gateway_auth_target
}

validate_retained_gateway_auth_target() {
  RETAINED_GATEWAY_AUTH_VALIDATED=false
  if [[ -z "${RETAINED_GATEWAY_AUTH_POOL_ID}" ]]; then
    return
  fi

  local detail actual_domain expected_name
  expected_name="${PROJECT_NAME}-${ENVIRONMENT_NAME}-gateway-auth"
  if ! detail=$(aws cognito-idp describe-user-pool \
    --user-pool-id "${RETAINED_GATEWAY_AUTH_POOL_ID}" \
    --region "${AWS_REGION}" \
    --output json 2>&1); then
    if [[ "${detail}" == *"ResourceNotFoundException"* ]]; then
      log_info "Retained gateway-auth pool ${RETAINED_GATEWAY_AUTH_POOL_ID} is already absent."
      RETAINED_GATEWAY_AUTH_POOL_ID=""
      RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX=""
      RETAINED_GATEWAY_AUTH_VALIDATED=true
      return
    fi
    log_error "Could not read retained gateway-auth pool ${RETAINED_GATEWAY_AUTH_POOL_ID}."
    return 1
  fi

  if ! actual_domain=$(printf '%s' "${detail}" | python3 -c '
import json
import sys

pool = json.load(sys.stdin).get("UserPool") or {}
tags = pool.get("UserPoolTags") or {}
expected_name, project, environment, stack_name, stack_id = sys.argv[1:]
problems = []
if pool.get("Name") != expected_name:
    problems.append("name")
if tags.get("Project") != project:
    problems.append("Project tag")
if tags.get("Environment") != environment:
    problems.append("Environment tag")
if tags.get("aws:cloudformation:stack-name") != stack_name:
    problems.append("CloudFormation stack-name tag")
if stack_id and tags.get("aws:cloudformation:stack-id") != stack_id:
    problems.append("CloudFormation stack-id tag")
if problems:
    print("identity mismatch: " + ", ".join(problems), file=sys.stderr)
    raise SystemExit(2)
print(pool.get("Domain") or "")
' "${expected_name}" "${PROJECT_NAME}" "${ENVIRONMENT_NAME}" "${STACK_NAME}" "${STACK_ID}"); then
    log_error "Pool ${RETAINED_GATEWAY_AUTH_POOL_ID} is not attributable to the exact stack."
    log_error "Refusing retained gateway-auth deletion."
    return 1
  fi

  if [[ -n "${actual_domain}" && -n "${RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX}" \
    && "${actual_domain}" != "${RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX}" ]]; then
    log_error "Retained gateway-auth domain changed from the captured exact stack output."
    log_error "Refusing to delete either the domain or pool."
    return 1
  fi
  RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX="${actual_domain}"
  RETAINED_GATEWAY_AUTH_VALIDATED=true
  log_success "Retained gateway-auth target verified by exact ID and stack identity."
}

delete_retained_gateway_auth_resources() {
  if [[ -z "${RETAINED_GATEWAY_AUTH_POOL_ID}" ]]; then
    return
  fi
  if [[ "${RETAINED_GATEWAY_AUTH_VALIDATED}" != "true" ]]; then
    log_error "Retained gateway-auth target was not validated; refusing deletion."
    return 1
  fi

  local detail actual_domain result domain_owner attempt
  if ! detail=$(aws cognito-idp describe-user-pool \
    --user-pool-id "${RETAINED_GATEWAY_AUTH_POOL_ID}" \
    --region "${AWS_REGION}" \
    --output json 2>&1); then
    if [[ "${detail}" == *"ResourceNotFoundException"* ]]; then
      log_info "Retained gateway-auth pool is already absent."
      return
    fi
    log_error "Could not re-read retained gateway-auth pool immediately before deletion."
    return 1
  fi
  if ! actual_domain=$(printf '%s' "${detail}" | python3 -c \
    'import json,sys; print((json.load(sys.stdin).get("UserPool") or {}).get("Domain") or "")'); then
    log_error "Could not parse retained gateway-auth pool immediately before deletion."
    return 1
  fi
  if [[ -n "${actual_domain}" && "${actual_domain}" != "${RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX}" ]]; then
    log_error "Retained gateway-auth domain changed after validation; refusing deletion."
    return 1
  fi

  if [[ -n "${actual_domain}" ]]; then
    log_info "Deleting retained gateway-auth domain ${actual_domain}..."
    if ! result=$(aws cognito-idp delete-user-pool-domain \
      --user-pool-id "${RETAINED_GATEWAY_AUTH_POOL_ID}" \
      --domain "${actual_domain}" \
      --region "${AWS_REGION}" 2>&1); then
      if [[ "${result}" != *"ResourceNotFoundException"* ]]; then
        log_error "Failed to delete retained gateway-auth domain ${actual_domain}."
        return 1
      fi
    fi

    # Domain removal can be eventually consistent. Do not race delete-user-pool
    # or mistake an in-progress release for zero residue.
    domain_owner="pending"
    for attempt in $(seq 1 15); do
      if ! result=$(aws cognito-idp describe-user-pool-domain \
        --domain "${actual_domain}" \
        --region "${AWS_REGION}" \
        --output json 2>&1); then
        log_error "Could not verify retained gateway-auth domain deletion."
        return 1
      fi
      if ! domain_owner=$(printf '%s' "${result}" | python3 -c \
        'import json,sys; print((json.load(sys.stdin).get("DomainDescription") or {}).get("UserPool") or "")'); then
        log_error "Could not parse retained gateway-auth domain deletion result."
        return 1
      fi
      [[ -z "${domain_owner}" ]] && break
      [[ "${attempt}" -lt 15 ]] && sleep 2
    done
    if [[ -n "${domain_owner}" ]]; then
      log_error "Retained gateway-auth domain is still assigned after deletion."
      return 1
    fi
  fi

  log_info "Deleting retained gateway-auth pool ${RETAINED_GATEWAY_AUTH_POOL_ID}..."
  if ! result=$(aws cognito-idp delete-user-pool \
    --user-pool-id "${RETAINED_GATEWAY_AUTH_POOL_ID}" \
    --region "${AWS_REGION}" 2>&1); then
    if [[ "${result}" != *"ResourceNotFoundException"* ]]; then
      log_error "Failed to delete retained gateway-auth pool ${RETAINED_GATEWAY_AUTH_POOL_ID}."
      return 1
    fi
  fi

  for attempt in $(seq 1 15); do
    if result=$(aws cognito-idp describe-user-pool \
      --user-pool-id "${RETAINED_GATEWAY_AUTH_POOL_ID}" \
    --region "${AWS_REGION}" \
    --output json 2>&1); then
      [[ "${attempt}" -lt 15 ]] && sleep 2
      continue
    fi
    if [[ "${result}" == *"ResourceNotFoundException"* ]]; then
      result=""
      break
    fi
    log_error "Could not verify retained gateway-auth pool deletion."
    return 1
  done
  if [[ -n "${result}" ]]; then
    log_error "Retained gateway-auth pool still exists after deletion."
    return 1
  fi

  if [[ -n "${RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX}" ]]; then
    if ! result=$(aws cognito-idp describe-user-pool-domain \
      --domain "${RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX}" \
      --region "${AWS_REGION}" \
      --output json 2>&1); then
      log_error "Could not perform final retained gateway-auth domain verification."
      return 1
    fi
    if ! domain_owner=$(printf '%s' "${result}" | python3 -c \
      'import json,sys; print((json.load(sys.stdin).get("DomainDescription") or {}).get("UserPool") or "")'); then
      log_error "Could not parse retained gateway-auth domain deletion result."
      return 1
    fi
    if [[ -n "${domain_owner}" ]]; then
      log_error "Retained gateway-auth domain is still assigned after deletion."
      return 1
    fi
  fi

  log_success "Retained gateway-auth pool and hosted domain are absent."
}

# Deletes only the secrets under a name prefix that this stack owns, and reports
# the rest. $1 = human label, $2 = extra JMESPath predicate (already ANDed).
sweep_owned_secrets() {
  local label="$1" predicate="$2"
  local all_arns
  if ! all_arns=$(aws secretsmanager list-secrets \
    --region "${AWS_REGION}" \
    --query "SecretList[?${predicate}].ARN" \
    --output text 2>/dev/null); then
    mark_owner_unreadable "${label} inventory"
    return
  fi

  local s_arn
  for s_arn in ${all_arns}; do
    read_secret_owner_tag "${s_arn}"
    if [[ "${OWNER_READ_STATE}" == "unreadable" ]]; then
      continue
    fi
    if ! is_owned_by_this_stack "${OWNER_READ_VALUE}"; then
      skip_foreign "${label} ${s_arn}" "${OWNER_READ_VALUE}"
      continue
    fi

    # Stack-wide orphan cleanup has exact stack ownership, but not the stronger
    # per-deployment binding used by the product delete path. Use Secrets
    # Manager's recoverable seven-day deletion instead of force-delete.
    log_info "  Scheduling orphan ${label} for deletion: ${s_arn}"
    if ! aws secretsmanager delete-secret \
      --secret-id "${s_arn}" \
      --recovery-window-in-days 7 \
      --region "${AWS_REGION}" >/dev/null 2>&1; then
      DELETE_FAILURES=$((DELETE_FAILURES + 1))
      log_error "  FAILED to schedule ${label} for deletion: ${s_arn}"
    else
      SCHEDULED_SECRET_DELETIONS=$((SCHEDULED_SECRET_DELETIONS + 1))
    fi
  done
}

delete_owned_iam_role() {
  local role_name="$1" label="$2"
  read_iam_role_owner_tag "${role_name}"
  if [[ "${OWNER_READ_STATE}" == "unreadable" ]]; then
    return
  fi
  if ! is_owned_by_this_stack "${OWNER_READ_VALUE}"; then
    skip_foreign "${label} ${role_name}" "${OWNER_READ_VALUE}"
    return
  fi

  local managed inline policy_arn policy_name
  if ! managed=$(aws iam list-attached-role-policies \
    --role-name "${role_name}" \
    --query "AttachedPolicies[].PolicyArn" \
    --output text 2>/dev/null); then
    DELETE_FAILURES=$((DELETE_FAILURES + 1))
    log_error "  FAILED to list managed policies for ${label} ${role_name}"
    return
  fi
  for policy_arn in ${managed}; do
    if ! aws iam detach-role-policy \
      --role-name "${role_name}" \
      --policy-arn "${policy_arn}" >/dev/null 2>&1; then
      DELETE_FAILURES=$((DELETE_FAILURES + 1))
      log_error "  FAILED to detach ${policy_arn} from ${label} ${role_name}"
      return
    fi
  done

  if ! inline=$(aws iam list-role-policies \
    --role-name "${role_name}" \
    --query "PolicyNames[]" \
    --output text 2>/dev/null); then
    DELETE_FAILURES=$((DELETE_FAILURES + 1))
    log_error "  FAILED to list inline policies for ${label} ${role_name}"
    return
  fi
  for policy_name in ${inline}; do
    if ! aws iam delete-role-policy \
      --role-name "${role_name}" \
      --policy-name "${policy_name}" >/dev/null 2>&1; then
      DELETE_FAILURES=$((DELETE_FAILURES + 1))
      log_error "  FAILED to delete inline policy ${policy_name} from ${label} ${role_name}"
      return
    fi
  done

  log_info "  Deleting orphan ${label}: ${role_name}"
  if ! aws iam delete-role --role-name "${role_name}" >/dev/null 2>&1; then
    DELETE_FAILURES=$((DELETE_FAILURES + 1))
    log_error "  FAILED to delete ${label} ${role_name}"
  fi
}

# ── Step 5: Sweep for orphaned AgentCore-* resources ─────────────────

sweep_orphan_resources() {
  log_info "Sweeping for orphaned AgentCore-* resources owned by this stack..."

  # Runtime execution roles. Dynamically created ones are named
  #    "AgentCoreRuntime-{runtime name}" (runtime_deployer.create_runtime_iam_role,
  #    per_agent_identity._ROLE_NAME_PREFIX) and now carry the owner tag, so match
  #    on the bare prefix and let the tag decide.
  #
  #    The old filter here was "AgentCoreRuntime-${PROJECT_NAME}", which looked
  #    conservative and was the opposite. IAM is not regional, and the CDK shared
  #    runtime role is named AgentCoreRuntime-{project}-{env}-shared in the home
  #    region and AgentCoreRuntime-{project}-{env}-{region}-shared elsewhere
  #    (infra/stacks/platform/lambdas.py:82), so BOTH names start with
  #    "AgentCoreRuntime-{project}". Verified live 2026-09-04 against this
  #    account: a us-east-1 teardown matched
  #    AgentCoreRuntime-agentcore-workflow-dev-eu-central-1-shared and deleted the
  #    *Frankfurt* deployment's shared role, which every agent there assumes —
  #    unrecoverable without a redeploy plus AgentCore's 17-20 min IAM-cache wait
  #    (see the Bug 60 note in lambdas.py). Deleting dev broke prod.
  #
  #    The "-shared" roles are excluded outright rather than left to the tag gate:
  #    they are CloudFormation-owned, so cdk destroy (step 7) deletes this
  #    region's and no one should ever delete another region's. IAM roles get no
  #    aws:cloudformation:* system tags, so the name is the only signal available.
  local roles
  if ! roles=$(aws iam list-roles \
    --query "Roles[?starts_with(RoleName, 'AgentCoreRuntime-')].RoleName" \
    --output text 2>/dev/null); then
    mark_owner_unreadable "runtime IAM role inventory"
    roles=""
  fi
  for role_name in ${roles}; do
    if [[ "${role_name}" == *-shared ]]; then
      log_info "  Leaving CloudFormation-managed shared runtime role to cdk destroy: ${role_name}"
      continue
    fi
    delete_owned_iam_role "${role_name}" "runtime IAM role"
  done

  # Cognito user pools created by gateway deployments. The pool NAME is
  #    "AgentCore-{user's gateway name}", so the "AgentCore" prefix matches any
  #    pool from any deployment of this platform — and any unrelated product that
  #    happens to name a pool that way. gateway_deployer._create_cognito_oauth
  #    stamps the owner tag at creation; that tag, not the prefix, authorizes the
  #    delete. Pools that don't carry it are reported and left alone.
  local pools
  if ! pools=$(aws cognito-idp list-user-pools \
    --max-results 60 \
    --region "${AWS_REGION}" \
    --query "UserPools[?starts_with(Name, 'AgentCore')].Id" \
    --output text 2>/dev/null); then
    mark_owner_unreadable "Cognito user pool inventory"
    pools=""
  fi
  for pool_id in ${pools}; do
    read_cognito_pool "${pool_id}"
    if [[ "${OWNER_READ_STATE}" == "unreadable" ]]; then
      continue
    fi
    if ! is_owned_by_this_stack "${OWNER_READ_VALUE}"; then
      skip_foreign "Cognito user pool ${pool_id}" "${OWNER_READ_VALUE}"
      continue
    fi

    log_info "  Deleting orphan Cognito user pool: ${pool_id}"
    if [[ -n "${POOL_DOMAIN}" ]]; then
      if ! aws cognito-idp delete-user-pool-domain \
        --user-pool-id "${pool_id}" \
        --domain "${POOL_DOMAIN}" \
        --region "${AWS_REGION}" >/dev/null 2>&1; then
        DELETE_FAILURES=$((DELETE_FAILURES + 1))
        log_error "  FAILED to delete domain ${POOL_DOMAIN} for Cognito pool ${pool_id}"
        continue
      fi
    fi
    if ! aws cognito-idp delete-user-pool \
      --user-pool-id "${pool_id}" \
      --region "${AWS_REGION}" >/dev/null 2>&1; then
      DELETE_FAILURES=$((DELETE_FAILURES + 1))
      log_error "  FAILED to delete Cognito user pool ${pool_id}"
    fi
  done

  # OTEL auth-header secrets created by /api/observability/credentials.
  #    Sweeps only per-agent secrets created by POST /api/observability/credentials
  #    (provider-prefixed: agentcore-otel/langfuse/* or agentcore-otel/custom/*).
  #    Explicitly EXCLUDES the admin-managed platform secret at
  #    agentcore-otel/platform/* — that secret outlives any individual stack
  #    by design (see scripts/bootstrap-otel-secret.sh header).
  #    Verified 2026-05-15: cleanup.sh used to delete the platform secret
  #    silently, breaking the next deploy. See tasks/lessons.md Bug 24.
  #    The "agentcore-otel/" prefix names the PRODUCT, so it matches every
  #    deployment's per-agent OTEL secrets in the account — hence the owner-tag
  #    gate, stamped by routers/observability.py at creation.
  sweep_owned_secrets "per-agent OTEL secret" \
    "starts_with(Name, 'agentcore-otel/') && !starts_with(Name, 'agentcore-otel/platform/')"

  # SaaS connector secrets minted by the gateway step / direct deploy.
  #     Owner-scoped naming: agentcore-connector/{owner}/{uuid}. These hold the
  #     raw API key / OAuth client secret, so sweep any that survived a
  #     partial-failed deploy whose deployment record never landed.
  #
  #     This is the worst case for an unscoped prefix sweep and the reason the
  #     owner tag exists: {owner} is a Cognito sub, which is unique per POOL, not
  #     per account, so nothing in the name distinguishes deployments. Tearing
  #     down one deployment used to destroy another live deployment's raw
  #     customer credentials, unrecoverably and without a warning.
  sweep_owned_secrets "connector secret" "starts_with(Name, 'agentcore-connector/')"

  # AgentCoreMemory-* IAM exec roles (memory_step / direct deploy mint these
  #     as AgentCoreMemory-<memory_name>). Defense-in-depth for the in-product
  #     manifest path: a partial-failed deploy whose record never landed can
  #     leave the role behind.
  #
  #     IAM is not regional and the role name is just AgentCoreMemory-{memory
  #     name}, so this prefix matches every deployment's memory roles in the
  #     account — including the same {project}-{env} deployed to a second region.
  #     That is why the owner tag carries the region too. memory_step.py and
  #     services/deployment.py stamp it at create_role time.
  local memory_roles
  if ! memory_roles=$(aws iam list-roles \
    --query "Roles[?starts_with(RoleName, 'AgentCoreMemory-')].RoleName" \
    --output text 2>/dev/null); then
    mark_owner_unreadable "memory IAM role inventory"
    memory_roles=""
  fi
  for role_name in ${memory_roles}; do
    delete_owned_iam_role "${role_name}" "memory IAM role"
  done

  if [[ "${SKIPPED_FOREIGN}" -gt 0 ]]; then
    log_warn "Left ${SKIPPED_FOREIGN} resource(s) in place because they are not tagged ${OWNER_TAG_KEY}=${STACK_OWNER_ID}."
    log_warn "  This is intentional: another deployment in this account may still be using them."
    log_warn "  Each one is listed above. Untagged resources require manual ownership proof."
  fi
  if [[ "${SKIPPED_UNREADABLE}" -gt 0 || "${DELETE_FAILURES}" -gt 0 ]]; then
    log_error "Orphan sweep was incomplete: ${SKIPPED_UNREADABLE} ownership read failure(s), ${DELETE_FAILURES} deletion failure(s)."
    log_error "Stopping before CDK destroy; fix access/errors and retry."
    return 1
  fi
  log_success "Orphan resource sweep complete."
}

# ── Step 6: Run CDK destroy ──────────────────────────────────────────

run_cdk_destroy() {
  log_info "Destroying CDK stack '${STACK_NAME}'..."
  log_info "This removes API Gateway, Lambda functions, Step Functions, DynamoDB tables, S3, CloudFront, and WAF."
  (
    cd "${PROJECT_ROOT}/infra"
    "${CDK_BIN}" destroy "${STACK_NAME}" \
      --force \
      -c environment_name="${ENVIRONMENT_NAME}" \
      -c aws_region="${AWS_REGION}" \
      -c project_name="${PROJECT_NAME}"
  )
  log_success "CDK destroy completed."
}

# ── Step 7: Verify stack removed ──────────────────────────────────────

verify_resources_removed() {
  log_info "Verifying stack '${STACK_NAME}' has been removed..."

  # Allow a brief moment for CloudFormation to finalize deletion
  sleep 5

  local describe_result
  if describe_result=$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}" \
    --query "Stacks[0].StackStatus" \
    --output text 2>&1); then
    log_error "Stack '${STACK_NAME}' still exists with status: ${describe_result}"
    return 1
  fi
  if [[ "${describe_result}" == *"does not exist"* ]]; then
    log_success "Stack '${STACK_NAME}' has been successfully removed."
    return
  fi
  log_error "Could not verify stack removal; an AWS read failure is not proof of deletion."
  return 1
}

# ── Print summary ─────────────────────────────────────────────────────

print_summary() {
  echo ""
  echo "=============================================="
  echo "  Cleanup Complete! (Serverless)"
  echo "=============================================="
  echo ""
  echo "  Stack:   ${STACK_NAME}"
  echo "  Region:  ${AWS_REGION}"
  echo ""
  if [[ "${STACK_EXISTS}" == "true" ]]; then
    echo "  CloudFormation stack removal was verified."
    echo "  Recorded dynamic resources passed guarded teardown."
  else
    echo "  The CloudFormation stack was already absent."
  fi
  echo "  Exact-owner orphan sweep completed."
  echo "  Exact retained gateway-auth resources are absent."
  if [[ "${SCHEDULED_SECRET_DELETIONS}" -gt 0 ]]; then
    echo "  ${SCHEDULED_SECRET_DELETIONS} orphan secret(s) are in a recoverable"
    echo "  seven-day Secrets Manager deletion window."
  fi
  if [[ "${SKIPPED_FOREIGN}" -gt 0 ]]; then
    echo "  ${SKIPPED_FOREIGN} foreign/untagged candidate(s) were left untouched."
  fi
  echo "=============================================="
}

# ── Main ──────────────────────────────────────────────────────────────

confirm_destroy() {
  # SECURITY: Require explicit confirmation before destructive operations.
  # CI must provide the exact regional stack identity as a second factor; a
  # bare FORCE_DESTROY=true plus default variables is too easy to mis-target.
  if [[ "${FORCE_DESTROY:-false}" == "true" ]]; then
    if [[ "${CLEANUP_CONFIRM_STACK_OWNER:-}" != "${STACK_OWNER_ID}" ]]; then
      log_error "FORCE_DESTROY requires CLEANUP_CONFIRM_STACK_OWNER=${STACK_OWNER_ID}"
      return 1
    fi
    log_info "Non-interactive cleanup scope confirmed as ${STACK_OWNER_ID}."
    return
  fi

  echo ""
  log_warn "This will PERMANENTLY DELETE all resources in stack '${STACK_NAME}':"
  echo "  - API Gateway, Lambda functions, Step Functions"
  echo "  - DynamoDB tables (workflows + deployments + flows data)"
  echo "  - S3 buckets (frontend assets + artifacts + logs)"
  echo "  - CloudFront distribution, WAF WebACL, IAM roles"
  echo "  - Recorded AgentCore resources whose guarded teardown confirms ownership"
  echo "  - Orphan roles/pools/secrets tagged AgentCoreStack=${STACK_OWNER_ID}"
  echo "  - Exact RETAINed gateway-auth pool/domain captured from this stack"
  echo ""
  read -r -p "Type '${STACK_OWNER_ID}' to confirm this exact scope: " response
  if [[ "${response}" != "${STACK_OWNER_ID}" ]]; then
    log_info "Cleanup cancelled."
    exit 0
  fi
}

validate_cleanup_options() {
  # These legacy escape hatches authorized account-wide/name-only deletion.
  # They are rejected rather than silently ignored so old automation cannot
  # believe it requested a behavior that no production-safe cleanup supports.
  if [[ -n "${CLEANUP_INCLUDE_UNTAGGED:-}" || -n "${CLEANUP_INCLUDE_FOREIGN_RUNTIMES:-}" ]]; then
    log_error "CLEANUP_INCLUDE_UNTAGGED and CLEANUP_INCLUDE_FOREIGN_RUNTIMES were removed."
    log_error "Untagged or foreign resources require independent manual ownership proof."
    return 1
  fi
}

main() {
  log_info "Starting cleanup of ${PROJECT_NAME} (${ENVIRONMENT_NAME}) in ${AWS_REGION}"

  validate_cleanup_options
  check_prerequisites
  check_aws_credentials
  check_stack_exists
  confirm_destroy
  if [[ "${STACK_EXISTS}" == "true" ]]; then
    # This must precede every deletion path. A fresh checkout with no
    # node_modules must either install the reviewed CLI or stop untouched.
    install_cdk_dependencies_for_cleanup
  fi
  prepare_retained_gateway_auth_target
  if [[ "${STACK_EXISTS}" == "true" ]]; then
    cleanup_deployment_resources
  fi
  sweep_orphan_resources
  if [[ "${STACK_EXISTS}" == "true" ]]; then
    run_cdk_destroy
    verify_resources_removed
  fi
  delete_retained_gateway_auth_resources
  print_summary
}

# Only run when executed, not when sourced. Sourcing is how the ownership gate in
# sweep_orphan_resources gets tested against real AWS (scripts/verify-cleanup-ownership.sh):
# the whole point of that gate is which resources it refuses to delete, and a
# transcription of the logic into a test would not be the logic that ships.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
