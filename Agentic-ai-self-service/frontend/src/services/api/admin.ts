/**
 * Admin API domain module (Phase 7 multi-region/account deployment targets).
 */

import { apiRequest } from './client';

// ============================================================================
// Types
// ============================================================================

export interface DeployTargetsConfig {
  enabled: boolean;
  regions: string[];
  accounts: Array<{ account_id: string; role_arn: string; region: string }>;
}

// ============================================================================
// Admin Operations
// ============================================================================

/** Phase 7 (opt-in) — multi-region/account deployment targets config. */
export async function getDeployTargets(): Promise<DeployTargetsConfig> {
  return apiRequest<DeployTargetsConfig>(`/api/admin/deploy-targets`);
}

/** Phase 7 — explicitly enable/disable multi-region/account deployment. */
export async function enableDeployTargets(enabled: boolean): Promise<{ enabled: boolean }> {
  return apiRequest<{ enabled: boolean }>(`/api/admin/deploy-targets/enable`, {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  });
}

/** Phase 7 — add an allowlisted deploy region. */
export async function addDeployRegion(region: string): Promise<{ regions: string[] }> {
  return apiRequest<{ regions: string[] }>(`/api/admin/deploy-targets/regions`, {
    method: 'POST',
    body: JSON.stringify({ region }),
  });
}

/** Phase 7 — register a cross-account deploy target (validated server-side). */
export async function addDeployAccount(accountId: string, roleArn: string, region: string): Promise<{ account_id: string; validated: boolean }> {
  return apiRequest<{ account_id: string; validated: boolean }>(`/api/admin/deploy-targets/accounts`, {
    method: 'POST',
    body: JSON.stringify({ account_id: accountId, role_arn: roleArn, region }),
  });
}
