/**
 * API Client Service for backend integration.
 * Barrel re-exporting all domain modules with unchanged names/signatures.
 * Requirements: 9.1, 11.1
 */

// ============================================================================
// Re-export client infrastructure
// ============================================================================

export {
  API_BASE_URL,
  isApiError,
  getErrorMessage,
  getErrorStatus,
  isNotReadyError,
  apiRequest,
  type ApiError,
} from './api/client';

// ============================================================================
// Re-export workflows domain
// ============================================================================

export {
  createWorkflow,
  getWorkflow,
  updateWorkflow,
  deleteWorkflow,
  validateWorkflow,
  importWorkflow,
  exportWorkflow,
  createFlow,
  listFlows,
  getFlow,
  updateFlow,
  deleteFlow,
  healthCheck,
  type WorkflowCreateRequest,
  type WorkflowUpdateRequest,
  type WorkflowResponse,
  type DeleteResponse,
  type ImportRequest,
  type ImportResponse,
  type ExportResponse,
} from './api/workflows';

// ============================================================================
// Re-export deployments domain
// ============================================================================

export {
  deployWorkflow,
  type DeployRequest,
  type DeploymentResult,
} from './api/deployments';

// ============================================================================
// Re-export versions domain
// ============================================================================

export {
  listVersions,
  getSlots,
  promoteVersion,
  rollbackRuntime,
  type AgentVersionSummary,
  type RuntimeSlotsSummary,
  type PromoteResult,
} from './api/versions';

// ============================================================================
// Re-export evaluations domain
// ============================================================================

export {
  getEvaluationConfig,
  listEvaluationResults,
  type EvaluationConfigSummary,
  type EvaluationResultRow,
  type EvaluationResultsSummary,
} from './api/evaluations';

// ============================================================================
// Re-export observability domain
// ============================================================================

export {
  getDashboardUrl,
  getCost,
  getTraces,
  getAudit,
  type DashboardUrlSummary,
  type CostSummary,
  type TraceSpan,
  type TraceWaterfall,
  type AuditSummary,
} from './api/observability';

// ============================================================================
// Re-export triggers domain
// ============================================================================

export {
  listTriggers,
  createTrigger,
  deleteTrigger,
  type TriggerSummary,
  type CreateTriggerInput,
} from './api/triggers';

// ============================================================================
// Re-export HITL domain
// ============================================================================

export {
  listHitlPending,
  decideHitl,
  type HitlRequestSummary,
} from './api/hitl';

// ============================================================================
// Re-export prompts domain
// ============================================================================

export {
  listPrompts as listPromptsApi,
  createPrompt as createPromptApi,
  getPrompt as getPromptApi,
  updatePrompt as updatePromptApi,
  deletePrompt as deletePromptApi,
  addPromptVersion as addPromptVersionApi,
  promotePromptVersion as promotePromptVersionApi,
  resolvePrompt as resolvePromptApi,
  type PromptVersion,
  type PromptEntry,
  type CreatePromptRequest,
  type AddPromptVersionRequest,
  type ResolvePromptResponse,
} from './api/prompts';

// ============================================================================
// Re-export registry domain
// ============================================================================

export {
  publishToRegistry as publishToRegistryApi,
  searchRegistry as searchRegistryApi,
  getRegistryEntry as getRegistryEntryApi,
  cloneFromRegistry as cloneFromRegistryApi,
  deleteRegistryEntry as deleteRegistryEntryApi,
  approveRegistry as approveRegistryApi,
  rejectRegistry as rejectRegistryApi,
  getAwsRegistryConfig,
  enableAwsRegistry,
  searchAwsRegistry,
  getLiteLLMRegistryConfig,
  enableLiteLLMRegistry,
  disableLiteLLMRegistry,
  listLiteLLMServers,
  type RegistryEntry,
  type PublishRegistryRequest,
  type RegistryCanvasSnapshot,
  type RegistryCloneResponse,
  type LiteLLMRegistryConfig,
  type LiteLLMServer,
  type RegistryCapabilities,
} from './api/registry';

