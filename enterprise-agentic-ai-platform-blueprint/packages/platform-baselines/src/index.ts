/**
 * @agenticai/platform-baselines
 *
 * Single source of truth for platform-wide constants that flow into:
 *   - SCP-01 (Bedrock model allow-list) — spec §2.2.2 L576-608
 *   - Bedrock VPCE endpoint policy resource scope — spec §2.3.5 L1120-1142
 *   - LiteLLM router config (D-01 compensating control)
 *   - IAM `Resource` clauses on AgentCore execution roles — spec §2.4.3
 *   - SCP-06 region allow-list — spec §2.2.7 L788-822
 *
 * Conformance CI (`tests/conformance/model-allowlist-ssot.test.ts`) fails if
 * any of the five downstream representations diverge from the values exported
 * here.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export {
  PLATFORM_ALLOWED_MODELS,
  SYSTEM_INFERENCE_PROFILE_PREFIXES,
  allowedModelArns,
  allowedBedrockResources,
} from './allowed-models';
export { PLATFORM_APPROVED_REGIONS } from './approved-regions';
export { GUARDRAIL_PROFILES, type GuardrailProfile } from './guardrail-profiles';
