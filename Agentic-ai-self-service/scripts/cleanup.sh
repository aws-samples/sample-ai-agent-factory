#!/usr/bin/env bash
# Cleanup script for the AgentCore Visual Workflow Platform (Serverless).
#
# Tears down all AWS resources — both CDK-managed and dynamically created:
#   1. Check prerequisites (AWS CLI)
#   2. Validate AWS credentials
#   3. Check if the stack exists
#   4. Clean up dynamically-created deployment resources (runtimes, gateways, etc.)
#   5. Sweep for orphaned AgentCore-* resources
#   6. Empty S3 buckets
#   7. Run cdk destroy --force
#   8. Verify all resources are removed
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

get_stack_output() {
  local output_key="$1"
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='${output_key}'].OutputValue" \
    --output text 2>/dev/null || echo ""
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

  # Check Node.js (needed for npx cdk destroy)
  if ! command -v node &> /dev/null; then
    log_error "Node.js is not installed. Required for CDK destroy. Please install Node.js (v18+)."
    missing=1
  else
    log_success "Node.js $(node --version) is available."
  fi

  # Check npx (needed for cdk destroy)
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
  if ! aws sts get-caller-identity --region "${AWS_REGION}" > /dev/null 2>&1; then
    log_error "AWS credentials are not configured or are invalid."
    log_error "Please configure credentials with 'aws configure' or set AWS_PROFILE."
    exit 1
  fi
  local account_id
  account_id=$(aws sts get-caller-identity --region "${AWS_REGION}" --query "Account" --output text)
  log_success "Authenticated to AWS account: ${account_id}"
}

# ── Step 3: Check stack exists ────────────────────────────────────────

check_stack_exists() {
  log_info "Checking if stack '${STACK_NAME}' exists in region '${AWS_REGION}'..."
  if ! aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}" > /dev/null 2>&1; then
    log_warn "Stack '${STACK_NAME}' does not exist or has already been deleted."
    log_warn "Skipping to orphan resource sweep..."
    # Still sweep for orphaned resources even if stack is gone
    sweep_orphan_resources
    log_success "Cleanup complete (stack was already deleted)."
    exit 0
  fi
  log_success "Stack '${STACK_NAME}' found."
}

# ── Step 4: Clean up dynamically-created deployment resources ────────

cleanup_deployment_resources() {
  local table_name="${PROJECT_NAME}-${ENVIRONMENT_NAME}-deployments"

  log_info "Scanning deployments table '${table_name}' for active resources..."

  # Check if table exists
  if ! aws dynamodb describe-table \
    --table-name "${table_name}" \
    --region "${AWS_REGION}" > /dev/null 2>&1; then
    log_warn "Deployments table '${table_name}' not found. Skipping per-deployment cleanup."
    return
  fi

  # Scan for all deployment records with runtime_id or mcp_server_runtime_id
  local scan_result
  scan_result=$(aws dynamodb scan \
    --table-name "${table_name}" \
    --region "${AWS_REGION}" \
    --projection-expression "deployment_id, runtime_id, mcp_server_runtime_id, gateway_result, policy_result, memory_result, guardrails_result, knowledge_base_result" \
    --output json 2>/dev/null || echo '{"Items":[]}')

  local count
  count=$(echo "${scan_result}" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('Items',[])))" 2>/dev/null || echo "0")

  if [[ "${count}" == "0" ]]; then
    log_info "No deployment records found."
    return
  fi

  log_info "Found ${count} deployment record(s). Cleaning up resources..."

  # Process each deployment record
  echo "${scan_result}" | python3 -c "
import sys, json

data = json.load(sys.stdin)
items = data.get('Items', [])

def ddb_str(item, key):
    v = item.get(key, {})
    return v.get('S', '')

def ddb_map(item, key):
    \"\"\"Extract a DynamoDB map to a flat dict of string values.\"\"\"
    v = item.get(key, {})
    m = v.get('M', {})
    result = {}
    for k, val in m.items():
        if 'S' in val:
            result[k] = val['S']
        elif 'BOOL' in val:
            result[k] = str(val['BOOL']).lower()
    return result

def ddb_str_list(mapval, key):
    \"\"\"Extract a DynamoDB string-list (L of S) nested under a map key.\"\"\"
    v = mapval.get(key, {})
    return [e.get('S', '') for e in v.get('L', []) if e.get('S')]

