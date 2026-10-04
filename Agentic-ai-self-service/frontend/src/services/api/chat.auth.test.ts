/**
 * The chat API must not bypass error normalization.
 *
 * F-36 fixed `getErrorMessage` so a 401 reads "Your session has expired …". Driving
 * the *deployed* app with Playwright afterwards showed the chat sidebar still
 * saying **"Agent list failed (401)"**, because `listMyAgents` called `authFetch`
 * directly and threw `new Error(\`Agent list failed (${status})\`)`. An `Error` has
 * no `status`, so `getErrorMessage` fell through to `error.message` and
 * `isNotReadyError` read 0. The client-level fix was correct and simply did not
 * apply here.
 *
 * That is the failure mode these tests exist to pin: not "is the message right"
 * (client.test.ts covers that) but "does every throw carry a status at all". A
 * unit test of the helper cannot see a caller that never calls it, which is why
 * this was found live and not by the 326 tests that were already green.
 *
 * The 401 bodies used below are the two real ones: `{"message":"Unauthorized"}`
 * from API Gateway's JWT authorizer (observed live) and the backend's single 401
 * site, `{"detail":"Caller identity not available"}` in `services/auth.py`.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { SESSION_EXPIRED_MESSAGE, getErrorMessage, isApiError, isNotReadyError } from './client';
import { listMyAgents, streamInvoke } from './chat';

type AuthFetchStub = (...args: unknown[]) => Promise<Response>;
const globals = globalThis as typeof globalThis & { __authFetch?: AuthFetchStub };

vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => globals.__authFetch!(...args),
}));

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const AUTHORIZER_401 = { message: 'Unauthorized' };
const BACKEND_401 = { detail: 'Caller identity not available' };

afterEach(() => {
  delete globals.__authFetch;
});

describe('listMyAgents', () => {
  it('throws a status-carrying error for the authorizer 401, not a bare Error', async () => {
    globals.__authFetch = vi.fn(async () => json(401, AUTHORIZER_401));
    const err = await listMyAgents().catch((e) => e);
    // The regression: `isApiError` false here is what broke every downstream
    // status check, and it is invisible to a test that only asserts the message.
    expect(isApiError(err)).toBe(true);
    expect((err as { status: number }).status).toBe(401);
    expect(getErrorMessage(err)).toBe(SESSION_EXPIRED_MESSAGE);
  });

  it('does the same for the backend 401 site', async () => {
    globals.__authFetch = vi.fn(async () => json(401, BACKEND_401));
    const err = await listMyAgents().catch((e) => e);
    expect((err as { status: number }).status).toBe(401);
    expect(getErrorMessage(err)).toBe(SESSION_EXPIRED_MESSAGE);
  });

  it('never renders the old developer-facing string', async () => {
    globals.__authFetch = vi.fn(async () => json(401, AUTHORIZER_401));
    const err = await listMyAgents().catch((e) => e);
    // Asserting the absence of the exact text a real user was shown.
    expect(getErrorMessage(err)).not.toMatch(/Agent list failed/);
    expect(getErrorMessage(err)).not.toMatch(/\(401\)/);
  });

  it('keeps a 404 classifiable as an empty state', async () => {
    // The point of carrying the status is that the *other* statuses keep working
    // too. If this regressed to a bare Error, `isNotReadyError` would return
    // false and a genuinely empty account would render as a red error.
    globals.__authFetch = vi.fn(async () => json(404, { detail: 'nothing here' }));
    const err = await listMyAgents().catch((e) => e);
    expect(isNotReadyError(err)).toBe(true);
    expect(getErrorMessage(err)).toBe('nothing here');
  });

  it('still returns the agent list on success', async () => {
    // The happy path has to be asserted or the whole suite above is satisfied by
    // a function that throws unconditionally.
    const list = [{ deployment_id: 'd-1', runtime_id: 'rt-1', status: 'succeeded' }];
    globals.__authFetch = vi.fn(async () => json(200, list));
    await expect(listMyAgents()).resolves.toEqual(list);
  });
});

describe('streamInvoke', () => {
  it('throws a status-carrying 401 and does NOT retry the non-streaming path', async () => {
    const calls: string[] = [];
    globals.__authFetch = vi.fn(async (url: unknown) => {
      calls.push(String(url));
      return json(401, AUTHORIZER_401);
    });
    const err = await streamInvoke({ runtimeId: 'rt-1', input: 'hi' }, () => {}).catch((e) => e);
    expect((err as { status: number }).status).toBe(401);
    expect(getErrorMessage(err)).toBe(SESSION_EXPIRED_MESSAGE);
    // One request, not two. An auth failure is not "SSE unavailable", and the
    // second call would have been guaranteed to fail the same way while
    // discarding the status into "Invocation failed".
    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain('/api/test-runtime-stream');
  });

  it('treats a 403 the same way', async () => {
    globals.__authFetch = vi.fn(async () => json(403, { detail: 'Missing required scope' }));
    const err = await streamInvoke({ runtimeId: 'rt-1', input: 'hi' }, () => {}).catch((e) => e);
    expect((err as { status: number }).status).toBe(403);
    expect(getErrorMessage(err)).toBe('Missing required scope');
  });

  it('surfaces the status when the fallback invoke fails at the HTTP level', async () => {
    // `r2.ok` was never checked, so a 500 became "Invocation failed" with the
    // status thrown away. The stream call returns a non-SSE 200 to reach the
    // fallback at all.
    let n = 0;
    globals.__authFetch = vi.fn(async () => {
      n += 1;
      return n === 1 ? json(200, { not: 'sse' }) : json(500, { detail: 'runtime exploded' });
    });
    const err = await streamInvoke({ runtimeId: 'rt-1', input: 'hi' }, () => {}).catch((e) => e);
    expect(isApiError(err)).toBe(true);
    expect((err as { status: number }).status).toBe(500);
    expect(getErrorMessage(err)).toBe('runtime exploded');
  });

  it('still reports a body-level failure from a 200 fallback', async () => {
    // The pre-existing `data.success` check must survive: a 200 carrying
    // `{success: false}` is an application failure, not an HTTP one.
    let n = 0;
    globals.__authFetch = vi.fn(async () => {
      n += 1;
      return n === 1 ? json(200, { not: 'sse' }) : json(200, { success: false, error: 'agent refused' });
    });
    await expect(streamInvoke({ runtimeId: 'rt-1', input: 'hi' }, () => {})).rejects.toThrow('agent refused');
  });
});
