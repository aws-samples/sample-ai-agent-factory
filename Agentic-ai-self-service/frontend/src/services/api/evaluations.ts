/**
 * Evaluations API domain module (Phase 1 Gap 1C).
 */

import { apiRequest } from './client';

// ============================================================================
// Types
// ============================================================================

export interface EvaluationConfigSummary {
  runtime_name: string;
  version_id: string;
  runtime_id: string;
  config_id: string;
  config_name: string;
  evaluators: string[];
  sampling_rate: number | null;
  status?: string | null;
}

export interface EvaluationResultRow {
  eid: string;
  runs?: string;
  avg_score?: string;
  latest_score?: string;
}

export interface EvaluationResultsSummary {
  runtime_name: string;
  version_id: string;
  runtime_id: string;
  log_group_name: string;
  from_ts: number;
  to_ts: number;
  query_status?: string;
  results: EvaluationResultRow[];
  message?: string;
}

// ============================================================================
// Evaluation Operations
// ============================================================================

/**
 * Get the evaluation config (evaluator IDs + sampling rate) for the
 * runtime's current production version. 404 if no eval is registered.
 */
export async function getEvaluationConfig(runtimeName: string): Promise<EvaluationConfigSummary> {
  return apiRequest<EvaluationConfigSummary>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/evaluation-config`
  );
}

/**
 * List recent per-evaluator scores from CloudWatch Logs Insights.
 */
export async function listEvaluationResults(
  runtimeName: string,
  hours = 24
): Promise<EvaluationResultsSummary> {
  return apiRequest<EvaluationResultsSummary>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/evaluations?hours=${hours}`
  );
}
