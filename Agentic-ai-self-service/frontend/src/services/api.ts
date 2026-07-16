/**
 * API Client Service for backend integration.
 * Implements workflow CRUD operations, validation calls, and deployment calls.
 * Requirements: 9.1, 11.1
 */

import type { WorkflowDefinition, DeploymentStatus } from '../types/workflow';
import type { ValidationResult } from '../types/validation';
import type { Flow, FlowCreateRequest, FlowUpdateRequest, FlowResponse, FlowListResponse } from '../types/flow';
import { authFetch } from '../auth/authFetch';

// ============================================================================
// Configuration
// ============================================================================

/**
 * Base URL for the backend API.
 * Can be configured via environment variable.
 */
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '';

// ============================================================================
// Types
// ============================================================================

export interface ApiError {
  message: string;
  status: number;
  details?: unknown;
}

export interface WorkflowCreateRequest {
  name: string;
  description?: string;
  version?: string;
  nodes?: unknown[];
  edges?: unknown[];
  viewport?: {
    x: number;
    y: number;
    zoom: number;
  };
  metadata: {
    author: string;
    tags?: string[];
    awsRegion: string;
    deploymentStatus?: DeploymentStatus;
  };
}

export interface WorkflowUpdateRequest {
  name?: string;
  description?: string;
  version?: string;
  nodes?: unknown[];
  edges?: unknown[];
  viewport?: {
    x: number;
    y: number;
    zoom: number;
  };
  metadata?: {
    author: string;
    tags?: string[];
    awsRegion: string;
    deploymentStatus?: DeploymentStatus;
  };
}

export interface WorkflowResponse {
  workflow: WorkflowDefinition;
  message: string;
}

export interface DeleteResponse {
  success: boolean;
  message: string;
}

export interface DeployRequest {
  aws_region: string;
  vpc_config?: Record<string, unknown>;
  enable_cloudwatch?: boolean;
  enable_cloudtrail?: boolean;
}

export interface DeploymentResult {
  deployment_id: string;
  status: 'success' | 'failed' | 'in_progress';
  endpoint_url?: string;
  error_message?: string;
  created_resources: string[];
}

export interface ImportRequest {
  workflow_json: Record<string, unknown>;
}

export interface ImportResponse {
  workflow: WorkflowDefinition;
  message: string;
  validation_errors: string[];
}

export interface ExportResponse {
  workflow_json: Record<string, unknown>;
  message: string;
}

// ============================================================================
// Versioning (Phase 1 Gap 1A)
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
// Evaluations (Phase 1 Gap 1C)
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
// Observability Dashboard (Phase 1 Gap 1D)
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

// Phase 3 Gap 3F — scheduled / event triggers.
export interface TriggerSummary {
  runtime_name: string;
  trigger_id: string;
  type: string;
  status: string;
  target_runtime_arn: string;
  schedule?: string | null;
  pattern?: Record<string, unknown> | null;
  webhook_secret_ref?: string | null;
  webhook_out_url?: string | null;
  created_at: number;
  updated_at: number;
}

export interface CreateTriggerInput {
  type: 'cron' | 'eventbridge' | 's3' | 'webhook';
  schedule?: string;
  pattern?: Record<string, unknown>;
  webhook_out_url?: string;
}

// Phase 2 Gap 2D — human-in-the-loop.
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
// API Client Class
// ============================================================================

export class ApiClient {
  private baseUrl: string;

  constructor(baseUrl: string = API_BASE_URL) {
    this.baseUrl = baseUrl;
  }

  // ==========================================================================
  // Private Helper Methods
  // ==========================================================================

  private async request<T>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<T> {
    const url = `${this.baseUrl}${endpoint}`;

    const defaultHeaders: HeadersInit = {
      'Content-Type': 'application/json',
    };

    const response = await authFetch(url, {
      ...options,
      headers: {
        ...defaultHeaders,
        ...options.headers,
      },
    });

    if (!response.ok) {
      let errorDetails: unknown;
      try {
        errorDetails = await response.json();
      } catch {
        errorDetails = await response.text();
      }

      const error: ApiError = {
        message: this.extractErrorMessage(errorDetails, response.statusText),
        status: response.status,
        details: errorDetails,
      };
      throw error;
    }

    // Guard against non-JSON responses (e.g., CloudFront returning HTML for 404s)
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      const text = await response.text();
      const error: ApiError = {
        message: 'Unexpected response from server',
        status: response.status,
        details: text,
      };
      throw error;
    }