// `export … from` re-exports without binding locally, so the ApiClient methods
// below need their own type import.
import type { LiteLLMRegistryConfig, LiteLLMServer } from './api/registry';

// ============================================================================
// Re-export connectors domain
// ============================================================================

export {
  listConnectors as listConnectorsApi,
  getConnector as getConnectorApi,
  type ConnectorSummary,
  type ConnectorToolSchema,
  type ConnectorDetail,
} from './api/connectors';

// ============================================================================
// Re-export MCP servers domain
// ============================================================================

export {
  listMcpServers as listMcpServersApi,
  getMcpServer as getMcpServerApi,
  type McpServerSummary,
  type McpServerDetail,
} from './api/mcpServers';

// ============================================================================
// Re-export tools domain
// ============================================================================

export {
  generateTool as generateToolApi,
  testTool as testToolApi,
  type ToolGenerateRequest,
  type GeneratedTool,
  type ToolGenerateResponse,
  type TestCase,
  type TestResult,
  type ToolTestRequest,
  type ToolTestResponse,
} from './api/tools';

// ============================================================================
// Re-export agents domain
// ============================================================================

export {
  generateCanvas as generateCanvasApi,
  type AgentGenerateRequest,
  type GeneratedNode,
  type GeneratedEdge,
  type GeneratedCanvasSpec,
  type AgentGenerateResponse,
} from './api/agents';

// ============================================================================
// Re-export admin domain
// ============================================================================

export {
  getDeployTargets,
  enableDeployTargets,
  addDeployRegion,
  addDeployAccount,
  type DeployTargetsConfig,
} from './api/admin';

// ============================================================================
// Re-export models domain
// ============================================================================

export {
  listModels as listModelsApi,
  getTokenInfo as getTokenInfoApi,
  type LiveModelOption,
  type AnnotatedClaim,
  type TokenInfo,
} from './api/models';

// ============================================================================
// Re-export chat domain
// ============================================================================

export {
  listMyAgents as listMyAgentsApi,
  streamInvoke as streamInvokeApi,
  type DeployedAgentSummary,
} from './api/chat';

// ============================================================================
// Legacy ApiClient class (for backward compatibility)
// ============================================================================

import { apiRequest } from './api/client';
import type { WorkflowDefinition } from '../types/workflow';
import type { ValidationResult } from '../types/validation';
import type { Flow, FlowCreateRequest, FlowUpdateRequest, FlowResponse, FlowListResponse } from '../types/flow';
import type {
  WorkflowCreateRequest,
  WorkflowUpdateRequest,
  WorkflowResponse,
  DeleteResponse,
  ImportRequest,
  ImportResponse,
  ExportResponse,
} from './api/workflows';
import type {
  DeployRequest,
  DeploymentResult,
} from './api/deployments';
import type {
  AgentVersionSummary,
  RuntimeSlotsSummary,
  PromoteResult,
} from './api/versions';
import type {
  EvaluationConfigSummary,
  EvaluationResultsSummary,
} from './api/evaluations';
import type {
  DashboardUrlSummary,
  CostSummary,
  TraceWaterfall,
  AuditSummary,
} from './api/observability';
import type {
  TriggerSummary,
  CreateTriggerInput,
} from './api/triggers';
import type {
  HitlRequestSummary,
} from './api/hitl';

export class ApiClient {
  private baseUrl: string;

  constructor(baseUrl?: string) {
    this.baseUrl = baseUrl || import.meta.env.VITE_API_BASE_URL || '';
  }

  // Workflows
  async createWorkflow(data: WorkflowCreateRequest): Promise<WorkflowResponse> {
    return apiRequest<WorkflowResponse>('/api/workflows', { method: 'POST', body: JSON.stringify(data) }, this.baseUrl);
  }

  async getWorkflow(workflowId: string): Promise<WorkflowDefinition> {
    return apiRequest<WorkflowDefinition>(`/api/workflows/${workflowId}`, {}, this.baseUrl);
  }

