import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  AgentCoreChatElement,
  AgentCoreClient,
  AgentCoreClientError,
} from '../../public/agentcore-chat-v1.js';

const TOKEN = 'header.payload.signature';

function response(body, { status = 200, headers = {} } = {}) {
  const normalized = new Headers(headers);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: normalized,
    text: async () => JSON.stringify(body),
  };
}

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe('AgentCoreClient v1', () => {
  it('invokes only the stable production alias with a request-local bearer', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(response({
      success: true,
      response: 'hello',
      sessionId: 'session-1',
      runtimeName: 'orders_agent',
      versionId: 'version-1',
      deploymentId: 'deployment-1',
    }));
    const tokenProvider = vi.fn().mockResolvedValue(TOKEN);
    const client = new AgentCoreClient({
      baseUrl: 'https://chat.example.com',
      tokenProvider,
      fetchImpl,
    });

    const result = await client.invoke({
      runtimeName: 'orders_agent',
      input: 'Where is order 1?',
      sessionId: 'session-1',
      history: [{ role: 'assistant', content: 'How can I help?' }],
    });

    expect(result.response).toBe('hello');
    expect(tokenProvider).toHaveBeenCalledOnce();
    expect(fetchImpl).toHaveBeenCalledOnce();
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe('https://chat.example.com/api/runtimes/orders_agent/invoke');
    expect(init.credentials).toBe('omit');
    expect(init.redirect).toBe('error');
    expect(init.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    expect(init.body).not.toContain(TOKEN);
    expect(JSON.parse(init.body)).toEqual({
      input: 'Where is order 1?',
      sessionId: 'session-1',
      history: [{ role: 'assistant', content: 'How can I help?' }],
    });
  });

  it('rejects target injection before token or network access', async () => {
    const fetchImpl = vi.fn();
    const tokenProvider = vi.fn();
    const client = new AgentCoreClient({
      baseUrl: 'https://chat.example.com',
      tokenProvider,
      fetchImpl,
    });

    await expect(client.invoke({
      runtimeName: '../staging/runtime',
      input: 'hello',
    })).rejects.toThrow(/runtimeName/);
    expect(tokenProvider).not.toHaveBeenCalled();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('surfaces exact authorization exhaustion and never retries a POST', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(response(
      { detail: 'Invocation authorization exhausted' },
      { status: 429, headers: { 'Retry-After': '3600' } },
    ));
    const client = new AgentCoreClient({
      baseUrl: 'https://chat.example.com',
      tokenProvider: async () => TOKEN,
      fetchImpl,
    });

    const error = await client.invoke({ runtimeName: 'orders_agent', input: 'hello' })
      .then(() => null, (caught) => caught);
    expect(error).toBeInstanceOf(AgentCoreClientError);
    expect(error.status).toBe(429);
    expect(error.code).toBe('authorization_exhausted');
    expect(error.retryAfter).toBe(3600);
    expect(fetchImpl).toHaveBeenCalledOnce();
  });

  it('does not persist or accept tokens through URL or constructor strings', () => {
    expect(() => new AgentCoreClient({
      baseUrl: `https://user:${TOKEN}@chat.example.com`,
      tokenProvider: async () => TOKEN,
    })).toThrow(/credentials/);
    expect(() => new AgentCoreClient({
      baseUrl: 'https://chat.example.com',
      tokenProvider: TOKEN,
    })).toThrow(/tokenProvider/);
  });
});

describe('<agentcore-chat>', () => {
  it('renders model output as text and dispatches a credential-free result event', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(response({
      success: true,
      response: '<img src=x onerror=alert(1)>',
      sessionId: 'session-2',
      runtimeName: 'orders_agent',
      versionId: 'version-2',
      deploymentId: 'deployment-2',
    }));
    const element = document.createElement('agentcore-chat');
    expect(element).toBeInstanceOf(AgentCoreChatElement);
    element.setAttribute('api-base-url', 'https://chat.example.com');
    element.setAttribute('runtime-name', 'orders_agent');
    element.tokenProvider = async () => TOKEN;
    element.fetchImpl = fetchImpl;
    const emitted = vi.fn();
    element.addEventListener('agentcore-response', emitted);
    document.body.append(element);

    await element.sendMessage('hello');

    const log = element.shadowRoot.querySelector('[role="log"]');
    expect(log.textContent).toContain('<img src=x onerror=alert(1)>');
    expect(log.querySelector('img')).toBeNull();
    expect(emitted).toHaveBeenCalledOnce();
    expect(JSON.stringify(emitted.mock.calls[0][0].detail)).not.toContain(TOKEN);
  });

  it('does not treat an access-token attribute as authentication', async () => {
    const fetchImpl = vi.fn();
    const element = document.createElement('agentcore-chat');
    element.setAttribute('api-base-url', 'https://chat.example.com');
    element.setAttribute('runtime-name', 'orders_agent');
    element.setAttribute('access-token', TOKEN);
    element.fetchImpl = fetchImpl;
    document.body.append(element);

    await expect(element.sendMessage('hello')).rejects.toMatchObject({
      code: 'network_error',
    });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
