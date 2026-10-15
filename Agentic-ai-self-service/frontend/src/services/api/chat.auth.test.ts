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
  it('throws a status-carrying 401 and does not create or poll a second turn', async () => {
    const calls: string[] = [];
    globals.__authFetch = vi.fn(async (url: unknown) => {
      calls.push(String(url));
      return json(401, AUTHORIZER_401);
    });
    const err = await streamInvoke({ runtimeId: 'rt-1', input: 'hi' }, () => {}).catch((e) => e);
    expect((err as { status: number }).status).toBe(401);
    expect(getErrorMessage(err)).toBe(SESSION_EXPIRED_MESSAGE);
    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain('/api/test-runtime-async');
  });

  it('treats a start-time 403 the same way', async () => {
    globals.__authFetch = vi.fn(async () => json(403, { detail: 'Missing required scope' }));
    const err = await streamInvoke({ runtimeId: 'rt-1', input: 'hi' }, () => {}).catch((e) => e);
    expect((err as { status: number }).status).toBe(403);
    expect(getErrorMessage(err)).toBe('Missing required scope');
  });

  it('surfaces a status-carrying poll failure and attempts scratch cleanup', async () => {
    const methods: string[] = [];
    globals.__authFetch = vi.fn(async (...args: unknown[]) => {
      const init = args[1] as RequestInit | undefined;
      const method = init?.method || 'GET';
      methods.push(method);
      if (method === 'POST') return json(202, { testId: 'runtime-test-abc123def456', status: 'running' });
      if (method === 'DELETE') return json(200, { deleted: true });
      return json(500, { detail: 'runtime exploded' });
    });
    const err = await streamInvoke({ runtimeId: 'rt-1', input: 'hi' }, () => {}).catch((e) => e);
    expect(isApiError(err)).toBe(true);
    expect((err as { status: number }).status).toBe(500);
    expect(getErrorMessage(err)).toBe('runtime exploded');
    expect(methods).toEqual(['POST', 'GET', 'DELETE']);
  });

  it('still reports a completed body-level failure and cleans its row', async () => {
    const methods: string[] = [];
    globals.__authFetch = vi.fn(async (...args: unknown[]) => {
      const init = args[1] as RequestInit | undefined;
      const method = init?.method || 'GET';
      methods.push(method);
      if (method === 'POST') return json(202, { testId: 'runtime-test-abc123def456', status: 'running' });
      if (method === 'DELETE') return json(200, { deleted: true });
      return json(200, { status: 'completed', success: false, error: 'agent refused' });
    });
    await expect(streamInvoke({ runtimeId: 'rt-1', input: 'hi' }, () => {})).rejects.toThrow('agent refused');
    expect(methods).toEqual(['POST', 'GET', 'DELETE']);
  });
});
