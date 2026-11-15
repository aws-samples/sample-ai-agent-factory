/**
 * Workspace policy admin API (plan §3.B).
 *
 * CamelCase on the wire, unlike the snake_case domain modules beside it: the backend's
 * `WorkspacePolicy` schema belongs to the Spec v2 family (`to_camel`, strict, unknown keys refused),
 * and the UI sends the document exactly as the schema defines it so a `422` names the real field.
 * Every route is behind the `workspacePolicy` feature flag (off: the exact 404 of a nonexistent
 * route) and the `admin` scope; the panel that uses this module is gated the same way.
 *
 * A policy is never replaced blind. Every stored policy has a revision, served as the `ETag` of a GET
 * or PUT (a quoted integer). A PUT says what it is based on: `If-Match: "<revision>"` to replace the
 * policy as read, or `If-None-Match: *` to create one that does not exist yet. A stale revision is
 * `412 policy_revision_conflict` (someone else saved first; the caller re-reads and reapplies) and a PUT
 * without either header is `428`. This module carries the revision alongside the document and fails
 * closed when the server does not return one: without it a later save could not be made safely.
 */

import { authFetch } from '../../auth/authFetch';
import { API_BASE_URL, apiErrorFromResponse, apiRequest, isApiError, type ApiError } from './client';

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

/** A policy as the server holds it: the document and the revision a replacement must name. */
export interface StoredWorkspacePolicy {
  policy: WorkspacePolicyDocument;
  revision: number;
}

/**
 * What a PUT claims the server holds: `'create'` (the policy must not exist yet, `If-None-Match: *`)
 * or the revision the form was loaded from (`If-Match`).
 */
export type PutPrecondition = 'create' | { revision: number };

/** The org-wide policy that governs everyone who is in no configured workspace. */
export const DEFAULT_WORKSPACE_ID = 'default';

/** HTTP status of a stale save: the policy changed since it was read (or now exists / no longer exists). */
export const POLICY_REVISION_CONFLICT_STATUS = 412;

// ============================================================================
// Revisions
// ============================================================================

/**
 * The revision an `ETag` header names, or `null` when the header is absent or not a quoted integer.
 * The backend serves strong validators only (`"3"`); a bare integer is accepted too.
 */
export function revisionFromEtag(etag: string | null): number | null {
  if (etag === null) return null;
  const match = /^\s*"?(\d+)"?\s*$/.exec(etag);
  return match ? Number(match[1]) : null;
}

/** The precondition header a PUT carries for `precondition`. */
export function preconditionHeaders(precondition: PutPrecondition): Record<string, string> {
  if (precondition === 'create') {
    return { 'If-None-Match': '*' };
  }
  if (!Number.isInteger(precondition.revision) || precondition.revision < 0) {
    throw new Error(`A policy revision is a non-negative integer, not ${String(precondition.revision)}`);
  }
  return { 'If-Match': `"${precondition.revision}"` };
}

/** True when `error` is the backend's refusal of a stale or misdirected save (`412`). */
export function isRevisionConflict(error: unknown): error is ApiError {
  return isApiError(error) && error.status === POLICY_REVISION_CONFLICT_STATUS;
}

/**
 * The revision the server holds now, as reported by a `412` body (`detail.currentRevision`), or `null`
 * when the policy no longer exists or the body did not say.
 */
export function currentRevisionOf(error: unknown): number | null {
  if (!isRevisionConflict(error) || typeof error.details !== 'object' || error.details === null) return null;
  const detail = (error.details as { detail?: unknown }).detail;
  if (typeof detail !== 'object' || detail === null) return null;
  const current = (detail as { currentRevision?: unknown }).currentRevision;
  return typeof current === 'number' && Number.isInteger(current) ? current : null;
}

async function storedPolicyFrom(response: Response): Promise<StoredWorkspacePolicy> {
  if (!response.ok) {
    throw await apiErrorFromResponse(response);
  }
  const revision = revisionFromEtag(response.headers.get('ETag'));
  if (revision === null) {
    // Fail closed: a document whose revision is unknown cannot be saved back safely, so do not hand it out.
    const error: ApiError = {
      message:
        'The server did not return the policy revision (ETag), so this policy cannot be edited safely. ' +
        'Reload; if it persists, the API is not exposing the ETag header.',
      status: response.status,
    };
    throw error;
  }
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) {
    const error: ApiError = { message: 'Unexpected response from server', status: response.status };
    throw error;
  }
  return { policy: (await response.json()) as WorkspacePolicyDocument, revision };
}

// ============================================================================
// Operations
// ============================================================================

export async function listWorkspacePolicies(baseUrl: string = API_BASE_URL): Promise<WorkspacePolicyDocument[]> {
  return apiRequest<WorkspacePolicyDocument[]>(`/api/workspaces/policies`, {}, baseUrl);
}

/** Read one policy with the revision a later save must name (the `ETag`). */
export async function getWorkspacePolicy(
  workspaceId: string,
  baseUrl: string = API_BASE_URL,
): Promise<StoredWorkspacePolicy> {
  const response = await authFetch(`${baseUrl}/api/workspaces/${encodeURIComponent(workspaceId)}/policy`, {
    method: 'GET',
  });
  return storedPolicyFrom(response);
}

/**
 * Create (`'create'`) or replace (`{ revision }`) a policy. The path's id is the document's; the backend
 * refuses a mismatch (400), a stale or misdirected precondition (412) and a missing one (428).
 */
export async function putWorkspacePolicy(
  policy: WorkspacePolicyDocument,
  precondition: PutPrecondition,
  baseUrl: string = API_BASE_URL,
): Promise<StoredWorkspacePolicy> {
  const response = await authFetch(`${baseUrl}/api/workspaces/${encodeURIComponent(policy.workspaceId)}/policy`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...preconditionHeaders(precondition) },
    body: JSON.stringify(policy),
  });
  return storedPolicyFrom(response);
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
