/**
 * Workspace policy admin API (plan §3.B).
 *
 * CamelCase on the wire, unlike the snake_case domain modules beside it: the backend's
 * `WorkspacePolicy` schema belongs to the Spec v2 family (`to_camel`, strict, unknown keys refused),
 * and the UI sends the document exactly as the schema defines it so a `422` names the real field.
 * Every route is behind the `workspacePolicy` feature flag (off: the exact 404 of a nonexistent
 * route) and the `admin` scope; the panel that uses this module is gated the same way.
 */

import { authFetch } from '../../auth/authFetch';
import { API_BASE_URL, apiErrorFromResponse, apiRequest } from './client';

// ============================================================================
// Types (mirror backend/src/app/schemas/workspace_policy.py)
// ============================================================================

export type RiskTier = 'low' | 'medium' | 'high';
export type AllowedToolRiskClass = 'read' | 'write' | 'pay';
export type AllowedDataClass = 'public' | 'internal' | 'confidential' | 'pii';
export type ChannelType = 'web' | 'slack' | 'teams' | 'email' | 'api' | 'mcp' | 'a2a';
export type GuardrailProfile = 'off' | 'standard' | 'strict';
export type PlatformVersion = 'V1' | 'V2';

export const RISK_TIERS: readonly RiskTier[] = ['low', 'medium', 'high'];
export const ALLOWED_TOOL_RISK_CLASSES: readonly AllowedToolRiskClass[] = ['read', 'write', 'pay'];
export const ALLOWED_DATA_CLASSES: readonly AllowedDataClass[] = ['public', 'internal', 'confidential', 'pii'];
export const CHANNEL_TYPES: readonly ChannelType[] = ['web', 'slack', 'teams', 'email', 'api', 'mcp', 'a2a'];
export const GUARDRAIL_PROFILES: readonly GuardrailProfile[] = ['off', 'standard', 'strict'];

/** An EMPTY list means "unconstrained on this axis"; a locked-down workspace lists exactly what it allows. */
export interface AllowedResources {
  providers: string[];
  models: string[];
  connectors: string[];
  toolRiskClasses: AllowedToolRiskClass[];
  dataClasses: AllowedDataClass[];
  channels: ChannelType[];
  regions: string[];
}

export interface WorkspaceBudgets {
  /** The default an agent gets when its spec declares none. */
  perAgentMonthlyUsd: number | null;
  /** Caps what any ONE agent may declare; admission enforces it. */
  maxAgentMonthlyUsd: number | null;
  /** Recorded for metering and chargeback; admission does not enforce it. */
  perWorkspaceMonthlyUsd: number | null;
}

/** Approver groups per tier. An EMPTY list auto-approves; every named group must approve otherwise. */
export interface ApprovalRouting {
  low: string[];
  medium: string[];
  high: string[];
}

export interface WorkspaceDefaults {
  platformVersion: PlatformVersion | null;
  guardrailProfile: GuardrailProfile;
  memory: string;
  /** The tier an unclassified tool or knowledge item floors the agent to. */
  unclassifiedRiskTier: RiskTier;
  /** Whether a builder's own riskClass / dataClass labels count for admission. */
  trustBuilderClassifications: boolean;
}

export interface WorkspacePolicyDocument {
  workspaceId: string;
  name: string;
  adminGroups: string[];
  /** Cognito groups whose members belong to this workspace; `default` needs none. */
  memberGroups: string[];
  allowed: AllowedResources;
  budgets: WorkspaceBudgets;
  approval: ApprovalRouting;
  defaults: WorkspaceDefaults;
}

/** The org-wide policy that governs everyone who is in no configured workspace. */
export const DEFAULT_WORKSPACE_ID = 'default';

// ============================================================================
// Operations
// ============================================================================

export async function listWorkspacePolicies(baseUrl: string = API_BASE_URL): Promise<WorkspacePolicyDocument[]> {
  return apiRequest<WorkspacePolicyDocument[]>(`/api/workspaces/policies`, {}, baseUrl);
}

export async function getWorkspacePolicy(
  workspaceId: string,
  baseUrl: string = API_BASE_URL,
): Promise<WorkspacePolicyDocument> {
  return apiRequest<WorkspacePolicyDocument>(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/policy`,
    {},
    baseUrl,
  );
}

/** Create or replace a policy. The path's id is the document's; the backend refuses a mismatch (400). */
export async function putWorkspacePolicy(
  policy: WorkspacePolicyDocument,
  baseUrl: string = API_BASE_URL,
): Promise<WorkspacePolicyDocument> {
  return apiRequest<WorkspacePolicyDocument>(
    `/api/workspaces/${encodeURIComponent(policy.workspaceId)}/policy`,
    { method: 'PUT', body: JSON.stringify(policy) },
    baseUrl,
  );
}

/** Delete a policy (204, idempotent). A 204 has no JSON body, so this does not go through `apiRequest`. */
export async function deleteWorkspacePolicy(workspaceId: string, baseUrl: string = API_BASE_URL): Promise<void> {
  const response = await authFetch(`${baseUrl}/api/workspaces/${encodeURIComponent(workspaceId)}/policy`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    throw await apiErrorFromResponse(response);
  }
}