    return response.json() as Promise<T>;
  }

  private extractErrorMessage(details: unknown, fallback: string): string {
    if (typeof details === 'string') {
      return details;
    }
    if (typeof details === 'object' && details !== null) {
      const obj = details as Record<string, unknown>;
      if (typeof obj.detail === 'string') {
        return obj.detail;
      }
      if (typeof obj.message === 'string') {
        return obj.message;
      }
      if (typeof obj.detail === 'object' && obj.detail !== null) {
        const detailObj = obj.detail as Record<string, unknown>;
        if (typeof detailObj.message === 'string') {
          return detailObj.message;
        }
        if (Array.isArray(detailObj.errors)) {
          return detailObj.errors.join(', ');
        }
      }
    }
    return fallback;
  }

  // ==========================================================================
  // Health Check
  // ==========================================================================

  /**
   * Checks if the backend API is healthy.
   */
  async healthCheck(): Promise<{ status: string }> {
    return this.request<{ status: string }>('/health');
  }

  // ==========================================================================
  // Workflow CRUD Operations
  // ==========================================================================

  /**
   * Creates a new workflow.
   * Requirement 9.1: Auto-save workflow
   */
  async createWorkflow(data: WorkflowCreateRequest): Promise<WorkflowResponse> {
    return this.request<WorkflowResponse>('/api/workflows', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  /**
   * Gets a workflow by ID.
   * Requirement 9.5: Restore last saved workflow
   */
  async getWorkflow(workflowId: string): Promise<WorkflowDefinition> {
    return this.request<WorkflowDefinition>(`/api/workflows/${workflowId}`);
  }

  /**
   * Updates an existing workflow.
   * Requirement 9.1: Auto-save workflow
   */
  async updateWorkflow(
    workflowId: string,
    data: WorkflowUpdateRequest
  ): Promise<WorkflowResponse> {
    return this.request<WorkflowResponse>(`/api/workflows/${workflowId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  }

  /**
   * Deletes a workflow by ID.
   */
  async deleteWorkflow(workflowId: string): Promise<DeleteResponse> {
    return this.request<DeleteResponse>(`/api/workflows/${workflowId}`, {
      method: 'DELETE',
    });
  }

  // ==========================================================================
  // Validation
  // ==========================================================================

  /**
   * Validates a workflow configuration.
   * Requirements: 8.1, 8.2, 8.3
   */
  async validateWorkflow(workflowId: string): Promise<ValidationResult> {
    return this.request<ValidationResult>(`/api/workflows/${workflowId}/validate`, {
      method: 'POST',
    });
  }

  // ==========================================================================
  // Import/Export
  // ==========================================================================

  /**
   * Imports a workflow from JSON.
   * Requirements: 14.1, 14.2, 14.3
   */
  async importWorkflow(data: ImportRequest): Promise<ImportResponse> {
    return this.request<ImportResponse>('/api/workflows/import', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  /**
   * Exports a workflow as JSON.
   * Requirements: 14.1, 14.2
   */
  async exportWorkflow(workflowId: string): Promise<ExportResponse> {
    return this.request<ExportResponse>(`/api/workflows/${workflowId}/export`);
  }

  // ==========================================================================
  // Flow CRUD Operations
  // ==========================================================================

  /**
   * Creates a new flow.
   */
  async createFlow(data: FlowCreateRequest): Promise<FlowResponse> {
    return this.request<FlowResponse>('/api/flows', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  /**
   * Lists all flows.
   */
  async listFlows(): Promise<FlowListResponse> {
    return this.request<FlowListResponse>('/api/flows');
  }

  /**
   * Gets a flow by ID.
   */
  async getFlow(flowId: string): Promise<Flow> {
    return this.request<Flow>(`/api/flows/${flowId}`);
  }

  /**
   * Updates an existing flow.
   */
  async updateFlow(
    flowId: string,
    data: FlowUpdateRequest
  ): Promise<FlowResponse> {
    return this.request<FlowResponse>(`/api/flows/${flowId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  }

  /**
   * Deletes a flow by ID.
   */
  async deleteFlow(flowId: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(`/api/flows/${flowId}`, {
      method: 'DELETE',
    });
  }

  // ==========================================================================
  // Deployment
  // ==========================================================================

  /**
   * Deploys a workflow to AWS.
   * Requirements: 11.1, 11.5, 11.6, 11.7
   */
  async deployWorkflow(
    workflowId: string,
    config: DeployRequest
  ): Promise<DeploymentResult> {
    return this.request<DeploymentResult>(`/api/workflows/${workflowId}/deploy`, {
      method: 'POST',
      body: JSON.stringify(config),
    });
  }

  // ==========================================================================
  // Versioning (Phase 1 Gap 1A)
  // ==========================================================================

  /**
   * List all versions of a friendly runtime name owned by the caller.
   * Returns newest-first.
   */
  async listVersions(runtimeName: string): Promise<AgentVersionSummary[]> {
    return this.request<AgentVersionSummary[]>(
      `/api/runtimes/${encodeURIComponent(runtimeName)}/versions`
    );
  }

  /**
   * Get the current production / staging slot pointers for a runtime.
   */
  async getSlots(runtimeName: string): Promise<RuntimeSlotsSummary> {
    return this.request<RuntimeSlotsSummary>(
      `/api/runtimes/${encodeURIComponent(runtimeName)}/slots`
    );
  }

  /**
   * Promote a specific version to the production or staging slot.
   */
  async promoteVersion(
    runtimeName: string,
    versionId: string,
    slot: 'staging' | 'production' = 'production'
  ): Promise<PromoteResult> {
    return this.request<PromoteResult>(
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
  async rollbackRuntime(runtimeName: string): Promise<PromoteResult> {
    return this.request<PromoteResult>(
      `/api/runtimes/${encodeURIComponent(runtimeName)}/rollback`,
      { method: 'POST' }
    );
  }

  // ==========================================================================
  // Evaluations (Phase 1 Gap 1C)
  // ==========================================================================

  /**
   * Get the evaluation config (evaluator IDs + sampling rate) for the
   * runtime's current production version. 404 if no eval is registered.
   */
  async getEvaluationConfig(runtimeName: string): Promise<EvaluationConfigSummary> {
    return this.request<EvaluationConfigSummary>(
      `/api/runtimes/${encodeURIComponent(runtimeName)}/evaluation-config`
    );
  }

  /**
   * List recent per-evaluator scores from CloudWatch Logs Insights.
   */
  async listEvaluationResults(
    runtimeName: string,
    hours = 24
  ): Promise<EvaluationResultsSummary> {
    return this.request<EvaluationResultsSummary>(
      `/api/runtimes/${encodeURIComponent(runtimeName)}/evaluations?hours=${hours}`
    );
  }

  // ==========================================================================
  // Observability Dashboard (Phase 1 Gap 1D)
  // ==========================================================================

  /**
   * Get the auto-generated CloudWatch dashboard URL for a runtime.
   */
  async getDashboardUrl(runtimeName: string): Promise<DashboardUrlSummary> {
    return this.request<DashboardUrlSummary>(
      `/api/runtimes/${encodeURIComponent(runtimeName)}/dashboard-url`
    );
  }

  // ==========================================================================
  // Cost analytics (Phase 2 Gap 2B)
  // ==========================================================================

  /** Cost + token rollup for a runtime over an optional window (unix seconds). */
  async getCost(
    runtimeName: string,
    opts?: { from?: number; to?: number }
  ): Promise<CostSummary> {
    const qs = new URLSearchParams();
    if (opts?.from) qs.set('from', String(opts.from));
    if (opts?.to) qs.set('to', String(opts.to));
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    return this.request<CostSummary>(
      `/api/runtimes/${encodeURIComponent(runtimeName)}/cost${suffix}`
    );
  }

  /** Phase 5 (Loom) — OTEL span waterfall for a runtime's production version. */
  async getTraces(
    runtimeName: string,
    opts?: { from?: number; to?: number; traceId?: string }
  ): Promise<TraceWaterfall> {
    const qs = new URLSearchParams();
    if (opts?.from) qs.set('from', String(opts.from));
    if (opts?.to) qs.set('to', String(opts.to));
    if (opts?.traceId) qs.set('traceId', opts.traceId);
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    return this.request<TraceWaterfall>(
      `/api/runtimes/${encodeURIComponent(runtimeName)}/traces${suffix}`
    );
  }

  /** Phase 5 (Loom) — admin action-audit summary (admin scope). */
  async getAudit(limit = 200): Promise<AuditSummary> {
    return this.request<AuditSummary>(`/api/admin/audit?limit=${limit}`);
  }

  /** Phase 6 (Loom) — AWS Agent Registry federation config/status. */
  async getAwsRegistryConfig(): Promise<{ enabled: boolean; registry_id: string | null; available: boolean }> {
    return this.request(`/api/registry/aws-config`);
  }

  /** Phase 6 — enable AWS Agent Registry federation with a registryId (admin). */
  async enableAwsRegistry(registryId: string): Promise<{ enabled: boolean; registry_id: string; available: boolean }> {
    return this.request(`/api/registry/aws-config`, {
      method: 'POST',
      body: JSON.stringify({ registry_id: registryId }),
    });
  }

  /** Phase 6 — semantic search across the AWS Agent Registry. */
  async searchAwsRegistry(q: string): Promise<{ enabled: boolean; results: Array<Record<string, unknown>> }> {
    return this.request(`/api/registry/aws-search?q=${encodeURIComponent(q)}`);
  }

  /** Phase 7 (opt-in) — multi-region/account deployment targets config. */
  async getDeployTargets(): Promise<{
    enabled: boolean;
    regions: string[];
    accounts: Array<{ account_id: string; role_arn: string; region: string }>;
  }> {
    return this.request(`/api/admin/deploy-targets`);
  }

  /** Phase 7 — explicitly enable/disable multi-region/account deployment. */
  async enableDeployTargets(enabled: boolean): Promise<{ enabled: boolean }> {
    return this.request(`/api/admin/deploy-targets/enable`, {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    });
  }

  /** Phase 7 — add an allowlisted deploy region. */
  async addDeployRegion(region: string): Promise<{ regions: string[] }> {
    return this.request(`/api/admin/deploy-targets/regions`, {
      method: 'POST',
      body: JSON.stringify({ region }),
    });
  }

  /** Phase 7 — register a cross-account deploy target (validated server-side). */
  async addDeployAccount(accountId: string, roleArn: string, region: string): Promise<{ account_id: string; validated: boolean }> {
    return this.request(`/api/admin/deploy-targets/accounts`, {
      method: 'POST',
      body: JSON.stringify({ account_id: accountId, role_arn: roleArn, region }),
    });
  }

  // ==========================================================================
  // Scheduled / event triggers (Phase 3 Gap 3F)
  // ==========================================================================

  async listTriggers(runtimeName: string): Promise<TriggerSummary[]> {
    return this.request<TriggerSummary[]>(
      `/api/runtimes/${encodeURIComponent(runtimeName)}/triggers`
    );
  }

  async createTrigger(
    runtimeName: string,
    input: CreateTriggerInput
  ): Promise<TriggerSummary> {
    return this.request<TriggerSummary>(
      `/api/runtimes/${encodeURIComponent(runtimeName)}/triggers`,
      { method: 'POST', body: JSON.stringify(input) }
    );
  }

  async deleteTrigger(
    runtimeName: string,
    triggerId: string
  ): Promise<{ success: boolean; trigger_id: string; message: string }> {
    return this.request(
      `/api/runtimes/${encodeURIComponent(runtimeName)}/triggers/${encodeURIComponent(triggerId)}`,
      { method: 'DELETE' }
    );
  }

  // ==========================================================================
  // Human-in-the-loop (Phase 2 Gap 2D)
  // ==========================================================================

  /** The caller's pending approval queue across all their runtimes. */
  async listHitlPending(): Promise<HitlRequestSummary[]> {
    return this.request<HitlRequestSummary[]>(`/api/hitl/pending`);
  }

  async decideHitl(
    requestId: string,
    runtimeId: string,
    decision: 'approve' | 'reject',
    comment = ''
  ): Promise<{ success: boolean; request_id: string; status: string; message: string }> {
    return this.request(
      `/api/hitl/${encodeURIComponent(requestId)}/decision`,
      {
        method: 'POST',
        body: JSON.stringify({ decision, comment, runtime_id: runtimeId }),
      }
    );
  }
}

// ============================================================================
// Singleton Instance
// ============================================================================

let apiClientInstance: ApiClient | null = null;

/**
 * Gets the singleton ApiClient instance.
 */
export function getApiClient(): ApiClient {
  if (!apiClientInstance) {
    apiClientInstance = new ApiClient();
  }
  return apiClientInstance;
}

/**
 * Resets the singleton instance (for testing).
 */
export function resetApiClient(): void {
  apiClientInstance = null;
}

/**
 * Creates a new ApiClient instance with custom base URL.
 */
export function createApiClient(baseUrl?: string): ApiClient {
  return new ApiClient(baseUrl);
}

// ============================================================================
// Type Guards
// ============================================================================

/**
 * Type guard to check if an error is an ApiError.
 */
export function isApiError(error: unknown): error is ApiError {
  return (
    typeof error === 'object' &&
    error !== null &&
    'message' in error &&
    'status' in error &&
    typeof (error as ApiError).message === 'string' &&
    typeof (error as ApiError).status === 'number'
  );
}

/**
 * Extracts error message from any error type.
 */
export function getErrorMessage(error: unknown): string {
  if (isApiError(error)) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  if (typeof error === 'string') {
    return error;
  }
  return 'An unknown error occurred';
}

/** HTTP status of an ApiError, or 0. Used by runtime-scoped panels to treat a
 *  "runtime not deployed / no data yet" (401/403/404) as a friendly empty state
 *  rather than a scary error banner. */
export function getErrorStatus(error: unknown): number {
  if (isApiError(error)) {
    return error.status ?? 0;
  }
  return 0;
}

/** True when the error means "this runtime has no data yet" (not deployed, or
 *  no versions/triggers/cost/dashboard recorded) — render an empty state. */
export function isNotReadyError(error: unknown): boolean {
  const s = getErrorStatus(error);
  return s === 401 || s === 403 || s === 404;
}

// ============================================================================
// AI Tool Generator Types
// ============================================================================

export interface ToolGenerateRequest {
  prompt: string;
  conversationHistory?: Array<{ role: string; content: string }>;
  existingTool?: Record<string, unknown>;
}

export interface GeneratedTool {
  toolName: string;
  displayName: string;
  description: string;
  lambdaCode: string;
  inputSchema: Record<string, unknown>;
}

export interface ToolGenerateResponse {
  success: boolean;
  tool?: GeneratedTool;
  message: string;
  error?: string;
  responseType?: 'clarification' | 'generation';
  testCases?: TestCase[];
}

// ============================================================================
// AI Tool Testing Types
// ============================================================================

export interface TestCase {
  name: string;
  input: Record<string, unknown>;
  expectedOutputKeys: string[];
  description: string;
}

export interface TestResult {
  testCaseName: string;
  passed: boolean;
  actualOutput?: Record<string, unknown>;
  error?: string;
  durationMs: number;
}

export interface ToolTestRequest {
  lambdaCode: string;
  testCases: TestCase[];
}

export interface ToolTestResponse {
  success: boolean;
  results: TestResult[];
  allPassed: boolean;
  error?: string;
}

// ============================================================================
// AI Tool Generator API Function
// ============================================================================

/**
 * Generate a Lambda tool using AI from a natural language description.
 * Calls POST /api/generate-tool on the deployment API.
 *
 * - Clarification mode (no history): synchronous response
 * - Generation mode (has history): async — returns jobId, polls until complete
 */
export async function generateToolApi(
  data: ToolGenerateRequest,
  baseUrl: string = API_BASE_URL,
): Promise<ToolGenerateResponse> {
  const url = `${baseUrl}/api/generate-tool`;
  const response = await authFetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const err = await response.json();
      detail = err.detail || err.message || detail;
    } catch {
      // ignore parse errors
    }
    return { success: false, message: '', error: `Request failed (${response.status}): ${detail}` };
  }

  const result = await response.json();

  // Async mode: generation returns {jobId, status: "running"}
  if (result.jobId && result.status === 'running') {
    return pollGenerateJob(result.jobId, baseUrl);
  }

  // Sync mode: clarification returns ToolGenerateResponse directly
  return result as ToolGenerateResponse;
}

async function pollGenerateJob(
  jobId: string,
  baseUrl: string,
  maxAttempts: number = 40,
  intervalMs: number = 2000,
): Promise<ToolGenerateResponse> {
  const pollUrl = `${baseUrl}/api/generate-tool/${jobId}`;

  for (let i = 0; i < maxAttempts; i++) {
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    try {
      const resp = await authFetch(pollUrl);
      if (!resp.ok) continue;
      const data = await resp.json();
      if (data.status === 'running') continue;
      // Completed — map to ToolGenerateResponse
      return data as ToolGenerateResponse;
    } catch {
      // Network error — retry
    }
  }

  return { success: false, message: '', error: 'Tool generation timed out after 80 seconds' };
}

// ============================================================================
// AI Tool Testing API Function
// ============================================================================

/**
 * Test a generated Lambda tool by deploying it temporarily and running test cases.
 * Calls POST /api/test-tool on the deployment API.
 */
/**
 * Test a generated Lambda tool using async polling.
 * POST starts the test (returns testId), then polls GET until complete.
 * This avoids the API Gateway 30s timeout for long-running tests.
 */
export async function testToolApi(
  data: ToolTestRequest,
  baseUrl: string = API_BASE_URL,
): Promise<ToolTestResponse> {
  // Step 1: Start async test
  const startUrl = `${baseUrl}/api/test-tool`;
  const startResponse = await authFetch(startUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });

  if (!startResponse.ok) {
    let detail = startResponse.statusText;
    try {
      const err = await startResponse.json();
      detail = err.detail || err.message || detail;
    } catch { /* ignore */ }
    return { success: false, results: [], allPassed: false, error: `Request failed (${startResponse.status}): ${detail}` };
  }

  const { testId } = await startResponse.json() as { testId: string };

  // Step 2: Poll for results (every 3s, up to 2 minutes)
  const pollUrl = `${baseUrl}/api/test-tool/${testId}`;
  const maxAttempts = 40;
  for (let i = 0; i < maxAttempts; i++) {
    await new Promise(r => setTimeout(r, 3000));

    try {
      const pollResponse = await authFetch(pollUrl);
      if (!pollResponse.ok) continue;

      const result = await pollResponse.json() as { status: string; success?: boolean; allPassed?: boolean; results?: TestResult[]; error?: string };
      if (result.status === 'running') continue;

      // Test completed
      return {
        success: result.success ?? false,
        allPassed: result.allPassed ?? false,
        results: result.results ?? [],
        error: result.error,
      };
    } catch {
      // Network error, retry
      continue;
    }
  }

  return { success: false, results: [], allPassed: false, error: 'Test timed out after 2 minutes' };
}

// ============================================================================
// AI Agent (Canvas) Generator — Phase 1 Gap 1E
// ============================================================================

export interface AgentGenerateRequest {
  prompt: string;
  conversationHistory?: Array<{ role: 'user' | 'assistant'; content: string }>;
}

export interface GeneratedNode {
  idSuffix: string;
  type: string;
  label: string;
  position: { x: number; y: number };
  configuration: Record<string, unknown>;
}

export interface GeneratedEdge {
  sourceIdSuffix: string;
  targetIdSuffix: string;
  connectionType: 'data' | 'control';
}

export interface GeneratedCanvasSpec {
  name: string;
  description?: string;
  nodes: GeneratedNode[];
  edges: GeneratedEdge[];
  rationale?: string;
}

export interface AgentGenerateResponse {
  success: boolean;
  responseType: 'clarification' | 'spec';
  message?: string;
  spec?: GeneratedCanvasSpec;
  error?: string;
}

/**
 * Generate an AgentCore canvas spec from a natural language description.
 * Mirrors generateToolApi: first call returns a clarification message;
 * subsequent calls (history populated) return a {nodes, edges} spec.
 */
export async function generateCanvasApi(
  data: AgentGenerateRequest,
  baseUrl: string = API_BASE_URL,
): Promise<AgentGenerateResponse> {
  const url = `${baseUrl}/api/generate-canvas`;
  const response = await authFetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const err = await response.json();
      detail = err?.detail?.error || err?.detail || err?.message || detail;
    } catch {
      // ignore
    }
    return {
      success: false,
      responseType: 'spec',
      error: `Request failed (${response.status}): ${detail}`,
    };
  }
  return (await response.json()) as AgentGenerateResponse;
}

