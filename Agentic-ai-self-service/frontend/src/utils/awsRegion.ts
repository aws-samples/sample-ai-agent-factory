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
export const DEFAULT_AWS_REGION = 'us-east-1';

/**
 * The region this frontend build was deployed to.
 */
export function getDeploymentRegion(): string {
  return import.meta.env.VITE_AWS_REGION || DEFAULT_AWS_REGION;
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
  if (region.startsWith('eu-')) return 'eu';
  if (region.startsWith('ap-')) return 'apac';
  return 'us';
}