for item in items:
    dep_id = ddb_str(item, 'deployment_id')
    runtime_id = ddb_str(item, 'runtime_id')
    mcp_id = ddb_str(item, 'mcp_server_runtime_id')
    gw = ddb_map(item, 'gateway_result')
    gw_raw = item.get('gateway_result', {}).get('M', {})
    connector_providers = ddb_str_list(gw_raw, 'connector_credential_providers')
    connector_secrets = ddb_str_list(gw_raw, 'connector_secret_arns')
    policy = ddb_map(item, 'policy_result')
    memory = ddb_map(item, 'memory_result')
    guardrails = ddb_map(item, 'guardrails_result')
    kb = ddb_map(item, 'knowledge_base_result')

    print(json.dumps({
        'deployment_id': dep_id,
        'runtime_id': runtime_id,
        'mcp_server_runtime_id': mcp_id,
        'gateway_id': gw.get('gateway_id', ''),
        'connector_credential_providers': connector_providers,
        'connector_secret_arns': connector_secrets,
        'policy_engine_id': policy.get('engine_id', ''),
        'memory_id': memory.get('memory_id', ''),
        'guardrail_id': guardrails.get('guardrail_id', ''),
        'guardrail_created_by_flow': guardrails.get('created_by_flow', 'false'),
        'kb_id': kb.get('kb_id', ''),
        'kb_data_source_id': kb.get('data_source_id', ''),
        'kb_created_by_flow': kb.get('created_by_flow', 'false'),
    }))
