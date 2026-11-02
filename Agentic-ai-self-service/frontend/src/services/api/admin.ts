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
  region_targets?: Array<{
    region: string;
    account_id?: string | null;
    artifact_bucket?: string | null;
    evaluation_kms_key_arn?: string | null;
  }>;
  accounts: Array<{
    account_id: string;
    role_arn: string;
    runtime_role_arn: string;
    mcp_runtime_role_arn: string;
    harness_role_arn: string;
    artifact_bucket: string;
    evaluation_kms_key_arn: string;
    region: string;
  }>;
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

/** Phase 7 — register an allowlisted region and validate its code bucket. */
export async function addDeployRegion(
  region: string,
  evaluationKmsKeyArn: string,
  artifactBucket?: string,
): Promise<{
  region: string;
  account_id: string;
  artifact_bucket: string;
  evaluation_kms_key_arn: string;
  validated: boolean;
  regions: string[];
}> {
  return apiRequest(`/api/admin/deploy-targets/regions`, {
    method: 'POST',
    body: JSON.stringify({
      region,
      evaluation_kms_key_arn: evaluationKmsKeyArn,
      ...(artifactBucket ? { artifact_bucket: artifactBucket } : {}),
    }),
  });
}

/** Phase 7 — register a cross-account deploy target (validated server-side). */
export async function addDeployAccount(
  accountId: string,
  roleArn: string,
  region: string,
  evaluationKmsKeyArn: string,
  runtimeRoleArn?: string,
  mcpRuntimeRoleArn?: string,
  harnessRoleArn?: string,
  artifactBucket?: string,
): Promise<{
  account_id: string;
  runtime_role_arn: string;
  mcp_runtime_role_arn: string;
  harness_role_arn: string;
  artifact_bucket: string;
  evaluation_kms_key_arn: string;
  validated: boolean;
}> {
  return apiRequest(`/api/admin/deploy-targets/accounts`, {
    method: 'POST',
    body: JSON.stringify({
      account_id: accountId,
      role_arn: roleArn,
      region,
      evaluation_kms_key_arn: evaluationKmsKeyArn,
      ...(runtimeRoleArn ? { runtime_role_arn: runtimeRoleArn } : {}),
      ...(mcpRuntimeRoleArn
        ? { mcp_runtime_role_arn: mcpRuntimeRoleArn }
        : {}),
      ...(harnessRoleArn ? { harness_role_arn: harnessRoleArn } : {}),
      ...(artifactBucket ? { artifact_bucket: artifactBucket } : {}),
    }),
  });
}
