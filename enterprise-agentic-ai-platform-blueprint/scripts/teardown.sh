#!/usr/bin/env bash
# teardown.sh — reverse-dependency cleanup of a reference deployment.
#
# DESTROYS STACKS AND MANY RESOURCES. Not idempotent across Control-Tower
# account closures — Control Tower accounts enter a 90-day SUSPENDED state.
# See README section 16 (Cleanup) for the full teardown order and caveats.
#
# Round 1B fail-closed behaviour (tasks/todo.md §Round 1 B):
#
#   * Every stack is mapped to the `bin/agentic-ai-platform.ts` stage that
#     declares it, and `cdk destroy` is invoked with that stage (plus the
#     context the stage needs). Without `--context stage=…` the app synthesises
#     an empty assembly and `cdk destroy` matches nothing.
#   * A stack that does not exist is reported ABSENT and skipped. A stack that
#     exists but fails to destroy is reported FAILED and the script exits
#     non-zero. The previous `|| echo "(stack not present…)"` collapsed both
#     cases into success.
#   * Any other unexpected condition — missing CLI, missing required context,
#     a describe/list call that fails for a reason other than "does not exist"
#     — aborts before anything is destroyed.
#
# Usage:
#   bash scripts/teardown.sh                 # interactive, destroys
#   bash scripts/teardown.sh --dry-run       # print the plan, touch nothing
#   bash scripts/teardown.sh --stack NAME    # restrict to NAME (repeatable)
#
# Exit codes:
#   0  all listed stacks destroyed or already absent
#   1  at least one stack failed to destroy
#   2  configuration error (missing CLI, missing required context)
#   3  unexpected AWS error while inspecting stacks
#   4  aborted at the confirmation prompt
#
# Context is read from the environment so no account id is baked into the repo:
#   AGENTICAI_ORGANIZATION_ID            AGENTICAI_PIPELINE_ROLE_ARN
#   AGENTICAI_PLATFORM_ACCOUNT_ID        AGENTICAI_AUDIT_ACCOUNT_ID
#   AGENTICAI_LOG_ARCHIVE_ACCOUNT_ID     AGENTICAI_WORKLOAD_ACCOUNT_ID
#   AGENTICAI_PLATFORM_NONPROD_ACCOUNT_ID AGENTICAI_PLATFORM_PROD_ACCOUNT_ID
#   AGENTICAI_WORKLOAD_NONPROD_ACCOUNT_ID AGENTICAI_WORKLOAD_PROD_ACCOUNT_ID
#   AGENTICAI_WORKLOAD_NONPROD_AVAILABILITY_ZONES AGENTICAI_WORKLOAD_PROD_AVAILABILITY_ZONES
#   AGENTICAI_GITHUB_REPO                AGENTICAI_GITHUB_CONNECTION_ARN
#   AGENTICAI_D03_PLATFORM_ACCOUNT_ID    AGENTICAI_D03_WORKLOAD_ACCOUNT_IDS
#   AGENTICAI_D03_EXTERNAL_ID            AGENTICAI_GA_REGISTRY_CONTEXT_FILE
#   AGENTICAI_APPROVER_ROLE_ARN          AGENTICAI_TENANT_ID (default demo)
#   AGENTICAI_AGENT_ID (default primary) AGENTICAI_ENV_NAME (default nonprod)
#   AGENTICAI_APPLICATION_ID            AGENTICAI_COST_CENTRE
#   AGENTICAI_INFERENCE_MODEL_RATE_LIMITS (required JSON array for Platform)
#   AGENTICAI_GA_REGISTRY_NONPROD_CONTEXT_FILE AGENTICAI_GA_REGISTRY_PROD_CONTEXT_FILE
#   AGENTICAI_GA_REGISTRY_EXPECTED_TOOL_IDS     AGENTICAI_WORKSTREAM_GATEWAY_REGION
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

DRY_RUN=false
RESTRICT_STACKS=()
CDK_CONTEXT_ARGS=()

TENANT_ID="${AGENTICAI_TENANT_ID:-demo}"
AGENT_ID="${AGENTICAI_AGENT_ID:-primary}"
ENV_NAME="${AGENTICAI_ENV_NAME:-nonprod}"

# Reverse dependency order: consumers before producers.
BASE_STACKS=(
  "AgenticAI-${TENANT_ID}-${AGENT_ID}-prod-RuntimeMemory"
  "AgenticAI-${TENANT_ID}-${AGENT_ID}-nonprod-RuntimeMemory"
  "AgenticAI-${TENANT_ID}-${AGENT_ID}-prod-ToolGateway"
  "AgenticAI-${TENANT_ID}-${AGENT_ID}-nonprod-ToolGateway"
  "AgenticAI-${TENANT_ID}-${AGENT_ID}-prod-RegistryRoles"
  "AgenticAI-${TENANT_ID}-${AGENT_ID}-nonprod-RegistryRoles"
  "AgenticAI-WorkloadPipelineStack"
  "AgenticAI-PlatformPipelineStack"
  "AgenticAI-GapClosureStack"
  "AgenticAI-D03-WorkloadAgentStack"
  "AgenticAI-D03-PlatformCoreStack"
  "AgenticAI-Workload-AppStack"
  "AgenticAI-Workload-NetworkStack"
  "AgenticAI-Platform-InferenceGatewayStack"
  "AgenticAI-Platform-RegistryStack"
  "AgenticAI-Platform-GuardrailStack"
  "AgenticAI-Platform-AuditStack"
  "AgenticAI-Platform-LogArchiveStack"
  "AgenticAI-Management-OrgStack"
)

