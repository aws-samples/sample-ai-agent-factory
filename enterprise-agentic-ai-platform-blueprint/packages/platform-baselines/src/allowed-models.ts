/**
 * PLATFORM_ALLOWED_MODELS — the Bedrock foundation models that SCP-01 permits.
 *
 * Spec reference: §2.2.2 L599-600. The spec's example allow-list is:
 *   - anthropic.claude-sonnet-4-5-20250929-v1:0
 *   - anthropic.claude-haiku-4-5-20251001-v1:0
 *
 * Any change here must be propagated by CI conformance tests into:
 *   - SCP-01 policy body (packages/organizations/scps/scp-01-model-allowlist.ts)
 *   - Bedrock Runtime VPCE policy (packages/agentic-vpc/endpoint-policies.ts)
 *   - LiteLLM router config (packages/litellm-gateway/config/litellm-config.template.yaml)
 *   - AgentCore execution-role Bedrock resource scope (packages/agentic-app/iam.ts)
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

/**
 * Bedrock foundation-model identifiers on the platform allow-list.
 * Identifiers are region-agnostic; `allowedModelArns` expands them per region.
 */
export const PLATFORM_ALLOWED_MODELS: readonly string[] = [
  'anthropic.claude-sonnet-4-5-20250929-v1:0',
  'anthropic.claude-haiku-4-5-20251001-v1:0',
] as const;

/**
 * Expand the model identifiers into fully-qualified Bedrock foundation-model
 * ARNs for a given region. Spec §2.2.2 uses the `arn:aws:bedrock:<region>::foundation-model/<id>`
 * form (the empty account segment is intentional — foundation-models are
 * service-owned).
 */
export function allowedModelArns(region: string): readonly string[] {
  return PLATFORM_ALLOWED_MODELS.map(
    (modelId) => `arn:aws:bedrock:${region}::foundation-model/${modelId}`,
  );
}

/**
 * System-defined cross-region inference profiles Bedrock requires for
 * Claude 4.5 invocations in `us-east-1` / `us-west-2` (on-demand throughput
 * against the bare foundation-model ARN is **not** supported for these
 * models). Prefix depends on region family: `us.*` for US regions, `eu.*`
 * for EU, plus `global.*` for global. (APAC was removed from the approved
 * region list; re-add `apac` here if a future revision re-adds APAC regions.)
 *
 * IAM + VPCE + SCP scopes MUST include both the foundation-model ARN and
 * the relevant inference-profile ARN for the target region — the profile
 * routes to the foundation model internally and both are checked.
 */
export const SYSTEM_INFERENCE_PROFILE_PREFIXES: readonly string[] = [
  'us',
  'eu',
  'global',
] as const;

/**
 * Map of system-inference-profile prefix → list of backing regions the
 * profile may route to. When a caller invokes `us.anthropic.claude-...`, AWS
 * internally routes to `foundation-model/anthropic.claude-...` in ANY of the
 * regions below. IAM authorisation is evaluated on the routed-to ARN, so the
 * caller's policy MUST permit `foundation-model/...` in every possible
 * destination region for the inference profile to work.
 *
 * Keep this list up-to-date with the AWS docs:
 *   https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference-support.html
 */
export const INFERENCE_PROFILE_BACKING_REGIONS: Readonly<Record<string, readonly string[]>> = {
  us: ['us-east-1', 'us-east-2', 'us-west-2'],
  eu: ['eu-west-1', 'eu-west-2', 'eu-west-3', 'eu-central-1', 'eu-north-1', 'eu-south-1', 'eu-south-2'],
  // Global profiles can route anywhere in the approved set; callers should
  // scope narrowly if concerned. APAC backing regions deliberately excluded.
  global: ['us-east-1', 'us-east-2', 'us-west-2', 'eu-west-1', 'eu-central-1'],
};

/**
 * Emit both foundation-model ARNs (across all regions an inference profile
 * might route to) and cross-region inference-profile ARNs for the target
 * region + account. The account segment is empty for foundation-models
 * (service-owned) and the current account for inference-profiles.
 *
 * M-E fix (security agent F-08): the previous implementation IAM-allowed
 * EU foundation-model ARNs even though `PLATFORM_APPROVED_REGIONS` excludes
 * them. Filter the backing-region union by `PLATFORM_APPROVED_REGIONS` so
 * the IAM allow-list never grants access to a region the platform has
 * deliberately excluded. SCP-06 is the authoritative gate but defence-in-
 * depth at IAM matters when SCPs aren't attached (live deploys).
 */
import { PLATFORM_APPROVED_REGIONS } from './approved-regions';

export function allowedBedrockResources(region: string, accountId: string): readonly string[] {
  const approved = new Set<string>(PLATFORM_APPROVED_REGIONS);
  const foundationArns: string[] = [];
  const backingRegions = new Set<string>([region]);
  for (const prefix of SYSTEM_INFERENCE_PROFILE_PREFIXES) {
    const regions = INFERENCE_PROFILE_BACKING_REGIONS[prefix] ?? [];
    for (const r of regions) {
      if (approved.has(r)) backingRegions.add(r);
    }
  }
  for (const r of backingRegions) {
    for (const modelId of PLATFORM_ALLOWED_MODELS) {
      foundationArns.push(`arn:aws:bedrock:${r}::foundation-model/${modelId}`);
    }
  }

  const profileArns: string[] = [];
  for (const prefix of SYSTEM_INFERENCE_PROFILE_PREFIXES) {
    for (const modelId of PLATFORM_ALLOWED_MODELS) {
      profileArns.push(
        `arn:aws:bedrock:${region}:${accountId}:inference-profile/${prefix}.${modelId}`,
      );
    }
  }
  return [...foundationArns, ...profileArns];
}
