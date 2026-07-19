/**
 * Human-in-the-loop API domain module (Phase 2 Gap 2D).
 */

import { apiRequest } from './client';

// ============================================================================
// Types
// ============================================================================

export interface HitlRequestSummary {
  runtime_id: string;
  request_id: string;
  status: string;
  action: string;
  reason: string;
  created_at: number;
  comment?: string | null;
  decided_at?: string | null;
}

// ============================================================================
// HITL Operations
// ============================================================================

/** The caller's pending approval queue across all their runtimes. */
export async function listHitlPending(): Promise<HitlRequestSummary[]> {
  return apiRequest<HitlRequestSummary[]>(`/api/hitl/pending`);
}

export async function decideHitl(
  requestId: string,
  runtimeId: string,
  decision: 'approve' | 'reject',
  comment = ''
): Promise<{ success: boolean; request_id: string; status: string; message: string }> {
  return apiRequest(
    `/api/hitl/${encodeURIComponent(requestId)}/decision`,
    {
      method: 'POST',
      body: JSON.stringify({ decision, comment, runtime_id: runtimeId }),
    }
  );
}
