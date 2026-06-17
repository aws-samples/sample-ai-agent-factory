#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Self-paced (self-service) deployer for "Building an Enterprise Agentic AI
# Platform on Amazon Bedrock AgentCore".
#
# This is the single entry point for participants running the workshop in their
# OWN AWS account (outside an AWS-run Workshop Studio event). It is a thin front
# end over the repository's deploy-cfn.sh engine: it resolves your target region,
# runs a few preflight checks (region allow-list + Bedrock/AgentCore dependency
# probes), deploys ALL workshop CloudFormation stacks listed in contentspec.yaml
# (including the browser-based Code Editor IDE), and then prints the IDE URL and
# password so you can open the same environment AWS-run event participants get.
#
# The workshop deploys into your chosen supported region. Region is resolved
# from (in order): --region flag, AWS_REGION, AWS_DEFAULT_REGION, then
# `aws configure get region`. The recommended (validated) regions are:
#   us-west-2 (default), us-east-1, eu-west-1
# (AgentCore Registry is not GA in eu-central-1 / ap-southeast-1 — those break
#  Modules 3b/4, so they are intentionally excluded.)
#
# Usage:   ./scripts/self-service-deploy.sh [--region <region>]
# Cleanup: ./deploy-cfn.sh destroy        (see content/cleanup/)

set -euo pipefail

# Recommended/validated regions. Outside this list we WARN (not hard-fail) and
# rely on the live Bedrock/AgentCore dependency probes below to confirm support.
ALLOWED_REGIONS=(us-west-2 us-east-1 eu-west-1)

# --- Region resolver: flag > AWS_REGION > AWS_DEFAULT_REGION > aws config -----
# Refuse ONLY when empty; never refuse on inequality to a literal, and never
# silently fall back to a default region.
REGION_FLAG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION_FLAG="${2:-}"; shift 2 ;;
    --region=*) REGION_FLAG="${1#*=}"; shift ;;
    -h|--help)
      echo "Usage: $0 [--region <region>]"
      exit 0 ;;
    *)
      echo "ERROR: unknown argument '$1'"
      echo "Usage: $0 [--region <region>]"
      exit 1 ;;
  esac
done

REGION="$REGION_FLAG"
[[ -n "$REGION" ]] || REGION="${AWS_REGION:-}"
[[ -n "$REGION" ]] || REGION="${AWS_DEFAULT_REGION:-}"
[[ -n "$REGION" ]] || REGION="$(aws configure get region 2>/dev/null || true)"

if [[ -z "$REGION" ]]; then
  echo "ERROR: no AWS region resolved. Set one of the following and re-run:"
  echo "  - pass --region <region>            (e.g. --region us-west-2)"
  echo "  - export AWS_REGION=<region>"
  echo "  - aws configure set region <region>"
  echo
  echo "Recommended regions: ${ALLOWED_REGIONS[*]}"
  exit 1
fi

# Propagate to every child aws/CFN call so they all target the same region.
export AWS_REGION="$REGION"

# deploy-cfn.sh and contentspec.yaml live at the repository root; this wrapper
# lives in scripts/. Run everything from the repo root so deploy-cfn.sh can find
# contentspec.yaml.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=============================================================="
echo " Agentic AI Platform — Self-Paced Deployment"
echo "=============================================================="
echo "Repo root: $REPO_ROOT"
echo

# --- Preflight: required tooling -------------------------------------------
missing=()
for tool in aws yq; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "ERROR: required tool(s) not found on PATH: ${missing[*]}"
  echo "  - aws : AWS CLI v2  (https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)"
  echo "  - yq  : YAML processor  (https://github.com/mikefarah/yq)"
  exit 1
fi

# --- Preflight: authenticated AWS account ----------------------------------
if ! CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)"; then
  echo "ERROR: unable to call AWS STS. Configure credentials for a dedicated,"
  echo "       disposable AWS account you own. The deploy creates IAM roles and"
  echo "       infrastructure, so the principal needs broad permissions:"
  echo "       use AdministratorAccess, or attach the scoped"
  echo "       policies in static/cfn/self-service-deploy-policy-{1..4}.json."
  echo "       Try:  aws configure   (or set AWS_PROFILE / AWS_ACCESS_KEY_ID etc.)"
  exit 1
fi
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "AWS identity : $CALLER_ARN"
echo "AWS account  : $ACCOUNT_ID"