" 2>/dev/null | while IFS= read -r dep_json; do
    local dep_id runtime_id mcp_id gw_id policy_id memory_id guardrail_id kb_id

    dep_id=$(echo "${dep_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('deployment_id',''))" 2>/dev/null)
    runtime_id=$(echo "${dep_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('runtime_id',''))" 2>/dev/null)
    mcp_id=$(echo "${dep_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mcp_server_runtime_id',''))" 2>/dev/null)
    gw_id=$(echo "${dep_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('gateway_id',''))" 2>/dev/null)
    policy_id=$(echo "${dep_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('policy_engine_id',''))" 2>/dev/null)
    memory_id=$(echo "${dep_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('memory_id',''))" 2>/dev/null)
    guardrail_id=$(echo "${dep_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('guardrail_id',''))" 2>/dev/null)
    kb_id=$(echo "${dep_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('kb_id',''))" 2>/dev/null)

    log_info "  Cleaning deployment: ${dep_id:-unknown}"

    # Delete MCP server runtime + its execution role (same pattern as the
    # main runtime below).
    if [[ -n "${mcp_id}" ]]; then
      local mcp_role_arn=""
      mcp_role_arn=$(aws bedrock-agentcore-control get-agent-runtime \
        --agent-runtime-id "${mcp_id}" --region "${AWS_REGION}" \
        --query 'roleArn' --output text 2>/dev/null || echo "")
      log_info "    Deleting MCP server runtime: ${mcp_id}"
      aws bedrock-agentcore-control delete-agent-runtime \
        --agent-runtime-id "${mcp_id}" --region "${AWS_REGION}" 2>/dev/null || true

      if [[ -n "${mcp_role_arn}" && "${mcp_role_arn}" != "None" ]]; then
        local mcp_role_name="${mcp_role_arn##*/}"
        if [[ -n "${mcp_role_name}" ]]; then
          log_info "    Deleting MCP runtime execution role: ${mcp_role_name}"
          local mcp_managed mcp_inline
          mcp_managed=$(aws iam list-attached-role-policies \
            --role-name "${mcp_role_name}" \
            --query "AttachedPolicies[].PolicyArn" \
            --output text 2>/dev/null || echo "")
          for pol_arn in ${mcp_managed}; do
            aws iam detach-role-policy \
              --role-name "${mcp_role_name}" --policy-arn "${pol_arn}" 2>/dev/null || true
          done
          mcp_inline=$(aws iam list-role-policies \
            --role-name "${mcp_role_name}" \
            --query "PolicyNames[]" \
            --output text 2>/dev/null || echo "")
          for pol_name in ${mcp_inline}; do
            aws iam delete-role-policy \
              --role-name "${mcp_role_name}" --policy-name "${pol_name}" 2>/dev/null || true
          done
          aws iam delete-role --role-name "${mcp_role_name}" 2>/dev/null || true
        fi
      fi
    fi

    # Delete policy engine (detach + delete policies + delete engine)
    if [[ -n "${policy_id}" ]]; then
      log_info "    Deleting policy engine: ${policy_id}"
      # Delete policies first
      local pol_list
      pol_list=$(aws bedrock-agentcore-control list-policies \
        --policy-engine-id "${policy_id}" --region "${AWS_REGION}" \
        --query "policies[].policyId" --output text 2>/dev/null || echo "")
      for pol_id in ${pol_list}; do
        aws bedrock-agentcore-control delete-policy \
          --policy-engine-id "${policy_id}" --policy-id "${pol_id}" \
          --region "${AWS_REGION}" 2>/dev/null || true
      done
      sleep 3
      aws bedrock-agentcore-control delete-policy-engine \
        --policy-engine-id "${policy_id}" --region "${AWS_REGION}" 2>/dev/null || true
    fi

    # Delete memory
    if [[ -n "${memory_id}" ]]; then
      log_info "    Deleting memory: ${memory_id}"
      aws bedrock-agentcore-control delete-memory \
        --memory-id "${memory_id}" --region "${AWS_REGION}" 2>/dev/null || true
    fi

    # Delete guardrail (only if we created it)
    local gr_created
    gr_created=$(echo "${dep_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('guardrail_created_by_flow','false'))" 2>/dev/null)
    if [[ -n "${guardrail_id}" && "${gr_created}" == "true" ]]; then
      log_info "    Deleting guardrail: ${guardrail_id}"
      aws bedrock delete-guardrail \
        --guardrail-identifier "${guardrail_id}" --region "${AWS_REGION}" 2>/dev/null || true
    fi

    # Delete gateway (targets first, then gateway)
    if [[ -n "${gw_id}" ]]; then
      log_info "    Deleting gateway: ${gw_id}"
      local target_list
      target_list=$(aws bedrock-agentcore-control list-gateway-targets \
        --gateway-identifier "${gw_id}" --region "${AWS_REGION}" \
        --query "gatewayTargetSummaries[].targetId" --output text 2>/dev/null || echo "")
      for tid in ${target_list}; do
        aws bedrock-agentcore-control delete-gateway-target \
          --gateway-identifier "${gw_id}" --target-id "${tid}" \
          --region "${AWS_REGION}" 2>/dev/null || true
      done
      sleep 3
      aws bedrock-agentcore-control delete-gateway \
        --gateway-identifier "${gw_id}" --region "${AWS_REGION}" 2>/dev/null || true
    fi

    # Delete connector credential providers + secrets recorded in the
    # gateway_result (mirrors cleanup_gateway_resources on the Lambda delete
    # path). Provider NAMES and secret ARNs are persisted so teardown can
    # reach them even after the gateway is gone.
    local conn_providers conn_secrets
    conn_providers=$(echo "${dep_json}" | python3 -c "import sys,json; print(' '.join(json.load(sys.stdin).get('connector_credential_providers',[])))" 2>/dev/null)
    conn_secrets=$(echo "${dep_json}" | python3 -c "import sys,json; print(' '.join(json.load(sys.stdin).get('connector_secret_arns',[])))" 2>/dev/null)
    for cp_name in ${conn_providers}; do
      log_info "    Deleting connector credential provider: ${cp_name}"
      # Provider may be either API-key or OAuth2 — try both (the wrong one is a no-op).
      aws bedrock-agentcore-control delete-api-key-credential-provider \
        --name "${cp_name}" --region "${AWS_REGION}" 2>/dev/null || true
      aws bedrock-agentcore-control delete-oauth2-credential-provider \
        --name "${cp_name}" --region "${AWS_REGION}" 2>/dev/null || true
    done
    for c_arn in ${conn_secrets}; do
      log_info "    Deleting connector secret: ${c_arn}"
      aws secretsmanager delete-secret \
        --secret-id "${c_arn}" --force-delete-without-recovery \
        --region "${AWS_REGION}" 2>/dev/null || true
    done

    # Delete knowledge base (if we created it)
    local kb_created kb_ds_id
    kb_created=$(echo "${dep_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('kb_created_by_flow','false'))" 2>/dev/null)
    kb_ds_id=$(echo "${dep_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('kb_data_source_id',''))" 2>/dev/null)
    if [[ -n "${kb_id}" && "${kb_created}" == "true" ]]; then
      log_info "    Deleting knowledge base: ${kb_id}"
      if [[ -n "${kb_ds_id}" ]]; then
        aws bedrock-agent delete-data-source \
          --knowledge-base-id "${kb_id}" --data-source-id "${kb_ds_id}" \
          --region "${AWS_REGION}" 2>/dev/null || true
      fi
      aws bedrock-agent delete-knowledge-base \
        --knowledge-base-id "${kb_id}" --region "${AWS_REGION}" 2>/dev/null || true
    fi

    # Delete agent runtime + its execution role.
    # We capture the role ARN BEFORE deleting the runtime because
    # get-agent-runtime fails on a deleted runtime, leaving the role orphaned.
    # Verified 2026-05-15 — orphan AgentCore* roles accumulated until tester
    # manually purged them. See tasks/lessons.md Bug 25.
    if [[ -n "${runtime_id}" ]]; then
      local runtime_role_arn=""
      runtime_role_arn=$(aws bedrock-agentcore-control get-agent-runtime \
        --agent-runtime-id "${runtime_id}" --region "${AWS_REGION}" \
        --query 'roleArn' --output text 2>/dev/null || echo "")
      log_info "    Deleting agent runtime: ${runtime_id}"
      aws bedrock-agentcore-control delete-agent-runtime \
        --agent-runtime-id "${runtime_id}" --region "${AWS_REGION}" 2>/dev/null || true

      if [[ -n "${runtime_role_arn}" && "${runtime_role_arn}" != "None" ]]; then
        # roleArn format: arn:aws:iam::<acct>:role/<RoleName>
        local runtime_role_name="${runtime_role_arn##*/}"
        if [[ -n "${runtime_role_name}" ]]; then
          log_info "    Deleting runtime execution role: ${runtime_role_name}"
          # Detach managed policies
          local managed_pols
          managed_pols=$(aws iam list-attached-role-policies \
            --role-name "${runtime_role_name}" \
            --query "AttachedPolicies[].PolicyArn" \
            --output text 2>/dev/null || echo "")
          for pol_arn in ${managed_pols}; do
            aws iam detach-role-policy \
              --role-name "${runtime_role_name}" --policy-arn "${pol_arn}" 2>/dev/null || true
          done
          # Delete inline policies
          local inline_pols
          inline_pols=$(aws iam list-role-policies \
            --role-name "${runtime_role_name}" \
            --query "PolicyNames[]" \
            --output text 2>/dev/null || echo "")
          for pol_name in ${inline_pols}; do
            aws iam delete-role-policy \
              --role-name "${runtime_role_name}" --policy-name "${pol_name}" 2>/dev/null || true
          done
          aws iam delete-role --role-name "${runtime_role_name}" 2>/dev/null || true
        fi
      fi
    fi
  done

  log_success "Per-deployment cleanup complete."
}

