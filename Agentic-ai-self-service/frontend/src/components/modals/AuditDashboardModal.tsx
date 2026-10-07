import { AuditDashboard } from '../observability/AuditDashboard';
import { ModalShell } from './ModalShell';

export interface AuditDashboardModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export function AuditDashboardModal({ isOpen, onClose }: AuditDashboardModalProps) {
  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      title="Append-only audit"
      width="min(960px, 92vw)"
      data-testid="audit-dashboard-modal"
    >
      <div className="min-h-0 overflow-y-auto">
        <AuditDashboard />
      </div>
    </ModalShell>
  );
}

export default AuditDashboardModal;
