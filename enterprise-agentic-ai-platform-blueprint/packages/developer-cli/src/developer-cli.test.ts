/**
 * Unit tests for @agenticai/developer-cli helpers.
 *
 * Pin the pure functions (scaffoldAgentRepo, cdk-context helpers, runLocalEval,
 * formatSearchResults, renderPullRequestBody). The CLI dispatcher in
 * `src/cli.ts` is intentionally a thin handler — testing the helpers covers
 * the meaningful logic.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  appendSubscription,
  filterApproved,
  formatEvalReport,
  formatSearchResults,
  listSubscriptions,
  readAgenticContext,
  removeSubscription,
  renderPullRequestBody,
  runLocalEval,
  scaffoldAgentRepo,
  validateForSynth,
  type EvalRunRow,
  type RegistrySearchResult,
} from './index';
import { parseArgs } from './cli';

const REGISTRY_ID = 'reg-platform-abc123';

describe('scaffoldAgentRepo', () => {
  it('emits a complete agent skeleton with cdk.context.json + agent.py + eval/cases.jsonl', () => {
    const files = scaffoldAgentRepo({
      tenantId: 'retail',
      agentId: 'productqa',
      workstreamId: 'retail',
      platformRegistryId: REGISTRY_ID,
    });
    expect(files.has('cdk.context.json')).toBe(true);
    expect(files.has('agent.py')).toBe(true);
    expect(files.has('prompts/system.md')).toBe(true);
    expect(files.has('eval/cases.jsonl')).toBe(true);
    expect(files.has('bedrock.config.yaml')).toBe(true);
    expect(files.has('README.md')).toBe(true);
    expect(files.has('.gitignore')).toBe(true);
  });

  it('cdk.context.json contains tenantId, agentId, registryId and an empty subscriptions list', () => {
    const files = scaffoldAgentRepo({
      tenantId: 'retail',
      agentId: 'productqa',
      workstreamId: 'retail',
      platformRegistryId: REGISTRY_ID,
    });
    const ctx = JSON.parse(files.get('cdk.context.json')!);
    expect(ctx['agenticai/tenantId']).toBe('retail');
    expect(ctx['agenticai/agentId']).toBe('productqa');
    expect(ctx['agenticai/d03RegistryId']).toBe(REGISTRY_ID);
    expect(ctx['agenticai/subscribedRegistryRecords']).toEqual([]);
  });

  it('rejects non-kebab tenantId', () => {
    expect(() =>
      scaffoldAgentRepo({
        tenantId: 'BadTenant',
        agentId: 'a',
        workstreamId: 'a',
        platformRegistryId: REGISTRY_ID,
      }),
    ).toThrow(/tenantId/);
  });

  it('rejects malformed registry id', () => {
    expect(() =>
      scaffoldAgentRepo({
        tenantId: 'retail',
        agentId: 'a',
        workstreamId: 'a',
        platformRegistryId: 'NOT VALID',
      }),
    ).toThrow(/platformRegistryId/);
  });

  it('rejects 13-digit platformAccountId', () => {
    expect(() =>
      scaffoldAgentRepo({
        tenantId: 'a',
        agentId: 'b',
        workstreamId: 'c',
        platformRegistryId: REGISTRY_ID,
        platformAccountId: '1234567890123',
      }),
    ).toThrow(/platformAccountId/);
  });
});

describe('cdk-context helpers', () => {
  const baseCtx = readAgenticContext({
    'agenticai/tenantId': 'retail',
    'agenticai/agentId': 'productqa',
    'agenticai/d03RegistryId': REGISTRY_ID,
    'agenticai/subscribedRegistryRecords': ['tool-echo'],
  });

  it('appendSubscription is idempotent', () => {
    const a = appendSubscription(baseCtx, 'tool-ping');
    const b = appendSubscription(a, 'tool-ping');
    expect(listSubscriptions(a)).toEqual(['tool-echo', 'tool-ping']);
    expect(listSubscriptions(b)).toEqual(['tool-echo', 'tool-ping']);
  });

  it('removeSubscription is idempotent and removes the record', () => {
    const a = removeSubscription(baseCtx, 'tool-echo');
    const b = removeSubscription(a, 'tool-echo');
    expect(listSubscriptions(a)).toEqual([]);
    expect(listSubscriptions(b)).toEqual([]);
  });

  it('appendSubscription rejects malformed record ids', () => {
    expect(() => appendSubscription(baseCtx, 'NOT VALID')).toThrow(/recordId/);
  });

  it('readAgenticContext rejects arrays + null', () => {
    expect(() => readAgenticContext([1, 2, 3])).toThrow();
    expect(() => readAgenticContext(null)).toThrow();
  });

  it('validateForSynth returns specific missing-key errors', () => {
    const partial = readAgenticContext({
      'agenticai/tenantId': 'a',
      'agenticai/subscribedRegistryRecords': [],
    });
    const errs = validateForSynth(partial);
    expect(errs).toContain('missing context key: agenticai/agentId');
    expect(errs).toContain('missing context key: agenticai/d03RegistryId');
    expect(errs.some((e) => e.includes('subscribedRegistryRecords'))).toBe(true);
  });

  it('validateForSynth returns empty array for a well-formed context', () => {
    expect(validateForSynth(baseCtx)).toEqual([]);
  });
});

describe('runLocalEval', () => {
  function makeRow(overrides: Partial<EvalRunRow> = {}): EvalRunRow {
    return {
      caseId: 'c-1',
      regressionPassed: true,
      guardrailViolated: false,
      qualityScore: 95,
      toolSuccess: true,
      firstTokenLatencyMs: 800,
      refusalCorrect: null,
      costUsd: 0.01,
      ...overrides,
    };
  }

  it('passes when every row meets every threshold', () => {
    const report = runLocalEval([makeRow(), makeRow({ caseId: 'c-2' })]);
    expect(report.overallPassed).toBe(true);
    for (const c of report.categories) expect(c.passed).toBe(true);
  });

  it('fails the regression category when pass-rate dips below threshold', () => {
    const rows: EvalRunRow[] = Array.from({ length: 100 }, (_, i) =>
      makeRow({ caseId: `c-${i}`, regressionPassed: i < 90 }),
    );
    const report = runLocalEval(rows);
    expect(report.overallPassed).toBe(false);
    const reg = report.categories.find((c) => c.category === 'regression')!;
    expect(reg.passed).toBe(false);
    expect(reg.observed).toBeCloseTo(90, 1);
  });

  it('fails the cost category when one row blows the per-prompt USD ceiling', () => {
    const report = runLocalEval([makeRow(), makeRow({ caseId: 'c-2', costUsd: 1.0 })]);
    const cost = report.categories.find((c) => c.category === 'cost')!;
    expect(cost.passed).toBe(false);
    expect(cost.observed).toBeCloseTo(1.0, 4);
  });

  it('refusal category is 100% when no adversarial cases are present', () => {
    const report = runLocalEval([makeRow(), makeRow({ caseId: 'c-2' })]);
    const refusal = report.categories.find((c) => c.category === 'refusal')!;
    expect(refusal.observed).toBe(100);
    expect(refusal.passed).toBe(true);
  });

  it('refusal category fails when adversarial cases are not refused', () => {
    const rows: EvalRunRow[] = [
      makeRow({ caseId: 'a-1', refusalCorrect: false }),
      makeRow({ caseId: 'a-2', refusalCorrect: false }),
    ];
    const report = runLocalEval(rows);
    const refusal = report.categories.find((c) => c.category === 'refusal')!;
    expect(refusal.observed).toBe(0);
    expect(refusal.passed).toBe(false);
  });

  it('throws on empty input — empty cases.jsonl is a developer error', () => {
    expect(() => runLocalEval([])).toThrow(/at least one/);
  });
});

describe('formatEvalReport', () => {
  it('formats a passing report with all 7 categories present', () => {
    const report = runLocalEval([
      {
        caseId: 'c-1',
        regressionPassed: true,
        guardrailViolated: false,
        qualityScore: 90,
        toolSuccess: true,
        firstTokenLatencyMs: 1000,
        refusalCorrect: null,
        costUsd: 0.02,
      },
    ]);
    const out = formatEvalReport(report);
    for (const cat of [
      'regression',
      'guardrail',
      'quality',
      'tool-success',
      'first-token-latency',
      'refusal',
      'cost',
    ]) {
      expect(out).toContain(cat);
    }
    expect(out).toContain('Overall: PASS');
  });
});

describe('formatSearchResults / filterApproved', () => {
  const sample: RegistrySearchResult[] = [
    {
      recordId: 'tool-echo',
      name: 'Echo',
      description: 'Echoes input',
      status: 'APPROVED',
      resourceType: 'mcp_tool',
      ownerTeam: 'platform-ai',
    },
    {
      recordId: 'tool-deprecated',
      name: 'Old',
      description: 'do not use',
      status: 'DEPRECATED',
      resourceType: 'mcp_tool',
    },
  ];

  it('filterApproved keeps only APPROVED records', () => {
    const filtered = filterApproved(sample);
    expect(filtered).toHaveLength(1);
    expect(filtered[0].recordId).toBe('tool-echo');
  });

  it('formatSearchResults handles an empty list', () => {
    expect(formatSearchResults([])).toBe('No matching records.');
  });

  it('formatSearchResults includes record id, status, and owner-team in the table', () => {
    const out = formatSearchResults(sample);
    expect(out).toContain('tool-echo');
    expect(out).toContain('APPROVED');
    expect(out).toContain('platform-ai');
  });
});

describe('renderPullRequestBody', () => {
  const evalRows: EvalRunRow[] = [
    {
      caseId: 'c-1',
      regressionPassed: true,
      guardrailViolated: false,
      qualityScore: 90,
      toolSuccess: true,
      firstTokenLatencyMs: 1000,
      refusalCorrect: null,
      costUsd: 0.02,
    },
  ];

  it('lists the subscribed records and the eval table', () => {
    const body = renderPullRequestBody({
      tenantId: 'retail',
      agentId: 'productqa',
      workstreamId: 'retail',
      subscribedRecordIds: ['tool-echo', 'tool-ping'],
      evalReport: runLocalEval(evalRows),
      registryId: REGISTRY_ID,
    });
    expect(body).toContain('retail/productqa');
    expect(body).toContain('`tool-echo`');
    expect(body).toContain('`tool-ping`');
    expect(body).toContain('regression');
    expect(body).toContain('Overall:** PASS');
  });

  it('renders a placeholder when there are zero subscriptions', () => {
    const body = renderPullRequestBody({
      tenantId: 'retail',
      agentId: 'productqa',
      workstreamId: 'retail',
      subscribedRecordIds: [],
      evalReport: runLocalEval(evalRows),
      registryId: REGISTRY_ID,
    });
    expect(body).toContain('no tool subscriptions');
  });
});

describe('parseArgs', () => {
  it('separates positional and flag args', () => {
    const a = parseArgs(['init', 'retail', 'productqa', '--registry', 'reg-x', '--kind', 'task']);
    expect(a.positional).toEqual(['init', 'retail', 'productqa']);
    expect(a.flags.get('registry')).toBe('reg-x');
    expect(a.flags.get('kind')).toBe('task');
  });

  it('treats a bare --flag without value as boolean true', () => {
    const a = parseArgs(['init', '--dry-run']);
    expect(a.flags.get('dry-run')).toBe('true');
  });
});
