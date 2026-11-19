import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AdmissionBadge } from './AdmissionBadge';
import { admissionTitle, approvalLine, pendingApproverGroups } from './admissionState';
import type { AdmissionApproval, VersionAdmission } from '../../services/api';

const WRITE_REASON = "write tool(s) ['jira'] can change external state";

function admitted(overrides: Partial<VersionAdmission> = {}): VersionAdmission {
  return {
    outcome: 'admitted',
    workspaceId: 'finance',
    tier: 'medium',
    reasons: [{ code: 'write_tool', kind: 'floor', tier: 'medium', message: WRITE_REASON }],
    approvers: ['g-approvers'],
    violations: [],
    admissionVersion: 1,
    classifierVersion: 2,
    ...overrides,
  };
}

const approvedBy = (group: string): AdmissionApproval => ({
  group,
  approverSub: `sub-${group}`,
  approvedAt: '2026-01-01T00:00:00Z',
});

describe('AdmissionBadge', () => {
  it('renders nothing for a version deployed without workspace admission', () => {
    const { container } = render(<AdmissionBadge admission={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the tier, names the pending approver group and carries the reasons in its tooltip', () => {
    render(<AdmissionBadge admission={admitted()} approvals={[]} />);
    const badge = screen.getByTestId('admission-badge');
    expect(badge).toHaveTextContent('medium risk');
    expect(badge).toHaveTextContent('awaiting g-approvers');
    expect(badge).toHaveAttribute('title', expect.stringContaining(WRITE_REASON));
    expect(badge).toHaveAttribute('title', expect.stringContaining('Workspace finance: medium risk'));
    expect(badge.className).toContain('amber');
  });

  it('drops the pending marker once every group has approved', () => {
    render(<AdmissionBadge admission={admitted()} approvals={[approvedBy('g-approvers')]} />);
    const badge = screen.getByTestId('admission-badge');
    expect(badge).toHaveTextContent('medium risk');
    expect(badge).not.toHaveTextContent('awaiting');
    expect(badge).toHaveAttribute('title', expect.stringContaining('Approved by g-approvers'));
  });

  it('counts several pending groups and colours a high tier red', () => {
    render(<AdmissionBadge admission={admitted({ tier: 'high', approvers: ['g-security', 'g-approvers'] })} />);
    const badge = screen.getByTestId('admission-badge');
    expect(badge).toHaveTextContent('high risk');
    expect(badge).toHaveTextContent('awaiting 2 groups');
    expect(badge.className).toContain('red');
  });

  it('colours a low tier green and calls it auto-approved when no group is named', () => {
    render(<AdmissionBadge admission={admitted({ tier: 'low', approvers: [], reasons: [] })} />);
    const badge = screen.getByTestId('admission-badge');
    expect(badge).toHaveTextContent('low risk');
    expect(badge.className).toContain('emerald');
    expect(badge).toHaveAttribute('title', expect.stringContaining('Auto-approved by the workspace policy'));
  });
});

describe('admission helpers', () => {
  it('pending groups keep policy order and ignore approvals by groups the policy never named', () => {
    const admission = admitted({ approvers: ['g-security', 'g-approvers'] });
    expect(pendingApproverGroups(admission, [approvedBy('g-approvers'), approvedBy('g-other')])).toEqual([
      'g-security',
    ]);
    expect(pendingApproverGroups(admission, null)).toEqual(['g-security', 'g-approvers']);
  });

  it('the approval line distinguishes auto-approved, approved and awaiting', () => {
    expect(approvalLine(admitted({ approvers: [] }))).toBe('Auto-approved by the workspace policy');
    expect(approvalLine(admitted(), [approvedBy('g-approvers')])).toBe('Approved by g-approvers');
    expect(approvalLine(admitted(), [])).toBe('Awaiting approval from g-approvers');
  });

  it('the title is the workspace and tier, one line per reason, then the approval line', () => {
    expect(admissionTitle(admitted(), []).split('\n')).toEqual([
      'Workspace finance: medium risk',
      `- ${WRITE_REASON}`,
      'Awaiting approval from g-approvers',
    ]);
  });
});