  async updateWorkflow(workflowId: string, data: WorkflowUpdateRequest): Promise<WorkflowResponse> {
    return apiRequest<WorkflowResponse>(`/api/workflows/${workflowId}`, { method: 'PUT', body: JSON.stringify(data) }, this.baseUrl);
  }

  async deleteWorkflow(workflowId: string): Promise<DeleteResponse> {
    return apiRequest<DeleteResponse>(`/api/workflows/${workflowId}`, { method: 'DELETE' }, this.baseUrl);
  }

  async validateWorkflow(workflowId: string): Promise<ValidationResult> {
    return apiRequest<ValidationResult>(`/api/workflows/${workflowId}/validate`, { method: 'POST' }, this.baseUrl);
  }

  async importWorkflow(data: ImportRequest): Promise<ImportResponse> {
    return apiRequest<ImportResponse>('/api/workflows/import', { method: 'POST', body: JSON.stringify(data) }, this.baseUrl);
  }

  async exportWorkflow(workflowId: string): Promise<ExportResponse> {
    return apiRequest<ExportResponse>(`/api/workflows/${workflowId}/export`, {}, this.baseUrl);
  }

  async healthCheck(): Promise<{ status: string }> {
    return apiRequest<{ status: string }>('/health', {}, this.baseUrl);
  }

  // Flows
  async createFlow(data: FlowCreateRequest): Promise<FlowResponse> {
    return apiRequest<FlowResponse>('/api/flows', { method: 'POST', body: JSON.stringify(data) }, this.baseUrl);
  }

  async listFlows(): Promise<FlowListResponse> {
    return apiRequest<FlowListResponse>('/api/flows', {}, this.baseUrl);
  }

  async getFlow(flowId: string): Promise<Flow> {
    return apiRequest<Flow>(`/api/flows/${flowId}`, {}, this.baseUrl);
  }

  async updateFlow(flowId: string, data: FlowUpdateRequest): Promise<FlowResponse> {
    return apiRequest<FlowResponse>(`/api/flows/${flowId}`, { method: 'PUT', body: JSON.stringify(data) }, this.baseUrl);
  }

  async deleteFlow(flowId: string): Promise<{ message: string }> {
    return apiRequest<{ message: string }>(`/api/flows/${flowId}`, { method: 'DELETE' }, this.baseUrl);
  }

  // Deployments
  async deployWorkflow(workflowId: string, config: DeployRequest): Promise<DeploymentResult> {
    return apiRequest<DeploymentResult>(`/api/workflows/${workflowId}/deploy`, { method: 'POST', body: JSON.stringify(config) }, this.baseUrl);
  }

  // Versions
  async listVersions(runtimeName: string): Promise<AgentVersionSummary[]> {
    return apiRequest<AgentVersionSummary[]>(`/api/runtimes/${encodeURIComponent(runtimeName)}/versions`, {}, this.baseUrl);
  }

  async getSlots(runtimeName: string): Promise<RuntimeSlotsSummary> {
    return apiRequest<RuntimeSlotsSummary>(`/api/runtimes/${encodeURIComponent(runtimeName)}/slots`, {}, this.baseUrl);
  }

  async promoteVersion(runtimeName: string, versionId: string, slot: 'staging' | 'production' = 'production'): Promise<PromoteResult> {
    return apiRequest<PromoteResult>(`/api/runtimes/${encodeURIComponent(runtimeName)}/versions/${encodeURIComponent(versionId)}/promote`, { method: 'POST', body: JSON.stringify({ slot }) }, this.baseUrl);
  }

  async rollbackRuntime(runtimeName: string): Promise<PromoteResult> {
    return apiRequest<PromoteResult>(`/api/runtimes/${encodeURIComponent(runtimeName)}/rollback`, { method: 'POST' }, this.baseUrl);
  }

