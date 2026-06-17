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
}

export interface FlowSummary {
  id: string;
  name: string;
  deploymentStatus: DeploymentStatus;
  createdAt: string;
  updatedAt: string;
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
