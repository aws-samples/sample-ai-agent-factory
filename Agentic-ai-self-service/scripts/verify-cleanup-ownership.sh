#!/usr/bin/env bash
# Proves, against real AWS, that `cleanup.sh` deletes only what this stack owns.
#
# Why this is a live script and not a unit test: the behavior under test is a
# refusal. Everything cleanup.sh sweeps is matched by an account-global NAME
# PREFIX — Cognito "AgentCore*", secrets "agentcore-connector/" and
# "agentcore-otel/", IAM roles "AgentCoreMemory-*" / "AgentCoreRuntime-*" — so on
# an account running two deployments of this platform (dev + prod, or two teams,
# which is routine because customers deploy and delete often) a teardown used to
# destroy the other deployment's resources, including secrets holding raw
# customer credentials. A mocked test would assert against a transcription of
# the JMESPath filters rather than the filters the AWS CLI actually evaluates,
# which is precisely where the bug lived.
#
# It plants three decoys per swept namespace — owned by this stack, owned by a
# DIFFERENT stack, and untagged — then runs the real sweep_orphan_resources and
# asserts exactly one of the three is gone. Anything it plants, it removes,
# including on failure.
#
# Usage:  AWS_REGION=us-east-1 ./scripts/verify-cleanup-ownership.sh
#
# SAFETY: run this only in an account you are willing to sweep. It executes the
# real sweep, so any genuinely orphaned platform resource tagged for this stack
# will be deleted — that is the sweep working as designed.

set -uo pipefail

ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_NAME="${PROJECT_NAME:-agentcore-workflow}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

THIS_STACK="${PROJECT_NAME}-${ENVIRONMENT_NAME}-${AWS_REGION}"
# A plausible co-resident deployment: same project, different environment. This is
# the case ManagedBy=agentcore-flows cannot distinguish, which is why the owner
# tag names the stack instance instead of the product.
OTHER_STACK="${PROJECT_NAME}-prod-${AWS_REGION}"
SUFFIX="ownguard$(date +%s)"

PASS=0
FAIL=0

die() {
  echo "FATAL: $*" >&2
  exit 1
}

# A decoy that failed to plant would make its assertion meaningless in BOTH
# directions, so refuse to run rather than report on nothing.
require() {
  [[ -n "${2:-}" ]] || die "could not plant decoy: $1"
  echo "  planted ${1} -> ${2}"
}

ok() {
  PASS=$((PASS + 1))
  echo "  PASS  $*"
}
bad() {
  FAIL=$((FAIL + 1))
  echo "  FAIL  $*"
}

# ── Decoy bookkeeping ────────────────────────────────────────────────
#
# Cleanup enumerates by the run's unique SUFFIX rather than by ARNs collected at
# plant time. The plant helpers are called in command substitutions, which run in
# a subshell, so appending to an array inside them was silently lost and the trap
# ran with nothing to remove -- verified by an earlier run that left decoy pools
# and secrets (including a decoy tagged for a foreign stack) behind in the
# account. Enumerating also means a run that dies mid-plant still cleans up.
secret_exists() {
  # describe-secret keeps answering for a while after a force delete, returning a
  # DeletedDate instead of 404 — so presence of the record is NOT evidence the
  # secret survived. Read DeletedDate and treat any value as gone.
  local deleted
  deleted=$(aws secretsmanager describe-secret --secret-id "$1" --region "${AWS_REGION}" \
    --query "DeletedDate" --output text 2>/dev/null) || return 1
  [[ -z "${deleted}" || "${deleted}" == "None" ]]
}
pool_exists() {
  aws cognito-idp describe-user-pool --user-pool-id "$1" --region "${AWS_REGION}" \
    --query "UserPool.Id" --output text >/dev/null 2>&1
}
role_exists() {
  aws iam get-role --role-name "$1" --query "Role.RoleName" --output text >/dev/null 2>&1
}

plant_secret() {
  local name="$1" owner="$2" tags=""
  if [[ -n "${owner}" ]]; then
    tags="Key=AgentCoreStack,Value=${owner} Key=ManagedBy,Value=agentcore-flows"
  fi
  # Placeholder value — never a real credential. The sweep matches on name and
  # tags and never reads the value.
  local arn
  # shellcheck disable=SC2086
  arn=$(aws secretsmanager create-secret --name "${name}" \
    --secret-string '{"decoy":"not-a-credential"}' \
    --description "cleanup ownership guard decoy - safe to delete" \
    ${tags:+--tags ${tags}} \
    --region "${AWS_REGION}" --query "ARN" --output text) || return 1
  echo "${arn}"
}

