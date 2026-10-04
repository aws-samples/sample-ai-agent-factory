/**
 * The client must not give up before the backend does.
 *
 * `testTool` polls until the test row stops saying "running". That budget was 40
 * attempts at 3s — two minutes — and it was set when the tool-test sandbox was an
 * ordinary Lambda that goes Active in about two seconds. F-11 put the sandbox in a
 * VPC with no route out, and a VPC-attached function first waits on AWS building a
 * Hyperplane ENI for its (subnet, security-group) pair. Measured live in the
 * acfe2e-p0920 sandbox VPC across three consecutive runs: 223.3s, 223.9s, then 6.1s
 * once the mapping was warm.
 *
 * So the browser gave up at 120s on runs the backend went on to finish. That is a
 * false failure of the worst kind: the user is told a correct tool is broken, and
 * the panel's auto-fix loop is then pointed at code that has nothing wrong with it.
 *
 * These tests pin the budget by *behaviour* — how long the client is actually
 * willing to wait — rather than by reading a constant, because the constant is
 * local to the module and the thing that matters is the wait.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { testTool } from './tools';

// The backend's own worst case, from tool_tester.ACTIVE_WAIT_SECONDS_VPC (300s)
// plus the test cases and cleanup that follow it.
const BACKEND_WORST_CASE_SECONDS = 300;

let pollCount = 0;

/** Answer the POST once, then answer every poll with `status: running`. */
function alwaysRunning(finishOnPoll = Infinity) {
  pollCount = 0;
  return vi.fn(async (url: string) => {
    if (String(url).endsWith('/api/test-tool')) {
      return { ok: true, json: async () => ({ testId: 'test-abc' }) } as Response;
    }
    pollCount += 1;
    if (pollCount >= finishOnPoll) {
      return {
        ok: true,
        json: async () => ({
          status: 'completed',
          success: true,
          allPassed: true,
          results: [{ testCaseName: 'x', passed: true, durationMs: 4 }],
          sandboxIsolated: true,
        }),
      } as Response;
    }
    return { ok: true, json: async () => ({ status: 'running' }) } as Response;
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('fetch', alwaysRunning());
  // authFetch attaches a bearer token; no session is needed for these.
  vi.stubGlobal('localStorage', {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('testTool poll budget', () => {
  it('waits longer than the backend can take to provision an isolated sandbox', async () => {
    let settled = false;
    const promise = testTool({ lambdaCode: 'x', testCases: [] } as never).then((r) => {
      settled = true;
      return r;
    });

    // Advance past the backend's worst case. The client must still be polling: if
    // it has already returned, it returned a failure for a test that is fine.
    await vi.advanceTimersByTimeAsync(BACKEND_WORST_CASE_SECONDS * 1000);
    expect(settled).toBe(false);
    // And it must have got there by polling, not by sleeping through it.
    expect(pollCount).toBeGreaterThan(40); // the old budget, which stopped here

    await vi.advanceTimersByTimeAsync(10 * 60 * 1000);
    await promise;
  });

  it('does give up eventually rather than polling forever', async () => {
    // The opposite failure: a client that never stops leaves a spinner on screen
    // for a test that died, with no way for the user to tell.
    const promise = testTool({ lambdaCode: 'x', testCases: [] } as never);
    await vi.advanceTimersByTimeAsync(60 * 60 * 1000);
    const result = await promise;

    expect(result.success).toBe(false);
    expect(result.allPassed).toBe(false);
    expect(result.error).toBeTruthy();
  });

  it('says what the wait was for, not just that it timed out', async () => {
    // "Test timed out after 2 minutes" told the user nothing they could act on.
    const promise = testTool({ lambdaCode: 'x', testCases: [] } as never);
    await vi.advanceTimersByTimeAsync(60 * 60 * 1000);
    const result = await promise;

    expect(result.error).toMatch(/isolated sandbox|network/i);
    expect(result.error).toMatch(/retry/i);
  });

  it('still returns as soon as the test completes', async () => {
    // The happy path, and the reason the budget being large is not itself a cost:
    // a warm sandbox answers on the first poll and the client returns immediately.
    vi.stubGlobal('fetch', alwaysRunning(1));
    const promise = testTool({ lambdaCode: 'x', testCases: [] } as never);
    await vi.advanceTimersByTimeAsync(4000);
    const result = await promise;

    expect(result.allPassed).toBe(true);
    expect(pollCount).toBe(1);
  });
});