usage() {
  sed -n '2,30p' "$0"
}

fail() {
  local code="$1"
  shift
  printf 'ERROR: %s\n' "$*" >&2
  exit "$code"
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=true ;;
      --stack)
        [ "$#" -ge 2 ] || fail 2 "--stack requires a stack name"
        RESTRICT_STACKS+=("$2")
        shift
        ;;
      -h|--help) usage; exit 0 ;;
      *) fail 2 "unknown argument '$1' (see --help)" ;;
    esac
    shift
  done
}

require_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || fail 2 "required command '$tool' not found on PATH"
  done
}

# ---------------------------------------------------------------------------
# Stack -> stage mapping. Every stack destroyed here is declared by exactly one
# `stage` branch of bin/agentic-ai-platform.ts.
# ---------------------------------------------------------------------------
stage_for_stack() {
  case "$1" in
    AgenticAI-*-RuntimeMemory|AgenticAI-*-ToolGateway|AgenticAI-*-RegistryRoles) printf 'pipeline\n' ;;
    AgenticAI-WorkloadPipelineStack|AgenticAI-PlatformPipelineStack) printf 'pipeline\n' ;;
    AgenticAI-GapClosureStack) printf 'gap-closure\n' ;;
    AgenticAI-D03-WorkstreamGateway-*) printf 'd03-workstream-gateway\n' ;;
    AgenticAI-D03-WorkloadAgentStack) printf 'd03-workload\n' ;;
    AgenticAI-D03-PlatformCoreStack) printf 'd03-platform\n' ;;
    AgenticAI-Workload-AppStack|AgenticAI-Workload-NetworkStack) printf 'workload\n' ;;
    AgenticAI-Platform-InferenceGatewayStack|AgenticAI-Platform-RegistryStack|AgenticAI-Platform-GuardrailStack) printf 'platform\n' ;;
    AgenticAI-Platform-AuditStack|AgenticAI-Platform-LogArchiveStack) printf 'platform\n' ;;
    AgenticAI-Management-OrgStack) printf 'management\n' ;;
    *) return 1 ;;
  esac
}

# Environment variables the stack's stage needs before `cdk destroy` can even
# synthesise the app. Empty means "no required context".
required_env_for_stack() {
  case "$1" in
    AgenticAI-WorkloadPipelineStack|AgenticAI-*-RuntimeMemory|AgenticAI-*-ToolGateway|AgenticAI-*-RegistryRoles)
      printf '%s\n' "AGENTICAI_GITHUB_REPO AGENTICAI_GITHUB_CONNECTION_ARN AGENTICAI_PLATFORM_NONPROD_ACCOUNT_ID AGENTICAI_PLATFORM_PROD_ACCOUNT_ID AGENTICAI_WORKLOAD_NONPROD_ACCOUNT_ID AGENTICAI_WORKLOAD_PROD_ACCOUNT_ID AGENTICAI_WORKLOAD_NONPROD_AVAILABILITY_ZONES AGENTICAI_WORKLOAD_PROD_AVAILABILITY_ZONES AGENTICAI_GA_REGISTRY_NONPROD_CONTEXT_FILE AGENTICAI_GA_REGISTRY_PROD_CONTEXT_FILE AGENTICAI_GA_REGISTRY_EXPECTED_TOOL_IDS" ;;
    AgenticAI-PlatformPipelineStack)
      printf '%s\n' "AGENTICAI_GITHUB_REPO AGENTICAI_GITHUB_CONNECTION_ARN AGENTICAI_ORGANIZATION_ID AGENTICAI_PLATFORM_NONPROD_ACCOUNT_ID AGENTICAI_PLATFORM_PROD_ACCOUNT_ID AGENTICAI_AUDIT_ACCOUNT_ID AGENTICAI_LOG_ARCHIVE_ACCOUNT_ID AGENTICAI_WORKLOAD_NONPROD_ACCOUNT_ID AGENTICAI_WORKLOAD_PROD_ACCOUNT_ID AGENTICAI_PIPELINE_ROLE_ARN AGENTICAI_INFERENCE_MODEL_RATE_LIMITS" ;;
    AgenticAI-GapClosureStack)
      printf '%s\n' "AGENTICAI_APPROVER_ROLE_ARN" ;;
    AgenticAI-D03-WorkstreamGateway-*)
      printf '%s\n' "AGENTICAI_D03_PLATFORM_ACCOUNT_ID AGENTICAI_GA_REGISTRY_CONTEXT_FILE AGENTICAI_GA_REGISTRY_EXPECTED_TOOL_IDS" ;;
    AgenticAI-D03-WorkloadAgentStack)
      printf '%s\n' "AGENTICAI_D03_PLATFORM_ACCOUNT_ID AGENTICAI_D03_EXTERNAL_ID" ;;
    AgenticAI-D03-PlatformCoreStack)
      printf '%s\n' "AGENTICAI_D03_WORKLOAD_ACCOUNT_IDS AGENTICAI_D03_EXTERNAL_ID" ;;
    AgenticAI-Workload-AppStack|AgenticAI-Workload-NetworkStack)
      printf '%s\n' "AGENTICAI_WORKLOAD_ACCOUNT_ID" ;;
    AgenticAI-Platform-InferenceGatewayStack|AgenticAI-Platform-RegistryStack|AgenticAI-Platform-GuardrailStack)
      printf '%s\n' "AGENTICAI_ORGANIZATION_ID AGENTICAI_PLATFORM_ACCOUNT_ID AGENTICAI_PIPELINE_ROLE_ARN AGENTICAI_INFERENCE_MODEL_RATE_LIMITS" ;;
    AgenticAI-Platform-AuditStack)
      printf '%s\n' "AGENTICAI_ORGANIZATION_ID AGENTICAI_AUDIT_ACCOUNT_ID" ;;
    AgenticAI-Platform-LogArchiveStack)
      printf '%s\n' "AGENTICAI_ORGANIZATION_ID AGENTICAI_LOG_ARCHIVE_ACCOUNT_ID" ;;
    *) printf '\n' ;;
  esac
}

