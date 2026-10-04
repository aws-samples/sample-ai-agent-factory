/**
 * Triggers API domain module (Phase 3 Gap 3F — scheduled / event triggers).
 */

import { apiRequest } from './client';

// ============================================================================
// Types
// ============================================================================

export type TriggerType = 'cron' | 'eventbridge' | 's3' | 'webhook';

export type TriggerStatus =
  | 'active'
  | 'provisioning'
  | 'error'
  | 'deleting'
  | 'disabled'
  | 'registered';

export interface TriggerSummary {
  runtime_name: string;
  trigger_id: string;
  type: TriggerType;
  status: TriggerStatus;
  target_runtime_arn: string;
  schedule?: string | null;
  pattern?: Record<string, unknown> | null;
  webhook_out_url?: string | null;
  webhook_path?: string | null;
  /**
   * Returned once, by webhook creation only. The backend never persists or
   * returns this value from list operations.
   */
  webhook_signing_secret?: string | null;
  last_error_code?: string | null;
  created_at: number;
  updated_at: number;
}

export interface CreateTriggerInput {
  type: TriggerType;
  schedule?: string;
  pattern?: Record<string, unknown>;
  webhook_out_url?: string;
}

// ============================================================================
// Trigger Operations
// ============================================================================

export async function listTriggers(runtimeName: string): Promise<TriggerSummary[]> {
  return apiRequest<TriggerSummary[]>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/triggers`
  );
}

export async function createTrigger(
  runtimeName: string,
  input: CreateTriggerInput
): Promise<TriggerSummary> {
  return apiRequest<TriggerSummary>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/triggers`,
    { method: 'POST', body: JSON.stringify(input) }
  );
}

export async function deleteTrigger(
  runtimeName: string,
  triggerId: string
): Promise<{ success: boolean; trigger_id: string; message: string }> {
  return apiRequest(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/triggers/${encodeURIComponent(triggerId)}`,
    { method: 'DELETE' }
  );
}
