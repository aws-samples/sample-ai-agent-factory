/**
 * AI Tool Generator API domain module.
 */

import { authFetch } from '../../auth/authFetch';
import { API_BASE_URL } from './client';

// ============================================================================
// Types
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
// Tool Generator Operations
// ============================================================================

/**
 * Generate a Lambda tool using AI from a natural language description.
 * Calls POST /api/generate-tool on the deployment API.
 *
 * - Clarification mode (no history): synchronous response
 * - Generation mode (has history): async — returns jobId, polls until complete
 */
export async function generateTool(
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

/**
 * Test a generated Lambda tool using async polling.
 * POST starts the test (returns testId), then polls GET until complete.
 * This avoids the API Gateway 30s timeout for long-running tests.
 */
export async function testTool(
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