plant_pool() {
  local name="$1" owner="$2" tag_arg=()
  if [[ -n "${owner}" ]]; then
    tag_arg=(--user-pool-tags "AgentCoreStack=${owner},ManagedBy=agentcore-flows")
  fi
  local pid
  pid=$(aws cognito-idp create-user-pool --pool-name "${name}" ${tag_arg[@]+"${tag_arg[@]}"} \
    --region "${AWS_REGION}" --query "UserPool.Id" --output text) || return 1
  echo "${pid}"
}

plant_role() {
  local name="$1" owner="$2" tag_arg=()
  if [[ -n "${owner}" ]]; then
    tag_arg=(--tags "Key=AgentCoreStack,Value=${owner}" "Key=ManagedBy,Value=agentcore-flows")
  fi
  aws iam create-role --role-name "${name}" \
    --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --description "cleanup ownership guard decoy - safe to delete" \
    ${tag_arg[@]+"${tag_arg[@]}"} >/dev/null || return 1
  echo "${name}"
}

# Removes every decoy carrying $1 (defaults to this run's SUFFIX). Exported as a
# parameter so a previous run's leftovers can be cleaned by passing its suffix.
teardown_decoys() {
  local suffix="${1:-${SUFFIX}}"
  echo
  echo "-- Removing decoys matching ${suffix} --"
  local item
  for item in $(aws secretsmanager list-secrets --region "${AWS_REGION}" \
    --query "SecretList[?contains(Name, '${suffix}')].ARN" --output text 2>/dev/null); do
    aws secretsmanager delete-secret --secret-id "${item}" \
      --force-delete-without-recovery --region "${AWS_REGION}" >/dev/null 2>&1 &&
      echo "  removed secret ${item}"
  done
  for item in $(aws cognito-idp list-user-pools --max-results 60 --region "${AWS_REGION}" \
    --query "UserPools[?contains(Name, '${suffix}')].Id" --output text 2>/dev/null); do
    aws cognito-idp delete-user-pool --user-pool-id "${item}" \
      --region "${AWS_REGION}" >/dev/null 2>&1 && echo "  removed pool ${item}"
  done
  for item in $(aws iam list-roles \
    --query "Roles[?contains(RoleName, '${suffix}')].RoleName" --output text 2>/dev/null); do
    aws iam delete-role --role-name "${item}" >/dev/null 2>&1 && echo "  removed role ${item}"
  done
}
trap teardown_decoys EXIT

# ── Plant ────────────────────────────────────────────────────────────
echo "Stack under test : ${THIS_STACK}"
echo "Foreign stack    : ${OTHER_STACK}"
echo
echo "── Planting decoys ──"

CONN_OWN=$(plant_secret "agentcore-connector/${SUFFIX}/own" "${THIS_STACK}")
require "CONN_OWN" "${CONN_OWN}"
CONN_FOREIGN=$(plant_secret "agentcore-connector/${SUFFIX}/foreign" "${OTHER_STACK}")
require "CONN_FOREIGN" "${CONN_FOREIGN}"
CONN_UNTAGGED=$(plant_secret "agentcore-connector/${SUFFIX}/untagged" "")
require "CONN_UNTAGGED" "${CONN_UNTAGGED}"
OTEL_OWN=$(plant_secret "agentcore-otel/custom/${SUFFIX}-own" "${THIS_STACK}")
require "OTEL_OWN" "${OTEL_OWN}"
OTEL_FOREIGN=$(plant_secret "agentcore-otel/custom/${SUFFIX}-foreign" "${OTHER_STACK}")
require "OTEL_FOREIGN" "${OTEL_FOREIGN}"
# The admin-managed platform secret is excluded by name, not by tag, and outlives
# every stack (scripts/bootstrap-otel-secret.sh). Prove the exclusion still holds
# even when the secret carries THIS stack's owner tag.
OTEL_PLATFORM=$(plant_secret "agentcore-otel/platform/${SUFFIX}" "${THIS_STACK}")
require "OTEL_PLATFORM" "${OTEL_PLATFORM}"