missing_env_for_stack() {
  local stack="$1" var missing=""
  for var in $(required_env_for_stack "$stack"); do
    if [ -z "${!var:-}" ]; then
      missing="$missing $var"
    fi
  done
  case "$stack" in
    AgenticAI-WorkloadPipelineStack|AgenticAI-*-RuntimeMemory|AgenticAI-*-ToolGateway|AgenticAI-*-RegistryRoles)
      if [ -n "${AGENTICAI_GATEWAY_POLICY_ENGINE_MODE:-}" ] &&
         [ "${AGENTICAI_GATEWAY_POLICY_ENGINE_MODE}" != "OFF" ]; then
        for var in \
          AGENTICAI_GATEWAY_POLICY_ENGINE_NONPROD_IAM_ROLE_ARNS \
          AGENTICAI_GATEWAY_POLICY_ENGINE_PROD_IAM_ROLE_ARNS; do
          if [ -z "${!var:-}" ]; then
            missing="$missing $var"
          fi
        done
      fi
      ;;
  esac
  printf '%s\n' "${missing# }"
}

# Populate CDK_CONTEXT_ARGS with the `--context` pairs the stack's stage needs.
set_context_args_for_stack() {
  local stack="$1" stage
  stage=$(stage_for_stack "$stack") || fail 2 "no stage mapping for stack '$stack'"
  CDK_CONTEXT_ARGS=(--context "stage=$stage")

  case "$stack" in
    AgenticAI-WorkloadPipelineStack|AgenticAI-*-RuntimeMemory|AgenticAI-*-ToolGateway|AgenticAI-*-RegistryRoles)
      add_context "agenticai/pipelineSelection=workload"
      add_context "agenticai/githubRepo=${AGENTICAI_GITHUB_REPO:-}"
      add_context "agenticai/githubConnectionArn=${AGENTICAI_GITHUB_CONNECTION_ARN:-}"
      add_context "agenticai/platformNonprodAccountId=${AGENTICAI_PLATFORM_NONPROD_ACCOUNT_ID:-}"
      add_context "agenticai/platformProdAccountId=${AGENTICAI_PLATFORM_PROD_ACCOUNT_ID:-}"
      add_context "agenticai/workloadNonprodAccountId=${AGENTICAI_WORKLOAD_NONPROD_ACCOUNT_ID:-}"
      add_context "agenticai/workloadProdAccountId=${AGENTICAI_WORKLOAD_PROD_ACCOUNT_ID:-}"
      add_context "agenticai/workloadNonprodAvailabilityZones=${AGENTICAI_WORKLOAD_NONPROD_AVAILABILITY_ZONES:-}"
      add_context "agenticai/workloadProdAvailabilityZones=${AGENTICAI_WORKLOAD_PROD_AVAILABILITY_ZONES:-}"
      add_context "agenticai/enableGaRegistryConsumer=true"
      case "$stack" in
        AgenticAI-*-RuntimeMemory)
          add_context "agenticai/enablePipelineRuntimeMemory=true"
          ;;
        *)
          if [ "${AGENTICAI_ENABLE_PIPELINE_RUNTIME_MEMORY:-false}" = "true" ]; then
            add_context "agenticai/enablePipelineRuntimeMemory=true"
          fi
          ;;
      esac
      add_context "agenticai/gaRegistryExpectedToolIds=${AGENTICAI_GA_REGISTRY_EXPECTED_TOOL_IDS:-}"
      add_context "agenticai/gaRegistryNonprodContextFile=${AGENTICAI_GA_REGISTRY_NONPROD_CONTEXT_FILE:-}"
      add_context "agenticai/gaRegistryProdContextFile=${AGENTICAI_GA_REGISTRY_PROD_CONTEXT_FILE:-}"
      add_context "agenticai/workstreamGatewayRegion=${AGENTICAI_WORKSTREAM_GATEWAY_REGION:-us-west-2}"
      if [ -n "${AGENTICAI_GATEWAY_POLICY_ENGINE_MODE:-}" ]; then
        add_context "agenticai/gatewayPolicyEngineMode=${AGENTICAI_GATEWAY_POLICY_ENGINE_MODE}"
        add_context "agenticai/gatewayPolicyEngineNonprodIamRoleArns=${AGENTICAI_GATEWAY_POLICY_ENGINE_NONPROD_IAM_ROLE_ARNS:-}"
        add_context "agenticai/gatewayPolicyEngineProdIamRoleArns=${AGENTICAI_GATEWAY_POLICY_ENGINE_PROD_IAM_ROLE_ARNS:-}"
      fi
      add_context "agenticai/applicationId=${AGENTICAI_APPLICATION_ID:-$TENANT_ID}"
      add_context "agenticai/costCentre=${AGENTICAI_COST_CENTRE:-engineering}"
      add_tenant_context
      ;;
    AgenticAI-PlatformPipelineStack)
      add_context "agenticai/pipelineSelection=platform"
      add_context "agenticai/githubRepo=${AGENTICAI_GITHUB_REPO:-}"
      add_context "agenticai/githubConnectionArn=${AGENTICAI_GITHUB_CONNECTION_ARN:-}"
      add_context "agenticai/organizationId=${AGENTICAI_ORGANIZATION_ID:-}"
      add_context "agenticai/platformNonprodAccountId=${AGENTICAI_PLATFORM_NONPROD_ACCOUNT_ID:-}"
      add_context "agenticai/platformProdAccountId=${AGENTICAI_PLATFORM_PROD_ACCOUNT_ID:-}"
      add_context "agenticai/auditAccountId=${AGENTICAI_AUDIT_ACCOUNT_ID:-}"
      add_context "agenticai/logArchiveAccountId=${AGENTICAI_LOG_ARCHIVE_ACCOUNT_ID:-}"
      add_context "agenticai/workloadNonprodAccountId=${AGENTICAI_WORKLOAD_NONPROD_ACCOUNT_ID:-}"
      add_context "agenticai/workloadProdAccountId=${AGENTICAI_WORKLOAD_PROD_ACCOUNT_ID:-}"
      add_context "agenticai/inferenceModelRateLimits=${AGENTICAI_INFERENCE_MODEL_RATE_LIMITS:-}"
      add_context "agenticai/applicationId=${AGENTICAI_APPLICATION_ID:-$TENANT_ID}"
      add_context "agenticai/costCentre=${AGENTICAI_COST_CENTRE:-platform}"
      add_tenant_context
      ;;
    AgenticAI-GapClosureStack)
      add_context "agenticai/approverRoleArn=${AGENTICAI_APPROVER_ROLE_ARN:-}"
      add_tenant_context
      ;;
    AgenticAI-D03-WorkstreamGateway-*)
      add_context "agenticai/d03PlatformAccountId=${AGENTICAI_D03_PLATFORM_ACCOUNT_ID:-}"
      # The legacy agenticai/d03AllowedToolIds path was retired; the stage now
      # synthesizes only from the pipeline-resolved GA Registry context.
      add_context "agenticai/gaRegistryContextFile=${AGENTICAI_GA_REGISTRY_CONTEXT_FILE:-}"
      add_context "agenticai/gaRegistryExpectedToolIds=${AGENTICAI_GA_REGISTRY_EXPECTED_TOOL_IDS:-}"
      add_workstream_identity_context "$stack"
      ;;
    AgenticAI-D03-WorkloadAgentStack)
      add_context "agenticai/d03PlatformAccountId=${AGENTICAI_D03_PLATFORM_ACCOUNT_ID:-}"
      add_context "agenticai/d03ExternalId=${AGENTICAI_D03_EXTERNAL_ID:-}"
      add_tenant_context
      ;;
    AgenticAI-D03-PlatformCoreStack)
      add_context "agenticai/d03WorkloadAccountIds=${AGENTICAI_D03_WORKLOAD_ACCOUNT_IDS:-}"
      add_context "agenticai/d03ExternalId=${AGENTICAI_D03_EXTERNAL_ID:-}"
      ;;
    AgenticAI-Workload-AppStack)
      add_context "agenticai/workloadAccountId=${AGENTICAI_WORKLOAD_ACCOUNT_ID:-}"
      # The app only declares this stack when the flag is set, so destroy must
      # pass it too or `cdk destroy` finds no such stack.
      add_context "agenticai/deployWorkloadApp=true"
      add_tenant_context
      ;;
    AgenticAI-Workload-NetworkStack)
      add_context "agenticai/workloadAccountId=${AGENTICAI_WORKLOAD_ACCOUNT_ID:-}"
      ;;
    AgenticAI-Platform-InferenceGatewayStack|AgenticAI-Platform-RegistryStack|AgenticAI-Platform-GuardrailStack)
      add_context "agenticai/organizationId=${AGENTICAI_ORGANIZATION_ID:-}"
      add_context "agenticai/platformAccountId=${AGENTICAI_PLATFORM_ACCOUNT_ID:-}"
      add_context "agenticai/pipelineRoleArn=${AGENTICAI_PIPELINE_ROLE_ARN:-}"
      add_context "agenticai/inferenceModelRateLimits=${AGENTICAI_INFERENCE_MODEL_RATE_LIMITS:-}"
      add_context "agenticai/applicationId=${AGENTICAI_APPLICATION_ID:-platform-inference}"
      add_context "agenticai/costCentre=${AGENTICAI_COST_CENTRE:-platform}"
      add_tenant_context
      ;;
    AgenticAI-Platform-AuditStack)
      add_context "agenticai/organizationId=${AGENTICAI_ORGANIZATION_ID:-}"
      add_context "agenticai/auditAccountId=${AGENTICAI_AUDIT_ACCOUNT_ID:-}"
      ;;
    AgenticAI-Platform-LogArchiveStack)
      add_context "agenticai/organizationId=${AGENTICAI_ORGANIZATION_ID:-}"
      add_context "agenticai/logArchiveAccountId=${AGENTICAI_LOG_ARCHIVE_ACCOUNT_ID:-}"
      ;;
    AgenticAI-Management-OrgStack) ;;
    *) fail 2 "no context mapping for stack '$stack'" ;;
  esac
}

