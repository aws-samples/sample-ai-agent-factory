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
  SUPPORTED_SINGLE_REGION_RELEASES,
  awsAgentRegistryUnavailableReason,
  getDeploymentRegion,
  getRegionFeatureProfile,
  getRegionPrefixFor,
  isReleaseRegion,
  managedWebSearchUnavailableReason,
  releaseRegionUnavailableReason,
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
    },
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
  it('pins the exact seven reviewed single-region releases', () => {
    expect([...SUPPORTED_SINGLE_REGION_RELEASES]).toEqual([
      'eu-central-1',
      'eu-west-1',
      'eu-west-2',
      'eu-west-3',
      'us-east-1',
      'us-east-2',
      'us-west-2',
    ]);
  });

  it.each(['us-east-1', 'us-east-2', 'us-west-2', 'eu-west-1'])(
    '%s keeps the full release feature profile',
    (region) => {
      expect(getRegionFeatureProfile(region)).toMatchObject({
        releaseSupported: true,
        managedWebSearch: true,
        awsAgentRegistry: true,
        unsupportedFeatures: [],
      });
    },
  );

  it.each(['eu-central-1', 'eu-west-2'])(
    '%s enables Registry federation but reports Web Search unavailable',
    (region) => {
      expect(getRegionFeatureProfile(region)).toMatchObject({
        releaseSupported: true,
        managedWebSearch: false,
        awsAgentRegistry: true,
        unsupportedFeatures: ['Amazon Bedrock AgentCore Web Search'],
      });
    },
  );

  it('keeps both optional services disabled in Paris', () => {
    expect(getRegionFeatureProfile('eu-west-3')).toMatchObject({
      releaseSupported: true,
      managedWebSearch: false,
      awsAgentRegistry: false,
      unsupportedFeatures: [
        'Amazon Bedrock AgentCore Web Search',
        'AWS Agent Registry federation',
      ],
    });
  });

  it.each(['us-west-1', 'eu-north-1', 'us-west-9', 'unknown'])(
    'does not infer release support for neighboring or unknown Region %s',
    (region) => {
      expect(getRegionFeatureProfile(region).releaseSupported).toBe(false);
    },
  );

  it('pins the AWS-documented commercial optional-feature Region sets', () => {
    expect([...MANAGED_WEB_SEARCH_SUPPORTED_REGIONS]).toEqual([
      'ap-northeast-1',
      'ap-southeast-2',
      'eu-west-1',
      'us-east-1',
      'us-east-2',
      'us-west-2',
    ]);
    expect([...AWS_AGENT_REGISTRY_SUPPORTED_REGIONS]).toEqual([
      'ap-northeast-1',
      'ap-southeast-2',
      'ca-central-1',
      'eu-central-1',
      'eu-west-1',
      'eu-west-2',
      'us-east-1',
      'us-east-2',
      'us-west-2',
    ]);
  });

  it('blocks only dedicated managed-search templates and tools where unavailable', () => {
    expect(templateUnavailableReason('web-search-agent', 'eu-central-1')).toMatch(/Frankfurt/);
    expect(templateUnavailableReason('web-search-agent', 'us-east-2')).toBeNull();
    expect(templateUnavailableReason('strands-gateway-agent', 'eu-west-2')).toBeNull();
    expect(templateUnavailableReason('customer-support-assistant', 'eu-west-3')).toBeNull();
    expect(paletteToolUnavailableReason('web_search', 'eu-west-3')).toMatch(/Paris/);
    expect(paletteToolUnavailableReason('weather_api', 'eu-west-3')).toBeNull();
  });

  it('names the DynamoDB fallback and actual supported release alternatives', () => {
    const registry = awsAgentRegistryUnavailableReason('eu-west-3');
    expect(registry).toMatch(/built-in DynamoDB agent registry remains available/);
    expect(registry).toMatch(/eu-central-1/);
    expect(registry).toMatch(/eu-west-2/);
    expect(registry).toMatch(/us-east-2/);
    expect(registry).toMatch(/us-west-2/);

    const webSearch = managedWebSearchUnavailableReason('eu-west-3');
    expect(webSearch).toMatch(/eu-west-1/);
    expect(webSearch).toMatch(/us-east-1/);
    expect(webSearch).toMatch(/us-east-2/);
    expect(webSearch).toMatch(/us-west-2/);
    expect(webSearch).not.toMatch(/eu-central-1/);

    expect(awsAgentRegistryUnavailableReason('eu-central-1')).toBeNull();
    expect(managedWebSearchUnavailableReason('us-east-2')).toBeNull();
  });
});

describe('release-Region predicate (client twin of require_release_region)', () => {
  it('admits exactly the seven selected Regions and nothing else', () => {
    for (const region of SUPPORTED_SINGLE_REGION_RELEASES) expect(isReleaseRegion(region)).toBe(true);
    for (const value of ['us-west-1', 'ap-southeast-2', 'eu-north-1', '', 'US-EAST-1', 'us-east-1 ', null, undefined, 7]) {
      expect(isReleaseRegion(value)).toBe(false);
    }
    expect(SUPPORTED_SINGLE_REGION_RELEASES).toHaveLength(7);
  });

  it('refuses with the same actionable text the backend returns', () => {
    const message = releaseRegionUnavailableReason('us-west-1', 'this live deployment');
    expect(message).toMatch(/^Region us-west-1 is not a selected production release Region, so this live deployment is refused\. /);
    for (const region of SUPPORTED_SINGLE_REGION_RELEASES) expect(message).toContain(`(${region})`);
    expect(releaseRegionUnavailableReason(undefined, 'x')).toMatch(/^Region <unset> /);
  });
});
