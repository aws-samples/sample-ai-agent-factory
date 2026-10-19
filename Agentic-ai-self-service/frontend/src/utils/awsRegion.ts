/**
 * The deployment region, as injected at build time.
 *
 * `VITE_AWS_REGION` is set by `scripts/deploy.sh` from the region the stack was
 * deployed to. This module is the single place that reads it, so that a
 * Frankfurt deployment does not end up with `us-east-1` written into workflow
 * metadata by one call site and `eu-central-1` by another.
 *
 * Kept deliberately dependency-free (no model catalog, no types) so that
 * serialization and auto-save can import it without pulling anything else in.
 */

/**
 * Fallback when `VITE_AWS_REGION` is absent — a local `npm run dev` without a
 * populated `.env`. Matches the CDK context default in `infra/cdk.json`.
 */
export const DEFAULT_AWS_REGION = "us-east-1";

/**
 * The region this frontend build was deployed to.
 */
export function getDeploymentRegion(): string {
  return import.meta.env.VITE_AWS_REGION || DEFAULT_AWS_REGION;
}

export const SUPPORTED_EMEA_SINGLE_REGION_RELEASES = [
  "eu-central-1",
  "eu-west-1",
  "eu-west-2",
  "eu-west-3",
] as const;

export const MANAGED_WEB_SEARCH_SUPPORTED_REGIONS = [
  "ap-northeast-1",
  "eu-west-1",
  "us-east-1",
] as const;

export const AWS_AGENT_REGISTRY_SUPPORTED_REGIONS = [
  "ap-northeast-1",
  "ap-southeast-2",
  "eu-west-1",
  "us-east-1",
  "us-west-2",
] as const;

const REGION_DISPLAY_NAMES: Record<string, string> = {
  "eu-central-1": "Europe (Frankfurt)",
  "eu-west-1": "Europe (Ireland)",
  "eu-west-2": "Europe (London)",
  "eu-west-3": "Europe (Paris)",
};

const MANAGED_WEB_SEARCH_TEMPLATE_IDS = new Set([
  "web-search-agent",
  "strands-gateway-agent",
]);
const MANAGED_WEB_SEARCH_TOOL_IDS = new Set([
  "web_search",
  "duckduckgo_search",
]);

export interface RegionFeatureProfile {
  region: string;
  displayName: string;
  releaseSupported: boolean;
  managedWebSearch: boolean;
  awsAgentRegistry: boolean;
  unsupportedFeatures: string[];
}

function includesRegion(regions: readonly string[], region: string): boolean {
  return regions.includes(region);
}

export function getRegionDisplayName(region: string): string {
  return REGION_DISPLAY_NAMES[region] ?? region;
}

export function getRegionFeatureProfile(
  region = getDeploymentRegion(),
): RegionFeatureProfile {
  const managedWebSearch = includesRegion(
    MANAGED_WEB_SEARCH_SUPPORTED_REGIONS,
    region,
  );
  const awsAgentRegistry = includesRegion(
    AWS_AGENT_REGISTRY_SUPPORTED_REGIONS,
    region,
  );
  return {
    region,
    displayName: getRegionDisplayName(region),
    releaseSupported: includesRegion(
      SUPPORTED_EMEA_SINGLE_REGION_RELEASES,
      region,
    ),
    managedWebSearch,
    awsAgentRegistry,
    unsupportedFeatures: [
      ...(!managedWebSearch ? ["Amazon Bedrock AgentCore Web Search"] : []),
      ...(!awsAgentRegistry ? ["AWS Agent Registry federation"] : []),
    ],
  };
}

export function managedWebSearchUnavailableReason(
  region = getDeploymentRegion(),
): string | null {
  if (includesRegion(MANAGED_WEB_SEARCH_SUPPORTED_REGIONS, region)) return null;
  return `Amazon Bedrock AgentCore Web Search is not available in ${getRegionDisplayName(region)} (${region}). Remove managed Web Search from this workflow or deploy it in eu-west-1.`;
}

export function awsAgentRegistryUnavailableReason(
  region = getDeploymentRegion(),
): string | null {
  if (includesRegion(AWS_AGENT_REGISTRY_SUPPORTED_REGIONS, region)) return null;
  return `AWS Agent Registry federation is not available in ${getRegionDisplayName(region)} (${region}). The built-in DynamoDB agent registry remains available.`;
}

export function templateUnavailableReason(
  templateId: string,
  region = getDeploymentRegion(),
): string | null {
  return MANAGED_WEB_SEARCH_TEMPLATE_IDS.has(templateId)
    ? managedWebSearchUnavailableReason(region)
    : null;
}

export function paletteToolUnavailableReason(
  toolId: string | undefined,
  region = getDeploymentRegion(),
): string | null {
  return toolId && MANAGED_WEB_SEARCH_TOOL_IDS.has(toolId)
    ? managedWebSearchUnavailableReason(region)
    : null;
}

/**
 * Derive the Bedrock cross-region inference prefix from an AWS region.
 * US regions → `us`, EU → `eu`, AP → `apac`, anything else → `us`.
 *
 * APAC is `apac`, not `ap` — `ap.` exists in no region. Verified against
 * `bedrock list-inference-profiles`; see the note in
 * `backend/src/app/services/region_models.py` for the country-prefix caveat
 * (`jp.` / `au.`) that applies to current-generation models in APAC.
 *
 * Must agree with `region_inference_prefix()` in
 * `backend/src/app/services/code_generator.py`, `_region_inference_prefix()` in
 * `backend/src/app/step_handlers/runtime_configure_step.py`, and the
 * `TOOL_GENERATOR_MODEL_ID` expression in `infra/stacks/platform/lambdas.py` —
 * otherwise the model a user picks here is not the model the deployed agent
 * invokes, and a `us.` profile does not exist in eu-central-1 at all.
 */
export function getRegionPrefixFor(region: string): string {
  if (region.startsWith("eu-")) return "eu";
  if (region.startsWith("ap-")) return "apac";
  return "us";
}
