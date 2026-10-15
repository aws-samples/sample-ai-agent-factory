import { describe, it, expect, vi, afterEach } from 'vitest';
import { streamInvokeApi } from './api';

type AuthFetchStub = (...args: unknown[]) => Promise<Response>;
const globals = globalThis as typeof globalThis & { __authFetch?: AuthFetchStub };

// authFetch is what streamInvokeApi calls; mock the authenticated async
// start/poll/cleanup contract without persisting a token.
vi.mock('../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => globals.__authFetch!(...args),
}));

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

describe('streamInvokeApi', () => {
  afterEach(() => {
    delete globals.__authFetch;
    vi.useRealTimers();
  });

  it('starts one turn, polls it, emits the completed answer, and removes the scratch row', async () => {
    vi.useFakeTimers();
    const calls: Array<{ url: string; method: string }> = [];
    let polls = 0;
    globals.__authFetch = vi.fn(async (rawUrl, rawInit) => {
      const url = String(rawUrl);
      const method = String((rawInit as RequestInit | undefined)?.method || 'GET');
      calls.push({ url, method });
      if (url.endsWith('/api/test-runtime-async') && method === 'POST') {
        return json(202, { testId: 'runtime-test-abc123def456', status: 'running' });
      }
      if (method === 'DELETE') return json(200, { deleted: true });
      polls += 1;
      return polls === 1
        ? json(200, { testId: 'runtime-test-abc123def456', status: 'running' })
        : json(200, {
            testId: 'runtime-test-abc123def456',
            status: 'completed',
            success: true,
            response: 'managed search answer',
            sessionId: 'sess-1',
          });
    });
    const tokens: string[] = [];
    const pending = streamInvokeApi({ runtimeId: 'rt-1', input: 'search' }, (token) => tokens.push(token));
    await vi.runAllTimersAsync();
    await expect(pending).resolves.toEqual({ sessionId: 'sess-1', fullText: 'managed search answer' });
    expect(tokens).toEqual(['managed search answer']);
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1);
    expect(calls.filter((call) => call.method === 'GET')).toHaveLength(2);
    expect(calls.filter((call) => call.method === 'DELETE')).toHaveLength(1);
    expect(calls.some((call) => call.url.includes('test-runtime-stream'))).toBe(false);
    expect(calls.some((call) => call.url.endsWith('/api/test-runtime'))).toBe(false);
  });

  it('reports a completed application failure without replaying the turn', async () => {
    const calls: Array<{ url: string; method: string }> = [];
    globals.__authFetch = vi.fn(async (rawUrl, rawInit) => {
      const url = String(rawUrl);
      const method = String((rawInit as RequestInit | undefined)?.method || 'GET');
      calls.push({ url, method });
      if (url.endsWith('/api/test-runtime-async') && method === 'POST') {
        return json(202, { testId: 'runtime-test-abc123def456', status: 'running' });
      }
      if (method === 'DELETE') return json(200, { deleted: true });
      return json(200, {
        testId: 'runtime-test-abc123def456',
        status: 'completed',
        success: false,
        error: 'agent refused',
      });
    });
    await expect(streamInvokeApi({ runtimeId: 'rt-1', input: 'hi' }, () => {})).rejects.toThrow('agent refused');
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1);
    expect(calls.filter((call) => call.method === 'DELETE')).toHaveLength(1);
  });
});


describe('streamInvokeApi production slot', () => {
  afterEach(() => { delete globals.__authFetch; });

  it('invokes the immutable production slot without runtime target fields', async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    globals.__authFetch = vi.fn(async (url, init) => {
      calls.push({ url: String(url), init: init as RequestInit });
      return new Response(JSON.stringify({
        success: true,
        response: 'delegated answer',
        sessionId: 'slot-session',
        versionId: 'version-1',
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    });

    const tokens: string[] = [];
    const out = await streamInvokeApi(
      { runtimeName: 'orders_agent', input: 'who am I' },
      (token) => tokens.push(token),
    );

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain('/api/runtimes/orders_agent/slots/production/invoke');
    const body = JSON.parse(String(calls[0].init?.body));
    expect(body).toEqual({ input: 'who am I' });
    expect(body.runtimeId).toBeUndefined();
    expect(body.versionId).toBeUndefined();
    expect(body.arn).toBeUndefined();
    expect(out).toEqual({ sessionId: 'slot-session', fullText: 'delegated answer' });
    expect(tokens).toEqual(['delegated answer']);
  });
});
