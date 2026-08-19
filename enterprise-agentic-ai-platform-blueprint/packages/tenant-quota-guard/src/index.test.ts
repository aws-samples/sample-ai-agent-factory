/**
 * Tests for the tenant-quota-guard pure-fn helpers.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { consumeTokens, windowKey } from './index';

describe('consumeTokens', () => {
  it('grants when within budget', () => {
    const r = consumeTokens({
      current: { tokensConsumed: 100, maxTokens: 1000, windowKey: '2026-05' },
      requestedTokens: 50,
    });
    expect(r.granted).toBe(true);
    expect(r.remaining).toBe(850);
    expect(r.newState.tokensConsumed).toBe(150);
  });

  it('denies when over budget', () => {
    const r = consumeTokens({
      current: { tokensConsumed: 990, maxTokens: 1000, windowKey: '2026-05' },
      requestedTokens: 100,
    });
    expect(r.granted).toBe(false);
    expect(r.remaining).toBe(10);
    expect(r.newState.tokensConsumed).toBe(990); // unchanged
  });

  it('grants exactly at the limit', () => {
    const r = consumeTokens({
      current: { tokensConsumed: 950, maxTokens: 1000, windowKey: '2026-05' },
      requestedTokens: 50,
    });
    expect(r.granted).toBe(true);
    expect(r.remaining).toBe(0);
  });
});

describe('windowKey', () => {
  const d = new Date('2026-05-15T13:42:00Z');
  it('monthly', () => expect(windowKey(d, 'monthly')).toBe('2026-05'));
  it('daily', () => expect(windowKey(d, 'daily')).toBe('2026-05-15'));
  it('hourly', () => expect(windowKey(d, 'hourly')).toBe('2026-05-15-13'));
});
