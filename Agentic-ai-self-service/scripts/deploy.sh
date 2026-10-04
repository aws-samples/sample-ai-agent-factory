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
# Scope enforcement. Ships enforcing (empty → the stack defaults to "true"): a caller
# without a route's scope gets 403. RBAC_ENFORCE=false is the advisory escape hatch,
# which logs a would-deny and allows; see docs/RBAC_ROLLOUT.md for the rollout. Exposed
# here because the alternative — a raw `cdk deploy -c rbac_enforce=false` — skips
# the COGNITO_USERS carry-forward below and would delete every provisioned user.
RBAC_ENFORCE="${RBAC_ENFORCE:-}"
STACK_NAME="${PROJECT_NAME}-${ENVIRONMENT_NAME}"
PROJECT_PYTHON=""

# Set when the frontend uploaded but its CloudFront cache could not be cleared.
# The summary still prints (the URLs are the useful part) and then the script
# exits non-zero, because a stale edge cache serves the PREVIOUS build.
INVALIDATION_FAILED=0
# Certified-dependencies mode (F-G03-003). Set ONLY by tmp/matrix/relaunch_deploy.py after a green
# final_certification: the prebuilt backend/lib and backend/agentcore-deps inventories are certified
# inputs of the frozen source tree, so this mode never rewrites them. It is not a blind skip: a gate
# (final_certification.py --gate-deps <tree> <files>) must pass before the installers would run,
# again immediately before the single strict synth, and again after the synth before any AWS
# mutation. Ordinary deploys (the default, "0") rebuild both inventories as before.
CERTIFIED_DEPS="${AGENTCORE_CERTIFIED_DEPS:-0}"

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

log_warning() {
  echo -e "\n\033[1;33m[WARNING]\033[0m $*" >&2
}

log_error() {
  echo -e "\n\033[1;31m[ERROR]\033[0m $*" >&2
}

# Reduce a URL to its bare host, so a CloudFront distribution can be looked up by
# the one attribute that is unique to it. Deliberately a separate function: it is
# pure string work, which means infra/tests can run this exact code.
cf_domain_from_url() {
  local url="${1:-}"
  url="${url#http://}"
  url="${url#https://}"
  url="${url%%/*}"
  printf '%s' "${url}"
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

  # Select one Python interpreter for dependency installation and every CDK app
  # launch, and export it as CDK_PYTHON so cdk.json's `"${CDK_PYTHON:-python3}" app.py`
  # runs the same interpreter we installed into (a bare `python3` inside the CLI can
  # resolve to a different installation when several coexist).
  local requested_python="${CDK_PYTHON:-python3}"
  if ! PROJECT_PYTHON="$(command -v "${requested_python}")"; then
    log_error "Python '${requested_python}' is not installed. Please install Python 3.12+ and try again."
    missing=1
  else
    export CDK_PYTHON="${PROJECT_PYTHON}"
    log_success "Python $("${PROJECT_PYTHON}" --version 2>&1) is available at ${PROJECT_PYTHON}."
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
  # The CDK CLI is not taken from the machine: infra/package.json pins it and
  # install_cdk_dependencies installs exactly that with `npm ci`, which needs both files.
  if [[ ! -f "${PROJECT_ROOT}/infra/package.json" || ! -f "${PROJECT_ROOT}/infra/package-lock.json" ]]; then
    log_error "infra/package.json and infra/package-lock.json (the CDK CLI pin) are required for a reproducible deploy."
    missing=1
  else
    log_success "CDK CLI pin present (infra/package.json + package-lock.json)."
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
  # The stack is region-agnostic, but two things differ outside us-east-1 and
  # the operator should know about them before a 15-minute deploy starts:
  #
  #   1. WAF. A CloudFront distribution accepts ONLY a CLOUDFRONT-scoped WebACL
  #      and AWS creates those exclusively in us-east-1. Outside us-east-1 the
  #      same rule set is applied REGIONALly to the Cognito user pool instead,
  #      and the distribution runs without a WebACL unless you pass
  #      CLOUDFRONT_WEB_ACL_ARN pointing at one you created in us-east-1.
  #   2. Account-global names (IAM role, CloudFront OAC, response-headers
  #      policy) are region-qualified outside us-east-1 so both can coexist.
  #
  # A malformed region is still a hard failure — it would otherwise surface as
  # an opaque CDK/CloudFormation error much later.
  if [[ ! "${AWS_REGION}" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]]; then
    log_error "AWS_REGION='${AWS_REGION}' is not a valid AWS region name."
    exit 1
  fi
  if [[ "${AWS_REGION}" != "us-east-1" ]]; then
    log_info "Deploying to '${AWS_REGION}' (not us-east-1)."
    log_info "  * CloudFront will have NO WebACL; the WAF rule set is applied to"
    log_info "    the Cognito user pool instead (CLOUDFRONT-scoped ACLs are"
    log_info "    us-east-1 only). Pass CLOUDFRONT_WEB_ACL_ARN=arn:... to also"
    log_info "    attach an edge ACL you created in us-east-1."
    log_info "  * Account-global resource names are suffixed with the region so"
    log_info "    this deployment can coexist with a us-east-1 one."
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
  check_agentcore_availability
}

# ── Step 2b: Probe AgentCore availability in the target region ────────

check_agentcore_availability() {
  # Agents are deployed to Bedrock AgentCore Runtime at *use* time, not at
  # stack-deploy time — so an unsupported region produces a stack that comes up
  # clean and then fails on the first agent deploy. Probe the control plane now
  # instead of maintaining a hardcoded region allowlist that goes stale.
  #
  # A permissions failure must NOT block the deploy: the deploying principal is
  # not necessarily the one that will create runtimes.
  log_info "Probing Bedrock AgentCore availability in ${AWS_REGION}..."
  local probe
  if probe=$(aws bedrock-agentcore-control list-agent-runtimes \
    --region "${AWS_REGION}" --max-results 1 2>&1); then
    log_success "Bedrock AgentCore control plane is reachable in ${AWS_REGION}."
    return
  fi
  if grep -qiE 'AccessDenied|not authorized|UnrecognizedClient|ExpiredToken' <<< "${probe}"; then
    log_info "AgentCore probe was denied by IAM — cannot confirm availability."
    log_info "Proceeding: the deploying principal need not be able to list runtimes."
    return
  fi
  if grep -qiE 'Could not connect|EndpointConnectionError|endpoint|InvalidClientTokenId|does not exist' <<< "${probe}"; then
    log_error "Bedrock AgentCore does not appear to be available in ${AWS_REGION}."
    log_error "The stack will deploy, but every AGENT deploy will fail at runtime creation."
    log_error "Set AGENTCORE_SKIP_REGION_CHECK=true to proceed anyway."
    log_error "AWS said: $(head -2 <<< "${probe}")"
    [[ "${AGENTCORE_SKIP_REGION_CHECK:-false}" == "true" ]] || exit 1
    log_info "AGENTCORE_SKIP_REGION_CHECK=true — continuing."
    return
  fi
  log_info "AgentCore probe was inconclusive; continuing. AWS said: $(head -2 <<< "${probe}")"
}

# ── Step 3: Install CDK dependencies ─────────────────────────────────

# The CDK CLI is versioned independently of aws-cdk-lib. A machine-global cdk
# (or npx resolving whatever is on PATH) can vary between machines, so two deploys
# could synthesize with different toolkits. infra/package.json + its lockfile
# pin the CLI; `npm ci` installs exactly that, and every cdk invocation below goes
# through CDK_BIN with no network fallback. Tests may point CDK_BIN at a stub.
CDK_BIN="${CDK_BIN:-${PROJECT_ROOT}/infra/node_modules/.bin/cdk}"

