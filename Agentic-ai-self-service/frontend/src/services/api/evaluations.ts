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

export interface PromotionPolicy {
  schema_version: 1;
  minimum_sessions: number;
  maximum_failed_sessions: number;
  maximum_ignored_sessions: number;
  maximum_evaluator_failures: number;
  max_evidence_age_seconds: number;
  evaluators: Record<string, number>;
  policy_digest: string;
}

export interface PromotionEvidence {
  schema_version: 1;
  verdict: 'pass';
  runtime_name: string;
  version_id: string;
  deployment_id: string;
  runtime_id: string;
  batch_evaluation_id: string;
  policy_digest: string;
  verified_at: string;
  completed_at: string;
  expires_at_epoch: number;
  evidence_digest: string;
  session_summary: {
    total: number;
    completed: number;
    failed: number;
    ignored: number;
    session_ids_digest: string;
  };
  evaluator_summaries: Array<{
    evaluator_id: string;
    average_score: number;
    minimum_score: number;
    total_evaluated: number;
    total_failed: number;
  }>;
}

export interface PromotionEvidenceSummary {
  runtime_name: string;
  version_id: string;
  production_gate_required: boolean;
  promotion_policy: PromotionPolicy | null;
  promotion_evidence: PromotionEvidence | null;
}

export interface PromotionEvaluationScenario {
  sessionId: string;
  testScenarioId: string;
  assertions?: string[];
  expectedToolNames?: string[];
  turns?: Array<{ prompt: string; expectedResponse: string }>;
}

export interface PromotionEvaluationStart {
  batch_evaluation_id: string;
  batch_evaluation_arn: string;
  status: string;
  policy: PromotionPolicy;
  scenario_count: number;
  request_digest: string;
}

export interface PromotionEvaluationStatus {
  batch_evaluation_id: string;
  status: string;
  created_at: string;
  updated_at: string;
  total_sessions: number;
  completed_sessions: number;
  failed_sessions: number;
  ignored_sessions: number;
  evaluator_summaries: Array<{
    evaluator_id: string;
    average_score: number;
    total_evaluated: number;
    total_failed: number;
  }>;
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


export async function getPromotionEvidence(
  runtimeName: string,
  versionId: string,
): Promise<PromotionEvidenceSummary> {
  return apiRequest<PromotionEvidenceSummary>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/versions/${encodeURIComponent(versionId)}/promotion-evidence`,
  );
}

export async function startPromotionEvaluation(
  runtimeName: string,
  versionId: string,
  scenarios: PromotionEvaluationScenario[],
): Promise<PromotionEvaluationStart> {
  return apiRequest<PromotionEvaluationStart>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/versions/${encodeURIComponent(versionId)}/promotion-evaluation/start`,
    { method: 'POST', body: JSON.stringify({ scenarios }) },
  );
}

export async function getPromotionEvaluation(
  runtimeName: string,
  versionId: string,
  batchEvaluationId: string,
): Promise<PromotionEvaluationStatus> {
  return apiRequest<PromotionEvaluationStatus>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/versions/${encodeURIComponent(versionId)}/promotion-evaluation/${encodeURIComponent(batchEvaluationId)}`,
  );
}

export async function attestPromotionEvaluation(
  runtimeName: string,
  versionId: string,
  batchEvaluationId: string,
): Promise<PromotionEvidenceSummary> {
  return apiRequest<PromotionEvidenceSummary>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/versions/${encodeURIComponent(versionId)}/promotion-evaluation/attest`,
    { method: 'POST', body: JSON.stringify({ batchEvaluationId }) },
  );
}