POOL_OWN=$(plant_pool "AgentCore-${SUFFIX}-own" "${THIS_STACK}")
require "POOL_OWN" "${POOL_OWN}"
POOL_FOREIGN=$(plant_pool "AgentCore-${SUFFIX}-foreign" "${OTHER_STACK}")
require "POOL_FOREIGN" "${POOL_FOREIGN}"
POOL_UNTAGGED=$(plant_pool "AgentCore-${SUFFIX}-untagged" "")
require "POOL_UNTAGGED" "${POOL_UNTAGGED}"

MEM_OWN=$(plant_role "AgentCoreMemory-${SUFFIX}-own" "${THIS_STACK}")
require "MEM_OWN" "${MEM_OWN}"
MEM_FOREIGN=$(plant_role "AgentCoreMemory-${SUFFIX}-foreign" "${OTHER_STACK}")
require "MEM_FOREIGN" "${MEM_FOREIGN}"
MEM_UNTAGGED=$(plant_role "AgentCoreMemory-${SUFFIX}-untagged" "")
require "MEM_UNTAGGED" "${MEM_UNTAGGED}"

RT_OWN=$(plant_role "AgentCoreRuntime-${SUFFIX}-own" "${THIS_STACK}")
require "RT_OWN" "${RT_OWN}"
RT_FOREIGN=$(plant_role "AgentCoreRuntime-${SUFFIX}-foreign" "${OTHER_STACK}")
require "RT_FOREIGN" "${RT_FOREIGN}"
# The cross-region regression: a us-east-1 teardown used to delete the Frankfurt
# deployment's CDK shared runtime role, which every agent there assumes. The real
# name is AgentCoreRuntime-{project}-{env}-{region}-shared; that plus the run
# suffix overruns IAM's 64-character role-name limit, so keep the two parts the
# sweep actually keys on -- the AgentCoreRuntime- prefix and the -shared suffix.
RT_CFN_OTHER_REGION=$(plant_role "AgentCoreRuntime-${SUFFIX}-eu-west-9-shared" "")
require "RT_CFN_OTHER_REGION" "${RT_CFN_OTHER_REGION}"

echo
echo "── Running the real sweep_orphan_resources from cleanup.sh ──"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/cleanup.sh"
set +e
sweep_orphan_resources

echo
echo "── Assertions ──"

assert_gone() {
  local kind="$1" id="$2" label="$3"
  if "${kind}_exists" "${id}"; then
    bad "${label} still exists — the sweep failed to delete a resource it owns"
  else
    ok "${label} deleted"
  fi
}
assert_survived() {
  local kind="$1" id="$2" label="$3"
  if "${kind}_exists" "${id}"; then
    ok "${label} survived"
  else
    bad "${label} was DELETED — cross-deployment destruction"
  fi
}

assert_gone     secret "${CONN_OWN}"            "connector secret owned by this stack"
assert_survived secret "${CONN_FOREIGN}"        "connector secret owned by ${OTHER_STACK}"
assert_survived secret "${CONN_UNTAGGED}"       "untagged connector secret"
assert_gone     secret "${OTEL_OWN}"            "OTEL secret owned by this stack"
assert_survived secret "${OTEL_FOREIGN}"        "OTEL secret owned by ${OTHER_STACK}"
assert_survived secret "${OTEL_PLATFORM}"       "agentcore-otel/platform/ secret (name-excluded)"

assert_gone     pool "${POOL_OWN}"              "Cognito pool owned by this stack"
assert_survived pool "${POOL_FOREIGN}"          "Cognito pool owned by ${OTHER_STACK}"
assert_survived pool "${POOL_UNTAGGED}"         "untagged Cognito pool"

assert_gone     role "${MEM_OWN}"               "memory role owned by this stack"
assert_survived role "${MEM_FOREIGN}"           "memory role owned by ${OTHER_STACK}"
assert_survived role "${MEM_UNTAGGED}"          "untagged memory role"

assert_gone     role "${RT_OWN}"                "runtime role owned by this stack"
assert_survived role "${RT_FOREIGN}"            "runtime role owned by ${OTHER_STACK}"
assert_survived role "${RT_CFN_OTHER_REGION}"   "another region's CDK shared runtime role"

echo
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