verify_cdk_cli_pinned() {  # READ-ONLY: the locally installed CDK CLI must be exactly the version infra/package.json pins
  local pinned installed
  pinned=$("${PROJECT_PYTHON:-python3}" -I -S -B -c "import json;print(json.load(open('${PROJECT_ROOT}/infra/package.json'))['devDependencies']['aws-cdk'])")
  installed=$("${CDK_BIN}" --version 2>/dev/null | awk '{print $1}')
  if [[ -z "${installed}" || "${installed}" != "${pinned}" ]]; then
    log_error "CDK CLI at ${CDK_BIN} is '${installed:-missing}', but infra/package.json pins ${pinned}; refusing to continue."
    exit 1
  fi
  log_success "CDK CLI ${installed} matches the pin (infra/package-lock.json)."
}

require_certified_interpreter() {  # certified mode: the interpreter IS the artifact-bound certified infra environment
  # Verified BEFORE it ever executes, with OS-owned tools only: the path shape, then the resolved binary and its bytes
  # against the facts the relauncher took from the certification artifact (AGENTCORE_INFRA_PY_REAL / _SHA256). A
  # matching-path interpreter with other bytes is refused without running a single instruction of it.
  PROJECT_PYTHON="${CDK_PYTHON:-}"
  case "${PROJECT_PYTHON}" in
    "${PROJECT_ROOT}"/tmp/.certenv.*/infra/bin/python) ;;
    *) log_error "certified mode requires CDK_PYTHON to name the certified infra environment (tmp/.certenv.<run>/infra/bin/python); got '${PROJECT_PYTHON:-unset}'"; exit 1 ;;
  esac
  local want_real="${AGENTCORE_INFRA_PY_REAL:-}" want_sha="${AGENTCORE_INFRA_PY_SHA256:-}" real sha
  if [[ -z "${want_real}" || ! "${want_sha}" =~ ^[0-9a-f]{64}$ ]]; then
    log_error "certified mode requires AGENTCORE_INFRA_PY_REAL and AGENTCORE_INFRA_PY_SHA256 (64 hex) from the artifact"; exit 1
  fi
  real="$(/usr/bin/readlink -f "${PROJECT_PYTHON}" 2>/dev/null || true)"
  if [[ -z "${real}" || "${real}" != "${want_real}" || ! -f "${real}" || -L "${real}" ]]; then
    log_error "the certified interpreter does not resolve to the artifact's interpreter (got '${real:-unresolvable}')"; exit 1
  fi
  sha="$(/usr/bin/shasum -a 256 "${real}" | /usr/bin/cut -c1-64)"
  if [[ "${sha}" != "${want_sha}" ]]; then
    log_error "the certified interpreter's bytes differ from the artifact (sha256 ${sha:0:16} != ${want_sha:0:16}); refusing before executing it"; exit 1
  fi
  export CDK_PYTHON="${PROJECT_PYTHON}"
  log_success "Certified interpreter: ${PROJECT_PYTHON} (${sha:0:16})"
}

install_cdk_dependencies() {
  log_info "Installing CDK dependencies..."
  cd "${PROJECT_ROOT}/infra"
  "${PROJECT_PYTHON}" -B -m pip install -r requirements.txt --quiet
  npm ci --ignore-scripts --no-audit --no-fund --loglevel=error
  cd "${PROJECT_ROOT}"
  verify_cdk_cli_pinned
}

# ── Step 4: Install backend dependencies ──────────────────────────────

install_backend_dependencies() {
  log_info "Installing backend dependencies..."
  cd "${PROJECT_ROOT}/backend"

  "${PROJECT_PYTHON}" -m pip install . --quiet

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

# ── Step 4d: Certified-dependencies gate (F-G03-003) ─────────────────

require_expected_account() {  # certified mode: the live STS account must be EXACTLY the one the relauncher validated
  local expected="${AGENTCORE_EXPECTED_ACCOUNT:-}" got
  if [[ ! "${expected}" =~ ^[0-9]{12}$ ]]; then
    log_error "certified mode requires AGENTCORE_EXPECTED_ACCOUNT (12 digits); got '${expected:-unset}'"; exit 1
  fi
  got="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
  if [[ "${got}" != "${expected}" ]]; then
    log_error "the live AWS identity is account '${got:-unknown}', not the expected ${expected}; refusing (${1:-gate})"; exit 1
  fi
}

certified_deps_gate() {
  require_expected_account "$1"
  # $1 = when (pre-install | pre-synth | post-synth). Fail-closed: any problem exits before the next step.
  local when="$1"
  local sha="${AGENTCORE_FROZEN_TREE_SHA256:-}" files="${AGENTCORE_FROZEN_TREE_FILES:-}"
  if [[ ! "${sha}" =~ ^[0-9a-f]{64}$ ]]; then
    log_error "certified-dependencies mode needs AGENTCORE_FROZEN_TREE_SHA256 (64 hex); refusing (${when})."
    exit 1
  fi
  if [[ ! "${files}" =~ ^[0-9]+$ ]]; then
    log_error "certified-dependencies mode needs AGENTCORE_FROZEN_TREE_FILES (integer); refusing (${when})."
    exit 1
  fi
  local gate="${PROJECT_ROOT}/tmp/matrix/final_certification.py"
  if [[ ! -f "${gate}" || -L "${gate}" ]]; then
    log_error "certified-dependencies gate ${gate} is missing or a symlink; refusing (${when})."
    exit 1
  fi
  mkdir -p "${PROJECT_ROOT}/.cdk-gate"
  local out="${PROJECT_ROOT}/.cdk-gate/deps-gate-${when}.json"
  # -I -S -B: the gate runs isolated (no site, no .pth, no PYTHON* env, no byte-code) from its first instruction; it is
  # stdlib-only and REFUSES any other launch.
  if ! "${PROJECT_PYTHON:-python3}" -I -S -B "${gate}" --gate-deps "${sha}" "${files}" > "${out}" 2>&1; then
    log_error "certified-dependencies gate FAILED (${when}); refusing to continue:"
    cat "${out}" >&2
    exit 1
  fi
  log_success "certified-dependencies gate passed (${when}): tree ${sha:0:16}/${files}; installers are NOT run."
}

install_or_verify_dependencies() {
  if [[ "${CERTIFIED_DEPS}" == "1" ]]; then
    certified_deps_gate pre-install
  else
    install_lambda_dependencies
    install_agentcore_deps
  fi
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
    "${CDK_BIN}" bootstrap "aws://${account_id}/${AWS_REGION}"
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
  "${PROJECT_PYTHON:-python3}" -B "${SCRIPT_DIR}/preflight-ddb-restore.py" \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}"
  log_success "DynamoDB preflight complete."
}

# ── Step 5b: Never silently delete provisioned Cognito users ──────────

# Each COGNITO_USERS email becomes a custom resource whose Delete handler calls
# AdminDeleteUser. So dropping an email from the list is the OFFBOARDING
# mechanism — deliberate and worth keeping. The hazard is that an *omitted*
# variable is indistinguishable from an intentionally emptied one: a routine
# `./scripts/deploy.sh` with COGNITO_USERS unset removes every provisioner and
# deletes every user it created, taking their password and group memberships
# with them. That is silent, unprompted data loss on the most ordinary command
# in the repo.
#
# So: an EMPTY list against an EXISTING stack carries forward whoever is already
# provisioned. Removing a user stays possible, but now requires saying so —
# either pass the reduced list, or COGNITO_USERS=none to clear it entirely.
preserve_existing_cognito_users() {
  if [[ "${COGNITO_USERS}" == "none" ]]; then
    log_warning "COGNITO_USERS=none — any users provisioned by a previous deploy WILL be deleted."
    COGNITO_USERS=""
    return
  fi
  [[ -n "${COGNITO_USERS}" ]] && return   # explicit list wins, removals included

  # Fail-closed lookup (P0-A2, 2026-09-25). This read decides whether the synth keeps or DELETES every provisioned
  # user, so only a VERIFIED absent stack may resolve to "no users". A lookup that fails for any other reason
  # (AccessDenied, throttling, network, an unparseable template) aborts BEFORE synthesis instead of silently
  # emptying the list. Runs before cdk_synth_pinned in both modes: the synth bakes COGNITO_USERS into context.
  local out err rc existing
  err="$(mktemp)"
  # `if var=$(cmd)` captures the status WITHOUT tripping `set -e` (a bare `var=$(cmd); rc=$?` exits first).
  if out="$(aws cloudformation get-template \
    --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
    --query 'TemplateBody' --output json 2> "${err}")"; then
    rc=0
  else
    rc=$?
  fi
  if (( rc != 0 )); then
    # Exact absent-stack classification: the AWS error code AND the exact GetTemplate message for THIS stack, as one
    # whole line. Any other ValidationError (or any other text) is unknown and fails closed.
    local stack_re
    stack_re="$(printf '%s' "${STACK_NAME}" | sed 's/[][\.*^$\\/+?(){}|]/\\&/g')"
    if grep -Eq "^An error occurred \(ValidationError\) when calling the GetTemplate operation: Stack with id ${stack_re} does not exist\$" "${err}"; then
      log_info "Stack '${STACK_NAME}' is absent (exact ValidationError + message): no users to carry forward."
      rm -f "${err}"
      return
    fi
    log_error "Could not read the existing template of '${STACK_NAME}' (rc=${rc}) and the stack is NOT provably absent."
    log_error "Refusing to synthesize: deploying without the carried user list would DELETE provisioned users."
    cat "${err}" >&2
    rm -f "${err}"
    exit 1
  fi
  rm -f "${err}"
  if ! existing="$(printf '%s' "${out}" | "${PROJECT_PYTHON:-python3}" -B -c '
