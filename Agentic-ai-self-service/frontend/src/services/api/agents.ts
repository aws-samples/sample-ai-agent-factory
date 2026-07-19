/**
 * AI Agent (Canvas) Generator API domain module (Phase 1 Gap 1E).
 */

import { authFetch } from '../../auth/authFetch';
import { API_BASE_URL } from './client';

// ============================================================================
// Types
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

// ============================================================================
// Agent Generator Operations
// ============================================================================

/**
 * Generate an AgentCore canvas spec from a natural language description.
 * Mirrors generateTool: first call returns a clarification message;
 * subsequent calls (history populated) return a {nodes, edges} spec.
 */
export async function generateCanvas(
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