  // Evaluations
  async getEvaluationConfig(runtimeName: string): Promise<EvaluationConfigSummary> {
    return apiRequest<EvaluationConfigSummary>(`/api/runtimes/${encodeURIComponent(runtimeName)}/evaluation-config`, {}, this.baseUrl);
  }

  async listEvaluationResults(runtimeName: string, hours = 24): Promise<EvaluationResultsSummary> {
    return apiRequest<EvaluationResultsSummary>(`/api/runtimes/${encodeURIComponent(runtimeName)}/evaluations?hours=${hours}`, {}, this.baseUrl);
  }

  // Observability
  async getDashboardUrl(runtimeName: string): Promise<DashboardUrlSummary> {
    return apiRequest<DashboardUrlSummary>(`/api/runtimes/${encodeURIComponent(runtimeName)}/dashboard-url`, {}, this.baseUrl);
  }

  async getCost(runtimeName: string, opts?: { from?: number; to?: number }): Promise<CostSummary> {
    const qs = new URLSearchParams();
    if (opts?.from) qs.set('from', String(opts.from));
    if (opts?.to) qs.set('to', String(opts.to));
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    return apiRequest<CostSummary>(`/api/runtimes/${encodeURIComponent(runtimeName)}/cost${suffix}`, {}, this.baseUrl);
  }

  async getTraces(runtimeName: string, opts?: { from?: number; to?: number; traceId?: string }): Promise<TraceWaterfall> {
    const qs = new URLSearchParams();
    if (opts?.from) qs.set('from', String(opts.from));
    if (opts?.to) qs.set('to', String(opts.to));
    if (opts?.traceId) qs.set('traceId', opts.traceId);
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    return apiRequest<TraceWaterfall>(`/api/runtimes/${encodeURIComponent(runtimeName)}/traces${suffix}`, {}, this.baseUrl);
  }

  async getAudit(limit = 200): Promise<AuditSummary> {
    return apiRequest<AuditSummary>(`/api/admin/audit?limit=${limit}`, {}, this.baseUrl);
  }

  // Triggers
  async listTriggers(runtimeName: string): Promise<TriggerSummary[]> {
    return apiRequest<TriggerSummary[]>(`/api/runtimes/${encodeURIComponent(runtimeName)}/triggers`, {}, this.baseUrl);
  }

  async createTrigger(runtimeName: string, input: CreateTriggerInput): Promise<TriggerSummary> {
    return apiRequest<TriggerSummary>(`/api/runtimes/${encodeURIComponent(runtimeName)}/triggers`, { method: 'POST', body: JSON.stringify(input) }, this.baseUrl);
  }

  async deleteTrigger(runtimeName: string, triggerId: string): Promise<{ success: boolean; trigger_id: string; message: string }> {
    return apiRequest(`/api/runtimes/${encodeURIComponent(runtimeName)}/triggers/${encodeURIComponent(triggerId)}`, { method: 'DELETE' }, this.baseUrl);
  }

  // HITL
  async listHitlPending(): Promise<HitlRequestSummary[]> {
    return apiRequest<HitlRequestSummary[]>(`/api/hitl/pending`, {}, this.baseUrl);
  }

  async decideHitl(requestId: string, runtimeId: string, decision: 'approve' | 'reject', comment = ''): Promise<{ success: boolean; request_id: string; status: string; message: string }> {
    return apiRequest(`/api/hitl/${encodeURIComponent(requestId)}/decision`, { method: 'POST', body: JSON.stringify({ decision, comment, runtime_id: runtimeId }) }, this.baseUrl);
  }

  // AWS Registry (Phase 6)
  /** `sdk_supported` is optional: an older backend omits it. False means that
   *  backend's boto3 lacks the GA `agent-registry` service models (a redeploy,
   *  not a config fix). `status` is the registry's lifecycle state, null if it
   *  could not be read — a non-READY registry is valid but not yet writable.
   *  Mirrors the declaration in services/api/registry.ts; keep both in step. */
  async getAwsRegistryConfig(): Promise<{ enabled: boolean; registry_id: string | null; available: boolean; sdk_supported?: boolean; status?: string | null }> {
    return apiRequest(`/api/registry/aws-config`, {}, this.baseUrl);
  }