# --- Preflight: region allow-list (warn only) ------------------------------
echo "Region       : $REGION"
in_allow_list="no"
for r in "${ALLOWED_REGIONS[@]}"; do
  [[ "$r" == "$REGION" ]] && in_allow_list="yes" && break
done
if [[ "$in_allow_list" == "no" ]]; then
  echo "WARNING: '$REGION' is not in the recommended/validated region list:"
  echo "         ${ALLOWED_REGIONS[*]}"
  echo "         Continuing, but the dependency probes below decide if it works."
fi
echo

# --- Preflight: live Bedrock + AgentCore dependency probes ------------------
# These run BEFORE any CloudFormation call so we fail loud and early instead of
# half-deploying into a region missing a required dependency. We never silently
# fall back to another region.
echo "Preflight: checking required services in $REGION ..."

# Bedrock: a Claude Sonnet cross-region inference profile must exist.
if ! PROFILES="$(aws bedrock list-inference-profiles --region "$REGION" \
      --query "inferenceProfileSummaries[].inferenceProfileId" \
      --output text 2>/dev/null)"; then
  echo
  echo "ERROR: unable to call 'bedrock list-inference-profiles' in $REGION."
  echo "       Amazon Bedrock may not be available there, or your principal"
  echo "       lacks bedrock:ListInferenceProfiles. The workshop agents require"
  echo "       a Claude Sonnet cross-region inference profile."
  echo "       Bedrock console: https://console.aws.amazon.com/bedrock/home?region=$REGION#/overview"
  exit 1
fi
if ! grep -qi 'claude-sonnet' <<<"$PROFILES"; then
  echo
  echo "ERROR: no Claude Sonnet inference profile found in $REGION."
  echo "       The workshop's default agent model requires a"
  echo "       'claude-sonnet' cross-region inference profile, which is not"
  echo "       available (or not enabled) in this region."
  echo "       Request model access / pick a supported region:"
  echo "       https://console.aws.amazon.com/bedrock/home?region=$REGION#/modelaccess"
  echo "       Recommended regions: ${ALLOWED_REGIONS[*]}"
  exit 1
fi
echo "  [OK] Bedrock Claude Sonnet inference profile available"

# AgentCore: control plane must be reachable in the region.
if ! aws bedrock-agentcore-control list-gateways --region "$REGION" \
      --max-results 1 >/dev/null 2>&1; then
  echo
  echo "ERROR: Amazon Bedrock AgentCore is not available in $REGION (the"
  echo "       'bedrock-agentcore-control list-gateways' probe failed), or your"
  echo "       principal lacks bedrock-agentcore-control permissions."
  echo "       AgentCore Gateway is required by this workshop."
  echo "       AgentCore console: https://console.aws.amazon.com/bedrock-agentcore/home?region=$REGION"
  echo "       Recommended regions: ${ALLOWED_REGIONS[*]}"
  exit 1
fi
echo "  [OK] AgentCore control plane reachable"
echo

echo "This will deploy ALL workshop stacks (LLM Gateway, MCP Registry,"
echo "Tools Gateway, AgentCore, and the Code Editor IDE) into account"
echo "$ACCOUNT_ID / $REGION. This creates billable resources"
echo "(DocumentDB, ECS Fargate, CloudFront, NAT Gateway, Lambda) and takes"
echo "roughly 30-45 minutes. Tear down afterwards with: ./deploy-cfn.sh destroy"
echo

# --- Deploy ----------------------------------------------------------------
./deploy-cfn.sh deploy

# --- Print IDE access details ----------------------------------------------
echo
echo "=============================================================="
echo " Workshop IDE access (code-editor stack)"
echo "=============================================================="
if ! aws cloudformation describe-stacks \
  --stack-name code-editor \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='URL' || OutputKey=='IdePassword'].[OutputKey,OutputValue]" \
  --output table 2>/dev/null; then
  echo "WARNING: could not read code-editor outputs. Check the stack in the"
  echo "         CloudFormation console (region $REGION)."
fi

echo
echo "Next steps:"
echo "  1. Verify the environment:   ./scripts/self-test.sh -r $REGION"
echo "  2. Open the URL above in a browser and sign in with the IdePassword value."
echo "  3. In the IDE, the workshop source is pre-staged at /workshop. Run all"
echo "     module commands in the IDE terminal."
echo "  4. Proceed to Module 1 in the workshop guide."
echo
