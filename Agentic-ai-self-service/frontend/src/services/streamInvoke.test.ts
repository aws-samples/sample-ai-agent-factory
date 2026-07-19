import { describe, it, expect, vi, afterEach } from 'vitest';
import { streamInvokeApi } from './api';

type AuthFetchStub = (...args: unknown[]) => Promise<Response>;
const globals = globalThis as typeof globalThis & { __authFetch?: AuthFetchStub };

// authFetch is what streamInvokeApi calls; mock it to return a scripted SSE body
// or a JSON fallback response.
vi.mock('../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => globals.__authFetch!(...args),
}));

function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  let i = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) controller.enqueue(encoder.encode(chunks[i++]));
      else controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } });
}

describe('streamInvokeApi', () => {
  afterEach(() => { delete globals.__authFetch; });

  it('accumulates tokens and captures the session id from the done event', async () => {
    globals.__authFetch = vi.fn(async () =>
      sseResponse([
        'data: {"type":"token","token":"Hello"}\n\n',
        'data: {"type":"token","token":" world"}\n\n',
        'data: {"type":"done","session_id":"sess-1","full_response":"Hello world"}\n\n',
      ]),
    );
    const tokens: string[] = [];
    const out = await streamInvokeApi(
      { runtimeId: 'rt-1', input: 'hi' }, (t) => tokens.push(t),
    );
    expect(tokens).toEqual(['Hello', ' world']);
    expect(out.fullText).toBe('Hello world');
    expect(out.sessionId).toBe('sess-1');
  });

  it('falls back to /api/test-runtime when the response is not an SSE stream', async () => {
    const calls: string[] = [];
    globals.__authFetch = vi.fn(async (url) => {
      calls.push(url);
      if (url.includes('test-runtime-stream')) {
        // Not an event-stream → triggers fallback.
        return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
      }
      return new Response(JSON.stringify({ success: true, response: 'fallback answer', sessionId: 'sess-2' }),
        { status: 200, headers: { 'content-type': 'application/json' } });
    });
    const tokens: string[] = [];
    const out = await streamInvokeApi({ runtimeId: 'rt-1', input: 'hi' }, (t) => tokens.push(t));
    expect(calls.some((u) => u.includes('test-runtime-stream'))).toBe(true);
    expect(calls.some((u) => u.endsWith('/api/test-runtime'))).toBe(true);
    expect(out.fullText).toBe('fallback answer');
    expect(out.sessionId).toBe('sess-2');
  });

  it('throws on a stream error event', async () => {
    globals.__authFetch = vi.fn(async () =>
      sseResponse(['data: {"type":"error","error":"boom"}\n\n']),
    );
    await expect(
      streamInvokeApi({ runtimeId: 'rt-1', input: 'hi' }, () => {}),
    ).rejects.toThrow('boom');
  });
});
