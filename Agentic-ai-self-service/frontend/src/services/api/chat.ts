/**
 * End-user chat API domain module (Loom-study Phase 3).
 */

import { authFetch } from '../../auth/authFetch';
import { API_BASE_URL } from './client';

// ============================================================================
// Types
// ============================================================================

export interface DeployedAgentSummary {
  deployment_id: string;
  runtime_id: string | null;
  runtime_arn?: string | null;
  agentcore_runtime_name?: string | null;
  status: string;
  memory_result?: Record<string, unknown> | null;
}

// ============================================================================
// Chat Operations
// ============================================================================

/** List the caller's own succeeded deployments (the chat agent picker). */
export async function listMyAgents(baseUrl: string = API_BASE_URL): Promise<DeployedAgentSummary[]> {
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
export async function streamInvoke(
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