# ── Step 5: Sweep for orphaned AgentCore-* resources ─────────────────

sweep_orphan_resources() {
  log_info "Sweeping for orphaned AgentCore-* resources owned by this stack..."
  # Filters use the stack's PROJECT_NAME prefix so sweeps cannot delete
  # AgentCore resources owned by other stacks/users in this account.
  # Verified 2026-05-15 — the broader `AgentCore*` filter wiped a foreign
  # runtime's IAM role; see tasks/lessons.md Bug 20.

  # 1. AgentCoreRuntime-${PROJECT_NAME}-* Lambda functions only
  local lambdas
  lambdas=$(aws lambda list-functions \
    --region "${AWS_REGION}" \
    --query "Functions[?starts_with(FunctionName, 'AgentCoreRuntime-${PROJECT_NAME}')].FunctionName" \
    --output text 2>/dev/null || echo "")
  for fn in ${lambdas}; do
    log_info "  Deleting orphan Lambda: ${fn}"
    aws lambda delete-function --function-name "${fn}" --region "${AWS_REGION}" 2>/dev/null || true
  done

  # 2. AgentCoreRuntime-${PROJECT_NAME}-* IAM roles only (detach policies first)
  local roles
  roles=$(aws iam list-roles \
    --query "Roles[?starts_with(RoleName, 'AgentCoreRuntime-${PROJECT_NAME}')].RoleName" \
    --output text 2>/dev/null || echo "")
  for role_name in ${roles}; do
    log_info "  Deleting orphan IAM role: ${role_name}"
    # Detach managed policies
    local policies
    policies=$(aws iam list-attached-role-policies \
      --role-name "${role_name}" \
      --query "AttachedPolicies[].PolicyArn" \
      --output text 2>/dev/null || echo "")
    for policy_arn in ${policies}; do
      aws iam detach-role-policy --role-name "${role_name}" --policy-arn "${policy_arn}" 2>/dev/null || true
    done
    # Delete inline policies
    local inline_policies
    inline_policies=$(aws iam list-role-policies \
      --role-name "${role_name}" \
      --query "PolicyNames[]" \
      --output text 2>/dev/null || echo "")
    for policy_name in ${inline_policies}; do
      aws iam delete-role-policy --role-name "${role_name}" --policy-name "${policy_name}" 2>/dev/null || true
    done
    aws iam delete-role --role-name "${role_name}" 2>/dev/null || true
  done

  # 3. Bedrock AgentCore runtimes — only those whose name starts with the
  #    project's runtime prefix used by direct deploys / SFN deploys.
  #    Set CLEANUP_INCLUDE_FOREIGN_RUNTIMES=1 to opt back in to the broad sweep.
  local runtimes
  if [[ "${CLEANUP_INCLUDE_FOREIGN_RUNTIMES:-0}" == "1" ]]; then
    log_warn "  CLEANUP_INCLUDE_FOREIGN_RUNTIMES=1 set — sweeping ALL runtimes in account"
    runtimes=$(aws bedrock-agentcore-control list-agent-runtimes \
      --region "${AWS_REGION}" \
      --query "agentRuntimeSummaries[].agentRuntimeId" \
      --output text 2>/dev/null || echo "")
  else
    # Match runtimes whose name starts with the deployment table's prefix.
    # Direct-deploy uses raw runtime_config.name, SFN uses sanitize_runtime_name(...).
    # Without a hard prefix, skip the sweep — per-deployment cleanup above already ran.
    runtimes=""
  fi
  for rt_id in ${runtimes}; do
    log_info "  Deleting orphan runtime: ${rt_id}"
    aws bedrock-agentcore-control delete-agent-runtime \
      --agent-runtime-id "${rt_id}" --region "${AWS_REGION}" 2>/dev/null || true
  done

  # 4. Bedrock Gateways — same opt-in gate.
  local gateways
  if [[ "${CLEANUP_INCLUDE_FOREIGN_RUNTIMES:-0}" == "1" ]]; then
    gateways=$(aws bedrock-agentcore-control list-gateways \
      --region "${AWS_REGION}" \
      --query "gatewaySummaries[].gatewayId" \
      --output text 2>/dev/null || echo "")
  else
    gateways=""
  fi
  for gw_id in ${gateways}; do
    log_info "  Deleting orphan gateway: ${gw_id}"
    local targets
    targets=$(aws bedrock-agentcore-control list-gateway-targets \
      --gateway-identifier "${gw_id}" --region "${AWS_REGION}" \
      --query "gatewayTargetSummaries[].targetId" \
      --output text 2>/dev/null || echo "")
    for tid in ${targets}; do
      aws bedrock-agentcore-control delete-gateway-target \
        --gateway-identifier "${gw_id}" --target-id "${tid}" \
        --region "${AWS_REGION}" 2>/dev/null || true
    done
    sleep 3
    aws bedrock-agentcore-control delete-gateway \
      --gateway-identifier "${gw_id}" --region "${AWS_REGION}" 2>/dev/null || true
  done

  # 5-7: OAuth2 providers / memories / policy engines — opt-in only.
  # Per-deployment cleanup already targets these by ID; the unbounded sweep
  # below is destructive in shared accounts. Set CLEANUP_INCLUDE_FOREIGN_RUNTIMES=1
  # to enable.
  local cred_providers memories engines
  if [[ "${CLEANUP_INCLUDE_FOREIGN_RUNTIMES:-0}" == "1" ]]; then
    cred_providers=$(aws bedrock-agentcore-control list-oauth2-credential-providers \
      --region "${AWS_REGION}" \
      --query "oauth2CredentialProviders[].name" \
      --output text 2>/dev/null || echo "")
    memories=$(aws bedrock-agentcore-control list-memories \
      --region "${AWS_REGION}" \
      --query "memorySummaries[].memoryId" \
      --output text 2>/dev/null || echo "")
    engines=$(aws bedrock-agentcore-control list-policy-engines \
      --region "${AWS_REGION}" \
      --query "policyEngineSummaries[].policyEngineId" \
      --output text 2>/dev/null || echo "")
  else
    cred_providers=""
    memories=""
    engines=""
  fi
  for cp_name in ${cred_providers}; do
    log_info "  Deleting orphan OAuth2 credential provider: ${cp_name}"
    aws bedrock-agentcore-control delete-oauth2-credential-provider \
      --name "${cp_name}" --region "${AWS_REGION}" 2>/dev/null || true
  done
  for mem_id in ${memories}; do
    log_info "  Deleting orphan memory: ${mem_id}"
    aws bedrock-agentcore-control delete-memory \
      --memory-id "${mem_id}" --region "${AWS_REGION}" 2>/dev/null || true
  done
  for eng_id in ${engines}; do
    log_info "  Deleting orphan policy engine: ${eng_id}"
    local eng_policies
    eng_policies=$(aws bedrock-agentcore-control list-policies \
      --policy-engine-id "${eng_id}" --region "${AWS_REGION}" \
      --query "policies[].policyId" \
      --output text 2>/dev/null || echo "")
    for pol_id in ${eng_policies}; do
      aws bedrock-agentcore-control delete-policy \
        --policy-engine-id "${eng_id}" --policy-id "${pol_id}" \
        --region "${AWS_REGION}" 2>/dev/null || true
    done
    sleep 3
    aws bedrock-agentcore-control delete-policy-engine \
      --policy-engine-id "${eng_id}" --region "${AWS_REGION}" 2>/dev/null || true
  done

  # 8. Cognito user pools created by gateway deployments (AgentCore-* pattern)
  # Loop to handle pagination (list-user-pools returns max 60 at a time)
  while true; do
    local pools
    pools=$(aws cognito-idp list-user-pools --max-results 60 --region "${AWS_REGION}" \
      --query "UserPools[?starts_with(Name, 'AgentCore')].Id" \
      --output text 2>/dev/null || echo "")
    if [[ -z "${pools}" ]]; then
      break
    fi
    for pool_id in ${pools}; do
      log_info "  Deleting orphan Cognito user pool: ${pool_id}"
      # Delete domain first (required before pool deletion)
      local domain
      domain=$(aws cognito-idp describe-user-pool --user-pool-id "${pool_id}" --region "${AWS_REGION}" \
        --query "UserPool.Domain" --output text 2>/dev/null || echo "")
      if [[ -n "${domain}" && "${domain}" != "None" ]]; then
        aws cognito-idp delete-user-pool-domain \
          --user-pool-id "${pool_id}" --domain "${domain}" --region "${AWS_REGION}" 2>/dev/null || true
      fi
      aws cognito-idp delete-user-pool \
        --user-pool-id "${pool_id}" --region "${AWS_REGION}" 2>/dev/null || true
    done
  done

  # 9. OTEL auth-header secrets created by /api/observability/credentials.
  #    Sweeps only per-agent secrets created by POST /api/observability/credentials
  #    (provider-prefixed: agentcore-otel/langfuse/* or agentcore-otel/custom/*).
  #    Explicitly EXCLUDES the admin-managed platform secret at
  #    agentcore-otel/platform/* — that secret outlives any individual stack
  #    by design (see scripts/bootstrap-otel-secret.sh header).
  #    Verified 2026-05-15: cleanup.sh used to delete the platform secret
  #    silently, breaking the next deploy. See tasks/lessons.md Bug 24.
  local otel_secrets
  otel_secrets=$(aws secretsmanager list-secrets --region "${AWS_REGION}" \
    --query "SecretList[?starts_with(Name, 'agentcore-otel/') && !starts_with(Name, 'agentcore-otel/platform/')].ARN" \
    --output text 2>/dev/null || echo "")
  for s_arn in ${otel_secrets}; do
    log_info "  Deleting orphan per-agent OTEL secret: ${s_arn}"
    aws secretsmanager delete-secret \
      --secret-id "${s_arn}" --force-delete-without-recovery \
      --region "${AWS_REGION}" 2>/dev/null || true
  done

  # 10. SaaS connector secrets minted by the gateway step / direct deploy.
  #     Owner-scoped naming: agentcore-connector/{owner}/{uuid}. These hold the
  #     raw API key / OAuth client secret, so sweep any that survived a
  #     partial-failed deploy whose deployment record never landed.
  local connector_secrets
  connector_secrets=$(aws secretsmanager list-secrets --region "${AWS_REGION}" \
    --query "SecretList[?starts_with(Name, 'agentcore-connector/')].ARN" \
    --output text 2>/dev/null || echo "")
  for s_arn in ${connector_secrets}; do
    log_info "  Deleting orphan connector secret: ${s_arn}"
    aws secretsmanager delete-secret \
      --secret-id "${s_arn}" --force-delete-without-recovery \
      --region "${AWS_REGION}" 2>/dev/null || true
  done

  # 11. SaaS connector credential providers (acc- name prefix) — opt-in only,
  #     same shared-account guard as the OAuth2/memory/engine sweep above.
  if [[ "${CLEANUP_INCLUDE_FOREIGN_RUNTIMES:-0}" == "1" ]]; then
    local acc_api_providers acc_oauth_providers
    acc_api_providers=$(aws bedrock-agentcore-control list-api-key-credential-providers \
      --region "${AWS_REGION}" \
      --query "apiKeyCredentialProviders[?starts_with(name, 'acc-')].name" \
      --output text 2>/dev/null || echo "")
    acc_oauth_providers=$(aws bedrock-agentcore-control list-oauth2-credential-providers \
      --region "${AWS_REGION}" \
      --query "oauth2CredentialProviders[?starts_with(name, 'acc-')].name" \
      --output text 2>/dev/null || echo "")
    for cp_name in ${acc_api_providers}; do
      log_info "  Deleting orphan connector API-key credential provider: ${cp_name}"
      aws bedrock-agentcore-control delete-api-key-credential-provider \
        --name "${cp_name}" --region "${AWS_REGION}" 2>/dev/null || true
    done
    for cp_name in ${acc_oauth_providers}; do
      log_info "  Deleting orphan connector OAuth2 credential provider: ${cp_name}"
      aws bedrock-agentcore-control delete-oauth2-credential-provider \
        --name "${cp_name}" --region "${AWS_REGION}" 2>/dev/null || true
    done
  fi

  # 12. AgentCoreMemory-* IAM exec roles (memory_step / direct deploy mint these
  #     as AgentCoreMemory-<memory_name>). Defense-in-depth for the in-product
  #     manifest path: a partial-failed deploy whose record never landed can
  #     leave the role behind. Prefix-scoped so it cannot touch foreign roles.
  local memory_roles
  memory_roles=$(aws iam list-roles \
    --query "Roles[?starts_with(RoleName, 'AgentCoreMemory-')].RoleName" \
    --output text 2>/dev/null || echo "")
  for role_name in ${memory_roles}; do
    log_info "  Deleting orphan memory IAM role: ${role_name}"
    local mem_managed mem_inline
    mem_managed=$(aws iam list-attached-role-policies \
      --role-name "${role_name}" \
      --query "AttachedPolicies[].PolicyArn" \
      --output text 2>/dev/null || echo "")
    for policy_arn in ${mem_managed}; do
      aws iam detach-role-policy --role-name "${role_name}" --policy-arn "${policy_arn}" 2>/dev/null || true
    done
    mem_inline=$(aws iam list-role-policies \
      --role-name "${role_name}" \
      --query "PolicyNames[]" \
      --output text 2>/dev/null || echo "")
    for policy_name in ${mem_inline}; do
      aws iam delete-role-policy --role-name "${role_name}" --policy-name "${policy_name}" 2>/dev/null || true
    done
    aws iam delete-role --role-name "${role_name}" 2>/dev/null || true
  done

  # 13. Harness->gateway outbound OAuth2 credential providers (harness-gw- name
  #     prefix; minted by harness_deployer.ensure_gateway_outbound_provider).
  #     Defense-in-depth for the manifest path. Opt-in (same shared-account
  #     guard as the acc-/foreign sweeps above) since provider deletes are
  #     destructive in shared accounts; the prefix is platform-owned.
  if [[ "${CLEANUP_INCLUDE_FOREIGN_RUNTIMES:-0}" == "1" ]]; then
    local harness_gw_providers
    harness_gw_providers=$(aws bedrock-agentcore-control list-oauth2-credential-providers \
      --region "${AWS_REGION}" \
      --query "oauth2CredentialProviders[?starts_with(name, 'harness-gw-')].name" \
      --output text 2>/dev/null || echo "")
    for cp_name in ${harness_gw_providers}; do
      log_info "  Deleting orphan harness gateway OAuth2 provider: ${cp_name}"
      aws bedrock-agentcore-control delete-oauth2-credential-provider \
        --name "${cp_name}" --region "${AWS_REGION}" 2>/dev/null || true
    done
  fi

  log_success "Orphan resource sweep complete."
}