import json, sys
raw = sys.stdin.read()
try:
    body = json.loads(raw)
except Exception as exc:  # noqa: BLE001
    print(f"existing template is not JSON: {type(exc).__name__}", file=sys.stderr)
    sys.exit(3)
if isinstance(body, str):          # get-template can hand back a YAML string
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        print("existing template is a YAML string and PyYAML is not available to parse it", file=sys.stderr)
        sys.exit(3)
    try:
        body = yaml.safe_load(body)
    except Exception as exc:  # noqa: BLE001
        print(f"existing template YAML did not parse: {type(exc).__name__}", file=sys.stderr)
        sys.exit(3)
if not isinstance(body, dict):
    print("existing template is not a mapping", file=sys.stderr)
    sys.exit(3)
# Key on the PROPERTY SHAPE (UserPoolId + a literal Email), not the resource
# type: CDK emits these as AWS::CloudFormation::CustomResource, not Custom::*,
# and that distinction is invisible until this returns nothing and the guard
# silently fails to guard.
emails = sorted({
    p["Email"]
    for r in (body.get("Resources") or {}).values()
    if isinstance(r, dict)
    for p in [r.get("Properties") or {}]
    if isinstance(p, dict) and "UserPoolId" in p
    and isinstance(p.get("Email"), str) and "@" in p["Email"]
})
print(",".join(emails))
')"; then
    log_error "The existing template of '${STACK_NAME}' could not be parsed; refusing to synthesize (users could be deleted)."
    exit 1
  fi

  if [[ -n "${existing}" ]]; then
    COGNITO_USERS="${existing}"
    log_warning "COGNITO_USERS was not set, but '${STACK_NAME}' already provisions: ${existing}"
    log_warning "Carrying them forward — deploying without the variable would DELETE them."
    log_info    "To remove a user, pass the reduced list explicitly; COGNITO_USERS=none clears all."
  else
    log_info "'${STACK_NAME}' exists and provisions no users (verified from its template)."
  fi
}

# ── Step 6: Run CDK deploy ────────────────────────────────────────────

# The three CDK safety gates are separate functions so certified mode can re-derive the frozen identity
# immediately before EVERY AWS mutation boundary (bootstrap, table restore, deploy) and run the strict synth +
# post-synth identity check BEFORE any of them. Ordinary mode runs them back to back (run_cdk_deploy).
cdk_synth_pinned() {
  log_info "Deploying CDK stack '${STACK_NAME}' to region '${AWS_REGION}'..."
  log_info "This creates API Gateway, Lambda functions, Step Functions, DynamoDB tables, S3, and CloudFront."
  log_info "Lambda code is packaged automatically by CDK from the backend directory."
  cd "${PROJECT_ROOT}/infra"

  # ── Exact-byte CDK safety gate ──────────────────────────────────────
  # install_lambda_dependencies (backend/lib) and install_agentcore_deps
  # (backend/agentcore-deps) have already run, and NOTHING between them and
  # this function rewrites those asset directories. So we synthesize ONCE into
  # a pinned cloud assembly, review it (strict synth + diff), then deploy that
  # SAME assembly with `--app` so CloudFormation receives the exact template
  # and asset manifest we reviewed — no re-synthesis, no intervening asset
  # rewrite. Shared context is applied ONLY on the synth that builds the
  # assembly; diff and deploy read the frozen assembly and take no context, so
  # synth/diff/deploy cannot drift. A strict-synth failure aborts before any
  # AWS mutation (fail-closed). Scope of `--strict`: it fails the synth on
  # construct-tree Annotations (warnings/errors added by constructs and cdk-nag);
  # it does NOT fail on CLI notices such as "N feature flags are not configured",
  # which exit 0. The feature-flag baseline is therefore pinned in infra/cdk.json,
  # not enforced by this flag.
  # Fixed, project-scoped gate locations. We deliberately do NOT honor an
  # environment variable for these paths: the assembly dir is `rm -rf`'d below,
  # and a caller-controlled path must never be a recursive-delete target.
  if [[ -z "${PROJECT_ROOT}" || "${PROJECT_ROOT}" == "/" ]]; then
    log_error "PROJECT_ROOT is unset or '/'; refusing to run the CDK safety gate."
    exit 1
  fi
  local gate_asm="${PROJECT_ROOT}/infra/cdk.out.preflight"
  local gate_log_dir="${PROJECT_ROOT}/.cdk-gate"
  # Defence-in-depth: only ever remove the exact expected project child.
  if [[ "${gate_asm}" != "${PROJECT_ROOT}/infra/cdk.out.preflight" ]]; then
    log_error "Refusing to remove unexpected assembly path '${gate_asm}'."
    exit 1
  fi
  if [[ -L "${gate_log_dir}" ]]; then
    log_error "${gate_log_dir} is a symlink; refusing to write gate receipts through it."
    exit 1
  fi
  mkdir -p "${gate_log_dir}"
  rm -rf -- "${gate_asm}"
  if [[ "${CERTIFIED_DEPS:-0}" == "1" ]]; then
    certified_deps_gate pre-synth
  fi

  # Optional platform OTEL defaults — feature is enabled iff both
  # OTEL_ENDPOINT and OTEL_AUTH_SECRET_ARN are set. Run scripts/bootstrap-otel-secret.sh
  # first to obtain a secret ARN.
  local -a cdk_ctx=(
    -c environment_name="${ENVIRONMENT_NAME}"
    -c aws_region="${AWS_REGION}"
    -c project_name="${PROJECT_NAME}"
    -c cognito_users="${COGNITO_USERS}"
    -c cloudfront_web_acl_arn="${CLOUDFRONT_WEB_ACL_ARN:-}"
    -c rbac_enforce="${RBAC_ENFORCE}"
    -c otel_endpoint="${OTEL_ENDPOINT:-}"
    -c otel_auth_secret_arn="${OTEL_AUTH_SECRET_ARN:-}"
    -c otel_sample_rate="${OTEL_SAMPLE_RATE:-1.0}"
    -c otel_service_name_prefix="${OTEL_SERVICE_NAME_PREFIX:-}"
  )

  log_info "CDK safety gate 1/3: strict synth of '${STACK_NAME}' into a pinned assembly..."
  if ! "${CDK_BIN}" synth "${STACK_NAME}" --strict --output "${gate_asm}" "${cdk_ctx[@]}" \
      > "${gate_log_dir}/synth.log" 2>&1; then
    log_error "Strict synth failed — refusing to deploy. Last lines:"
    tail -40 "${gate_log_dir}/synth.log" >&2
    exit 1
  fi
  if [[ ! -f "${gate_asm}/${STACK_NAME}.template.json" ]]; then
    log_error "Strict synth produced no template for '${STACK_NAME}' in ${gate_asm} — refusing to deploy."
    exit 1
  fi
  log_success "Strict synth clean; reviewed assembly pinned at ${gate_asm}."
  if [[ "${CERTIFIED_DEPS:-0}" == "1" ]]; then
    certified_deps_gate post-synth   # the synth read the certified inventories and moved nothing; proven before diff/deploy
  fi

  # Record the reviewed artifact digests (template, cloud-assembly manifest, every asset manifest) and a framed digest
  # of every referenced asset payload, both through stable O_NOFOLLOW single-link reads, into 0600 no-clobber receipts.
  # deploy consumes this same assembly via --app (no re-synth), so these receipts identify exactly what ships; they are
  # re-verified immediately before diff and before deploy.
  pinned_payload_tool invalidate         # BOTH prior receipts removed + directory fsynced BEFORE any artifact is read
  pinned_payload_tool record-all         # both receipt bodies computed first, then published as one owned transaction
  log_info "Reviewed assembly receipts recorded under ${gate_log_dir} (stable O_NOFOLLOW reads; artifacts + payloads)."

  cd "${PROJECT_ROOT}"
}