// ============================================================================
// Agent Registry — Phase 2 Gap 2A
// ============================================================================

export interface RegistryEntry {
  org_id: string;
  agent_slug: string;
  display_name: string;
  description: string;
  tags: string[];
  visibility: 'private' | 'org' | 'public';
  latest_version_id?: string | null;
  usage_count: number;
  source_runtime_name?: string | null;
  created_at: string;
  updated_at: string;
  is_owner: boolean;
  status?: string;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
  rejection_reason?: string | null;
  // Populated only by the single-entry GET (detail view). Null on list results —
  // the browse grid does not carry full snapshots. Lets the Components tab render
  // the blueprint's nodes/edges without triggering a clone.
  canvas_snapshot?: RegistryCanvasSnapshot | null;
}

export interface PublishRegistryRequest {
  display_name: string;
  description?: string;
  tags?: string[];
  visibility?: 'private' | 'org' | 'public';
  canvas_snapshot: Record<string, unknown>;
  source_runtime_name?: string;
  latest_version_id?: string;
}

/**
 * A registry canvas snapshot is a RAW React-Flow canvas — the exact
 * {name, nodes, edges} the store holds, captured verbatim at publish time.
 * It is NOT the NL-generator's GeneratedCanvasSpec ({idSuffix, configuration,
 * sourceIdSuffix}) shape. Kept loosely typed (nodes/edges as unknown[]) so this
 * module stays free of React-Flow store types; App.tsx casts to AgentCoreNode[]
 * /Edge[] when loading. (Mislabeling this as GeneratedCanvasSpec is exactly what
 * let the broken clone-apply cast compile and silently drop all edges.)
 */