# ── Step 6: Empty S3 bucket ──────────────────────────────────────────

extract_and_empty_s3_bucket() {
  log_info "Extracting S3 bucket name from stack outputs..."
  S3_BUCKET_NAME=$(get_stack_output "S3BucketName")

  if [[ -z "${S3_BUCKET_NAME}" ]]; then
    log_warn "Could not extract S3 bucket name from stack outputs. Bucket may have already been deleted."
    return
  fi

  log_info "Emptying S3 bucket: s3://${S3_BUCKET_NAME} ..."

  # Check if the bucket exists before attempting to empty it
  if ! aws s3api head-bucket --bucket "${S3_BUCKET_NAME}" --region "${AWS_REGION}" 2>/dev/null; then
    log_warn "Bucket '${S3_BUCKET_NAME}' does not exist or is not accessible. Skipping."
    return
  fi

  aws s3 rm "s3://${S3_BUCKET_NAME}" --recursive --region "${AWS_REGION}"
  log_success "S3 bucket emptied: ${S3_BUCKET_NAME}"
}

# ── Step 7: Run CDK destroy ──────────────────────────────────────────

run_cdk_destroy() {
  log_info "Destroying CDK stack '${STACK_NAME}'..."
  log_info "This removes API Gateway, Lambda functions, Step Functions, DynamoDB tables, S3, CloudFront, and WAF."
  cd "${PROJECT_ROOT}/infra"
  npx cdk destroy "${STACK_NAME}" \
    --force \
    -c environment_name="${ENVIRONMENT_NAME}" \
    -c aws_region="${AWS_REGION}" \
    -c project_name="${PROJECT_NAME}"
  cd "${PROJECT_ROOT}"
  log_success "CDK destroy completed."
}

