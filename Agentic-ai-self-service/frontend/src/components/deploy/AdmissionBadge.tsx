/**
 * AdmissionBadge — the risk tier a version was admitted at, with its reasons and approval state
 * (plan §3.B, step 5: "a tier badge with reasons on every agent card").
 *
 * Reads the version row's `admission` (the recorded `AdmissionDecision`) and `admission_approvals`.
 * A version without an admission (deployed while the `workspacePolicy` flag was off) renders
 * nothing: there is no tier to show, and inventing one would misstate what was decided.
 */

import type { AdmissionApproval, VersionAdmission } from '../../services/api';
import { admissionTitle, pendingApproverGroups } from './admissionState';

const TIER_CLASSES: Record<string, string> = {
  low: 'bg-emerald-100 text-emerald-800 border-emerald-200',
  medium: 'bg-amber-100 text-amber-800 border-amber-200',
  high: 'bg-red-100 text-red-800 border-red-200',
};

export interface AdmissionBadgeProps {
  admission?: VersionAdmission | null;
  approvals?: AdmissionApproval[] | null;
}

export function AdmissionBadge({ admission, approvals }: AdmissionBadgeProps) {
  if (!admission) {
    return null;
  }
  const pending = pendingApproverGroups(admission, approvals);
  const title = admissionTitle(admission, approvals);
  const tone = TIER_CLASSES[admission.tier] ?? 'bg-gray-100 text-gray-700 border-gray-200';
  return (
    <span
      data-testid="admission-badge"
      title={title}
      aria-label={title}
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-[10px] font-medium ${tone}`}
    >
      {admission.tier} risk
      {pending.length > 0 && (
        <span className="font-normal">
          · awaiting {pending.length === 1 ? pending[0] : `${pending.length} groups`}
        </span>
      )}
    </span>
  );
}