add_context() {
  CDK_CONTEXT_ARGS+=(--context "$1")
}

add_tenant_context() {
  add_context "agenticai/tenantId=$TENANT_ID"
  add_context "agenticai/agentId=$AGENT_ID"
  add_context "agenticai/envName=$ENV_NAME"
}

# Workstream gateway stack names embed tenant and agent; recover them from the
# stack name so a discovered stack is destroyed with its own identity rather
# than with the ambient defaults.
add_workstream_identity_context() {
  local suffix tenant agent
  suffix="${1#AgenticAI-D03-WorkstreamGateway-}"
  tenant="${suffix%%-*}"
  agent="${suffix#*-}"
  add_context "agenticai/tenantId=${tenant:-$TENANT_ID}"
  add_context "agenticai/agentId=${agent:-$AGENT_ID}"
  add_context "agenticai/envName=$ENV_NAME"
}

# ---------------------------------------------------------------------------
# Discovery and status
# ---------------------------------------------------------------------------

# Per-workstream gateway stacks are named at synth time, so they cannot be
# hard-coded. Discover them from CloudFormation; in --dry-run report the
# templated name derived from the ambient tenant/agent instead.
discover_workstream_gateway_stacks() {
  local out
  if [ "$DRY_RUN" = true ]; then
    printf 'AgenticAI-D03-WorkstreamGateway-%s-%s\n' "$TENANT_ID" "$AGENT_ID"
    return 0
  fi
  if ! out=$(aws cloudformation list-stacks \
      --query 'StackSummaries[?StackStatus!=`DELETE_COMPLETE`].StackName' \
      --output text 2>&1); then
    printf 'ERROR: list-stacks failed while discovering workstream gateway stacks: %s\n' \
      "$out" >&2
    return 1
  fi
  printf '%s\n' "$out" | tr '[:space:]' '\n' | sed -n '/^AgenticAI-D03-WorkstreamGateway-/p'
}