  async enableAwsRegistry(registryId: string): Promise<{ enabled: boolean; registry_id: string; available: boolean }> {
    return apiRequest(`/api/registry/aws-config`, { method: 'POST', body: JSON.stringify({ registry_id: registryId }) }, this.baseUrl);
  }

  // `status_authoritative: false` means each hit's `status` was dropped because the
  // control plane couldn't be reached — the data plane's own status is stale after a
  // redeploy, so it is not served as a fallback. Mirrored in ./api/registry.ts.
  async searchAwsRegistry(q: string): Promise<{ enabled: boolean; results: Array<Record<string, unknown>>; status_authoritative?: boolean }> {
    return apiRequest(`/api/registry/aws-search?q=${encodeURIComponent(q)}`, {}, this.baseUrl);
  }

  // LiteLLM registry backend (Workstream B) — an ALTERNATIVE catalog, not a
  // replacement for the two above: AgentCore/AWS federation stays untouched and
  // the built-in DynamoDB catalog remains the default.
  //
  // These reuse the LiteLLMRegistryConfig / LiteLLMServer types from
  // ./api/registry rather than re-declaring them inline, which is what the AWS
  // methods above do and what keeps drifting. Same reason the JSDoc there says
  // "keep both in step".
  async getLiteLLMRegistryConfig(): Promise<LiteLLMRegistryConfig> {
    return apiRequest(`/api/registry/litellm-config`, {}, this.baseUrl);
  }

  async enableLiteLLMRegistry(data: { base_url: string; api_key?: string; api_key_ref?: string; activate?: boolean }): Promise<LiteLLMRegistryConfig> {
    return apiRequest(`/api/registry/litellm-config`, { method: 'POST', body: JSON.stringify(data) }, this.baseUrl);
  }

  async disableLiteLLMRegistry(): Promise<LiteLLMRegistryConfig & { orphaned_api_key_ref?: string | null }> {
    return apiRequest(`/api/registry/litellm-config`, { method: 'DELETE' }, this.baseUrl);
  }

  async listLiteLLMServers(): Promise<{ configured: boolean; servers: LiteLLMServer[] }> {
    return apiRequest(`/api/registry/litellm-servers`, {}, this.baseUrl);
  }

  // Admin (Phase 7)
  async getDeployTargets(): Promise<{ enabled: boolean; regions: string[]; accounts: Array<{ account_id: string; role_arn: string; region: string }> }> {
    return apiRequest(`/api/admin/deploy-targets`, {}, this.baseUrl);
  }

  async enableDeployTargets(enabled: boolean): Promise<{ enabled: boolean }> {
    return apiRequest(`/api/admin/deploy-targets/enable`, { method: 'POST', body: JSON.stringify({ enabled }) }, this.baseUrl);
  }

  async addDeployRegion(region: string): Promise<{ regions: string[] }> {
    return apiRequest(`/api/admin/deploy-targets/regions`, { method: 'POST', body: JSON.stringify({ region }) }, this.baseUrl);
  }

  async addDeployAccount(accountId: string, roleArn: string, region: string): Promise<{ account_id: string; validated: boolean }> {
    return apiRequest(`/api/admin/deploy-targets/accounts`, { method: 'POST', body: JSON.stringify({ account_id: accountId, role_arn: roleArn, region }) }, this.baseUrl);
  }
}

// Singleton
let apiClientInstance: ApiClient | null = null;

export function getApiClient(): ApiClient {
  if (!apiClientInstance) {
    apiClientInstance = new ApiClient();
  }
  return apiClientInstance;
}

export function resetApiClient(): void {
  apiClientInstance = null;
}

export function createApiClient(baseUrl?: string): ApiClient {
  return new ApiClient(baseUrl);
}

export default ApiClient;
