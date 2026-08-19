#!/usr/bin/env bash
# teardown.sh — reverse-dependency cleanup of a reference deployment.
#
# DESTROYS STACKS AND MANY RESOURCES. Not idempotent across Control-Tower
# account closures — Control Tower accounts enter a 90-day SUSPENDED state.
# See README section 16 (Cleanup) for the full teardown order and caveats.
#
# Usage: bash scripts/teardown.sh
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

echo "============================================================"
echo "AgenticAI teardown"
echo "------------------------------------------------------------"
echo "This will destroy CDK stacks in reverse dependency order,"
echo "empty non-versioned buckets, and report resources that require"
echo "manual deletion (KMS pending-delete, retained-on-delete S3)."
echo ""
echo "Control-Tower closed accounts enter 90-day SUSPENDED state;"
echo "that portion is NOT automated here — perform via the AWS"
echo "Organizations console or 'aws organizations close-account'."
echo "============================================================"
read -r -p "Continue? [y/N] " yn
case "$yn" in
  y|Y) ;;
  *) echo "Aborted."; exit 1;;
esac

STACKS=(
  "AgenticAI-WorkloadPipelineStack"
  "AgenticAI-PlatformPipelineStack"
  "AgenticAI-Workload-AppStack"
  "AgenticAI-Workload-NetworkStack"
  "AgenticAI-Platform-RegistryStack"
  "AgenticAI-Platform-GuardrailStack"
  "AgenticAI-Platform-AuditStack"
  "AgenticAI-Platform-LogArchiveStack"
  "AgenticAI-Management-OrgStack"
)

for stack in "${STACKS[@]}"; do
  echo ""
  echo "-> Destroying $stack"
  npx cdk destroy --force "$stack" || echo "   (stack not present or already destroyed)"
done

echo ""
echo "Teardown script complete. Remaining manual steps:"
echo "  1. Empty + delete any retained S3 buckets (archive + CUR + access-logs)."
echo "  2. Cancel KMS keys pending deletion if desired (30-day minimum)."
echo "  3. Run aws organizations close-account per account via the management account."
echo "  4. Accounts enter SUSPENDED state for 90 days before full removal."