# Echo the CloudFormation status, or ABSENT when the stack does not exist.
# Returns non-zero for every other failure so callers can abort instead of
# mistaking an API error for an already-clean account.
stack_status() {
  local stack="$1" out
  if out=$(aws cloudformation describe-stacks --stack-name "$stack" \
      --query 'Stacks[0].StackStatus' --output text 2>&1); then
    case "$out" in
      DELETE_COMPLETE) printf 'ABSENT\n' ;;
      *) printf '%s\n' "$out" ;;
    esac
    return 0
  fi
  if printf '%s' "$out" | grep -qi 'does not exist'; then
    printf 'ABSENT\n'
    return 0
  fi
  printf 'ERROR: describe-stacks failed for %s: %s\n' "$stack" "$out" >&2
  return 1
}

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
build_stack_list() {
  local discovered stack
  PLANNED_STACKS=()
  discovered=$(discover_workstream_gateway_stacks) ||
    fail 3 "could not enumerate workstream gateway stacks"

  for stack in "${BASE_STACKS[@]}"; do
    # Gateways sit between the pipelines and the workload agent stack.
    if [ "$stack" = "AgenticAI-D03-WorkloadAgentStack" ] && [ -n "$discovered" ]; then
      local gw
      while IFS= read -r gw; do
        [ -n "$gw" ] && maybe_plan "$gw"
      done <<<"$discovered"
    fi
    maybe_plan "$stack"
  done

  [ "${#PLANNED_STACKS[@]}" -gt 0 ] || fail 2 "no stacks selected (check --stack values)"
}

