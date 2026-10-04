import { describe, expect, it } from 'vitest';
import { parsePlatformObservabilityDefaults } from './usePlatformObservabilityPolicy';

// The producer's REAL bytes, recorded live 2026-10-02 from GET /api/observability/platform-defaults on a
// platform without OTEL, and pinned on the producer's side by backend/tests/test_platform_defaults_wire_shape.py.
// Every earlier test mocked {enabled: false}, a shape the route never sends, which is how Save stayed disabled
// on every Runtime and Observability modal without one failing test.
const LIVE_DISABLED = '{"enabled":false,"endpoint":null,"sample_rate":null,"service_name_prefix":null}';
const ENABLED = '{"enabled":true,"endpoint":"https://otel.example.invalid/v1/traces","sample_rate":0.25,"service_name_prefix":"acf"}';

describe('parsePlatformObservabilityDefaults', () => {
  it('accepts the route\'s own disabled response, nulls and all', () => {
    expect(parsePlatformObservabilityDefaults(JSON.parse(LIVE_DISABLED))).toEqual({ enabled: false });
  });

  it('accepts an enabled policy and keeps its values', () => {
    expect(parsePlatformObservabilityDefaults(JSON.parse(ENABLED))).toEqual({
      enabled: true,
      endpoint: 'https://otel.example.invalid/v1/traces',
      sample_rate: 0.25,
      service_name_prefix: 'acf',
    });
  });

  it('still accepts the absent-field shape', () => {
    expect(parsePlatformObservabilityDefaults({ enabled: false })).toEqual({ enabled: false });
  });

  it.each([
    ['no enabled flag', { endpoint: null }],
    ['a non-boolean enabled', { enabled: 'false' }],
    ['a numeric endpoint', { enabled: true, endpoint: 5 }],
    ['a non-finite sample rate', { enabled: true, sample_rate: Number.POSITIVE_INFINITY }],
    ['a string sample rate', { enabled: true, sample_rate: '0.5' }],
    ['an object prefix', { enabled: true, service_name_prefix: {} }],
    ['null', null],
    ['an array', [false]],
  ])('fails closed on %s', (_label, value) => {
    expect(parsePlatformObservabilityDefaults(value)).toBeNull();
  });
});
