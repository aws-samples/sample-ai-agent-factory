/**
 * Prompt Library API domain module (Phase 3 Gap 3H).
 */

import { apiRequest } from './client';

// ============================================================================
// Types
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

// ============================================================================
// Prompt Operations
// ============================================================================

/** List/search library prompts visible to the caller. */
export async function listPrompts(
  opts: { q?: string; tag?: string; scope?: 'all' | 'mine' } = {}
): Promise<PromptEntry[]> {
  const params = new URLSearchParams();
  if (opts.q) params.set('q', opts.q);
  if (opts.tag) params.set('tag', opts.tag);
  if (opts.scope) params.set('scope', opts.scope);
  const qs = params.toString();
  return apiRequest<PromptEntry[]>(
    `/api/prompts${qs ? `?${qs}` : ''}`
  );
}

/** Create a library prompt (seeds an initial version from `body`). */
export async function createPrompt(data: CreatePromptRequest): Promise<PromptEntry> {
  return apiRequest<PromptEntry>(`/api/prompts`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

/** Fetch a single prompt (visibility-checked). */
export async function getPrompt(name: string): Promise<PromptEntry> {
  return apiRequest<PromptEntry>(
    `/api/prompts/${encodeURIComponent(name)}`
  );
}

/** Update prompt metadata (owner only). */
export async function updatePrompt(
  name: string,
  data: Partial<Pick<CreatePromptRequest, 'display_name' | 'description' | 'tags'>>
): Promise<PromptEntry> {
  return apiRequest<PromptEntry>(
    `/api/prompts/${encodeURIComponent(name)}`,
    {
      method: 'PUT',
      body: JSON.stringify(data),
    }
  );
}

/** Delete a prompt (owner only). */
export async function deletePrompt(name: string): Promise<void> {
  return apiRequest<void>(
    `/api/prompts/${encodeURIComponent(name)}`,
    { method: 'DELETE' }
  );
}

/** Append a new version to a prompt (owner only). Returns the new version id. */
export async function addPromptVersion(
  name: string,
  data: AddPromptVersionRequest
): Promise<{ prompt_name: string; version_id: string; default_version_id?: string | null }> {
  return apiRequest(
    `/api/prompts/${encodeURIComponent(name)}/versions`,
    {
      method: 'POST',
      body: JSON.stringify(data),
    }
  );
}

/** Set the default version of a prompt (owner only). */
export async function promotePromptVersion(
  name: string,
  versionId: string
): Promise<{ success: boolean; prompt_name: string; default_version_id: string }> {
  return apiRequest(
    `/api/prompts/${encodeURIComponent(name)}/promote/${encodeURIComponent(versionId)}`,
    { method: 'POST' }
  );
}

/** Resolve a prompt body (visibility-checked; default or explicit version). */
export async function resolvePrompt(
  name: string,
  version?: string
): Promise<ResolvePromptResponse> {
  const params = new URLSearchParams();
  if (version) params.set('version', version);
  const qs = params.toString();
  return apiRequest<ResolvePromptResponse>(
    `/api/prompts/${encodeURIComponent(name)}/resolve${qs ? `?${qs}` : ''}`
  );
}
