/**
 * submit — render the PR body the CLI uses for `agenticai submit`.
 *
 * Pure function: takes the eval report, the diff against last green eval,
 * and the stable subscribed-tool list — returns the markdown body. The CLI
 * handler is responsible for `gh pr create` / git plumbing.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import type { EvalReport } from "./dev-eval";

export interface SubmitContext {
  readonly tenantId: string;
  readonly agentId: string;
  readonly workstreamId: string;
  readonly subscribedToolIds: readonly string[];
  readonly evalReport: EvalReport;
  readonly previousEvalReport?: EvalReport;
}

export function renderPullRequestBody(ctx: SubmitContext): string {
  const lines: string[] = [];
  lines.push(`# ${ctx.tenantId}/${ctx.agentId} — submission`);
  lines.push("");
  lines.push(`**Workstream:** \`${ctx.workstreamId}\``);
  lines.push("**Registry:** `pipeline-resolved per environment`");
  lines.push("");
  lines.push("## Subscribed Registry tools");
  if (ctx.subscribedToolIds.length === 0) {
    lines.push("_(none — agent has no tool subscriptions)_");
  } else {
    for (const id of ctx.subscribedToolIds) {
      lines.push(`- \`${id}\``);
    }
  }
  lines.push("");
  lines.push("## Evaluation gate (local run)");
  lines.push("");
  lines.push(formatEvalTable(ctx.evalReport));
  lines.push("");
  if (ctx.previousEvalReport) {
    lines.push("## Diff against last green eval");
    lines.push("");
    lines.push(formatDiff(ctx.evalReport, ctx.previousEvalReport));
    lines.push("");
  }
  lines.push("## CI gate thresholds");
  lines.push("| Threshold | Value |");
  lines.push("| --- | --- |");
  for (const c of ctx.evalReport.categories) {
    lines.push(`| ${c.category} (${c.metric}) | ${c.threshold} |`);
  }
  return lines.join("\n");
}

function formatEvalTable(r: EvalReport): string {
  const rows: string[] = [];
  rows.push("| Category | Metric | Observed | Threshold | Pass |");
  rows.push("| --- | --- | --- | --- | --- |");
  for (const c of r.categories) {
    rows.push(
      `| ${c.category} | ${c.metric} | ${c.observed.toFixed(2)} | ${c.threshold.toFixed(2)} | ${c.passed ? "PASS" : "FAIL"} |`,
    );
  }
  rows.push("");
  rows.push(`**Total cases:** ${r.totalCases}`);
  rows.push(`**Overall:** ${r.overallPassed ? "PASS" : "FAIL"}`);
  return rows.join("\n");
}

function formatDiff(curr: EvalReport, prev: EvalReport): string {
  const lines: string[] = [];
  lines.push("| Category | Previous | Current | Δ |");
  lines.push("| --- | --- | --- | --- |");
  for (const c of curr.categories) {
    const p = prev.categories.find((pc) => pc.category === c.category);
    const prevObs = p ? p.observed : NaN;
    const delta = Number.isNaN(prevObs)
      ? "n/a"
      : (c.observed - prevObs).toFixed(2);
    lines.push(
      `| ${c.category} | ${Number.isNaN(prevObs) ? "n/a" : prevObs.toFixed(2)} | ${c.observed.toFixed(2)} | ${delta} |`,
    );
  }
  return lines.join("\n");
}
