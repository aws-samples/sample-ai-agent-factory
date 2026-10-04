/**
 * TypeScript interfaces for flow management.
 * Requirements: 1.1, 2.2, 5.1
 */

import type { WorkflowDefinition, DeploymentStatus } from './workflow';

// ============================================================================
// Flow Types
// ============================================================================

export interface Flow {
  id: string;
  name: string;
  workflow: WorkflowDefinition;
  deploymentStatus: DeploymentStatus;
  createdAt: string;
  updatedAt: string;
  /**
   * Optimistic-concurrency fence (F-15). Every save advances it; a save must
   * name the version it was built on and is refused (409) when the row moved.
   * Optional only for responses from a backend that predates the field.
   */
  version?: number;
}

export interface FlowSummary {
  id: string;
  name: string;
  deploymentStatus: DeploymentStatus;
  createdAt: string;
  updatedAt: string;
  version?: number;
}

// ============================================================================
// Flow Request Types
// ============================================================================

export interface FlowCreateRequest {
  name: string;
}

export interface FlowUpdateRequest {
  name?: string;
  workflow?: WorkflowDefinition;
  /** The `Flow.version` this update was built on; the server answers 409 if it has moved. */
  expectedVersion?: number;
}

// ============================================================================
// Flow Response Types
// ============================================================================

export interface FlowResponse {
  flow: Flow;
  message: string;
}

export interface FlowListResponse {
  flows: FlowSummary[];
}