pinned_payload_tool() {
  # $1 = record-artifacts | record | verify-pre-diff | verify-pre-deploy | record-dist | verify-dist.
  # One Python tool (stdlib only) for every receipt so recorder and verifier cannot drift. Every read is a stable
  # O_NOFOLLOW single-link read whose full identity (dev, inode, type/mode, nlink, size, mtime_ns, ctime_ns) holds
  # across the read; receipts are 0600, written to an exclusive same-directory temp, fsynced, link(2)-published
  # no-clobber, directory-fsynced and re-read stably before success; symlinks, hardlinks, traversal or alias paths,
  # duplicate JSON keys, unsupported packaging, wrong source type, duplicates, missing/extra items and any drift fail
  # closed before the next step.
  local action="$1"
  local gate_asm="${PROJECT_ROOT}/infra/cdk.out.preflight"
  local gate_log_dir="${PROJECT_ROOT}/.cdk-gate"
  if [[ -L "${gate_log_dir}" ]]; then
    log_error "${gate_log_dir} is a symlink; refusing to write receipts through it."
    exit 1
  fi
  if ! "${PROJECT_PYTHON:-python3}" -I -S -B - "${action}" "${gate_asm}" "${gate_log_dir}" "${STACK_NAME}" "${PROJECT_ROOT}" <<'PY'
import glob, hashlib, json, os, posixpath, re, stat, sys, tempfile

action, asm, logdir, stack, project_root = sys.argv[1:6]
asm = os.path.normpath(asm)
HASHES = os.path.join(logdir, "assembly-hashes.txt")
PAYLOADS = os.path.join(logdir, "assembly-payloads.txt")
DIST_RECEIPT = os.path.join(logdir, "frontend-dist.txt")
DIST_DIR = os.path.join(project_root, "frontend", "dist")
HEADER = f"reviewed assembly: {asm}"
DIST_HEADER = f"reviewed frontend dist: {os.path.normpath(DIST_DIR)}"


def fail(why):
    print(json.dumps({"ok": False, "action": action, "why": why}))
    sys.exit(1)


def ident(st):
    return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode), st.st_mode & 0o7777, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def stable_bytes(path, *, mode0600=False, dir_fd=None):
    """Bytes of a regular single-link file whose full identity held across the read. With dir_fd the path is a basename
    resolved relative to that bound directory handle (never through the pathname of the directory)."""
    label = os.path.relpath(path, asm) if path.startswith(asm + os.sep) else os.path.basename(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        raise OSError(f"{label}: {exc.strerror or type(exc).__name__}")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{label}: not a regular file")
        if st.st_nlink != 1:
            raise OSError(f"{label}: nlink={st.st_nlink}")
        if mode0600 and (st.st_mode & 0o777) != 0o600:
            raise OSError(f"{label}: mode {oct(st.st_mode & 0o777)} != 0600")
        chunks = []
        while True:
            c = os.read(fd, 1 << 20)
            if not c:
                break
            chunks.append(c)
        if ident(st) != ident(os.fstat(fd)):
            raise OSError(f"{label}: identity changed during the read")
        return b"".join(chunks), st
    finally:
        os.close(fd)


def no_duplicate_keys(pairs):
    seen = set()
    for k, _v in pairs:
        if k in seen:
            raise ValueError(f"duplicate JSON key {k!r}")
        seen.add(k)
    return dict(pairs)


def within(rel):
    """A canonical, safe, relative path strictly inside the assembly: exactly its own posix-normalized form, no empty /
    dot / dot-dot segment, never the assembly root itself, no backslash or NUL."""
    if not isinstance(rel, str) or not rel or rel in (".", "..") or rel.startswith("/") or "\\" in rel or "\0" in rel:
        return False
    if rel != posixpath.normpath(rel) or any(seg in ("", ".", "..") for seg in rel.split("/")):
        return False
    full = os.path.normpath(os.path.join(asm, rel))
    return full != asm and full.startswith(asm + os.sep)


def walk_entries(full, rel):
    """Every entry under a payload directory, sorted: (relpath, kind) with kind D (directory, empty ones included) or F.
    A symlink or non-regular entry anywhere is a failure."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(full):
        dirnames.sort()
        for dn in dirnames:
            dp = os.path.join(dirpath, dn)
            if os.path.islink(dp):
                raise OSError(f"{rel}: directory symlink {os.path.relpath(dp, full)}")
            entries.append((os.path.relpath(dp, full), "D"))
        for fn in sorted(filenames):
            fp = os.path.join(dirpath, fn)
            st = os.lstat(fp)
            if stat.S_ISLNK(st.st_mode):
                raise OSError(f"{rel}: symlink {os.path.relpath(fp, full)}")
            if not stat.S_ISREG(st.st_mode):
                raise OSError(f"{rel}: non-regular entry {os.path.relpath(fp, full)}")
            entries.append((os.path.relpath(fp, full), "F"))
    return sorted(entries)


def framed_digest(base_dir, rel, packaging):
    """Deterministic digest of a payload: for a directory every entry (directories too, empty ones included) in sorted
    order, each regular file framed with relpath, its FULL 16-bit mode (exactly what the CDK packager writes into the
    zip), size and bytes; for a file one frame. The directory entry set is walked again after hashing and must be
    identical (the set stayed stable across the walk)."""
    full = os.path.normpath(os.path.join(base_dir, rel)) if rel else base_dir
    st = os.lstat(full)
    if stat.S_ISLNK(st.st_mode):
        raise OSError(f"{rel or full} is a symlink")
    if packaging == "file":
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{rel} is declared packaging=file but is not a regular file")
        entries = [("", "F")]
    elif packaging in ("zip", "dir"):
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(f"{rel or full} is declared a directory payload but is not a directory")
        entries = walk_entries(full, rel or full)
    else:
        raise OSError(f"{rel}: unsupported packaging {packaging!r}")
    h = hashlib.sha256()
    for sub, kind in entries:
        if kind == "D":
            frame = f"D\0{sub}\0".encode()
        else:
            fp = os.path.join(full, sub) if sub else full
            data, fst = stable_bytes(fp)
            # the pinned CDK packager (aws-cdk 2.1138.0: yazl addBuffer(..., mode: stat.mode)) writes the FULL mode into the
            # asset zip's external attributes, so any permission change changes the uploaded bytes: frame the same value
            frame = f"F\0{sub}\0{fst.st_mode & 0xFFFF:o}\0{len(data)}\0".encode() + hashlib.sha256(data).digest()
        h.update(len(frame).to_bytes(4, "big") + frame)
    if packaging != "file" and walk_entries(full, rel or full) != entries:
        raise OSError(f"{rel or full}: directory entries changed during the walk")
    return h.hexdigest(), len(entries)


def referenced_payloads():
    """Every file-asset source.path referenced by every *.assets.json (exact set), validated as canonical."""
    paths = {}
    for mf in sorted(glob.glob(os.path.join(glob.escape(asm), "*.assets.json"))):
        try:
            data, _st = stable_bytes(mf)
            doc = json.loads(data, object_pairs_hook=no_duplicate_keys)
        except (OSError, ValueError) as exc:
            fail(f"assets manifest {os.path.basename(mf)} unreadable/malformed: {exc}")
        if not isinstance(doc, dict):
            fail(f"assets manifest {os.path.basename(mf)} is not an object")
        if doc.get("dockerImages"):
            fail(f"assets manifest {os.path.basename(mf)} references docker images: unsupported packaging for this gate")
        for aid, asset in (doc.get("files") or {}).items():
            src = (asset or {}).get("source") or {}
            rel, packaging = src.get("path"), src.get("packaging", "file")
            if not within(rel):
                fail(f"asset {aid} has an unsafe, aliased or non-relative source.path {rel!r}")
            if packaging not in ("zip", "file"):
                fail(f"asset {aid} uses unsupported packaging {packaging!r}")
            if rel in paths and paths[rel] != packaging:
                fail(f"asset source.path {rel} referenced with conflicting packaging")
            paths[rel] = packaging
    return paths


def read_receipt(path, *, header):
    """A receipt: 0600 regular single-link, exact single header first, then only well-formed unique lines."""
    try:
        raw, _st = stable_bytes(path, mode0600=True)
        text = raw.decode()
    except (OSError, UnicodeDecodeError) as exc:
        fail(f"receipt {os.path.basename(path)} unreadable: {exc}")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines or lines[0] != header:
        fail(f"receipt {os.path.basename(path)} lacks the exact header {header!r}")
    out = {}
    for line in lines[1:]:
        m = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not m or line.startswith("reviewed "):
            fail(f"receipt {os.path.basename(path)} has a malformed or extra line: {line[:80]!r}")
        if m.group(2) in out:
            fail(f"receipt {os.path.basename(path)} has a duplicate entry {m.group(2)}")
        out[m.group(2)] = m.group(1)
    return out


def logdir_still_bound(dfd):
    """The directory pathname must still name the handle we hold (a swapped directory fails)."""
    try:
        lst = os.lstat(logdir)
    except OSError:
        return False
    dst = os.fstat(dfd)
    return stat.S_ISDIR(lst.st_mode) and not stat.S_ISLNK(lst.st_mode) and (lst.st_dev, lst.st_ino) == (dst.st_dev, dst.st_ino)


def publish_receipt(path, lines):
    """Publication is entirely relative to the bound directory handle: an exclusive random temp created with dir_fd,
    every byte written and fsynced, link(2) with src/dst dir_fd (no-clobber), temp unlinked via dir_fd, directory fsync,
    a stable 0600 re-read through dir_fd, and finally the directory PATHNAME must still bind to the handle. Ownership is
    the temp's own dev+inode (from fstat before the link); the canonical basename must carry exactly it after link(2).
    Only what THIS invocation created is ever removed on failure, always through the same handle. Returns (dev, inode)."""
    import secrets

    name = os.path.basename(path)
    dfd = open_logdir()
    try:
        content = ("\n".join(lines) + "\n").encode()
        fd = tmpname = None
        linked = False
        our_ident = None
        try:
            tmpname = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
            fd = os.open(tmpname, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            os.fchmod(fd, 0o600)
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(fd)
            tst = os.fstat(fd)
            our_ident = (tst.st_dev, tst.st_ino)
            os.close(fd)
            fd = None
            try:
                os.link(tmpname, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except FileExistsError:
                raise OSError("a receipt already exists at the canonical name (not ours to replace)")
            linked = True
            lst = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if (lst.st_dev, lst.st_ino) != our_ident:
                linked = False  # the canonical is NOT our link: never touch it
                raise OSError("the canonical name does not carry our published inode")
            os.unlink(tmpname, dir_fd=dfd)
            tmpname = None
            os.fsync(dfd)
            back, pst = stable_bytes(name, mode0600=True, dir_fd=dfd)
            if back != content or (pst.st_dev, pst.st_ino) != our_ident:
                raise OSError("re-read differs from what was written")
            if not logdir_still_bound(dfd):
                raise OSError("the receipt directory pathname no longer names the directory written to (swapped)")
            return our_ident
        except OSError as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmpname is not None:
                try:
                    os.unlink(tmpname, dir_fd=dfd)
                except OSError:
                    pass
            if linked:
                try:
                    unpublish(path, our_ident, dfd)
                except OSError as cleanup_exc:
                    fail(f"could not publish {name}: {exc}; AND cleanup failed: {cleanup_exc}")
            fail(f"could not publish {name}: {exc}")
    finally:
        os.close(dfd)


def unpublish(path, ident_pair, dfd):
    """Remove a canonical receipt ONLY if the basename, statted through the open directory handle, is still the very
    inode this invocation published; a concurrent replacement is preserved. Unlink/fsync errors are raised, never
    swallowed, so a caller can never claim a removal that did not happen. Returns True when removed."""
    name = os.path.basename(path)
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if ident_pair is None or (st.st_dev, st.st_ino) != ident_pair:
        return False
    os.unlink(name, dir_fd=dfd)
    os.fsync(dfd)
    return True


def artifact_set():
    expected = {os.path.normpath(os.path.join(asm, f"{stack}.template.json")), os.path.normpath(os.path.join(asm, "manifest.json"))}
    expected |= {os.path.normpath(p) for p in glob.glob(os.path.join(glob.escape(asm), "*.assets.json"))}
    return expected


def open_logdir():
    o_dir = getattr(os, "O_DIRECTORY", 0)
    try:
        dfd = os.open(logdir, os.O_RDONLY | os.O_NOFOLLOW | o_dir)
    except OSError as exc:
        fail(f"{logdir} is not an openable real directory: {exc.strerror or type(exc).__name__}")
    dst = os.fstat(dfd)
    try:
        lst = os.lstat(logdir)
    except OSError as exc:
        os.close(dfd)
        fail(f"{logdir}: {exc.strerror or type(exc).__name__}")
    if not stat.S_ISDIR(dst.st_mode) or stat.S_ISLNK(lst.st_mode) or (lst.st_dev, lst.st_ino) != (dst.st_dev, dst.st_ino):
        os.close(dfd)
        fail(f"{logdir} is not the real directory this handle opened")
    return dfd


if action == "invalidate":
    dfd = open_logdir()
    try:
        for stale in (os.path.basename(HASHES), os.path.basename(PAYLOADS)):
            try:
                os.unlink(stale, dir_fd=dfd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                fail(f"could not invalidate {stale}: {exc.strerror or type(exc).__name__}")
        try:
            os.fsync(dfd)
        except OSError as exc:
            fail(f"could not fsync {logdir}: {exc}")
        if not logdir_still_bound(dfd):
            fail(f"{logdir} was swapped during invalidation")
    finally:
        os.close(dfd)
    print(json.dumps({"ok": True, "action": action}))
    sys.exit(0)

if action == "invalidate-dist":
    # exactly the dist receipt, through the bound handle; assembly receipts are untouched
    dfd = open_logdir()
    try:
        try:
            os.unlink(os.path.basename(DIST_RECEIPT), dir_fd=dfd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            fail(f"could not invalidate {os.path.basename(DIST_RECEIPT)}: {exc.strerror or type(exc).__name__}")
        try:
            os.fsync(dfd)
        except OSError as exc:
            fail(f"could not fsync {logdir}: {exc}")
        if not logdir_still_bound(dfd):
            fail(f"{logdir} was swapped during invalidation")
    finally:
        os.close(dfd)
    print(json.dumps({"ok": True, "action": action}))
    sys.exit(0)

def artifact_lines():
    lines = [HEADER]
    targets = sorted(artifact_set())
    if len(targets) < 3:
        fail(f"expected template + manifest + >=1 asset manifest, found {len(targets)}")
    for path in targets:
        try:
            data, _st = stable_bytes(path)
        except OSError as exc:
            fail(f"reviewed-assembly artifact unreadable: {exc}")
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {path}")
    return lines, len(targets)


def payload_lines():
    payloads = referenced_payloads()
    lines = [HEADER]
    for rel in sorted(payloads):
        try:
            digest, _count = framed_digest(asm, rel, payloads[rel])
        except OSError as exc:
            fail(f"payload {rel}: {exc}")
        lines.append(f"{digest}  {rel}")
    return lines, len(payloads)


if action == "record-all":
    # every validation and read happens BEFORE either receipt is published; then both are published as one owned
    # transaction: if the second publication fails, the first (still ours, verified by dev+inode) is removed and the
    # directory fsynced, so a failed run never leaves a citable half.
    a_lines, n_art = artifact_lines()
    p_lines, n_pay = payload_lines()
    first = publish_receipt(HASHES, a_lines)
    try:
        publish_receipt(PAYLOADS, p_lines)
    except SystemExit:
        dfd = open_logdir()
        try:
            try:
                removed = unpublish(HASHES, first, dfd)
            except OSError as exc:
                print(json.dumps({"ok": False, "action": action, "why": f"payload receipt failed AND the owned artifact receipt could not be rolled back: {exc}"}))
                raise
            if not removed:
                print(json.dumps({"ok": False, "action": action, "why": "payload receipt failed; the artifact receipt at the canonical path was not ours (preserved)"}))
        finally:
            os.close(dfd)
        raise
    print(json.dumps({"ok": True, "action": action, "artifacts": n_art, "payloads": n_pay}))
    sys.exit(0)

if action in ("record-artifacts", "record"):
    fail(f"{action} is refused: receipts are only ever published together by record-all (no callable half-publication)")

if action in ("verify-pre-diff", "verify-pre-deploy"):
    recorded = {os.path.normpath(k): v for k, v in read_receipt(HASHES, header=HEADER).items()}
    expected = artifact_set()
    if set(recorded) != expected:
        fail(f"artifact set differs from the reviewed one: missing={sorted(expected - set(recorded))} extra={sorted(set(recorded) - expected)}")
    for path, hexd in sorted(recorded.items()):
        try:
            data, _st = stable_bytes(path)
        except OSError as exc:
            fail(f"{os.path.basename(path)}: {exc}")
        if hashlib.sha256(data).hexdigest() != hexd:
            fail(f"{os.path.basename(path)} changed since review")
    recorded_payloads = read_receipt(PAYLOADS, header=HEADER)
    now_payloads = referenced_payloads()
    if set(recorded_payloads) != set(now_payloads):
        fail(f"payload set differs from the reviewed one: missing={sorted(set(now_payloads) - set(recorded_payloads))} extra={sorted(set(recorded_payloads) - set(now_payloads))}")
    for rel, hexd in sorted(recorded_payloads.items()):
        try:
            digest, _count = framed_digest(asm, rel, now_payloads[rel])
        except OSError as exc:
            fail(f"payload {rel}: {exc}")
        if digest != hexd:
            fail(f"payload {rel} changed since review")
    print(json.dumps({"ok": True, "action": action, "artifacts": len(recorded), "payloads": len(recorded_payloads)}))
    sys.exit(0)

if action == "record-dist":
    try:
        digest, count = framed_digest(DIST_DIR, "", "dir")
    except OSError as exc:
        fail(f"frontend dist: {exc}")
    if count == 0:
        fail("frontend dist is empty")
    publish_receipt(DIST_RECEIPT, [DIST_HEADER, f"{digest}  dist"])
    print(json.dumps({"ok": True, "action": action, "entries": count}))
    sys.exit(0)

if action == "verify-dist":
    recorded = read_receipt(DIST_RECEIPT, header=DIST_HEADER)
    if set(recorded) != {"dist"}:
        fail("frontend dist receipt does not record exactly the dist payload")
    try:
        digest, _count = framed_digest(DIST_DIR, "", "dir")
    except OSError as exc:
        fail(f"frontend dist: {exc}")
    if digest != recorded["dist"]:
        fail("frontend dist changed since it was built and reviewed")
    print(json.dumps({"ok": True, "action": action}))
    sys.exit(0)

fail(f"unknown action {action!r}")
PY
  then
    log_error "Pinned assembly ${action} FAILED; refusing to continue."
    exit 1
  fi
  log_info "Pinned assembly ${action}: ok."
}

verify_pinned_assembly() {
  # $1 = when (pre-diff | pre-deploy): artifacts + every referenced payload re-verified against both receipts.
  pinned_payload_tool "verify-$1"
}

cdk_diff_pinned() {
  local gate_asm="${PROJECT_ROOT}/infra/cdk.out.preflight"
  local gate_log_dir="${PROJECT_ROOT}/.cdk-gate"
  cd "${PROJECT_ROOT}/infra"
  verify_pinned_assembly pre-diff
  log_info "CDK safety gate 2/3: diff of the pinned assembly vs the deployed stack (recorded)..."
  # Runs on the frozen assembly (no re-synth) and MUST precede deploy. Without
  # --fail, `cdk diff` returns 0 even when there are legitimate changes (a fresh
  # stack, a real update), so those proceed; a NON-ZERO exit means diff itself
  # failed (bad credentials, unreadable assembly, CDK error) and MUST abort
  # before any AWS mutation. Do NOT add `|| true` here — it swallows real
  # failures (proven: a diff exiting 42 still reached deploy).
  # --method template: the diff is computed against the deployed template only; CDK's default (auto) may CREATE a
  # CloudFormation change set, which is an AWS mutation before the pre-deploy gate. Never pass --fail (see above).
  if ! "${CDK_BIN}" diff "${STACK_NAME}" --app "${gate_asm}" --method template > "${gate_log_dir}/diff.log" 2>&1; then
    log_error "cdk diff failed — refusing to deploy. Last lines:"
    tail -40 "${gate_log_dir}/diff.log" >&2
    exit 1
  fi
  log_info "Diff recorded at ${gate_log_dir}/diff.log."

  cd "${PROJECT_ROOT}"
}

cdk_deploy_pinned() {
  local gate_asm="${PROJECT_ROOT}/infra/cdk.out.preflight"
  local gate_log_dir="${PROJECT_ROOT}/.cdk-gate"
  cd "${PROJECT_ROOT}/infra"
  verify_pinned_assembly pre-deploy
  log_info "CDK safety gate 3/3: deploying the reviewed assembly (no re-synth)..."
  "${CDK_BIN}" deploy "${STACK_NAME}" \
    --app "${gate_asm}" \
    --exclusively \
    --require-approval never
  cd "${PROJECT_ROOT}"
  log_success "CDK stack deployed."
}

run_cdk_deploy() {
  cdk_synth_pinned
  cdk_diff_pinned
  cdk_deploy_pinned
}

# ── Step 7: Extract stack outputs ─────────────────────────────────────

extract_stack_outputs() {
  log_info "Extracting stack outputs..."
  API_GATEWAY_URL=$(get_stack_output "ApiGatewayUrl")
  CLOUDFRONT_URL=$(get_stack_output "CloudFrontUrl")
  S3_BUCKET_NAME=$(get_stack_output "S3BucketName")
  USER_POOL_ID=$(get_stack_output "UserPoolId")
  USER_POOL_CLIENT_ID=$(get_stack_output "UserPoolClientId")

  if [[ -z "${CLOUDFRONT_URL}" || -z "${S3_BUCKET_NAME}" ]]; then
    log_error "Failed to extract one or more stack outputs."
    log_error "CloudFront URL: ${CLOUDFRONT_URL:-<empty>}"
    log_error "S3 Bucket:      ${S3_BUCKET_NAME:-<empty>}"
    exit 1
  fi

  # Look up the CloudFront distribution ID for cache invalidation.
  #
  # Match on this stack's own distribution DOMAIN, not on Comment. CloudFront is a
  # global service, so --region does not scope the listing, and every region's
  # distribution carries the identical comment "<project>-<env> distribution"
  # (infra/stacks/platform/cloudfront_waf.py). Deploying a second region therefore
  # returned two tab-separated ids, and create-invalidation failed with
  # NoSuchDistribution — seen for real on the first eu-central-1 deploy while
  # us-east-1 was already live. The DomainName is per-distribution and the
  # CloudFrontUrl output above already carries exactly this stack's.
  local cf_domain
  cf_domain=$(cf_domain_from_url "${CLOUDFRONT_URL}")
  DISTRIBUTION_ID=$(aws cloudfront list-distributions \
    --region "${AWS_REGION}" \
    --query "DistributionList.Items[?DomainName=='${cf_domain}'].Id" \
    --output text 2>/dev/null || echo "")

  log_success "API Gateway URL: ${API_GATEWAY_URL}"
  log_success "CloudFront URL:  ${CLOUDFRONT_URL}"
  log_success "S3 Bucket:       ${S3_BUCKET_NAME}"
}

# ── Step 8: Build frontend ────────────────────────────────────────────

FE_INVENTORY=""
FE_HOMES=()
cleanup_fe_homes() { local d; for d in "${FE_HOMES[@]}"; do rm -rf "${d}"; done; }
frontend_inventory() {  # EXACT digest of the installed frontend dependency tree: every entry framed with its type, mode
  # and link count (bulk stat), every regular file's content digest, every symlink's target; a regular file with more
  # than one link refuses the derivation (rc 70). Build caches the gates write inside node_modules are pruned.
  ( cd "${PROJECT_ROOT}/frontend" || exit 70
    local -a prune=( \( -path node_modules/.vite -o -path node_modules/.vite-temp -o -path node_modules/.tmp -o -path node_modules/.vitest -o -path node_modules/.cache \) -prune -o )
    local listing
    # a directory's link count is 2 + its subdirectory count, so it moves whenever a PRUNED cache directory (.vite,
    # .tmp, ...) is created inside node_modules by the build; directories are framed without it (files keep theirs)
    listing="$(LC_ALL=C find node_modules "${prune[@]}" \( -type f -o -type l -o -type d \) -print0 | LC_ALL=C sort -z | xargs -0 stat -f '%HT|%Lp|%l|%N' | LC_ALL=C sed -E 's/^(Directory\|[0-7]+\|)[0-9]+\|/\1-|/')" || exit 70
    if grep -E -q '^Regular File\|[0-7]+\|([2-9]|[1-9][0-9]+)\|' <<< "${listing}"; then exit 70; fi
    {
      printf '%s\n' "${listing}"
      LC_ALL=C find node_modules "${prune[@]}" -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256
      LC_ALL=C find node_modules "${prune[@]}" -type l -print0 | LC_ALL=C sort -z | while IFS= read -r -d '' l; do printf 'L %s -> %s\n' "${l}" "$(readlink "${l}")"; done
    } | shasum -a 256 | cut -c1-64 )
}

verify_frontend_inventory() {  # the installed tree must be exactly what npm ci produced, at every later use
  local now; now="$(frontend_inventory)"
  if [[ -z "${FE_INVENTORY}" || "${now}" != "${FE_INVENTORY}" ]]; then
    log_error "frontend dependency inventory changed since install (${1:-check}): ${FE_INVENTORY:0:16} -> ${now:0:16}; refusing."; exit 1
  fi
}

build_frontend() {
  # Use CloudFront URL as the API base — CloudFront routes /api/* to API Gateway
  log_info "Building frontend with VITE_API_BASE_URL=${CLOUDFRONT_URL} ..."
  cd "${PROJECT_ROOT}/frontend"
  # The install and the build run in an EXPLICIT environment: no AWS_* name, no npm user/global config, no startup
  # hooks reach npm or the build (a dependency lifecycle script would otherwise run with live credentials); lifecycle
  # scripts are not run at all (the lock needs none: proven by a disposable credential-free install + production build).
  # npm verifies every package against the lock's integrity hashes; the resulting inventory digest is logged.
  # a PRIVATE empty HOME for npm/vite (nothing discovered through the account home), the npm cache named explicitly,
  # and --offline: every package comes from the cache and is verified against the lock's integrity hashes (a disposable
  # credential-free offline install + production build was proven for this lock).
  # the reinstall must reproduce the PREPARED install byte for byte, modes included: the caller's umask never decides
  # them (a 077 launcher would turn every 0644 into 0600 and the bound inventory would no longer verify)
  umask 022
  local fe_home; fe_home="$(mktemp -d "${TMPDIR:-/tmp}/fe_home.XXXXXX")"; chmod 700 "${fe_home}"
  FE_HOMES+=("${fe_home}"); trap cleanup_fe_homes EXIT   # removed on EVERY exit path (error, signal), not only success
  local -a fe_env=(env -i "PATH=${PATH}" "HOME=${fe_home}" "LANG=${LANG:-C.UTF-8}" CI=1 NPM_CONFIG_USERCONFIG=/dev/null NPM_CONFIG_GLOBALCONFIG=/var/empty/npmrc "NPM_CONFIG_CACHE=${NPM_CONFIG_CACHE:-${HOME}/.npm}")
  "${fe_env[@]}" npm ci --offline --ignore-scripts --no-audit --no-fund --loglevel=error
  if ! FE_INVENTORY="$(frontend_inventory)" || [[ ! "${FE_INVENTORY}" =~ ^[0-9a-f]{64}$ ]]; then
    log_error "the frontend dependency inventory cannot be derived (hard links or unreadable entries); refusing."; exit 1
  fi
  log_info "frontend node inventory ${FE_INVENTORY:0:16} (installed from the cache, npm-verified against package-lock.json)"
  local -a vite_env=("VITE_API_BASE_URL=${CLOUDFRONT_URL}" "VITE_AWS_REGION=${AWS_REGION}" "VITE_COGNITO_USER_POOL_ID=${USER_POOL_ID}" "VITE_COGNITO_CLIENT_ID=${USER_POOL_CLIENT_ID}")
  verify_frontend_inventory pre-build
  "${fe_env[@]}" "${vite_env[@]}" npm run build
  "${fe_env[@]}" "${vite_env[@]}" npm run verify:production-build
  verify_frontend_inventory post-build
  rm -rf "${fe_home}"
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
  # Not "skip quietly": the frontend is already in S3 at this point, so an
  # un-invalidated distribution keeps serving the previous build from its edges and
  # the deploy looks successful while the browser shows stale code.
  if [[ -z "${DISTRIBUTION_ID}" || "${DISTRIBUTION_ID}" == "None" ]]; then
    log_error "Could not resolve a CloudFront distribution for ${CLOUDFRONT_URL} — cache NOT invalidated."
    INVALIDATION_FAILED=1
    return
  fi
  if [[ "${DISTRIBUTION_ID}" == *[[:space:]]* ]]; then
    log_error "CloudFront lookup was ambiguous (${DISTRIBUTION_ID}) — cache NOT invalidated."
    INVALIDATION_FAILED=1
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
  echo "  No Docker, no ECS, no ALB, no NAT gateway. (One dedicated, isolated tool-sandbox VPC"
  echo "  with a CloudWatch Logs interface endpoint IS created — it consumes a VPC quota slot.)"
  echo ""
  echo "=============================================="
}

# ── Main ──────────────────────────────────────────────────────────────

preflight_vpc_capacity() {
  # The platform unconditionally creates a dedicated, isolated tool-sandbox VPC
  # (ToolSandboxVpc), so a stack that does not yet OWN one needs a free VPC slot
  # in the region, and the default quota is 5. Fail here -- before dependency
  # builds, bootstrap and any CloudFormation mutation -- instead of ~15 minutes
  # later inside CREATE_IN_PROGRESS, where a failed first create also cancels
  # 300 siblings. "Owns one" is proven by an AWS::EC2::VPC resource in a
  # *_COMPLETE state on the existing stack -- NOT by the stack merely existing:
  # ROLLBACK_COMPLETE, REVIEW_IN_PROGRESS and legacy stacks exist without one.
  # Fail-closed: if the count, the quota or the stack's resources cannot be read
  # we refuse, because proceeding is exactly the late failure this gate exists
  # to prevent. The only bypass is the explicit, logged SKIP_VPC_PREFLIGHT=true.
  # Honest limit: this is a point-in-time read; another creator can take the
  # last slot between this check and CloudFormation's CreateVpc, so
  # CloudFormation stays the authority -- this only fails early and clearly.
  # Limit source: Service Quotas L-F678F1CE ("VPCs per Region"); EC2
  # describe-account-attributes carries no valid VPC limit.
  if [[ "${SKIP_VPC_PREFLIGHT:-false}" == "true" ]]; then
    log_warning "SKIP_VPC_PREFLIGHT=true: VPC capacity preflight BYPASSED by explicit request; a fresh stack may fail at ToolSandboxVpc."
    return 0
  fi
  log_info "Preflight: VPC capacity in ${AWS_REGION} (the platform creates a dedicated tool-sandbox VPC)..."
  local int_re='^[0-9]+$' num_re='^[0-9]+(\.[0-9]+)?$'
  # Stack existence: ONLY the exact "does not exist" ValidationError means absent.
  # Any other failure (AccessDenied, throttling, network) is unknown -> fail closed,
  # never "absent" (which would route a real stack into the fresh-stack path).
  local stack_status stack_err
  stack_err="${gate_log_dir:-${PROJECT_ROOT}/.cdk-gate}/preflight-describe-stacks.err"; mkdir -p "$(dirname "${stack_err}")"
  if stack_status=$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
                      --query 'Stacks[0].StackStatus' --output text 2>"${stack_err}"); then
    :
  elif grep -q "(ValidationError).*Stack with id ${STACK_NAME} does not exist" "${stack_err}"; then
    stack_status="ABSENT"
  else
    log_error "Could not determine whether stack '${STACK_NAME}' exists (cloudformation:DescribeStacks failed):"
    sed -n '1,3p' "${stack_err}" >&2
    log_error "Refusing to continue: an unknown stack state must not be treated as 'absent'."
    exit 1
  fi
  if [[ "${stack_status}" != "ABSENT" ]]; then
    # Existing stack: skip ONLY if it demonstrably owns a VPC already. The CLI
    # paginates ListStackResources and applies --query PER PAGE (measured on a
    # 296-resource stack: `length(...)` printed 0, 0, 1 on three lines), so ask
    # for physical ids and count the vpc-* lines across all pages.
    local vpc_ids owned_vpcs
    if ! vpc_ids=$(aws cloudformation list-stack-resources --stack-name "${STACK_NAME}" --region "${AWS_REGION}" \
                     --query "StackResourceSummaries[?ResourceType=='AWS::EC2::VPC' && (ResourceStatus=='CREATE_COMPLETE' || ResourceStatus=='UPDATE_COMPLETE')].PhysicalResourceId" \
                     --output text 2>/dev/null); then
      log_error "Stack '${STACK_NAME}' is ${stack_status} but its resources could not be listed (cloudformation:ListStackResources); refusing to continue."
      exit 1
    fi
    owned_vpcs=$(printf '%s\n' "${vpc_ids}" | grep -c '^vpc-' || true)
    if (( owned_vpcs >= 1 )); then
      log_info "Stack '${STACK_NAME}' (${stack_status}) already owns its VPC (${owned_vpcs}); capacity check not applicable."
      return 0
    fi
    log_info "Stack '${STACK_NAME}' is ${stack_status} with NO VPC of its own; this deploy must create one."
  fi
  local vpc_count quota
  vpc_count=$(aws ec2 describe-vpcs --region "${AWS_REGION}" --query 'length(Vpcs)' --output text 2>/dev/null || echo "")
  quota=$(aws service-quotas get-service-quota --service-code vpc --quota-code L-F678F1CE \
            --region "${AWS_REGION}" --query 'Quota.Value' --output text 2>/dev/null || echo "")
  if [[ ! "${vpc_count}" =~ ${int_re} || ! "${quota}" =~ ${num_re} ]]; then
    log_error "Cannot read VPC capacity (count='${vpc_count}', quota='${quota}'); refusing to continue."
    log_error "  Required: ec2:DescribeVpcs and servicequotas:GetServiceQuota in ${AWS_REGION}."
    log_error "  If you have verified capacity another way, re-run with SKIP_VPC_PREFLIGHT=true (logged)."
    exit 1
  fi
  local quota_int="${quota%.*}"
  if (( vpc_count >= quota_int )); then
    log_error "No free VPC slot in ${AWS_REGION}: ${vpc_count}/${quota_int} VPCs in use (Service Quotas L-F678F1CE)."
    log_error "'${STACK_NAME}' would fail at ToolSandboxVpc and roll back. Remediation: request an increase"
    log_error "  aws service-quotas request-service-quota-increase --service-code vpc --quota-code L-F678F1CE --desired-value <N> --region ${AWS_REGION}"
    log_error "  or delete a VPC you own, then re-run. No AWS resource was created or modified by this run."
    exit 1
  fi
  log_success "VPC capacity OK: ${vpc_count}/${quota_int} in use in ${AWS_REGION}."
}

main() {
  log_info "Starting serverless deployment of ${PROJECT_NAME} (${ENVIRONMENT_NAME}) to ${AWS_REGION}"

  if [[ "${CERTIFIED_DEPS}" == "1" ]]; then
    # Certified mode runs ZERO installers: the Python environments are the certifier-built, lock-verified ones
    # (CDK_PYTHON names the retained certified infra interpreter) and the pinned CDK CLI is only VERIFIED, never
    # installed; any dependency mutation here would invalidate the very inventory the gates certify. The first gate
    # runs BEFORE anything else executes on that interpreter: the gate itself is the stdlib-only -I -S -B bootstrap,
    # and it re-derives the interpreter's hash, both environments and the CDK toolchain inventory from the artifact.
    require_certified_interpreter
    certified_deps_gate bootstrap
    check_prerequisites
    check_aws_credentials
    preflight_vpc_capacity
    verify_cdk_cli_pinned
    # F-G03-003: no AWS mutation before the strict synth and its post-synth identity check have passed, and the
    # frozen identity is re-derived immediately before each mutation boundary. bootstrap_cdk can create the
    # CDKToolkit stack and preflight_restore_tables can recreate tables, so both sit AFTER the synth gate here.
    install_or_verify_dependencies      # gate pre-install; installers skipped
    preserve_existing_cognito_users     # READ-ONLY, fail-closed; must precede the synth that bakes COGNITO_USERS in
    cdk_synth_pinned                    # gate pre-synth, strict synth, gate post-synth, digests
    certified_deps_gate pre-bootstrap
    bootstrap_cdk
    certified_deps_gate pre-restore
    preflight_restore_tables
    cdk_diff_pinned                     # verifies the pinned assembly, then diff
    certified_deps_gate pre-deploy
    cdk_deploy_pinned
    certified_deps_gate post-deploy     # the long CDK deployment is over: the source must not have moved meanwhile
    extract_stack_outputs
    certified_deps_gate pre-frontend-build
    pinned_payload_tool invalidate-dist # a stale dist receipt from an earlier deploy can never be cited
    build_frontend
    pinned_payload_tool record-dist     # the built dist is reviewed as a payload ...
    certified_deps_gate pre-sync
    pinned_payload_tool verify-dist     # ... and re-verified immediately before it is uploaded
    verify_frontend_inventory pre-sync
    upload_frontend_to_s3
    certified_deps_gate pre-invalidate
    pinned_payload_tool verify-dist
    invalidate_cloudfront_cache
  else
    check_prerequisites
    check_aws_credentials
    preflight_vpc_capacity
    install_cdk_dependencies
    install_backend_dependencies
    install_or_verify_dependencies
    bootstrap_cdk
    preflight_restore_tables
    preserve_existing_cognito_users
    run_cdk_deploy
    extract_stack_outputs
    build_frontend
    upload_frontend_to_s3
    invalidate_cloudfront_cache
  fi
  print_summary

  if [[ "${INVALIDATION_FAILED}" == "1" ]]; then
    log_error "Deployment finished, but the CloudFront cache was NOT invalidated (see above)."
    log_error "The edges may still serve the previous frontend build. Re-run, or invalidate manually."
    exit 1
  fi
}

# Sourcing this file (tests do) defines the functions without running the deployment.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
