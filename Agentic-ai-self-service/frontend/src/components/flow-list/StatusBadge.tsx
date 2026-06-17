/**
 * Visual deployment status indicator badge.
 * Renders a colored badge for each DeploymentStatus value.
 * Requirements: 5.1, 5.2
 */

import type { DeploymentStatus } from '../../types/workflow';

// ============================================================================
// Props
// ============================================================================

export interface StatusBadgeProps {
  status: DeploymentStatus;
}

// ============================================================================
// Status Configuration
// ============================================================================

const STATUS_CONFIG: Record<DeploymentStatus, { label: string; className: string }> = {
  not_deployed: {
    label: 'Not Deployed',
    className: 'bg-gray-100 text-gray-700',
  },
  deploying: {
    label: 'Deploying',
    className: 'bg-amber-100 text-amber-700',
  },
  deployed: {
    label: 'Deployed',
    className: 'bg-green-100 text-green-700',
  },
  failed: {
    label: 'Failed',
    className: 'bg-red-100 text-red-700',
  },
};

// ============================================================================
// Component
// ============================================================================

export function StatusBadge({ status }: StatusBadgeProps) {
  const config = STATUS_CONFIG[status];

  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${config.className}`}
    >
      {config.label}
    </span>
  );
}
