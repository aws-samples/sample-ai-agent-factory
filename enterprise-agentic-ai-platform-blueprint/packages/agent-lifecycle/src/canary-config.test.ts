/**
 * Tests for canary configuration SSOT.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { DEFAULT_CANARY_CONFIG, validateCanaryConfig } from './canary-config';

describe('canary config SSOT', () => {
  it('locks the default canary to 5% as a platform default', () => {
    expect(DEFAULT_CANARY_CONFIG.canaryPercent).toBe(5);
  });

  it('locks the default soak to 30 minutes', () => {
    expect(DEFAULT_CANARY_CONFIG.soakDuration).toBe(30);
  });

  it('rejects canaryPercent outside [1, 50]', () => {
    expect(() => validateCanaryConfig({ ...DEFAULT_CANARY_CONFIG, canaryPercent: 0 })).toThrow();
    expect(() => validateCanaryConfig({ ...DEFAULT_CANARY_CONFIG, canaryPercent: 51 })).toThrow();
  });

  it('rejects out-of-range soak', () => {
    expect(() => validateCanaryConfig({ ...DEFAULT_CANARY_CONFIG, soakDuration: 4 })).toThrow();
    expect(() => validateCanaryConfig({ ...DEFAULT_CANARY_CONFIG, soakDuration: 5000 })).toThrow();
  });

  it('rejects out-of-range tolerance', () => {
    expect(() => validateCanaryConfig({ ...DEFAULT_CANARY_CONFIG, tolerancePctPoints: -1 })).toThrow();
    expect(() => validateCanaryConfig({ ...DEFAULT_CANARY_CONFIG, tolerancePctPoints: 101 })).toThrow();
  });
});
