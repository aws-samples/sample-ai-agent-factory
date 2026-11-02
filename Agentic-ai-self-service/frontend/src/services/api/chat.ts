/**
 * End-user chat API domain module (Loom-study Phase 3).
 */

import { authFetch } from '../../auth/authFetch';
import { API_BASE_URL, apiErrorFromResponse, apiRequest } from './client';

// ============================================================================
// Types
// ============================================================================

export interface DeployedAgentSummary {
  deployment_id: string;
  runtime_id: string | null;
  runtime_name?: string | null;
  runtime_arn?: string | null;
  agentcore_runtime_name?: string | null;
  status: string;
  memory_result?: Record<string, unknown> | null;
}

// ============================================================================
// Chat Operations
// ============================================================================

/** List the caller's own succeeded deployments (the chat agent picker).
 *
 *  Goes through `apiRequest` rather than calling `authFetch` directly. It used to
 *  do the latter and `throw new Error("Agent list failed (" + status + ")")`,
 *  which is an error with no `status` field — so none of this module's
 *  status-aware handling applied to it, and an expired session showed the end
 *  user "Agent list failed (401)" in the chat sidebar. Measured live against the
 *  deployed app. There is nothing special about this endpoint that justified
 *  hand-rolling the fetch. */
export async function listMyAgents(baseUrl: string = API_BASE_URL): Promise<DeployedAgentSummary[]> {
  return apiRequest<DeployedAgentSummary[]>(
    '/api/deployments?status=succeeded',
    { method: 'GET' },
    baseUrl,
  );
}

/**
 * Invoke a deployed runtime without API Gateway's 30-second synchronous ceiling.
 * Runtime-id calls start one owner-bound asynchronous turn, poll its result, and
 * clean up the scratch row; they never replay a timed-out model/tool turn.
 * Production-slot calls retain their immutable slot invocation contract.
 */
export async function streamInvoke(
  params: { runtimeId?: string; runtimeName?: string; input: string; sessionId?: string | null },
  onToken: (t: string) => void,
  baseUrl: string = API_BASE_URL,
): Promise<{ sessionId: string | null; fullText: string }> {
  if (params.runtimeName) {
    // The production slot resolves slot -> immutable version -> deployment ->
    // exact Runtime ARN server-side.  No account, role, ARN, version, or
    // qualifier is accepted from the browser.
    const slotResponse = await authFetch(
      `${baseUrl}/api/runtimes/${encodeURIComponent(params.runtimeName)}/slots/production/invoke`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          input: params.input,
          ...(params.sessionId ? { sessionId: params.sessionId } : {}),
        }),
      },
    );
    if (!slotResponse.ok) throw await apiErrorFromResponse(slotResponse);
    const slotResult = (await slotResponse.json()) as {
      success?: boolean;
      response?: string;
      error?: string;
      sessionId?: string | null;
    };
    if (!slotResult.success) throw new Error(slotResult.error || 'Invocation failed');
    const text = slotResult.response || '';
    if (text) onToken(text);
    return { sessionId: slotResult.sessionId || params.sessionId || null, fullText: text };
  }
  if (!params.runtimeId) throw new Error('A runtime name or runtime id is required');
  const body = JSON.stringify({
    runtimeId: params.runtimeId,
    input: params.input,
    ...(params.sessionId ? { sessionId: params.sessionId } : {}),
  });
  const started = await authFetch(`${baseUrl}/api/test-runtime-async`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
  });
  if (!started.ok) throw await apiErrorFromResponse(started);
  const startBody = (await started.json()) as { testId?: string; status?: string };
  const testId = startBody.testId;
  if (!testId || startBody.status !== 'running') throw new Error('Async invocation did not start');

  const pollUrl = `${baseUrl}/api/test-runtime-async/${encodeURIComponent(testId)}`;
  const deadline = Date.now() + 9 * 60 * 1000;
  try {
    while (Date.now() < deadline) {
      const polled = await authFetch(pollUrl, { method: 'GET' });
      if (!polled.ok) throw await apiErrorFromResponse(polled);
      const result = (await polled.json()) as {
        status?: string;
        success?: boolean;
        response?: string;
        error?: string;
        sessionId?: string | null;
      };
      if (result.status === 'running') {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        continue;
      }
      if (result.status !== 'completed') throw new Error('Async invocation returned an invalid state');
      if (!result.success) throw new Error(result.error || 'Invocation failed');
      const text = result.response || '';
      if (text) onToken(text);
      return { sessionId: result.sessionId || params.sessionId || null, fullText: text };
    }
    throw new Error('Invocation exceeded the 9 minute async limit');
  } finally {
    // Best-effort immediate scratch-row cleanup. The server also writes a bounded
    // TTL, so a lost cleanup response cannot make customer content permanent.
    await authFetch(pollUrl, { method: 'DELETE' }).catch(() => undefined);
  }
}