# ── Step 8: Verify resources removed ─────────────────────────────────

verify_resources_removed() {
  log_info "Verifying stack '${STACK_NAME}' has been removed..."

  # Allow a brief moment for CloudFormation to finalize deletion
  sleep 5

  if aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}" > /dev/null 2>&1; then

    local stack_status
    stack_status=$(aws cloudformation describe-stacks \
      --stack-name "${STACK_NAME}" \
      --region "${AWS_REGION}" \
      --query "Stacks[0].StackStatus" \
      --output text 2>/dev/null || echo "UNKNOWN")

    if [[ "${stack_status}" == "DELETE_IN_PROGRESS" ]]; then
      log_warn "Stack deletion is still in progress. Check the AWS Console for status."
    else
      log_error "Stack '${STACK_NAME}' still exists with status: ${stack_status}"
      log_error "Manual cleanup may be required."
      exit 1
    fi
  else
    log_success "Stack '${STACK_NAME}' has been successfully removed."
  fi
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
  echo "  Removed: API Gateway, Lambda, Step Functions,"
  echo "           DynamoDB tables, S3, CloudFront, WAF,"
  echo "           IAM roles, Cognito User Pool"
  echo "  Removed: AgentCore runtimes, gateways, Cognito"
  echo "           pools, Knowledge Bases, Guardrails,"
  echo "           Memory, Policy Engines, IAM roles"
  echo ""
  echo "  All resources have been removed."
  echo "=============================================="
}

