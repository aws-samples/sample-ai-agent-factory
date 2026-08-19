/**
 * Platform-defined Bedrock Guardrail profiles.
 *
 * Spec reference: §2.4.4 L1786-1792. Three profiles MUST exist:
 *   - Baseline — mandatory default for every workload
 *   - Internal Tool — opt-in, requires platform approval
 *   - Customer-Facing — strictest; opt-in, requires platform approval
 *
 * The profile definitions themselves live in packages/bedrock-guardrails/,
 * which is implemented in Phase 3. This file is the enumerative source of
 * truth for profile IDs used by SCPs, VPCE policies, IAM scopes, and CI
 * conformance tests.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export type GuardrailProfile = 'baseline' | 'internal-tool' | 'customer-facing';

export const GUARDRAIL_PROFILES: Readonly<Record<GuardrailProfile, string>> = {
  'baseline': 'agenticai-guardrail-baseline',
  'internal-tool': 'agenticai-guardrail-internal-tool',
  'customer-facing': 'agenticai-guardrail-customer-facing',
} as const;
