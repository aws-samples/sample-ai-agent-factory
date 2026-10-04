import { beforeEach, describe, expect, it, vi } from 'vitest';
import { authFetch } from '../../auth/authFetch';
import { callMcpTool, discoverMcpTools } from './runtimeMcp';

vi.mock('../../auth/authFetch', () => ({
  authFetch: vi.fn(),
}));

const mockAuthFetch = vi.mocked(authFetch);

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

describe('runtime MCP API client', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('discovers tools using only the product deployment id', async () => {
    mockAuthFetch.mockResolvedValueOnce(jsonResponse({
      protocolVersion: '2025-11-25',
      sessionId: 'session-1',
      serverInfo: { name: 'server', version: '1.0' },
      tools: [],
    }));

    await discoverMcpTools('deployment-1');

    expect(mockAuthFetch).toHaveBeenCalledOnce();
    const [url, init] = mockAuthFetch.mock.calls[0];
    expect(url).toBe('/api/test-mcp-runtime/tools');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({
      deploymentId: 'deployment-1',
    });
  });

  it('calls a selected tool without accepting an ARN, region, or method', async () => {
    mockAuthFetch.mockResolvedValueOnce(jsonResponse({
      protocolVersion: '2025-11-25',
      sessionId: 'session-1',
      content: [{ type: 'text', text: 'weather-canary' }],
      isError: false,
    }));

    await callMcpTool(
      'deployment-1',
      'get_weather',
      { city: 'Dublin' },
      'session-1',
    );

    const [url, init] = mockAuthFetch.mock.calls[0];
    expect(url).toBe('/api/test-mcp-runtime/call');
    expect(JSON.parse(String(init?.body))).toEqual({
      deploymentId: 'deployment-1',
      toolName: 'get_weather',
      arguments: { city: 'Dublin' },
      sessionId: 'session-1',
    });
  });
});