export interface RegistryCanvasSnapshot {
  name: string;
  nodes: unknown[];
  edges: unknown[];
}

export interface RegistryCloneResponse {
  agent_slug: string;
  display_name: string;
  canvas_snapshot: RegistryCanvasSnapshot;
}

// ---------------------------------------------------------------------------
// Verified external MCP-server catalog (browsable in the Registry UI)
// ---------------------------------------------------------------------------

export interface McpServerSummary {
  id: string;
  display_name: string;
  publisher: string;
  category: string;
  /** Integration tier: direct-none | direct-apikey | direct-oauth | adapter-3lo | adapter-stdio */
  tier: string;
  /** live | docs | community */
  verified: string;
  auth_type: string;
  live_testable: boolean;
  endpoint?: string | null;
}

export interface McpServerDetail extends McpServerSummary {
  credentials_needed: string;
  example_tools: string[];
  api_key_descriptor?: Record<string, unknown> | null;
  oauth_descriptor?: Record<string, unknown> | null;
}

/** List the verified external MCP-server catalog (registry:read). */
export async function listMcpServersApi(
  baseUrl: string = API_BASE_URL,
): Promise<McpServerSummary[]> {
  const response = await authFetch(`${baseUrl}/api/mcp-servers`, { method: 'GET' });
  if (!response.ok) {
    throw new Error(`MCP servers fetch failed (${response.status})`);
  }
  return (await response.json()) as McpServerSummary[];
}

