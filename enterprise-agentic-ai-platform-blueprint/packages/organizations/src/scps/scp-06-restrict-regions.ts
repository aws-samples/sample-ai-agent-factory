/**
 * SCP-06 — Restrict Region Usage.
 *
 * Spec §2.2.7 L788-822. Denies all non-global actions in regions outside
 * the approved list. Global services (IAM, STS, Organizations, Support,
 * Budgets) are exempted because their endpoints are reached regardless of
 * regional SCP coverage.
 *
 * The approved-region list is sourced from @agenticai/platform-baselines
 * (`PLATFORM_APPROVED_REGIONS`).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { toScpDefinition, type ScpDefinition } from './index';

export function scp06RestrictRegions(approvedRegions: readonly string[]): ScpDefinition {
  if (approvedRegions.length === 0) {
    throw new Error('SCP-06 approved-region list must not be empty.');
  }

  const body = {
    Version: '2012-10-17',
    Statement: [
      {
        Sid: 'DenyActionsOutsideApprovedRegions',
        Effect: 'Deny',
        NotAction: [
          'iam:*',
          'sts:*',
          'organizations:*',
          'support:*',
          'budgets:*',
          'ce:*',
          'cur:*',
          'route53:*',
          'cloudfront:*',
          'waf:*',
          'wafv2:*',
          'globalaccelerator:*',
          'a4b:*',
          'aws-portal:*',
          'artifact:*',
          'health:*',
          'importexport:*',
        ],
        Resource: '*',
        Condition: {
          StringNotEquals: {
            'aws:RequestedRegion': Array.from(approvedRegions),
          },
        },
      },
    ],
  };

  return toScpDefinition(
    'scp-06',
    'AgenticAI-SCP-06-RestrictRegions',
    'Deny non-global actions outside the approved region list (spec §2.2.7).',
    body,
  );
}
