/**
 * PLATFORM_APPROVED_REGIONS — regions where workloads may operate.
 *
 * Spec reference: §2.2.7 L788-822. SCP-06 denies all non-IAM/STS/Orgs/Support/Budgets
 * actions in regions outside this allow-list. `us-west-2` is the
 * baseline reference-deployment region; `us-east-1` is retained because
 * several AWS services (Organizations, IAM Identity Center, CloudFront) are
 * global-from-us-east-1 and workloads occasionally touch them. It is also
 * the region where the D-03 v3 live verification was performed (2026-05-05).
 * `eu-west-1` is the EMEA onboarding target: as of 2026-09-26 it is the only
 * European Region that offers AWS Agent Registry, which the governed R2 tool
 * path requires. Adding it to this list authorizes only the Region boundary;
 * live readiness still requires the independent EMEA matrix in README §15.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

/**
 * Regions approved for workload operation. Extend only after updating the
 * region-onboarding runbook in README §11 Choice architecture.
 */
export const PLATFORM_APPROVED_REGIONS: readonly string[] = [
  'us-west-2',
  'us-east-1',
  'eu-west-1',
] as const;