/** Fetch one MCP server's detail (endpoint/auth/tools). */
export async function getMcpServerApi(
  serverId: string,
  baseUrl: string = API_BASE_URL,
): Promise<McpServerDetail> {
  const response = await authFetch(
    `${baseUrl}/api/mcp-servers/${encodeURIComponent(serverId)}`,
    { method: 'GET' },
  );
  if (!response.ok) {
    throw new Error(`MCP server fetch failed (${response.status})`);
  }
  return (await response.json()) as McpServerDetail;
}

// ---------------------------------------------------------------------------
// Identity: token-info (Loom-study 1.3) — the caller's decoded claims/scopes
// ---------------------------------------------------------------------------

export interface AnnotatedClaim {
  claim: string;
  value: unknown;
  note: string;
}

export interface TokenInfo {
  sub: string;
  claims: AnnotatedClaim[];
  groups: string[];
  scopes: string[];
}

// ---------------------------------------------------------------------------
// End-user chat (Loom-study Phase 3) — list deployed agents + stream an invoke
// ---------------------------------------------------------------------------

export interface DeployedAgentSummary {
  deployment_id: string;
  runtime_id: string | null;
  runtime_arn?: string | null;
  agentcore_runtime_name?: string | null;
  status: string;
  memory_result?: Record<string, unknown> | null;
}

