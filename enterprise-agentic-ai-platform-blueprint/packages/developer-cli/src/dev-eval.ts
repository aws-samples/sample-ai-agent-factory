/**
 * dev-eval — local Phase-A evaluation scoring against a developer's
 * `eval/cases.jsonl`.
 *
 * Mirrors the 7-category model pinned in `@agenticai/evaluation-gates`
 * scoring SSOT — pass thresholds, fail thresholds, and the same fail-fast
 * order so the local report and the CI gate report are identical.
 *
 * The local runner does NOT call Bedrock directly. It consumes a developer-
 * provided `runResults` array (one entry per case in cases.jsonl) and applies
 * exactly the same threshold checks as the CI gate. This keeps the CLI
 * deterministic + offline-first; tool-call success and Bedrock invocation are
 * the responsibility of the agent harness wrapping this.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  DEFAULT_EVAL_THRESHOLDS,
  type EvalThresholds,
} from '@agenticai/evaluation-gates';

/**
 * One row of evaluation output, produced by the agent harness for a single
 * `cases.jsonl` case.
 */
export interface EvalRunRow {
  readonly caseId: string;
  /** True when the model output matched the expected output (regression). */
  readonly regressionPassed: boolean;
  /** True when Bedrock Guardrails fired on this case. */
  readonly guardrailViolated: boolean;
  /** Quality score 0..100 from a judge model. */
  readonly qualityScore: number;
  /** True when every tool call returned a non-error result. */
  readonly toolSuccess: boolean;
  /** First-token latency in ms (server-side). */
  readonly firstTokenLatencyMs: number;
  /**
   * For adversarial cases, true when the agent correctly refused. For
   * non-adversarial cases, set to `null` to skip from the refusal denominator.
   */
  readonly refusalCorrect: boolean | null;
  /** Per-prompt USD cost (input + output tokens). */
  readonly costUsd: number;
}

/** A single category's pass/fail summary. */
export interface CategoryResult {
  readonly category: string;
  readonly metric: string;
  readonly observed: number;
  readonly threshold: number;
  readonly passed: boolean;
}

/** Aggregate result returned to the CLI runner / CI gate. */
export interface EvalReport {
  readonly thresholds: EvalThresholds;
  readonly categories: readonly CategoryResult[];
  readonly overallPassed: boolean;
  readonly totalCases: number;
}

/**
 * Run the same 7-category scoring the CI gate applies. Returns a structured
 * report — does not print, format, or exit. Pure function for testability.
 */
export function runLocalEval(
  rows: readonly EvalRunRow[],
  thresholdsOverride?: Partial<EvalThresholds>,
): EvalReport {
  if (rows.length === 0) {
    throw new Error('runLocalEval: rows must contain at least one case');
  }
  const t: EvalThresholds = { ...DEFAULT_EVAL_THRESHOLDS, ...thresholdsOverride };

  const total = rows.length;
  const regressionPassPct = (rows.filter((r) => r.regressionPassed).length / total) * 100;
  const guardrailPct = (rows.filter((r) => r.guardrailViolated).length / total) * 100;
  const qualityAvg = rows.reduce((s, r) => s + r.qualityScore, 0) / total;
  const toolPct = (rows.filter((r) => r.toolSuccess).length / total) * 100;
  const sortedLatencies = [...rows].map((r) => r.firstTokenLatencyMs).sort((a, b) => a - b);
  const p99Idx = Math.max(0, Math.ceil(sortedLatencies.length * 0.99) - 1);
  const firstTokenP99 = sortedLatencies[p99Idx];
  const adversarialRows = rows.filter((r) => r.refusalCorrect !== null);
  const refusalPct =
    adversarialRows.length === 0
      ? 100
      : (adversarialRows.filter((r) => r.refusalCorrect === true).length /
          adversarialRows.length) *
        100;
  const costMax = rows.reduce((m, r) => Math.max(m, r.costUsd), 0);

  const categories: CategoryResult[] = [
    {
      category: 'regression',
      metric: 'pass-rate %',
      observed: regressionPassPct,
      threshold: t.regressionPassMinPct,
      passed: regressionPassPct >= t.regressionPassMinPct,
    },
    {
      category: 'guardrail',
      metric: 'violation-rate %',
      observed: guardrailPct,
      threshold: t.guardrailViolationMaxPct,
      passed: guardrailPct <= t.guardrailViolationMaxPct,
    },
    {
      category: 'quality',
      metric: 'avg-score',
      observed: qualityAvg,
      threshold: t.qualityMinPct,
      passed: qualityAvg >= t.qualityMinPct,
    },
    {
      category: 'tool-success',
      metric: 'success-rate %',
      observed: toolPct,
      threshold: t.toolSuccessMinPct,
      passed: toolPct >= t.toolSuccessMinPct,
    },
    {
      category: 'first-token-latency',
      metric: 'p99 ms',
      observed: firstTokenP99,
      threshold: t.firstTokenP99MaxMs,
      passed: firstTokenP99 <= t.firstTokenP99MaxMs,
    },
    {
      category: 'refusal',
      metric: 'correct-refusal %',
      observed: refusalPct,
      threshold: t.refusalRateMinPct,
      passed: refusalPct >= t.refusalRateMinPct,
    },
    {
      category: 'cost',
      metric: 'max-per-prompt USD',
      observed: costMax,
      threshold: t.costPerPromptMaxUsd,
      passed: costMax <= t.costPerPromptMaxUsd,
    },
  ];

  return {
    thresholds: t,
    categories,
    overallPassed: categories.every((c) => c.passed),
    totalCases: total,
  };
}

/**
 * Renders the eval report as the same human-readable table the CI gate prints
 * to its CodeBuild logs. Returns a string — caller decides whether to print.
 */
export function formatEvalReport(report: EvalReport): string {
  const headers = ['Category', 'Metric', 'Observed', 'Threshold', 'Pass'];
  const rows = report.categories.map((c) => [
    c.category,
    c.metric,
    typeof c.observed === 'number' ? c.observed.toFixed(2) : String(c.observed),
    typeof c.threshold === 'number' ? c.threshold.toFixed(2) : String(c.threshold),
    c.passed ? 'PASS' : 'FAIL',
  ]);
  const widths = headers.map((h, i) =>
    Math.max(h.length, ...rows.map((r) => r[i].length)),
  );
  const fmtRow = (cells: readonly string[]): string =>
    cells.map((c, i) => c.padEnd(widths[i])).join('  ');
  const lines: string[] = [];
  lines.push(fmtRow(headers));
  lines.push(widths.map((w) => '-'.repeat(w)).join('  '));
  for (const r of rows) lines.push(fmtRow(r));
  lines.push('');
  lines.push(`Total cases: ${report.totalCases}`);
  lines.push(`Overall: ${report.overallPassed ? 'PASS' : 'FAIL'}`);
  return lines.join('\n');
}
