/**
 * Pure helpers over a version's recorded workspace admission (plan §3.B), shared by the tier badge
 * and its tests. Kept apart from the component so the component file exports only a component.
 */

import type { AdmissionApproval, VersionAdmission } from '../../services/api';

/** Approver groups the admission routes to that have no approval record yet, in policy order. */
export function pendingApproverGroups(
  admission: VersionAdmission,
  approvals?: AdmissionApproval[] | null,
): string[] {
  const done = new Set((approvals ?? []).map((record) => record.group));
  return admission.approvers.filter((group) => !done.has(group));
}

/** The one-line approval state shown in the badge's tooltip. */
export function approvalLine(admission: VersionAdmission, approvals?: AdmissionApproval[] | null): string {
  if (admission.approvers.length === 0) {
    return 'Auto-approved by the workspace policy';
  }
  const pending = pendingApproverGroups(admission, approvals);
  if (pending.length === 0) {
    return `Approved by ${admission.approvers.join(', ')}`;
  }
  return `Awaiting approval from ${pending.join(', ')}`;
}

/** The tooltip: workspace and tier, one line per classifier reason, then the approval state. */
export function admissionTitle(admission: VersionAdmission, approvals?: AdmissionApproval[] | null): string {
  return [
    `Workspace ${admission.workspaceId}: ${admission.tier} risk`,
    ...admission.reasons.map((reason) => `- ${reason.message}`),
    approvalLine(admission, approvals),
  ].join('\n');
}