/** List the caller's own succeeded deployments (the chat agent picker). */
export async function listMyAgentsApi(baseUrl: string = API_BASE_URL): Promise<DeployedAgentSummary[]> {
  const response = await authFetch(`${baseUrl}/api/deployments?status=succeeded`, { method: 'GET' });
  if (!response.ok) {
    throw new Error(`Agent list failed (${response.status})`);
  }
  return (await response.json()) as DeployedAgentSummary[];
}

/**
 * Stream an invocation of a deployed runtime, calling onToken for each streamed
 * token. Resolves to {sessionId, fullText}. Reuses the /api/test-runtime-stream
 * SSE contract (data: {type: token|done|error}). Falls back to the non-streaming
 * /api/test-runtime when SSE isn't available.
 */
export async function streamInvokeApi(
  params: { runtimeId: string; input: string; sessionId?: string | null },
  onToken: (t: string) => void,
  baseUrl: string = API_BASE_URL,
): Promise<{ sessionId: string | null; fullText: string }> {
  const body = JSON.stringify({
    runtimeId: params.runtimeId,
    input: params.input,
    ...(params.sessionId ? { sessionId: params.sessionId } : {}),
  });
  // authFetch adds Authorization + X-Session-Id.
  const resp = await authFetch(`${baseUrl}/api/test-runtime-stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
  });
  const ct = resp.headers.get('content-type') || '';
  if (resp.ok && resp.body && ct.includes('text/event-stream')) {
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let full = '';
    let sid: string | null = params.sessionId ?? null;
    let buf = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop() || '';
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const evt = JSON.parse(line.slice(6));
          if (evt.type === 'token' && evt.token) {
            full += evt.token;
            onToken(evt.token);
          } else if (evt.type === 'done') {
            sid = evt.session_id || sid;
            if (evt.full_response) full = evt.full_response;
          } else if (evt.type === 'error') {
            const err = new Error(evt.error || 'Stream error');
            (err as { __streamError?: boolean }).__streamError = true;
            throw err;
          }
        } catch (e) {
          // Re-throw our deliberate stream-error events; swallow JSON.parse
          // failures on malformed/partial SSE lines (which are not tagged).
          if (e instanceof Error && (e as { __streamError?: boolean }).__streamError) throw e;
        }
      }
    }
    if (full) return { sessionId: sid, fullText: full };
  }
  // Fallback: non-streaming invoke.
  const r2 = await authFetch(`${baseUrl}/api/test-runtime`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
  });
  const data = (await r2.json()) as { success?: boolean; response?: string; error?: string; sessionId?: string };
  if (!data.success) throw new Error(data.error || 'Invocation failed');
  const text = data.response || '';
  if (text) onToken(text);
  return { sessionId: data.sessionId || params.sessionId || null, fullText: text };
}

export interface LiveModelOption {
  provider: string;
  modelId: string;
  label: string;
  maxTokens: number;
  source?: string;
}

/**
 * Fetch the live Bedrock model catalog (Loom-study 5.1). The model picker can
 * call this to reflect models actually available in the account instead of the
 * hardcoded list; callers should fall back to the static AVAILABLE_MODELS on
 * error so the picker is never empty.
 */
export async function listModelsApi(baseUrl: string = API_BASE_URL): Promise<LiveModelOption[]> {
  const response = await authFetch(`${baseUrl}/api/models`, { method: 'GET' });
  if (!response.ok) {
    throw new Error(`Model catalog fetch failed (${response.status})`);
  }
  return (await response.json()) as LiveModelOption[];
}

/** Fetch the signed-in caller's decoded identity (claims + groups + scopes). */
export async function getTokenInfoApi(baseUrl: string = API_BASE_URL): Promise<TokenInfo> {
  const response = await authFetch(`${baseUrl}/api/identity/token-info`, { method: 'GET' });
  if (!response.ok) {
    throw new Error(`Token info fetch failed (${response.status})`);
  }
  return (await response.json()) as TokenInfo;
}

/** Publish a deployed agent's canvas snapshot to the org registry. */
export async function publishToRegistryApi(
  data: PublishRegistryRequest,
  baseUrl: string = API_BASE_URL,
): Promise<RegistryEntry> {
  const response = await authFetch(`${baseUrl}/api/registry`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    throw new Error(`Publish failed (${response.status})`);
  }
  return (await response.json()) as RegistryEntry;
}

/** Search/list registry entries visible to the caller. */
export async function searchRegistryApi(
  opts: { q?: string; tag?: string; scope?: 'all' | 'mine' | 'public' | 'pending' } = {},
  baseUrl: string = API_BASE_URL,
): Promise<RegistryEntry[]> {
  const params = new URLSearchParams();
  if (opts.q) params.set('q', opts.q);
  if (opts.tag) params.set('tag', opts.tag);
  if (opts.scope) params.set('scope', opts.scope);
  const qs = params.toString();
  const response = await authFetch(
    `${baseUrl}/api/registry${qs ? `?${qs}` : ''}`,
    { method: 'GET' },
  );
  if (!response.ok) {
    throw new Error(`Registry search failed (${response.status})`);
  }
  return (await response.json()) as RegistryEntry[];
}

/**
 * Fetch a single registry entry (detail view). Unlike the list, this response
 * carries `canvas_snapshot` so the Components tab can render the blueprint's
 * nodes/edges. This is a READ, not a clone — it does NOT increment usage.
 */
export async function getRegistryEntryApi(
  slug: string,
  baseUrl: string = API_BASE_URL,
): Promise<RegistryEntry> {
  const response = await authFetch(
    `${baseUrl}/api/registry/${encodeURIComponent(slug)}`,
    { method: 'GET' },
  );
  if (!response.ok) {
    throw new Error(`Registry entry fetch failed (${response.status})`);
  }
  return (await response.json()) as RegistryEntry;
}

/** Clone a registry entry — returns the canvas snapshot to drop on the canvas. */
export async function cloneFromRegistryApi(
  slug: string,
  baseUrl: string = API_BASE_URL,
): Promise<RegistryCloneResponse> {
  const response = await authFetch(
    `${baseUrl}/api/registry/${encodeURIComponent(slug)}/clone`,
    { method: 'POST' },
  );
  if (!response.ok) {
    throw new Error(`Clone failed (${response.status})`);
  }
  return (await response.json()) as RegistryCloneResponse;
}

/** Unpublish a registry entry (owner or admin). */
export async function deleteRegistryEntryApi(
  slug: string,
  baseUrl: string = API_BASE_URL,
): Promise<void> {
  const response = await authFetch(
    `${baseUrl}/api/registry/${encodeURIComponent(slug)}`,
    { method: 'DELETE' },
  );
  if (!response.ok) {
    throw new Error(`Unpublish failed (${response.status})`);
  }
}

/** Approve a pending registry entry (admin only). */
export async function approveRegistryApi(
  slug: string,
  baseUrl: string = API_BASE_URL,
): Promise<RegistryEntry> {
  const response = await authFetch(
    `${baseUrl}/api/registry/${encodeURIComponent(slug)}/approve`,
    { method: 'POST' },
  );
  if (!response.ok) {
    const msg = response.status === 403 ? 'Admin access required' : `Approve failed (${response.status})`;
    throw new Error(msg);
  }
  return (await response.json()) as RegistryEntry;
}

/** Reject a pending registry entry (admin only). */
export async function rejectRegistryApi(
  slug: string,
  reason?: string,
  baseUrl: string = API_BASE_URL,
): Promise<RegistryEntry> {
  const response = await authFetch(
    `${baseUrl}/api/registry/${encodeURIComponent(slug)}/reject`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: reason ? JSON.stringify({ reason }) : undefined,
    },
  );
  if (!response.ok) {
    const msg = response.status === 403 ? 'Admin access required' : `Reject failed (${response.status})`;
    throw new Error(msg);
  }
  return (await response.json()) as RegistryEntry;
}

// ============================================================================
// Prompt Library — Phase 3 Gap 3H
// ============================================================================

export interface PromptVersion {
  version_id: string;
  body: string;
  created_at: string;
  created_by: string;
}

export interface PromptEntry {
  org_id: string;
  prompt_name: string;
  display_name: string;
  description: string;
  tags: string[];
  versions: PromptVersion[];
  default_version_id?: string | null;
  created_at: string;
  updated_at: string;
  is_owner: boolean;
}

export interface CreatePromptRequest {
  display_name: string;
  description?: string;
  tags?: string[];
  body: string;
}

export interface AddPromptVersionRequest {
  body: string;
}

export interface ResolvePromptResponse {
  prompt_name: string;
  version_id: string;
  body: string;
}

/** List/search library prompts visible to the caller. */
export async function listPromptsApi(
  opts: { q?: string; tag?: string; scope?: 'all' | 'mine' } = {},
  baseUrl: string = API_BASE_URL,
): Promise<PromptEntry[]> {
  const params = new URLSearchParams();
  if (opts.q) params.set('q', opts.q);
  if (opts.tag) params.set('tag', opts.tag);
  if (opts.scope) params.set('scope', opts.scope);
  const qs = params.toString();
  const response = await authFetch(
    `${baseUrl}/api/prompts${qs ? `?${qs}` : ''}`,
    { method: 'GET' },
  );
  if (!response.ok) {
    throw new Error(`Prompt list failed (${response.status})`);
  }
  return (await response.json()) as PromptEntry[];
}

/** Create a library prompt (seeds an initial version from `body`). */
export async function createPromptApi(
  data: CreatePromptRequest,
  baseUrl: string = API_BASE_URL,
): Promise<PromptEntry> {
  const response = await authFetch(`${baseUrl}/api/prompts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    throw new Error(`Create prompt failed (${response.status})`);
  }
  return (await response.json()) as PromptEntry;
}

