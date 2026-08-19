/**
 * Tests for regression detector. Pure-function semantics — every assertion
 * is independent of the AWS SDK.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { DEFAULT_EVAL_THRESHOLDS } from '@agenticai/evaluation-gates';
import { computeRegression, type ScoreSample } from './regression';

const baseline: ScoreSample = {
  regressionPassPct: 96,
  guardrailViolationPct: 0.2,
  qualityPct: 90,
  toolSuccessPct: 99,
  firstTokenP99Ms: 1000,
  refusalRatePct: 99.5,
  costPerPromptUsd: 0.01,
};

describe('computeRegression', () => {
  it('reports nothing when latest matches baseline', () => {
    expect(computeRegression(baseline, baseline, DEFAULT_EVAL_THRESHOLDS)).toEqual({
      regressed: [],
      belowThreshold: [],
    });
  });

  it('detects a quality regression > tolerance', () => {
    const latest = { ...baseline, qualityPct: 75 };
    const r = computeRegression(latest, baseline, DEFAULT_EVAL_THRESHOLDS);
    expect(r.regressed).toContain('qualityPct');
    expect(r.belowThreshold).toContain('qualityPct');
  });

  it('detects a refusal-rate regression > tolerance', () => {
    const latest = { ...baseline, refusalRatePct: 80 };
    const r = computeRegression(latest, baseline, DEFAULT_EVAL_THRESHOLDS);
    expect(r.regressed).toContain('refusalRatePct');
    expect(r.belowThreshold).toContain('refusalRatePct');
  });

  it('detects guardrail-violation rise > tolerance', () => {
    const latest = { ...baseline, guardrailViolationPct: 15 };
    const r = computeRegression(latest, baseline, DEFAULT_EVAL_THRESHOLDS);
    expect(r.regressed).toContain('guardrailViolationPct');
    expect(r.belowThreshold).toContain('guardrailViolationPct');
  });

  it('detects latency drift > 20% of ceiling', () => {
    const latest = { ...baseline, firstTokenP99Ms: 1500 };
    const r = computeRegression(latest, baseline, DEFAULT_EVAL_THRESHOLDS);
    expect(r.regressed).toContain('firstTokenP99Ms');
  });

  it('detects cost drift > 50% of ceiling', () => {
    const latest = { ...baseline, costPerPromptUsd: 0.05 };
    const r = computeRegression(latest, baseline, DEFAULT_EVAL_THRESHOLDS);
    expect(r.regressed).toContain('costPerPromptUsd');
  });

  it('reports threshold breaches even when baseline drift is small', () => {
    const tightBaseline = { ...baseline, qualityPct: 86, refusalRatePct: 99.6 };
    const latest = { ...tightBaseline, qualityPct: 84, refusalRatePct: 98 };
    const r = computeRegression(latest, tightBaseline, DEFAULT_EVAL_THRESHOLDS);
    expect(r.belowThreshold).toEqual(expect.arrayContaining(['qualityPct', 'refusalRatePct']));
  });

  it('does not regress on tiny improvements', () => {
    const latest = { ...baseline, qualityPct: 91, refusalRatePct: 99.6, guardrailViolationPct: 0.1 };
    const r = computeRegression(latest, baseline, DEFAULT_EVAL_THRESHOLDS);
    expect(r.regressed).toEqual([]);
    expect(r.belowThreshold).toEqual([]);
  });
});
