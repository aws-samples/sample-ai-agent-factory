import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  callMcpTool,
  discoverMcpTools,
} from '../../services/api/runtimeMcp';
import { McpToolsPanel } from './McpToolsPanel';

vi.mock('../../services/api/runtimeMcp', () => ({
  discoverMcpTools: vi.fn(),
  callMcpTool: vi.fn(),
}));

const mockDiscover = vi.mocked(discoverMcpTools);
const mockCall = vi.mocked(callMcpTool);

const catalog = {
  protocolVersion: '2025-11-25',
  sessionId: 'server-session-1',
  serverInfo: { name: 'standalone-mcp', version: '1.0' },
  tools: [
    {
      name: 'get_weather',
      description: 'Read current weather',
      inputSchema: {
        type: 'object',
        properties: { city: { type: 'string' } },
        required: ['city'],
      },
    },
    {
      name: 'search_web',
      description: 'Search the web',
      inputSchema: {
        type: 'object',
        properties: { query: { type: 'string' } },
        required: ['query'],
      },
    },
  ],
};

describe('McpToolsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockDiscover.mockResolvedValue(catalog);
  });

  it('discovers schemas and calls a selected tool with the server session', async () => {
    mockCall.mockResolvedValue({
      protocolVersion: '2025-11-25',
      sessionId: 'server-session-2',
      content: [{ type: 'text', text: 'weather-canary:Dublin' }],
      structuredContent: { city: 'Dublin', source: 'controlled-test' },
      isError: false,
    });

    render(<McpToolsPanel deploymentId="deployment-1" />);

    const argumentsBox = await screen.findByLabelText('Arguments for get_weather');
    expect(screen.getByText('search_web')).toBeVisible();
    expect(argumentsBox).toHaveValue('{\n  "city": ""\n}');

    fireEvent.change(argumentsBox, {
      target: { value: '{"city":"Dublin"}' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Call get_weather' }));

    await waitFor(() => {
      expect(mockCall).toHaveBeenCalledWith(
        'deployment-1',
        'get_weather',
        { city: 'Dublin' },
        'server-session-1',
        expect.any(AbortSignal),
      );
    });
    expect(await screen.findByText(/controlled-test/)).toBeVisible();
    expect(mockDiscover).toHaveBeenCalledWith(
      'deployment-1',
      expect.any(AbortSignal),
    );
  });

  it('keeps malformed JSON in the browser and sends no tool call', async () => {
    render(<McpToolsPanel deploymentId="deployment-1" />);
    const argumentsBox = await screen.findByLabelText('Arguments for get_weather');
    fireEvent.change(argumentsBox, { target: { value: '{broken' } });
    fireEvent.click(screen.getByRole('button', { name: 'Call get_weather' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Arguments must be valid JSON.',
    );
    expect(mockCall).not.toHaveBeenCalled();
  });

  it('renders a retryable discovery error', async () => {
    mockDiscover.mockRejectedValueOnce({
      status: 503,
      message: 'The MCP runtime is temporarily unavailable.',
    });

    render(<McpToolsPanel deploymentId="deployment-1" />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The MCP runtime is temporarily unavailable.',
    );
    expect(screen.getByRole('button', { name: 'Retry discovery' })).toBeVisible();
  });
});