# ── Main ──────────────────────────────────────────────────────────────

confirm_destroy() {
  # SECURITY: Require explicit confirmation before destructive operations.
  # Skip prompt if FORCE_DESTROY=true is set (for CI/CD pipelines).
  if [[ "${FORCE_DESTROY:-false}" == "true" ]]; then
    log_info "FORCE_DESTROY=true — skipping confirmation."
    return
  fi

  echo ""
  log_warn "This will PERMANENTLY DELETE all resources in stack '${STACK_NAME}':"
  echo "  - API Gateway, Lambda functions, Step Functions"
  echo "  - DynamoDB tables (workflows + deployments + flows data)"
  echo "  - S3 buckets (frontend assets + artifacts + logs)"
  echo "  - CloudFront distribution, WAF WebACL, IAM roles"
  echo "  - ALL deployed AgentCore runtimes, gateways, and associated resources"
  echo "  - ALL AgentCore-* Lambda functions, IAM roles, Cognito pools"
  echo ""
  read -r -p "Are you sure? Type 'yes' to confirm: " response
  if [[ "${response}" != "yes" ]]; then
    log_info "Cleanup cancelled."
    exit 0
  fi
}

main() {
  log_info "Starting cleanup of ${PROJECT_NAME} (${ENVIRONMENT_NAME}) in ${AWS_REGION}"

  check_prerequisites
  check_aws_credentials
  check_stack_exists
  confirm_destroy
  cleanup_deployment_resources
  sweep_orphan_resources
  extract_and_empty_s3_bucket
  run_cdk_destroy
  verify_resources_removed
  print_summary
}

main "$@"
