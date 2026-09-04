/**
 * Agent Registry API domain module (Phase 2 Gap 2A + Phase 6 AWS Registry Federation).
 */

import { apiRequest } from './client';

// ============================================================================
// Types
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
  /**
   * Which catalog this row came from — 'platform' (the built-in DynamoDB
   * registry) or the name of an external backend that projected it, e.g.
   * 'litellm'. Optional because an older backend omits it; absent means
   * platform.
   *
   * `read_only` is the backend's own verdict on this row, not something the UI
   * should infer from `source`: which operations a projected entry supports is
   * the active provider's `capabilities()` decision, and the backend answers 501
   * either way. Trusting a locally-derived guess would let the UI offer a Clone
   * button that can only fail.
   */
  source?: string;
  read_only?: boolean;
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

// ============================================================================
// Registry Operations
// ============================================================================

/** Publish a deployed agent's canvas snapshot to the org registry. */
export async function publishToRegistry(data: PublishRegistryRequest): Promise<RegistryEntry> {
  return apiRequest<RegistryEntry>(`/api/registry`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

/** Search/list registry entries visible to the caller. */
export async function searchRegistry(
  opts: { q?: string; tag?: string; scope?: 'all' | 'mine' | 'public' | 'pending' } = {}
): Promise<RegistryEntry[]> {
  const params = new URLSearchParams();
  if (opts.q) params.set('q', opts.q);
  if (opts.tag) params.set('tag', opts.tag);
  if (opts.scope) params.set('scope', opts.scope);
  const qs = params.toString();
  return apiRequest<RegistryEntry[]>(
    `/api/registry${qs ? `?${qs}` : ''}`
  );
}

/**
 * Fetch a single registry entry (detail view). Unlike the list, this response
 * carries `canvas_snapshot` so the Components tab can render the blueprint's
 * nodes/edges. This is a READ, not a clone — it does NOT increment usage.
 */
export async function getRegistryEntry(slug: string): Promise<RegistryEntry> {
  return apiRequest<RegistryEntry>(
    `/api/registry/${encodeURIComponent(slug)}`
  );
}

/** Clone a registry entry — returns the canvas snapshot to drop on the canvas. */
export async function cloneFromRegistry(slug: string): Promise<RegistryCloneResponse> {
  return apiRequest<RegistryCloneResponse>(
    `/api/registry/${encodeURIComponent(slug)}/clone`,
    { method: 'POST' }
  );
}

/** Unpublish a registry entry (owner or admin). */
export async function deleteRegistryEntry(slug: string): Promise<void> {
  return apiRequest<void>(
    `/api/registry/${encodeURIComponent(slug)}`,
    { method: 'DELETE' }
  );
}

/** Approve a pending registry entry (admin only). */
export async function approveRegistry(slug: string): Promise<RegistryEntry> {
  return apiRequest<RegistryEntry>(
    `/api/registry/${encodeURIComponent(slug)}/approve`,
    { method: 'POST' }
  );
}

/** Reject a pending registry entry (admin only). */
export async function rejectRegistry(
  slug: string,
  reason?: string
): Promise<RegistryEntry> {
  return apiRequest<RegistryEntry>(
    `/api/registry/${encodeURIComponent(slug)}/reject`,
    {
      method: 'POST',
      body: reason ? JSON.stringify({ reason }) : undefined,
    }
  );
}

// ============================================================================
// AWS Agent Registry Federation (Phase 6)
// ============================================================================

/**
 * Phase 6 (Loom) — AWS Agent Registry federation config/status.
 *
 * `sdk_supported` is optional because an older backend won't return it: false
 * means the backend's boto3 lacks the GA `agent-registry` service models, which
 * is a redeploy, not a config fix.
 *
 * `status` is the registry's own lifecycle state (READY / CREATING / UPDATING /
 * DELETING / *_FAILED), or null when it could not be read. A registry that is
 * not READY is unavailable but perfectly valid — it just needs another moment —
 * so the UI must not report it as a bad registryId.
 *
 * NOTE: `ApiClient.getAwsRegistryConfig()` in ../api.ts declares this same shape
 * independently. Keep the two in step; only `tsc -b` catches a divergence.
 */
export async function getAwsRegistryConfig(): Promise<{
  enabled: boolean;
  registry_id: string | null;
  available: boolean;
  sdk_supported?: boolean;
  status?: string | null;
}> {
  return apiRequest(`/api/registry/aws-config`);
}

/** Phase 6 — enable AWS Agent Registry federation with a registryId (admin). */
export async function enableAwsRegistry(registryId: string): Promise<{ enabled: boolean; registry_id: string; available: boolean }> {
  return apiRequest(`/api/registry/aws-config`, {
    method: 'POST',
    body: JSON.stringify({ registry_id: registryId }),
  });
}

/** Phase 6 — semantic search across the AWS Agent Registry.
 *
 * Each hit's `status` is reconciled against the control plane, because the search
 * index keeps serving a redeployed record as APPROVED after it has been demoted to
 * DRAFT. `status_authoritative: false` means that reconciliation failed and `status`
 * was omitted rather than served stale. Declared independently in ../api.ts — only
 * `tsc -b` catches the two drifting apart.
 */
export async function searchAwsRegistry(q: string): Promise<{ enabled: boolean; results: Array<Record<string, unknown>>; status_authoritative?: boolean }> {
  return apiRequest(`/api/registry/aws-search?q=${encodeURIComponent(q)}`);
}

// ============================================================================
// LiteLLM registry backend (Workstream B)
// ============================================================================

/**
 * The active registry backend, plus the LiteLLM connection if one is configured.
 *
 * `verified: false` is NOT an error state. A self-hosted LiteLLM is often only
 * reachable from inside a VPC, and the control-plane Lambda has no VPC egress, so
 * a perfectly good config saves as unverified. The UI must say "could not be
 * reached from here" rather than "misconfigured" — a 401/404 is rejected outright
 * at save time and never reaches this state.
 *
 * Never carries the virtual key, only the Secrets Manager ARN it lives at.
 *
 * NOTE: mirrored on `ApiClient` in ../api.ts, like the AWS federation shapes
 * above. Only `tsc -b` catches the two drifting apart.
 */
export interface LiteLLMRegistryConfig {
  provider: 'dynamodb' | 'litellm' | string;
  configured: boolean;
  base_url?: string | null;
  api_key_ref?: string | null;
  verified: boolean;
  /**
   * What the ACTIVE backend supports, straight from its `capabilities()` — not a
   * property of LiteLLM in the abstract. Returned by GET so the panel can name
   * the authoritative catalog and explain the read-only rows without hardcoding
   * a second copy of the backend's own rules.
   */
  capabilities?: RegistryCapabilities;
  /** Present on POST only: why a save came back unverified. */
  detail?: string;
}

export interface RegistryCapabilities {
  provider: string;
  authoritative_catalog: string;
  supports_publish: boolean;
  supports_update: boolean;
  supports_delete: boolean;
  supports_review: boolean;
  supports_clone: boolean;
  read_only_sources: string[];
  notes: string;
}

export interface LiteLLMServer {
  name: string;
  slug: string;
  description?: string;
  enabled: boolean;
}

/** Read the registry backend setting and the LiteLLM connection, if any. */
export async function getLiteLLMRegistryConfig(): Promise<LiteLLMRegistryConfig> {
  return apiRequest(`/api/registry/litellm-config`);
}

/**
 * Point the registry at a LiteLLM proxy (admin). `activate` is what actually
 * switches the platform over; without it the connection is saved and probed but
 * the built-in catalog stays authoritative, so an admin can verify connectivity
 * before changing what every developer sees.
 */
export async function enableLiteLLMRegistry(data: {
  base_url: string;
  api_key?: string;
  api_key_ref?: string;
  activate?: boolean;
}): Promise<LiteLLMRegistryConfig> {
  return apiRequest(`/api/registry/litellm-config`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

/**
 * Revert to the platform's own catalog (admin). Returns the ARN of the minted
 * key, which is deliberately NOT deleted — it may be shared with a gateway node.
 */
export async function disableLiteLLMRegistry(): Promise<
  LiteLLMRegistryConfig & { orphaned_api_key_ref?: string | null }
> {
  return apiRequest(`/api/registry/litellm-config`, { method: 'DELETE' });
}

/**
 * The RAW upstream LiteLLM catalog, disabled servers included and flagged.
 *
 * Distinct from `searchRegistry()`, which projects only ENABLED servers: an admin
 * needs to tell "LiteLLM does not have it" apart from "LiteLLM has it disabled",
 * because only the second explains why a deploy was just blocked.
 */
export async function listLiteLLMServers(): Promise<{
  configured: boolean;
  servers: LiteLLMServer[];
}> {
  return apiRequest(`/api/registry/litellm-servers`);
}