/** Fetch a single prompt (visibility-checked). */
export async function getPromptApi(
  name: string,
  baseUrl: string = API_BASE_URL,
): Promise<PromptEntry> {
  const response = await authFetch(
    `${baseUrl}/api/prompts/${encodeURIComponent(name)}`,
    { method: 'GET' },
  );
  if (!response.ok) {
    throw new Error(`Get prompt failed (${response.status})`);
  }
  return (await response.json()) as PromptEntry;
}

/** Update prompt metadata (owner only). */
export async function updatePromptApi(
  name: string,
  data: Partial<Pick<CreatePromptRequest, 'display_name' | 'description' | 'tags'>>,
  baseUrl: string = API_BASE_URL,
): Promise<PromptEntry> {
  const response = await authFetch(
    `${baseUrl}/api/prompts/${encodeURIComponent(name)}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    },
  );
  if (!response.ok) {
    throw new Error(`Update prompt failed (${response.status})`);
  }
  return (await response.json()) as PromptEntry;
}

/** Delete a prompt (owner only). */
export async function deletePromptApi(
  name: string,
  baseUrl: string = API_BASE_URL,
): Promise<void> {
  const response = await authFetch(
    `${baseUrl}/api/prompts/${encodeURIComponent(name)}`,
    { method: 'DELETE' },
  );
  if (!response.ok) {
    throw new Error(`Delete prompt failed (${response.status})`);
  }
}

/** Append a new version to a prompt (owner only). Returns the new version id. */
export async function addPromptVersionApi(
  name: string,
  data: AddPromptVersionRequest,
  baseUrl: string = API_BASE_URL,
): Promise<{ prompt_name: string; version_id: string; default_version_id?: string | null }> {
  const response = await authFetch(
    `${baseUrl}/api/prompts/${encodeURIComponent(name)}/versions`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    },
  );
  if (!response.ok) {
    throw new Error(`Add prompt version failed (${response.status})`);
  }
  return await response.json();
}

/** Set the default version of a prompt (owner only). */
export async function promotePromptVersionApi(
  name: string,
  versionId: string,
  baseUrl: string = API_BASE_URL,
): Promise<{ success: boolean; prompt_name: string; default_version_id: string }> {
  const response = await authFetch(
    `${baseUrl}/api/prompts/${encodeURIComponent(name)}/promote/${encodeURIComponent(versionId)}`,
    { method: 'POST' },
  );
  if (!response.ok) {
    throw new Error(`Promote prompt version failed (${response.status})`);
  }
  return await response.json();
}

/** Resolve a prompt body (visibility-checked; default or explicit version). */
export async function resolvePromptApi(
  name: string,
  version?: string,
  baseUrl: string = API_BASE_URL,
): Promise<ResolvePromptResponse> {
  const params = new URLSearchParams();
  if (version) params.set('version', version);
  const qs = params.toString();
  const response = await authFetch(
    `${baseUrl}/api/prompts/${encodeURIComponent(name)}/resolve${qs ? `?${qs}` : ''}`,
    { method: 'GET' },
  );
  if (!response.ok) {
    throw new Error(`Resolve prompt failed (${response.status})`);
  }
  return (await response.json()) as ResolvePromptResponse;
}

// ============================================================================
// Connector Catalog — Phase 3 Gap 3E
// ============================================================================

export interface ConnectorSummary {
  id: string;
  display_name: string;
  icon: string;
  category: string;
  auth_type: 'oauth' | 'api_key';
  capabilities: string[];
}

export interface ConnectorToolSchema {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export interface ConnectorDetail extends ConnectorSummary {
  credential_schema: Record<string, unknown>;
  tool_schemas: ConnectorToolSchema[];
}

/** List the pre-built connector catalog (auth-gated, public catalog). */
export async function listConnectorsApi(
  baseUrl: string = API_BASE_URL,
): Promise<ConnectorSummary[]> {
  const response = await authFetch(`${baseUrl}/api/connectors`, { method: 'GET' });
  if (!response.ok) throw new Error(`Connector list failed (${response.status})`);
  return (await response.json()) as ConnectorSummary[];
}

/** Fetch one connector's detail (tool + credential schema). */
export async function getConnectorApi(
  id: string,
  baseUrl: string = API_BASE_URL,
): Promise<ConnectorDetail> {
  const response = await authFetch(
    `${baseUrl}/api/connectors/${encodeURIComponent(id)}`,
    { method: 'GET' },
  );
  if (!response.ok) throw new Error(`Connector fetch failed (${response.status})`);
  return (await response.json()) as ConnectorDetail;
}

export default ApiClient;