maybe_plan() {
  local stack="$1" want
  if [ "${#RESTRICT_STACKS[@]}" -gt 0 ]; then
    for want in "${RESTRICT_STACKS[@]}"; do
      if [ "$want" = "$stack" ]; then
        PLANNED_STACKS+=("$stack")
        return 0
      fi
    done
    return 0
  fi
  PLANNED_STACKS+=("$stack")
}

print_plan_header() {
  printf '============================================================\n'
  printf 'AgenticAI teardown plan%s\n' "$([ "$DRY_RUN" = true ] && printf ' (dry run — nothing will be destroyed)')"
  printf '%s\n' '------------------------------------------------------------'
  printf '%-46s %-24s %s\n' 'STACK' 'STAGE' 'STATUS'
}

# Resolve status + required context for each planned stack. Aborts before any
# destroy if context is missing or an AWS call fails unexpectedly.
plan_stacks() {
  local stack stage status missing config_errors=0
  STACK_STATUS=()
  print_plan_header
  for stack in "${PLANNED_STACKS[@]}"; do
    stage=$(stage_for_stack "$stack") || fail 2 "no stage mapping for stack '$stack'"
    if [ "$DRY_RUN" = true ]; then
      status='NOT CHECKED (dry run)'
    else
      status=$(stack_status "$stack") || fail 3 "cannot determine state of $stack"
    fi
    STACK_STATUS+=("$status")
    printf '%-46s %-24s %s\n' "$stack" "$stage" "$status"

    missing=$(missing_env_for_stack "$stack")
    if [ -n "$missing" ] && [ "$status" != "ABSENT" ]; then
      printf '    missing required context: %s\n' "$missing" >&2
      config_errors=$((config_errors + 1))
    fi
  done
  if [ "$config_errors" -gt 0 ] && [ "$DRY_RUN" != true ]; then
    fail 2 "$config_errors stack(s) are missing required context; nothing destroyed"
  fi
}

confirm() {
  local yn
  printf '\n'
  printf 'This destroys the stacks listed above, empties non-versioned buckets,\n'
  printf 'deletes their exact service-created CodeBuild/Lambda/Runtime log groups, and\n'
  printf 'reports resources needing manual deletion (KMS pending-delete,\n'
  printf 'retained-on-delete S3).\n\n'
  printf 'Control-Tower closed accounts enter a 90-day SUSPENDED state; that is\n'
  printf "NOT automated here — use the Organizations console or\n"
  printf "'aws organizations close-account'.\n"
  read -r -p "Continue? [y/N] " yn
  case "$yn" in
    y|Y) return 0 ;;
    *) printf 'Aborted.\n'; exit 4 ;;
  esac
}

# ---------------------------------------------------------------------------
# Destroy
# ---------------------------------------------------------------------------

# CodeBuild, Lambda, and AgentCore Runtime create service log groups outside
# CloudFormation. Capture exact physical ids before deleting the active stack,
# plus ids from prior deleted generations, so teardown retries can remove stale
# groups without broad name-prefix matching.
append_service_log_resource() {
  local resource_type="$1" physical_id="$2" group entry existing_group
  if [ -z "$physical_id" ] || [ "$physical_id" = "None" ]; then
    return 0
  fi
  case "$resource_type" in
    AWS::CodeBuild::Project) group="/aws/codebuild/$physical_id" ;;
    AWS::Lambda::Function) group="/aws/lambda/$physical_id" ;;
    AWS::BedrockAgentCore::Runtime)
      case "$physical_id" in
        arn:*:bedrock-agentcore:*:*:runtime/*)
          physical_id="${physical_id##*/}"
          ;;
      esac
      group="/aws/bedrock-agentcore/runtimes/${physical_id}-DEFAULT"
      ;;
    *) return 0 ;;
  esac

  for entry in "${GENERATED_SERVICE_LOG_RESOURCES[@]}"; do
    IFS=$'\t' read -r _ _ existing_group <<<"$entry"
    [ "$existing_group" = "$group" ] && return 0
  done
  GENERATED_SERVICE_LOG_RESOURCES+=(
    "$resource_type"$'\t'"$physical_id"$'\t'"$group"
  )
}

append_service_log_rows() {
  local rows="$1" resource_type physical_id
  while IFS=$'\t' read -r resource_type physical_id; do
    if [ -z "$resource_type" ] || [ "$resource_type" = "None" ]; then
      continue
    fi
    append_service_log_resource "$resource_type" "$physical_id"
  done <<<"$rows"
}

