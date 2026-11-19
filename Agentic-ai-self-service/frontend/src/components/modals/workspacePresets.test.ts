/**
 * Workspace preset contract fixtures.
 *
 * For every preset this writes backend/tests/fixtures/workspace_policy/presets/<id>.json (vitest
 * file snapshots: `vitest -u` rewrites them, a plain run fails when they drift). The backend
 * (backend/tests/test_workspace_presets.py) validates each against the real `WorkspacePolicy`
 * schema and pins what it admits, so a preset change shows up here first as a fixture diff the
 * backend has to agree with.
 */
import { describe, expect, it } from 'vitest';
import { WORKSPACE_PRESETS, applyPreset, emptyPolicy, type WorkspacePresetInput } from './workspacePresets';

const FIXTURE_DIR = '../../../../backend/tests/fixtures/workspace_policy/presets';

const INPUT: WorkspacePresetInput = {
  workspaceId: 'finance',
  name: 'Finance',
  memberGroups: ['g-finance-builders'],
  adminGroups: ['g-admins-super'],
  approverGroups: ['g-approvers'],
  securityGroups: ['g-security'],
};

function stable(value: unknown): string {
  return `${JSON.stringify(value, null, 2)}\n`;
}

describe('workspace policy presets: the documents the backend validates and pins', () => {
  it('offers exactly Locked-down, Standard and Developer', () => {
    expect(WORKSPACE_PRESETS.map((preset) => preset.id)).toEqual(['locked-down', 'standard', 'developer']);
  });

  it.each(WORKSPACE_PRESETS.map((preset) => [preset.id] as const))('%s', async (id) => {
    await expect(stable(applyPreset(id, INPUT))).toMatchFileSnapshot(`${FIXTURE_DIR}/${id}.json`);
  });

  it('routes medium risk to the approvers and high risk to security plus approvers, de-duplicated', () => {
    const document = applyPreset('standard', {
      ...INPUT,
      approverGroups: ['g-approvers', ' g-approvers '],
      securityGroups: ['g-security', 'g-approvers'],
    });
    expect(document.approval).toEqual({ low: [], medium: ['g-approvers'], high: ['g-security', 'g-approvers'] });
  });

  it('the default workspace never carries member groups', () => {
    expect(applyPreset('standard', { ...INPUT, workspaceId: 'default' }).memberGroups).toEqual([]);
    expect(applyPreset('standard', INPUT).memberGroups).toEqual(['g-finance-builders']);
  });

  it('locked-down allows verified read connectors only and floors unknown items to high', () => {
    const document = applyPreset('locked-down', INPUT);
    expect(document.allowed.providers).toEqual(['bedrock']);
    expect(document.allowed.toolRiskClasses).toEqual(['read']);
    expect(document.allowed.dataClasses).toEqual(['public', 'internal']);
    expect(document.defaults.guardrailProfile).toBe('strict');
    expect(document.defaults.unclassifiedRiskTier).toBe('high');
    expect(document.defaults.trustBuilderClassifications).toBe(false);
  });

  it('standard constrains the provider only and lets the tier do the governing', () => {
    const document = applyPreset('standard', INPUT);
    expect(document.allowed.providers).toEqual(['bedrock']);
    expect(document.allowed.toolRiskClasses).toEqual([]);
    expect(document.allowed.dataClasses).toEqual([]);
    expect(document.defaults.unclassifiedRiskTier).toBe('medium');
    expect(document.approval.medium).toEqual(['g-approvers']);
  });

  it('developer trusts builders, floors unclassified prototypes to low and auto-approves every tier', () => {
    const document = applyPreset('developer', INPUT);
    expect(document.allowed).toEqual(emptyPolicy('finance', 'Finance').allowed);
    expect(document.approval).toEqual({ low: [], medium: [], high: [] });
    expect(document.defaults.trustBuilderClassifications).toBe(true);
    expect(document.defaults.unclassifiedRiskTier).toBe('low');
  });

  it("the empty policy is the schema's own defaults", () => {
    expect(emptyPolicy('x', 'X')).toEqual({
      workspaceId: 'x',
      name: 'X',
      adminGroups: [],
      memberGroups: [],
      allowed: {
        providers: [],
        models: [],
        connectors: [],
        toolRiskClasses: [],
        dataClasses: [],
        channels: [],
        regions: [],
      },
      budgets: { perAgentMonthlyUsd: null, maxAgentMonthlyUsd: null, perWorkspaceMonthlyUsd: null },
      approval: { low: [], medium: [], high: [] },
      defaults: {
        platformVersion: null,
        guardrailProfile: 'standard',
        memory: 'shortTerm',
        unclassifiedRiskTier: 'medium',
        trustBuilderClassifications: false,
      },
    });
  });

  it('an unknown preset is refused', () => {
    expect(() => applyPreset('nope' as never, INPUT)).toThrow(/Unknown workspace preset/);
  });
});
