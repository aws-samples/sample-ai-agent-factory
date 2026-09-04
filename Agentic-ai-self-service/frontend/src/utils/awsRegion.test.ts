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
  DEFAULT_AWS_REGION,
  getDeploymentRegion,
  getRegionPrefixFor,
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
