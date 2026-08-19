/**
 * Tests for evaluation-gate scoring SSOT.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  DEFAULT_EVAL_THRESHOLDS,
  JUDGE_MODELS,
  validateThresholds,
} from './scoring';

describe('DEFAULT_EVAL_THRESHOLDS', () => {
  it('declares the seven scoring categories per BLUEPRINT_GAP_ANALYSIS Partial-1', () => {
    expect(Object.keys(DEFAULT_EVAL_THRESHOLDS).sort()).toEqual([
      'costPerPromptMaxUsd',
      'firstTokenP99MaxMs',
      'guardrailViolationMaxPct',
      'qualityMinPct',
      'refusalRateMinPct',
      'regressionPassMinPct',
      'toolSuccessMinPct',
    ]);
  });

  it('matches v0.3.x defaults for the five legacy categories', () => {
    expect(DEFAULT_EVAL_THRESHOLDS.regressionPassMinPct).toBe(95);
    expect(DEFAULT_EVAL_THRESHOLDS.guardrailViolationMaxPct).toBe(1);
    expect(DEFAULT_EVAL_THRESHOLDS.qualityMinPct).toBe(85);
    expect(DEFAULT_EVAL_THRESHOLDS.toolSuccessMinPct).toBe(98);
    expect(DEFAULT_EVAL_THRESHOLDS.firstTokenP99MaxMs).toBe(1500);
  });

  it('adds the two new categories Phase A introduces', () => {
    expect(DEFAULT_EVAL_THRESHOLDS.refusalRateMinPct).toBe(99);
    expect(DEFAULT_EVAL_THRESHOLDS.costPerPromptMaxUsd).toBe(0.05);
  });
});

describe('JUDGE_MODELS — locked decision per .claude/GAP_CLOSURE_PLAN.md §16-LOCKED', () => {
  it('uses Sonnet 4.5 for correctness', () => {
    expect(JUDGE_MODELS.correctness).toMatch(/claude-sonnet-4-5/);
  });

  it('uses Haiku 4.5 for refusal + toxicity', () => {
    expect(JUDGE_MODELS.refusal).toMatch(/claude-haiku-4-5/);
    expect(JUDGE_MODELS.toxicity).toMatch(/claude-haiku-4-5/);
  });

  it('only references models on the platform-baselines allow-list (Sonnet 4.5 + Haiku 4.5)', () => {
    const allModels = Object.values(JUDGE_MODELS);
    for (const m of allModels) {
      expect(m).toMatch(/claude-(sonnet-4-5|haiku-4-5)/);
    }
  });
});

describe('validateThresholds', () => {
  it('accepts the defaults', () => {
    expect(() => validateThresholds(DEFAULT_EVAL_THRESHOLDS)).not.toThrow();
  });

  it('rejects out-of-range percentages', () => {
    expect(() =>
      validateThresholds({ ...DEFAULT_EVAL_THRESHOLDS, regressionPassMinPct: 101 }),
    ).toThrow();
    expect(() =>
      validateThresholds({ ...DEFAULT_EVAL_THRESHOLDS, refusalRateMinPct: -1 }),
    ).toThrow();
  });

  it('rejects non-positive latency / cost', () => {
    expect(() =>
      validateThresholds({ ...DEFAULT_EVAL_THRESHOLDS, firstTokenP99MaxMs: 0 }),
    ).toThrow();
    expect(() =>
      validateThresholds({ ...DEFAULT_EVAL_THRESHOLDS, costPerPromptMaxUsd: 0 }),
    ).toThrow();
  });
});