capture_deleted_stack_service_logs() {
  local stack="$1" stack_ids stack_id events
  if ! stack_ids=$(aws cloudformation list-stacks \
      --stack-status-filter DELETE_COMPLETE \
      --query "StackSummaries[?StackName=='$stack'].StackId" \
      --output text 2>&1); then
    printf 'ERROR: list-stacks failed while recovering service logs for %s: %s\n' \
      "$stack" "$stack_ids" >&2
    return 1
  fi

  for stack_id in $stack_ids; do
    [ "$stack_id" = "None" ] && continue
    if ! events=$(aws cloudformation describe-stack-events \
        --stack-name "$stack_id" \
        --query "StackEvents[?ResourceType=='AWS::CodeBuild::Project' || ResourceType=='AWS::Lambda::Function' || ResourceType=='AWS::BedrockAgentCore::Runtime'].[ResourceType,PhysicalResourceId]" \
        --output text 2>&1); then
      printf 'ERROR: describe-stack-events failed for %s: %s\n' \
        "$stack_id" "$events" >&2
      return 1
    fi
    append_service_log_rows "$events"
  done
}

capture_active_stack_service_logs() {
  local stack="$1" resources
  if ! resources=$(aws cloudformation list-stack-resources \
      --stack-name "$stack" \
      --query "StackResourceSummaries[?ResourceType=='AWS::CodeBuild::Project' || ResourceType=='AWS::Lambda::Function' || ResourceType=='AWS::BedrockAgentCore::Runtime'].[ResourceType,PhysicalResourceId]" \
      --output text 2>&1); then
    printf 'ERROR: list-stack-resources failed for %s: %s\n' \
      "$stack" "$resources" >&2
    return 1
  fi
  append_service_log_rows "$resources"
}

capture_service_log_resources() {
  local stack="$1" include_active="$2"
  GENERATED_SERVICE_LOG_RESOURCES=()
  capture_deleted_stack_service_logs "$stack" || return 1
  if [ "$include_active" = true ]; then
    capture_active_stack_service_logs "$stack" || return 1
  fi
}

ensure_service_resource_absent() {
  local resource_type="$1" physical_id="$2" out
  case "$resource_type" in
    AWS::Lambda::Function)
      if out=$(aws lambda get-function \
          --function-name "$physical_id" \
          --query 'Configuration.FunctionArn' \
          --output text 2>&1); then
        printf 'ERROR: refusing to delete logs while Lambda function still exists: %s\n' \
          "$physical_id" >&2
        return 1
      fi
      if ! printf '%s' "$out" | grep -q 'ResourceNotFoundException'; then
        printf 'ERROR: get-function failed for %s: %s\n' "$physical_id" "$out" >&2
        return 1
      fi
      ;;
    AWS::CodeBuild::Project)
      if ! out=$(aws codebuild batch-get-projects \
          --names "$physical_id" \
          --query 'projects[].name' \
          --output text 2>&1); then
        printf 'ERROR: batch-get-projects failed for %s: %s\n' \
          "$physical_id" "$out" >&2
        return 1
      fi
      if [ -n "$out" ] && [ "$out" != "None" ]; then
        printf 'ERROR: refusing to delete logs while CodeBuild project still exists: %s\n' \
          "$physical_id" >&2
        return 1
      fi
      ;;
    AWS::BedrockAgentCore::Runtime)
      if out=$(aws bedrock-agentcore-control get-agent-runtime \
          --agent-runtime-id "$physical_id" \
          --query 'agentRuntimeId' \
          --output text 2>&1); then
        printf 'ERROR: refusing to delete logs while AgentCore Runtime still exists: %s\n' \
          "$physical_id" >&2
        return 1
      fi
      if ! printf '%s' "$out" | grep -q 'ResourceNotFoundException'; then
        printf 'ERROR: get-agent-runtime failed for %s: %s\n' \
          "$physical_id" "$out" >&2
        return 1
      fi
      ;;
  esac
}

cleanup_service_log_groups() {
  local entry resource_type physical_id group out failures=0
  for entry in "${GENERATED_SERVICE_LOG_RESOURCES[@]}"; do
    IFS=$'\t' read -r resource_type physical_id group <<<"$entry"
    if ! ensure_service_resource_absent "$resource_type" "$physical_id"; then
      failures=$((failures + 1))
      continue
    fi
    if out=$(aws logs delete-log-group --log-group-name "$group" 2>&1); then
      printf '   DELETED service log group %s\n' "$group"
    elif printf '%s' "$out" | grep -q 'ResourceNotFoundException'; then
      printf '   ABSENT  service log group %s\n' "$group"
    else
      printf 'ERROR: delete-log-group failed for %s: %s\n' "$group" "$out" >&2
      failures=$((failures + 1))
    fi
  done
  return "$failures"
}

