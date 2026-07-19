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

/** Phase 6 (Loom) — AWS Agent Registry federation config/status. */
export async function getAwsRegistryConfig(): Promise<{ enabled: boolean; registry_id: string | null; available: boolean }> {
  return apiRequest(`/api/registry/aws-config`);
}

/** Phase 6 — enable AWS Agent Registry federation with a registryId (admin). */
export async function enableAwsRegistry(registryId: string): Promise<{ enabled: boolean; registry_id: string; available: boolean }> {
  return apiRequest(`/api/registry/aws-config`, {
    method: 'POST',
    body: JSON.stringify({ registry_id: registryId }),
  });
}

/** Phase 6 — semantic search across the AWS Agent Registry. */
export async function searchAwsRegistry(q: string): Promise<{ enabled: boolean; results: Array<Record<string, unknown>> }> {
  return apiRequest(`/api/registry/aws-search?q=${encodeURIComponent(q)}`);
}
