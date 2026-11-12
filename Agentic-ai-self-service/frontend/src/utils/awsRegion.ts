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

export const SUPPORTED_SINGLE_REGION_RELEASES = [
  "eu-central-1",
  "eu-west-1",
  "eu-west-2",
  "eu-west-3",
  "us-east-1",
  "us-east-2",
  "us-west-2",
] as const;

/**
 * Whether `region` is one of the selected production release Regions.
 *
 * Mirrors `region_capabilities.require_release_region` on the backend. The UI uses
 * it to avoid OFFERING an inadmissible target and to refuse a submit before the
 * request leaves the browser; the backend remains the enforcement point.
 */
export function isReleaseRegion(region: unknown): region is (typeof SUPPORTED_SINGLE_REGION_RELEASES)[number] {
  return (
    typeof region === "string"
    && (SUPPORTED_SINGLE_REGION_RELEASES as readonly string[]).includes(region)
  );
}

/** The same actionable refusal text the backend returns, for a client-side refusal. */
export function releaseRegionUnavailableReason(region: unknown, surface: string): string {
  const shown = typeof region === "string" && region ? region : "<unset>";
  const selected = SUPPORTED_SINGLE_REGION_RELEASES
    .map((candidate) => `${getRegionDisplayName(candidate)} (${candidate})`)
    .join(", ");
  return `Region ${shown} is not a selected production release Region, so ${surface} is refused. `
    + `Deploy in exactly one of: ${selected}.`;
}

export const MANAGED_WEB_SEARCH_SUPPORTED_REGIONS = [
  "ap-northeast-1",
  "ap-southeast-2",
  "eu-west-1",
  "us-east-1",
  "us-east-2",
  "us-west-2",
] as const;

export const AWS_AGENT_REGISTRY_SUPPORTED_REGIONS = [
  "ap-northeast-1",
  "ap-southeast-2",
  "ca-central-1",
  "eu-central-1",
  "eu-west-1",
  "eu-west-2",
  "us-east-1",
  "us-east-2",
  "us-west-2",
] as const;

/**
 * Regions where AgentCore Runtime platform version V2 is documented
 * (runtime-how-it-works.html, "Platform versions > Supported Regions", read 2026-11-12).
 * Twin of RUNTIME_PLATFORM_V2_SUPPORTED_REGIONS in backend/.../region_capabilities.py.
 */
export const RUNTIME_PLATFORM_V2_SUPPORTED_REGIONS = [
  "ap-northeast-1",
  "ap-south-1",
  "ap-southeast-1",
  "ap-southeast-2",
  "eu-central-1",
  "eu-north-1",
  "eu-west-1",
  "us-east-1",
  "us-east-2",
  "us-west-2",
] as const;

/**
 * Regions where AgentCore Web Answer (synthesized, cited answers over the managed web
 * index) is documented: US East (N. Virginia) only (developer guide, "Add a Connector
 * target with Web Answer", and the 2026-11 launch note, read 2026-11-12). Twin of
 * WEB_ANSWER_SUPPORTED_REGIONS in backend/.../region_capabilities.py.
 */
export const WEB_ANSWER_SUPPORTED_REGIONS = ["us-east-1"] as const;

/**
 * Regions where Policy in AgentCore can call Amazon Bedrock Guardrails from a policy
 * (the policy node's "Gateway guardrails" profile). Developer guide, "Guardrails in
 * policies" > "Guardrails regional availability", read 2026-11-13. Of the release Regions
 * only N. Virginia, Ohio, Oregon and London have it. Twin of
 * GATEWAY_GUARDRAILS_SUPPORTED_REGIONS in backend/.../region_capabilities.py.
 */
export const GATEWAY_GUARDRAILS_SUPPORTED_REGIONS = [
  "ap-northeast-1",
  "ap-southeast-2",
  "eu-north-1",
  "eu-west-2",
  "us-east-1",
  "us-east-2",
  "us-west-2",
] as const;

const REGION_DISPLAY_NAMES: Record<string, string> = {
  "eu-central-1": "Europe (Frankfurt)",
  "eu-north-1": "Europe (Stockholm)",
  "eu-south-1": "Europe (Milan)",
  "eu-south-2": "Europe (Spain)",
  "eu-west-1": "Europe (Ireland)",
  "eu-west-2": "Europe (London)",
  "eu-west-3": "Europe (Paris)",
  "us-east-1": "US East (N. Virginia)",
  "us-east-2": "US East (Ohio)",
  "us-west-1": "US West (N. California)",
  "us-west-2": "US West (Oregon)",
};

const MANAGED_WEB_SEARCH_TEMPLATE_IDS = new Set(["web-search-agent"]);
const MANAGED_WEB_SEARCH_TOOL_IDS = new Set([
  "web_search",
  "duckduckgo_search",
]);
const MANAGED_WEB_ANSWER_TOOL_IDS = new Set(["web_answer"]);

export interface RegionFeatureProfile {
  region: string;
  displayName: string;
  releaseSupported: boolean;
  managedWebSearch: boolean;
  awsAgentRegistry: boolean;
  /** V2 is how a runtime is started, not a workflow feature: never in unsupportedFeatures. */
  runtimePlatformV2: boolean;
  /** Web Answer IS a workflow feature (a Gateway tool), so it is listed where absent. */
  webAnswer: boolean;
  /** Bedrock Guardrails evaluated by Policy in AgentCore at the Gateway; listed where absent. */
  gatewayGuardrails: boolean;
  unsupportedFeatures: string[];
}

