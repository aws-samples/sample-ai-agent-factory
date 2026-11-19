import { describe, expect, it } from 'vitest';
import {
  REDUCED_REVIEW_ACKNOWLEDGEMENT,
  documentFromForm,
  formFromDocument,
  parseBudget,
  presetInputFromForm,
  reviewersFromRouting,
  routingFromReviewers,
  routingIsReduced,
  splitList,
  summarizeAllowed,
  summarizeApproval,
} from './workspacePolicyForm';
import { WORKSPACE_PRESETS, applyPreset, emptyPolicy, type WorkspacePresetInput } from './workspacePresets';

const INPUT: WorkspacePresetInput = {
  workspaceId: 'finance',
  name: 'Finance',
  memberGroups: ['g-finance-builders'],
  adminGroups: ['g-admins-super'],
  approverGroups: ['g-approvers'],
  securityGroups: ['g-security'],
};

describe('workspace policy form conversions', () => {
  it('round-trips every preset document and the empty policy through the form', () => {
    for (const preset of WORKSPACE_PRESETS) {
      const document = applyPreset(preset.id, INPUT);
      // Developer auto-approves every tier on purpose, which the form requires an admin to acknowledge.
      const form = { ...formFromDocument(document), reducedReviewAcknowledged: preset.id === 'developer' };
      expect(documentFromForm(form)).toEqual(document);
    }
    const empty = emptyPolicy('x', 'X');
    expect(documentFromForm({ ...formFromDocument(empty), reducedReviewAcknowledged: true })).toEqual(empty);
  });

  it('refuses to save an empty medium or high routing unless auto-approval is acknowledged', () => {
    const form = formFromDocument(emptyPolicy('x', 'X'));
    expect(form.reducedReviewAcknowledged).toBe(false);
    expect(() => documentFromForm(form)).toThrow(REDUCED_REVIEW_ACKNOWLEDGEMENT);
    expect(() => documentFromForm({ ...form, approvalMedium: 'g-approvers' })).toThrow(/high risk has no approver groups|Medium or high/);
    expect(documentFromForm({ ...form, approvalMedium: 'g-approvers', approvalHigh: 'g-security' }).approval).toEqual({
      low: [],
      medium: ['g-approvers'],
      high: ['g-security'],
    });
    expect(documentFromForm({ ...form, reducedReviewAcknowledged: true }).approval).toEqual({ low: [], medium: [], high: [] });
  });

  it('derives the medium and high routing from the reviewer inputs, and back', () => {
    expect(routingFromReviewers('g-approvers', 'g-security')).toEqual({
      approvalMedium: 'g-approvers',
      approvalHigh: 'g-security, g-approvers',
    });
    expect(routingFromReviewers('g-a, g-a', '')).toEqual({ approvalMedium: 'g-a', approvalHigh: 'g-a' });
    expect(routingFromReviewers('', '')).toEqual({ approvalMedium: '', approvalHigh: '' });
    expect(reviewersFromRouting('g-approvers', 'g-privacy, g-approvers')).toEqual({
      approverGroups: 'g-approvers',
      securityGroups: 'g-privacy',
    });
    expect(reviewersFromRouting('', 'g-security')).toEqual({ approverGroups: '', securityGroups: 'g-security' });
  });

  it('a high routing with no reviewer beyond the medium approvers is reduced review and needs the acknowledgement', () => {
    expect(routingIsReduced(['g-a'], ['g-a'])).toBe(true);
    expect(routingIsReduced(['g-a'], ['g-sec', 'g-a'])).toBe(false);
    expect(routingIsReduced([], ['g-sec'])).toBe(true);
    expect(routingIsReduced(['g-a'], [])).toBe(true);
    const form = formFromDocument(emptyPolicy('x', 'X'));
    expect(() => documentFromForm({ ...form, approvalMedium: 'g-approvers', approvalHigh: 'g-approvers' })).toThrow(
      /adds no reviewer beyond the medium approvers/,
    );
    expect(
      documentFromForm({ ...form, approvalMedium: 'g-approvers', approvalHigh: 'g-approvers', reducedReviewAcknowledged: true })
        .approval.high,
    ).toEqual(['g-approvers']);
  });

  it('splits typed lists on commas, semicolons and whitespace, de-duplicating in order', () => {
    expect(splitList(' g-a, g-b;g-a\n g-c ')).toEqual(['g-a', 'g-b', 'g-c']);
    expect(splitList('')).toEqual([]);
  });

  it('reads an empty budget box as unset and refuses a negative or non-numeric one in plain language', () => {
    expect(parseBudget('', 'The cap')).toBeNull();
    expect(parseBudget(' 12.5 ', 'The cap')).toBe(12.5);
    expect(() => parseBudget('-5', 'The cap')).toThrow('The cap must be a non-negative number (got "-5").');
    expect(() => parseBudget('lots', 'The cap')).toThrow(/non-negative number/);
  });

  it('refuses a missing id or name before anything is sent', () => {
    const form = formFromDocument(emptyPolicy('', ''));
    expect(() => documentFromForm(form)).toThrow('A workspace id is required.');
    expect(() => documentFromForm({ ...form, workspaceId: 'sales' })).toThrow('A workspace name is required.');
  });

  it('recovers the preset inputs from the approval routing of an existing document', () => {
    const form = formFromDocument(applyPreset('standard', INPUT));
    expect(form.approverGroups).toBe('g-approvers');
    expect(form.securityGroups).toBe('g-security');
    expect(presetInputFromForm(form)).toEqual(INPUT);
  });

  it('keeps explicitly given preset inputs over the recovered ones', () => {
    const form = formFromDocument(applyPreset('developer', INPUT), { approverGroups: 'g-x', securityGroups: 'g-y' });
    expect(form.approverGroups).toBe('g-x');
    expect(form.securityGroups).toBe('g-y');
  });

  it('maps the platform version default between null and the empty select value', () => {
    const document = emptyPolicy('x', 'X');
    expect(formFromDocument(document).platformVersion).toBe('');
    document.defaults.platformVersion = 'V2';
    expect(formFromDocument(document).platformVersion).toBe('V2');
    expect(
      documentFromForm({ ...formFromDocument(document), platformVersion: '', reducedReviewAcknowledged: true }).defaults
        .platformVersion,
    ).toBeNull();
  });

  it('summarises the allow-lists and the approval routing for the list view', () => {
    expect(summarizeAllowed(emptyPolicy('x', 'X').allowed)).toBe('unconstrained');
    expect(summarizeAllowed(applyPreset('locked-down', INPUT).allowed)).toBe(
      'providers bedrock · tool classes read · data public/internal · channels web/api',
    );
    expect(summarizeApproval(applyPreset('standard', INPUT).approval)).toBe(
      'low auto · medium g-approvers · high g-security+g-approvers',
    );
    expect(summarizeApproval(applyPreset('developer', INPUT).approval)).toBe('low auto · medium auto · high auto');
  });
});
