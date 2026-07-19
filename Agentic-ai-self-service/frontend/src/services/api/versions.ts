/**
 * Versioning API domain module (Phase 1 Gap 1A).
 */

import { apiRequest } from './client';

// ============================================================================
// Types
// ============================================================================

export interface AgentVersionSummary {
  runtime_name: string;
  version_id: string;
  created_at: string;
  deployment_id: string;
  agentcore_runtime_name: string;
  runtime_id?: string | null;
  runtime_arn?: string | null;
  runtime_endpoint?: string | null;
  parent_version_id?: string | null;
  status: 'pending' | 'succeeded' | 'failed' | 'superseded';
  description?: string | null;
}

export interface RuntimeSlotsSummary {
  runtime_name: string;
  production_version_id?: string | null;
  staging_version_id?: string | null;
  previous_production_version_id?: string | null;
  last_promoted_at?: string | null;
}

export interface PromoteResult {
  success: boolean;
  runtime_name: string;
  promoted_version_id: string;
  slot: 'staging' | 'production';
  previous_version_id?: string | null;
  message: string;
}

// ============================================================================
// Version Operations
// ============================================================================

/**
 * List all versions of a friendly runtime name owned by the caller.
 * Returns newest-first.
 */
export async function listVersions(runtimeName: string): Promise<AgentVersionSummary[]> {
  return apiRequest<AgentVersionSummary[]>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/versions`
  );
}

/**
 * Get the current production / staging slot pointers for a runtime.
 */
export async function getSlots(runtimeName: string): Promise<RuntimeSlotsSummary> {
  return apiRequest<RuntimeSlotsSummary>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/slots`
  );
}

/**
 * Promote a specific version to the production or staging slot.
 */
export async function promoteVersion(
  runtimeName: string,
  versionId: string,
  slot: 'staging' | 'production' = 'production'
): Promise<PromoteResult> {
  return apiRequest<PromoteResult>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/versions/${encodeURIComponent(versionId)}/promote`,
    {
      method: 'POST',
      body: JSON.stringify({ slot }),
    }
  );
}

/**
 * Roll the production slot back to the previous version.
 */
export async function rollbackRuntime(runtimeName: string): Promise<PromoteResult> {
  return apiRequest<PromoteResult>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/rollback`,
    { method: 'POST' }
  );
}
