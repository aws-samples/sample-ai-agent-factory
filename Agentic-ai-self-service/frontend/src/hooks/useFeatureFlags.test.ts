import { describe, expect, it } from 'vitest';
import {
  allFlagsOff,
  isFeatureEnabled,
  KNOWN_FEATURE_FLAGS,
  parseFeatureFlags,
  type FeatureFlagsState,
} from './useFeatureFlags';

// The producer's shape: backend/src/app/routers/config.py FeatureFlagsResponse, pinned on the
// backend side by tests/test_feature_flags.py::test_features_route_lists_every_known_flag_with_its_description.
const LIVE_OFF = {
  features: { platformFlagsCanary: false },
  source: 'ssm',
  parameter: '/agentcore-workflow/dev/platform-features',
  ignored: [],
  ttlSeconds: 60,
  refreshedAt: '2026-11-12T20:52:41.580373+00:00',
  descriptions: { platformFlagsCanary: 'Operational self-test for the flag plumbing.' },
};

describe('parseFeatureFlags', () => {
  it('accepts the route response and keeps every field', () => {
    expect(parseFeatureFlags(LIVE_OFF)).toEqual(LIVE_OFF);
    const on = parseFeatureFlags({ ...LIVE_OFF, features: { platformFlagsCanary: true } });
    expect(on?.features.platformFlagsCanary).toBe(true);
  });

  it('turns a flag on only for literal true under a known name', () => {
    expect(parseFeatureFlags({ ...LIVE_OFF, features: { platformFlagsCanary: 'true' } })?.features).toEqual(
      allFlagsOff(),
    );
    expect(parseFeatureFlags({ ...LIVE_OFF, features: { platformFlagsCanary: 1 } })?.features).toEqual(allFlagsOff());
    // A flag a NEWER backend knows and this build does not is dropped, never surfaced.
    const extra = parseFeatureFlags({ ...LIVE_OFF, features: { platformFlagsCanary: false, copilot: true } });
    expect(Object.keys(extra!.features)).toEqual([...KNOWN_FEATURE_FLAGS]);
    // A flag this build knows and the backend omitted is off.
    expect(parseFeatureFlags({ ...LIVE_OFF, features: {} })?.features).toEqual(allFlagsOff());
  });

  it('reports what the backend ignored so an operator can see a typo', () => {
    expect(parseFeatureFlags({ ...LIVE_OFF, ignored: ['typo', 3, null] })?.ignored).toEqual(['typo']);
  });

  it.each([
    ['null', null],
    ['an array', [LIVE_OFF]],
    ['a string', '{"features":{}}'],
    ['no features object', { ...LIVE_OFF, features: 'none' }],
    ['features as an array', { ...LIVE_OFF, features: [] }],
    ['an unknown source', { ...LIVE_OFF, source: 'cache' }],
    ['a missing source', { features: {} }],
    ['an HTML error page shape', { message: 'Forbidden' }],
  ])('fails closed on %s', (_label, value) => {
    expect(parseFeatureFlags(value)).toBeNull();
  });

  it('tolerates optional metadata of the wrong type without turning anything on', () => {
    const parsed = parseFeatureFlags({ features: { platformFlagsCanary: true }, source: 'env', ttlSeconds: 'x' });
    expect(parsed).toEqual({
      features: { platformFlagsCanary: true },
      source: 'env',
      parameter: '',
      ignored: [],
      ttlSeconds: 0,
      refreshedAt: '',
      descriptions: {},
    });
  });
});

describe('isFeatureEnabled', () => {
  const ready = (on: boolean): FeatureFlagsState => ({
    status: 'ready',
    flags: parseFeatureFlags({ ...LIVE_OFF, features: { platformFlagsCanary: on } })!,
  });

  it('is off while loading and after an error, so a surface never flashes on', () => {
    expect(isFeatureEnabled({ status: 'loading' }, 'platformFlagsCanary')).toBe(false);
    expect(isFeatureEnabled({ status: 'error', message: '401' }, 'platformFlagsCanary')).toBe(false);
  });

  it('follows the resolved value once ready', () => {
    expect(isFeatureEnabled(ready(false), 'platformFlagsCanary')).toBe(false);
    expect(isFeatureEnabled(ready(true), 'platformFlagsCanary')).toBe(true);
  });

  it('mirrors the backend registry', () => {
    // Twin of KNOWN_FEATURE_FLAGS in backend/src/app/services/feature_flags.py; a flag is added
    // to both in the PR that adds its surface.
    expect([...KNOWN_FEATURE_FLAGS]).toEqual(['platformFlagsCanary']);
  });
});
