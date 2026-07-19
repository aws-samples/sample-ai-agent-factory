/**
 * Workflows API domain module.
 */

import { apiRequest } from './client';
import type { WorkflowDefinition, DeploymentStatus } from '../../types/workflow';
import type { ValidationResult } from '../../types/validation';
import type { Flow, FlowCreateRequest, FlowUpdateRequest, FlowResponse, FlowListResponse } from '../../types/flow';

// ============================================================================
// Types
// ============================================================================

export interface WorkflowCreateRequest {
  name: string;
  description?: string;
  version?: string;
  nodes?: unknown[];
  edges?: unknown[];
  viewport?: {
    x: number;
    y: number;
    zoom: number;
  };
  metadata: {
    author: string;
    tags?: string[];
    awsRegion: string;
    deploymentStatus?: DeploymentStatus;
  };
}

export interface WorkflowUpdateRequest {
  name?: string;
  description?: string;
  version?: string;
  nodes?: unknown[];
  edges?: unknown[];
  viewport?: {
    x: number;
    y: number;
    zoom: number;
  };
  metadata?: {
    author: string;
    tags?: string[];
    awsRegion: string;
    deploymentStatus?: DeploymentStatus;
  };
}

export interface WorkflowResponse {
  workflow: WorkflowDefinition;
  message: string;
}

export interface DeleteResponse {
  success: boolean;
  message: string;
}

export interface ImportRequest {
  workflow_json: Record<string, unknown>;
}

export interface ImportResponse {
  workflow: WorkflowDefinition;
  message: string;
  validation_errors: string[];
}

export interface ExportResponse {
  workflow_json: Record<string, unknown>;
  message: string;
}

// ============================================================================
// CRUD Operations
// ============================================================================

/**
 * Creates a new workflow.
 * Requirement 9.1: Auto-save workflow
 */
export async function createWorkflow(data: WorkflowCreateRequest): Promise<WorkflowResponse> {
  return apiRequest<WorkflowResponse>('/api/workflows', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

/**
 * Gets a workflow by ID.
 * Requirement 9.5: Restore last saved workflow
 */
export async function getWorkflow(workflowId: string): Promise<WorkflowDefinition> {
  return apiRequest<WorkflowDefinition>(`/api/workflows/${workflowId}`);
}

/**
 * Updates an existing workflow.
 * Requirement 9.1: Auto-save workflow
 */
export async function updateWorkflow(
  workflowId: string,
  data: WorkflowUpdateRequest
): Promise<WorkflowResponse> {
  return apiRequest<WorkflowResponse>(`/api/workflows/${workflowId}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

/**
 * Deletes a workflow by ID.
 */
export async function deleteWorkflow(workflowId: string): Promise<DeleteResponse> {
  return apiRequest<DeleteResponse>(`/api/workflows/${workflowId}`, {
    method: 'DELETE',
  });
}

// ============================================================================
// Validation
// ============================================================================

/**
 * Validates a workflow configuration.
 * Requirements: 8.1, 8.2, 8.3
 */
export async function validateWorkflow(workflowId: string): Promise<ValidationResult> {
  return apiRequest<ValidationResult>(`/api/workflows/${workflowId}/validate`, {
    method: 'POST',
  });
}

// ============================================================================
// Import/Export
// ============================================================================

/**
 * Imports a workflow from JSON.
 * Requirements: 14.1, 14.2, 14.3
 */
export async function importWorkflow(data: ImportRequest): Promise<ImportResponse> {
  return apiRequest<ImportResponse>('/api/workflows/import', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

/**
 * Exports a workflow as JSON.
 * Requirements: 14.1, 14.2
 */
export async function exportWorkflow(workflowId: string): Promise<ExportResponse> {
  return apiRequest<ExportResponse>(`/api/workflows/${workflowId}/export`);
}

// ============================================================================
// Flow CRUD Operations
// ============================================================================

/**
 * Creates a new flow.
 */
export async function createFlow(data: FlowCreateRequest): Promise<FlowResponse> {
  return apiRequest<FlowResponse>('/api/flows', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

/**
 * Lists all flows.
 */
export async function listFlows(): Promise<FlowListResponse> {
  return apiRequest<FlowListResponse>('/api/flows');
}

/**
 * Gets a flow by ID.
 */
export async function getFlow(flowId: string): Promise<Flow> {
  return apiRequest<Flow>(`/api/flows/${flowId}`);
}

/**
 * Updates an existing flow.
 */
export async function updateFlow(
  flowId: string,
  data: FlowUpdateRequest
): Promise<FlowResponse> {
  return apiRequest<FlowResponse>(`/api/flows/${flowId}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

/**
 * Deletes a flow by ID.
 */
export async function deleteFlow(flowId: string): Promise<{ message: string }> {
  return apiRequest<{ message: string }>(`/api/flows/${flowId}`, {
    method: 'DELETE',
  });
}

/**
 * Checks if the backend API is healthy.
 */
export async function healthCheck(): Promise<{ status: string }> {
  return apiRequest<{ status: string }>('/health');
}
