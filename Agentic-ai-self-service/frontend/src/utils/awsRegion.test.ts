/**
 * The deployment region is read in exactly one place so that a Frankfurt
 * deployment cannot end up with `us-east-1` in some call sites and
 * `eu-central-1` in others.
 *
 * `getRegionPrefixFor` must stay in agreement with three backend/infra
 * implementations — see the doc comment on the function itself. The consequence
 * of divergence is concrete: a `us.` Bedrock inference profile does not exist in
 * eu-central-1, so the agent accepts the model ID and then fails on every
 * invoke.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  AWS_AGENT_REGISTRY_SUPPORTED_REGIONS,
  DEFAULT_AWS_REGION,
  MANAGED_WEB_SEARCH_SUPPORTED_REGIONS,
  SUPPORTED_EMEA_SINGLE_REGION_RELEASES,
  awsAgentRegistryUnavailableReason,
  getDeploymentRegion,
  getRegionFeatureProfile,
  getRegionPrefixFor,
  managedWebSearchUnavailableReason,
  paletteToolUnavailableReason,
  templateUnavailableReason,
} from './awsRegion';

describe('getRegionPrefixFor', () => {
  it.each([
    ['us-east-1', 'us'],
    ['us-west-2', 'us'],
    ['eu-central-1', 'eu'],
    ['eu-west-1', 'eu'],
    // apac, not ap: `ap.` is not a real Bedrock prefix in any region.
    ['ap-northeast-1', 'apac'],
    ['ap-southeast-2', 'apac'],
  ])('maps %s to %s', (region, expected) => {
    expect(getRegionPrefixFor(region)).toBe(expected);
  });

  it.each(['', 'ca-central-1', 'sa-east-1', 'nonsense'])(
    'falls back to us for %s, which has no cross-region profile family',
    (region) => {
      expect(getRegionPrefixFor(region)).toBe('us');
    }
  );
});

describe('getDeploymentRegion', () => {
  const original = import.meta.env.VITE_AWS_REGION;

  beforeEach(() => {
    vi.stubEnv('VITE_AWS_REGION', '');
  });

  afterEach(() => {
    vi.stubEnv('VITE_AWS_REGION', original ?? '');
    vi.unstubAllEnvs();
  });

  it('reads VITE_AWS_REGION', () => {
    vi.stubEnv('VITE_AWS_REGION', 'eu-central-1');
    expect(getDeploymentRegion()).toBe('eu-central-1');
  });

  it('falls back to us-east-1 when unset (local dev without a .env)', () => {
    vi.stubEnv('VITE_AWS_REGION', '');
    expect(getDeploymentRegion()).toBe(DEFAULT_AWS_REGION);
    expect(DEFAULT_AWS_REGION).toBe('us-east-1');
  });
});


describe('regional feature profiles', () => {
  it('supports the four reviewed EMEA single-region deployments', () => {
    expect([...SUPPORTED_EMEA_SINGLE_REGION_RELEASES]).toEqual([
      'eu-central-1',
      'eu-west-1',
      'eu-west-2',
      'eu-west-3',
    ]);
  });

  it.each(['eu-central-1', 'eu-west-2', 'eu-west-3'])(
    '%s keeps core support and reports unavailable optional AWS services',
    (region) => {
      const profile = getRegionFeatureProfile(region);
      expect(profile.releaseSupported).toBe(true);
      expect(profile.managedWebSearch).toBe(false);
      expect(profile.awsAgentRegistry).toBe(false);
      expect(profile.unsupportedFeatures).toEqual([
        'Amazon Bedrock AgentCore Web Search',
        'AWS Agent Registry federation',
      ]);
    },
  );

  it('keeps the full feature profile in Ireland', () => {
    expect(getRegionFeatureProfile('eu-west-1')).toMatchObject({
      releaseSupported: true,
      managedWebSearch: true,
      awsAgentRegistry: true,
      unsupportedFeatures: [],
    });
  });

  it('does not infer Stockholm support from its enabled account status', () => {
    expect(getRegionFeatureProfile('eu-north-1').releaseSupported).toBe(false);
  });

  it('pins the AWS-documented optional-feature Region sets', () => {
    expect([...MANAGED_WEB_SEARCH_SUPPORTED_REGIONS]).toEqual([
      'ap-northeast-1',
      'eu-west-1',
      'us-east-1',
    ]);
    expect([...AWS_AGENT_REGISTRY_SUPPORTED_REGIONS]).toEqual([
      'ap-northeast-1',
      'ap-southeast-2',
      'eu-west-1',
      'us-east-1',
      'us-west-2',
    ]);
  });

  it('blocks only dedicated managed-search templates and tools outside Ireland', () => {
    expect(templateUnavailableReason('web-search-agent', 'eu-central-1')).toMatch(/Frankfurt/);
    expect(templateUnavailableReason('strands-gateway-agent', 'eu-west-2')).toBeNull();
    expect(templateUnavailableReason('customer-support-assistant', 'eu-west-3')).toBeNull();
    expect(paletteToolUnavailableReason('web_search', 'eu-west-3')).toMatch(/Paris/);
    expect(paletteToolUnavailableReason('weather_api', 'eu-west-3')).toBeNull();
  });

  it('names the DynamoDB fallback and Ireland alternative', () => {
    expect(awsAgentRegistryUnavailableReason('eu-central-1')).toMatch(
      /built-in DynamoDB agent registry remains available/,
    );
    expect(managedWebSearchUnavailableReason('eu-west-2')).toMatch(/deploy it in eu-west-1/);
    expect(awsAgentRegistryUnavailableReason('eu-west-1')).toBeNull();
    expect(managedWebSearchUnavailableReason('eu-west-1')).toBeNull();
  });
});
