/**
 * Observability API domain module (Phase 1 Gap 1D + Phase 2 Gap 2B + Phase 5 Loom).
 */

import { apiRequest } from './client';

// ============================================================================
// Types
// ============================================================================

export interface DashboardUrlSummary {
  runtime_name: string;
  version_id: string;
  runtime_id: string;
  dashboard_name: string;
  dashboard_url: string;
  exists: boolean;
}

// Phase 2 Gap 2B — cost analytics.
export interface CostSummary {
  runtime_name?: string;
  total_cost: number;
  total_in: number;
  total_out: number;
  by_model: Record<string, { input_tokens?: number; output_tokens?: number; cost?: number }>;
  from_ts?: number;
  to_ts?: number;
  currency?: string;
  // Phase 4 (Loom) FinOps — owner budget status annotated by the cost endpoint
  // when the caller has an owner budget set.
  owner_budget?: {
    spend: number;
    limit: number;
    used_pct: number;
    status: 'ok' | 'warn' | 'over';
  };
}

// Phase 5 (Loom) — OTEL trace waterfall.
export interface TraceSpan {
  span_id: string;
  parent_span_id: string;
  name: string;
  offset_ms: number;
  duration_ms: number;
  depth: number;
  children: TraceSpan[];
}
export interface TraceWaterfall {
  trace_id: string | null;
  start_ms: number;
  total_ms: number;
  spans: TraceSpan[];
  runtime_name?: string;
  query_status?: string;
}

// Phase 5 (Loom) — admin action-audit summary.
export interface AuditSummary {
  total: number;
  by_action: Record<string, number>;
  by_actor: Record<string, number>;
  // Loom-study 5.2 — analytics extras (optional for backward compat with older
  // backends that don't return them).
  distinct_actors?: number;
  distinct_sessions?: number;
  by_day?: Array<{ day: string; count: number }>;
  events: Array<{
    action: string; actor_sub: string; method: string; path: string;
    status_code: number; ts: string;
  }>;
}

// ============================================================================
// Observability Operations
// ============================================================================

/**
 * Get the auto-generated CloudWatch dashboard URL for a runtime.
 */
export async function getDashboardUrl(runtimeName: string): Promise<DashboardUrlSummary> {
  return apiRequest<DashboardUrlSummary>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/dashboard-url`
  );
}

/** Cost + token rollup for a runtime over an optional window (unix seconds). */
export async function getCost(
  runtimeName: string,
  opts?: { from?: number; to?: number }
): Promise<CostSummary> {
  const qs = new URLSearchParams();
  if (opts?.from) qs.set('from', String(opts.from));
  if (opts?.to) qs.set('to', String(opts.to));
  const suffix = qs.toString() ? `?${qs.toString()}` : '';
  return apiRequest<CostSummary>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/cost${suffix}`
  );
}

/** Phase 5 (Loom) — OTEL span waterfall for a runtime's production version. */
export async function getTraces(
  runtimeName: string,
  opts?: { from?: number; to?: number; traceId?: string }
): Promise<TraceWaterfall> {
  const qs = new URLSearchParams();
  if (opts?.from) qs.set('from', String(opts.from));
  if (opts?.to) qs.set('to', String(opts.to));
  if (opts?.traceId) qs.set('traceId', opts.traceId);
  const suffix = qs.toString() ? `?${qs.toString()}` : '';
  return apiRequest<TraceWaterfall>(
    `/api/runtimes/${encodeURIComponent(runtimeName)}/traces${suffix}`
  );
}

/** Phase 5 (Loom) — admin action-audit summary (admin scope). */
export async function getAudit(limit = 200): Promise<AuditSummary> {
  return apiRequest<AuditSummary>(`/api/admin/audit?limit=${limit}`);
}
