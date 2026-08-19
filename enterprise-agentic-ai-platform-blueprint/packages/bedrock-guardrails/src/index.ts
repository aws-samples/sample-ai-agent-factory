/**
 * @agenticai/bedrock-guardrails
 *
 * Three guardrail profiles (spec §2.4.4 L1786-1792):
 *   - Baseline         — mandatory default, HIGH content filters, Standard
 *                        prompt-attack detection, AU-specific PII masking.
 *   - Internal Tool    — opt-in; lower strictness for internal tooling.
 *   - Customer-Facing  — strictest; denied topics include financial advice.
 *
 * Plus the Guardrail Admin IAM role (spec §2.4.3 L1612-1634 / R-BED-011/012)
 * deployed only in the platform account. SCP-05 (Phase 1) denies guardrail
 * modification anywhere else.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export { GuardrailAdminRole, type GuardrailAdminRoleProps } from './guardrail-admin-role';
export { PlatformBaselineGuardrail, type PlatformBaselineGuardrailProps } from './baseline-guardrail';
