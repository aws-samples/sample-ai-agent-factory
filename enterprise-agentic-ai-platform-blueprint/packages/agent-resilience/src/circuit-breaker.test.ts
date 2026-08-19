/**
 * Tests for the circuit-breaker SSOT.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  DEFAULT_CIRCUIT_THRESHOLDS,
  DEFAULT_RETRY_POLICY,
  defaultFallbackChain,
  nextRetryDelayMs,
  validateFallbackChain,
} from './circuit-breaker';

describe('retry policy', () => {
  it('declares 3 attempts with exponential backoff', () => {
    expect(DEFAULT_RETRY_POLICY.maxAttempts).toBe(3);
    expect(DEFAULT_RETRY_POLICY.backoffMs).toEqual([200, 800, 3200]);
  });

  it('only retries 429/502/503 (no other 4xx)', () => {
    expect(DEFAULT_RETRY_POLICY.retryableStatuses).toEqual([429, 502, 503]);
    expect(DEFAULT_RETRY_POLICY.retryableStatuses).not.toContain(400);
    expect(DEFAULT_RETRY_POLICY.retryableStatuses).not.toContain(401);
    expect(DEFAULT_RETRY_POLICY.retryableStatuses).not.toContain(404);
  });

  it('nextRetryDelayMs returns the right curve point', () => {
    expect(nextRetryDelayMs(1)).toBe(200);
    expect(nextRetryDelayMs(2)).toBe(800);
    expect(nextRetryDelayMs(3)).toBe(-1);
    expect(nextRetryDelayMs(0)).toBe(-1);
  });
});

describe('circuit breaker thresholds', () => {
  it('uses 5%/5min/8000ms ceilings', () => {
    expect(DEFAULT_CIRCUIT_THRESHOLDS.errorRateMaxPct).toBe(5);
    expect(DEFAULT_CIRCUIT_THRESHOLDS.windowMinutes).toBe(5);
    expect(DEFAULT_CIRCUIT_THRESHOLDS.p99LatencyMaxMs).toBe(8000);
  });

  it('5 consecutive 5xx triggers fallback', () => {
    expect(DEFAULT_CIRCUIT_THRESHOLDS.consecutive5xxToFallback).toBe(5);
  });
});

describe('fallback chain', () => {
  it('primary = Sonnet 4.5, secondary = Haiku 4.5', () => {
    const c = defaultFallbackChain();
    expect(c.primary).toMatch(/claude-sonnet-4-5/);
    expect(c.secondary).toMatch(/claude-haiku-4-5/);
  });

  it('rejects primary == secondary', () => {
    const c = defaultFallbackChain();
    expect(() => validateFallbackChain({ primary: c.primary, secondary: c.primary })).toThrow();
  });

  it('rejects primary off the allow-list', () => {
    expect(() =>
      validateFallbackChain({ primary: 'meta.llama3', secondary: 'claude-haiku' }),
    ).toThrow();
  });
});