cleanup_absent_stack_service_logs() {
  local stack="$1"
  capture_service_log_resources "$stack" false || return 1
  if [ "${#GENERATED_SERVICE_LOG_RESOURCES[@]}" -eq 0 ]; then
    return 0
  fi
  printf '\n-> Cleaning exact service logs from deleted generations of %s\n' "$stack"
  cleanup_service_log_groups
}

destroy_stack() {
  local stack="$1"
  set_context_args_for_stack "$stack"
  capture_service_log_resources "$stack" true || return 1
  printf '\n-> Destroying %s (stage=%s)\n' "$stack" "$(stage_for_stack "$stack")"
  npx cdk destroy --force "${CDK_CONTEXT_ARGS[@]}" "$stack" || return 1
  cleanup_service_log_groups
}

# Producers that a consumer stack imports BY NAME (invisible to CloudFormation's
# export/import dependency tracking). The ToolGateway and RuntimeMemory stacks
# run their custom resources on execution roles created by the same-environment
# RegistryRoles stack; deleting RegistryRoles while a consumer still exists
# leaves that consumer unable to delete itself (Lambda fails every invoke before
# the handler runs, CloudFormation waits out its one-hour timeout, live-proven
# 2026-09-24). Every workstream stack is also declared by the Workload root.
blocked_producers_for() {
  local stack="$1" env
  case "$stack" in
    AgenticAI-*-RuntimeMemory|AgenticAI-*-ToolGateway)
      env="${stack%-*}"          # strip -RuntimeMemory / -ToolGateway
      env="${env##*-}"           # keep the trailing environment token
      printf '%s\n' "AgenticAI-${TENANT_ID}-${AGENT_ID}-${env}-RegistryRoles"
      printf '%s\n' "AgenticAI-WorkloadPipelineStack"
      ;;
    AgenticAI-*-RegistryRoles)
      printf '%s\n' "AgenticAI-WorkloadPipelineStack"
      ;;
  esac
}

is_blocked_producer() {
  local candidate="$1" blocked
  for blocked in "${BLOCKED_PRODUCERS[@]}"; do
    [ "$blocked" = "$candidate" ] && return 0
  done
  return 1
}

destroy_planned_stacks() {
  local i stack status failures=0 producer
  RESULTS=()
  BLOCKED_PRODUCERS=()
  for i in "${!PLANNED_STACKS[@]}"; do
    stack="${PLANNED_STACKS[$i]}"
    status="${STACK_STATUS[$i]}"
    if [ "$status" = "ABSENT" ]; then
      if cleanup_absent_stack_service_logs "$stack"; then
        printf '\n-> Skipping %s (ABSENT — exact historical service logs clean)\n' \
          "$stack"
        RESULTS+=("ABSENT   $stack")
      else
        printf 'ERROR: exact historical log cleanup failed for absent stack %s\n' \
          "$stack" >&2
        RESULTS+=("FAILED    $stack")
        failures=$((failures + 1))
      fi
      continue
    fi
    if is_blocked_producer "$stack"; then
      printf '\n-> BLOCKED %s: a stack that imports its roles by name failed to destroy; resolve that stack first, then re-run\n' \
        "$stack" >&2
      RESULTS+=("BLOCKED   $stack")
      failures=$((failures + 1))
      continue
    fi
    if destroy_stack "$stack"; then
      RESULTS+=("DESTROYED $stack")
    else
      printf 'ERROR: cdk destroy failed or exact post-destroy log cleanup failed for %s (was %s); continuing with unrelated stacks only\n' \
        "$stack" "$status" >&2
      RESULTS+=("FAILED    $stack")
      failures=$((failures + 1))
      while IFS= read -r producer; do
        [ -n "$producer" ] && BLOCKED_PRODUCERS+=("$producer")
      done < <(blocked_producers_for "$stack")
    fi
  done
  return "$failures"
}

print_summary() {
  local line
  printf '\n'
  printf '%s\n' '------------------------------------------------------------'
  printf 'Teardown result\n'
  for line in "${RESULTS[@]}"; do
    printf '  %s\n' "$line"
  done
  printf '\nRemaining manual steps:\n'
  printf '  1. Empty + delete retained S3 buckets (archive + CUR + access-logs).\n'
  printf '  2. Cancel KMS keys pending deletion if desired (30-day minimum).\n'
  printf '  3. Run aws organizations close-account per account from management.\n'
  printf '  4. Accounts stay SUSPENDED for 90 days before full removal.\n'
}

main() {
  parse_args "$@"
  if [ "$DRY_RUN" = true ]; then
    require_tools npx
  else
    require_tools npx aws
  fi

  build_stack_list
  plan_stacks

  if [ "$DRY_RUN" = true ]; then
    printf '\nDry run complete. No AWS calls to destroy were made.\n'
    exit 0
  fi

  confirm

  local failures=0
  destroy_planned_stacks || failures=$?
  print_summary
  if [ "$failures" -gt 0 ]; then
    printf '\n%s stack(s) failed to destroy cleanly. Teardown is INCOMPLETE.\n' "$failures" >&2
    exit 1
  fi
  printf '\nTeardown complete.\n'
}

main "$@"
