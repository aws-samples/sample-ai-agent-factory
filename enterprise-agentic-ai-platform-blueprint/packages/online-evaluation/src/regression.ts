/**
 * Regression detector — pure function consumed by both the watchdog Lambda
 * and the unit tests. No SDK calls, deterministic.
 *
 * Compares the latest score sample against a rolling 7-day baseline mean
 * for each of the 7 scoring categories. A regression fires when a score
 * drops by more than `regressionToleranceMaxPctPoints` percentage points
 * vs. the baseline (for "minimum" categories) or rises by that amount
 * (for "maximum" categories).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import type { EvalThresholds } from '@agenticai/evaluation-gates';

export interface ScoreSample {
  readonly regressionPassPct: number;
  readonly guardrailViolationPct: number;
  readonly qualityPct: number;
  readonly toolSuccessPct: number;
  readonly firstTokenP99Ms: number;
  readonly refusalRatePct: number;
  readonly costPerPromptUsd: number;
}

export interface RegressionResult {
  readonly regressed: readonly string[];
  readonly belowThreshold: readonly string[];
}

/**
 * Compute regression categories. A field is reported as `regressed` when
 * the latest sample is materially worse than the baseline mean by more than
 * `tolerancePctPoints`. A field is reported as `belowThreshold` when the
 * latest sample is outside the offline gate threshold (independent of
 * baseline drift).
 */
export function computeRegression(
  latest: ScoreSample,
  baseline: ScoreSample,
  thresholds: EvalThresholds,
  tolerancePctPoints = 10,
): RegressionResult {
  const regressed: string[] = [];
  const belowThreshold: string[] = [];

  // Categories where lower is worse — drop > tolerance flags regression.
  const minMetrics: readonly [keyof ScoreSample, number, number][] = [
    ['regressionPassPct', latest.regressionPassPct, baseline.regressionPassPct],
    ['qualityPct', latest.qualityPct, baseline.qualityPct],
    ['toolSuccessPct', latest.toolSuccessPct, baseline.toolSuccessPct],
    ['refusalRatePct', latest.refusalRatePct, baseline.refusalRatePct],
  ];
  for (const [k, l, b] of minMetrics) {
    if (b - l > tolerancePctPoints) regressed.push(k);
  }

  // Categories where higher is worse — rise > tolerance flags regression.
  const maxMetrics: readonly [keyof ScoreSample, number, number][] = [
    ['guardrailViolationPct', latest.guardrailViolationPct, baseline.guardrailViolationPct],
  ];
  for (const [k, l, b] of maxMetrics) {
    if (l - b > tolerancePctPoints) regressed.push(k);
  }

  // Latency + cost are absolute drifts in their own units (ms / USD).
  if (latest.firstTokenP99Ms - baseline.firstTokenP99Ms > thresholds.firstTokenP99MaxMs * 0.2) {
    regressed.push('firstTokenP99Ms');
  }
  if (latest.costPerPromptUsd - baseline.costPerPromptUsd > thresholds.costPerPromptMaxUsd * 0.5) {
    regressed.push('costPerPromptUsd');
  }

  // Threshold breaches independent of baseline.
  if (latest.regressionPassPct < thresholds.regressionPassMinPct) belowThreshold.push('regressionPassPct');
  if (latest.guardrailViolationPct > thresholds.guardrailViolationMaxPct) belowThreshold.push('guardrailViolationPct');
  if (latest.qualityPct < thresholds.qualityMinPct) belowThreshold.push('qualityPct');
  if (latest.toolSuccessPct < thresholds.toolSuccessMinPct) belowThreshold.push('toolSuccessPct');
  if (latest.firstTokenP99Ms > thresholds.firstTokenP99MaxMs) belowThreshold.push('firstTokenP99Ms');
  if (latest.refusalRatePct < thresholds.refusalRateMinPct) belowThreshold.push('refusalRatePct');
  if (latest.costPerPromptUsd > thresholds.costPerPromptMaxUsd) belowThreshold.push('costPerPromptUsd');

  return { regressed, belowThreshold };
}
