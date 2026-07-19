/**
 * Deployments API domain module.
 */

import { apiRequest } from './client';

// ============================================================================
// Types
// ============================================================================

export interface DeployRequest {
  aws_region: string;
  vpc_config?: Record<string, unknown>;
  enable_cloudwatch?: boolean;
  enable_cloudtrail?: boolean;
}

export interface DeploymentResult {
  deployment_id: string;
  status: 'success' | 'failed' | 'in_progress';
  endpoint_url?: string;
  error_message?: string;
  created_resources: string[];
}

// ============================================================================
// Deployment Operations
// ============================================================================

/**
 * Deploys a workflow to AWS.
 * Requirements: 11.1, 11.5, 11.6, 11.7
 */
export async function deployWorkflow(
  workflowId: string,
  config: DeployRequest
): Promise<DeploymentResult> {
  return apiRequest<DeploymentResult>(`/api/workflows/${workflowId}/deploy`, {
    method: 'POST',
    body: JSON.stringify(config),
  });
}