function includesRegion(regions: readonly string[], region: string): boolean {
  return regions.includes(region);
}

export function getRegionDisplayName(region: string): string {
  return REGION_DISPLAY_NAMES[region] ?? region;
}

function formatReleaseAlternatives(regions: readonly string[]): string {
  return SUPPORTED_SINGLE_REGION_RELEASES.filter((region) =>
    includesRegion(regions, region),
  )
    .map((region) => `${getRegionDisplayName(region)} (${region})`)
    .join(", ");
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
  const runtimePlatformV2 = includesRegion(
    RUNTIME_PLATFORM_V2_SUPPORTED_REGIONS,
    region,
  );
  const webAnswer = includesRegion(WEB_ANSWER_SUPPORTED_REGIONS, region);
  const gatewayGuardrails = includesRegion(
    GATEWAY_GUARDRAILS_SUPPORTED_REGIONS,
    region,
  );
  return {
    region,
    displayName: getRegionDisplayName(region),
    releaseSupported: includesRegion(
      SUPPORTED_SINGLE_REGION_RELEASES,
      region,
    ),
    managedWebSearch,
    awsAgentRegistry,
    runtimePlatformV2,
    webAnswer,
    gatewayGuardrails,
    unsupportedFeatures: [
      ...(!managedWebSearch ? ["Amazon Bedrock AgentCore Web Search"] : []),
      ...(!webAnswer ? ["Amazon Bedrock AgentCore Web Answer"] : []),
      ...(!gatewayGuardrails
        ? ["Amazon Bedrock Guardrails in AgentCore Policy"]
        : []),
      ...(!awsAgentRegistry ? ["AWS Agent Registry federation"] : []),
    ],
  };
}

/**
 * Why Web Answer cannot be used here, or null when it can. Mirrors the backend's
 * feature_unavailable_message("web_answer", region).
 */
export function webAnswerUnavailableReason(
  region = getDeploymentRegion(),
): string | null {
  if (includesRegion(WEB_ANSWER_SUPPORTED_REGIONS, region)) return null;
  const alternatives = formatReleaseAlternatives(WEB_ANSWER_SUPPORTED_REGIONS);
  return `Amazon Bedrock AgentCore Web Answer is not available in ${getRegionDisplayName(region)} (${region}). Remove the Web Answer tool from this workflow (managed Web Search remains available where its own Region list allows), or deploy it in one of these supported release Regions: ${alternatives}.`;
}

/**
 * Why Runtime platform V2 cannot be selected here, or null when it can. Mirrors the
 * backend's feature_unavailable_message("runtime_platform_v2", region).
 */
export function runtimePlatformV2UnavailableReason(
  region = getDeploymentRegion(),
): string | null {
  if (includesRegion(RUNTIME_PLATFORM_V2_SUPPORTED_REGIONS, region)) return null;
  const alternatives = formatReleaseAlternatives(
    RUNTIME_PLATFORM_V2_SUPPORTED_REGIONS,
  );
  return `AgentCore Runtime platform version V2 is not available in ${getRegionDisplayName(region)} (${region}). Deploy this runtime on platform version V1 (the default) here, or choose one of these supported release Regions for V2: ${alternatives}.`;
}

/**
 * Why a Gateway guardrails profile cannot be selected here, or null when it can. Mirrors
 * the backend's feature_unavailable_message("gateway_guardrails", region).
 */
export function gatewayGuardrailsUnavailableReason(
  region = getDeploymentRegion(),
): string | null {
  if (includesRegion(GATEWAY_GUARDRAILS_SUPPORTED_REGIONS, region)) return null;
  const alternatives = formatReleaseAlternatives(
    GATEWAY_GUARDRAILS_SUPPORTED_REGIONS,
  );
  return `Amazon Bedrock Guardrails in AgentCore Policy are not available in ${getRegionDisplayName(region)} (${region}). Set the policy node's Gateway guardrails profile to off (Cedar tool authorization still applies), or deploy it in one of these supported release Regions: ${alternatives}.`;
}

export function managedWebSearchUnavailableReason(
  region = getDeploymentRegion(),
): string | null {
  if (includesRegion(MANAGED_WEB_SEARCH_SUPPORTED_REGIONS, region)) return null;
  const alternatives = formatReleaseAlternatives(
    MANAGED_WEB_SEARCH_SUPPORTED_REGIONS,
  );
  return `Amazon Bedrock AgentCore Web Search is not available in ${getRegionDisplayName(region)} (${region}). Remove managed Web Search from this workflow or deploy it in one of these supported release Regions: ${alternatives}.`;
}

export function awsAgentRegistryUnavailableReason(
  region = getDeploymentRegion(),
): string | null {
  if (includesRegion(AWS_AGENT_REGISTRY_SUPPORTED_REGIONS, region)) return null;
  const alternatives = formatReleaseAlternatives(
    AWS_AGENT_REGISTRY_SUPPORTED_REGIONS,
  );
  return `AWS Agent Registry federation is not available in ${getRegionDisplayName(region)} (${region}). The built-in DynamoDB agent registry remains available; federation is available in these supported release Regions: ${alternatives}.`;
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
  if (!toolId) return null;
  if (MANAGED_WEB_SEARCH_TOOL_IDS.has(toolId)) return managedWebSearchUnavailableReason(region);
  if (MANAGED_WEB_ANSWER_TOOL_IDS.has(toolId)) return webAnswerUnavailableReason(region);
  return null;
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
