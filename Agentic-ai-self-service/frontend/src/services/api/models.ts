/**
 * Models and Identity API domain module (Loom-study Phases 1.3 + 5.1).
 */

import { authFetch } from '../../auth/authFetch';
import { API_BASE_URL } from './client';

// ============================================================================
// Types
// ============================================================================

export interface LiveModelOption {
  provider: string;
  modelId: string;
  label: string;
  maxTokens: number;
  source?: string;
}

// Identity: token-info (Loom-study 1.3) — the caller's decoded claims/scopes
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

// ============================================================================
// Model Operations
// ============================================================================

/**
 * Fetch the live Bedrock model catalog (Loom-study 5.1). The model picker can
 * call this to reflect models actually available in the account instead of the
 * hardcoded list; callers should fall back to the static AVAILABLE_MODELS on
 * error so the picker is never empty.
 */
export async function listModels(baseUrl: string = API_BASE_URL): Promise<LiveModelOption[]> {
  const response = await authFetch(`${baseUrl}/api/models`, { method: 'GET' });
  if (!response.ok) {
    throw new Error(`Model catalog fetch failed (${response.status})`);
  }
  return (await response.json()) as LiveModelOption[];
}

// ============================================================================
// Identity Operations
// ============================================================================

/** Fetch the signed-in caller's decoded identity (claims + groups + scopes). */
export async function getTokenInfo(baseUrl: string = API_BASE_URL): Promise<TokenInfo> {
  const response = await authFetch(`${baseUrl}/api/identity/token-info`, { method: 'GET' });
  if (!response.ok) {
    throw new Error(`Token info fetch failed (${response.status})`);
  }
  return (await response.json()) as TokenInfo;
}
