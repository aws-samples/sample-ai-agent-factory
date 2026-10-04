import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RuntimeConfiguration } from '../../types/components';
import { useWorkflowStore } from '../../store/workflowStore';
import { discoverMcpTools } from '../../services/api/runtimeMcp';
import { DeployPanel } from './DeployPanel';

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

vi.mock('../../services/api/runtimeMcp', () => ({
  discoverMcpTools: vi.fn(),
  callMcpTool: vi.fn(),
}));

const mockDiscover = vi.mocked(discoverMcpTools);
const MODEL_ONLY_FIELDS = [
  'framework',
  'model',
  'modelProvider',
  'providerApiKeyRef',
  'providerBaseUrl',
  'systemPrompt',
  'multiAgentPattern',
  'multiAgentConfig',
];

const config: RuntimeConfiguration = {
  name: 'standalone-mcp',
  entrypoint: 'agent.py',
  framework: 'strands_agents',
  model: {
    provider: 'bedrock',
    modelId: 'us.anthropic.claude-sonnet-5',
    temperature: 0.7,
    topP: 0.9,
  },
  systemPrompt: 'No model',
  deploymentType: 'direct_code_deploy',
  pythonRuntime: 'PYTHON_3_13',
  protocol: 'MCP',
  idleTimeout: 300,
  maxLifetime: 3600,
  enableOtel: false,
  modelProvider: 'bedrock',
  multiAgentPattern: 'none',
};

describe('DeployPanel standalone MCP experience', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useWorkflowStore.getState().resetWorkflowDocument(null);
    mockDiscover.mockResolvedValue({
      protocolVersion: '2025-11-25',
      serverInfo: { name: 'standalone-mcp', version: '1.0' },
      tools: [],
    });
    mockAuthFetch.mockImplementation(async (url: string) => {
      if (url === '/api/deploy') {
        return {
          ok: true,
          json: async () => ({
            success: true,
            deploymentId: 'deployment-1',
            runtimeId: 'runtime-1',
            endpoint: 'runtime-endpoint',
            runtimeProtocol: 'MCP',
          }),
        };
      }
      const empty = (
        url === '/api/settings/tags'
        || url === '/api/settings/tag-profiles'
      ) ? [] : {};
      return { ok: true, json: async () => empty };
    });
  });

  it('does not advertise HTTP-only chat or trigger surfaces', () => {
    render(
      <DeployPanel
        config={config}
        nodeId="runtime-node"
        templateId="mcp-server-runtime"
        isVisible
        onClose={() => {}}
      />,
    );

    expect(screen.queryByRole('tab', { name: /^Chat/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Triggers' })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'MCP Tools' })).toBeDisabled();
  });

  it('opens the MCP tools tab and never invokes an HTTP agent endpoint', async () => {
    render(
      <DeployPanel
        config={config}
        nodeId="runtime-node"
        templateId="mcp-server-runtime"
        isVisible
        onClose={() => {}}
      />,
    );
    const deploy = screen.getAllByRole('button', {
      name: /Deploy to AgentCore/i,
    })[0];
    await waitFor(() => expect(deploy).toBeEnabled());
    fireEvent.click(deploy);

    expect(await screen.findByRole('heading', { name: 'MCP Tools' })).toBeVisible();
    await waitFor(() => {
      expect(mockDiscover).toHaveBeenCalledWith(
        'deployment-1',
        expect.any(AbortSignal),
      );
    });
    expect(screen.queryByRole('tab', { name: /^Chat/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Triggers' })).not.toBeInTheDocument();
    expect(
      mockAuthFetch.mock.calls.some(([url]) => (
        url === '/api/test-runtime' || url === '/api/test-runtime-stream'
      )),
    ).toBe(false);
  });

  it('sends a model-free config to both export APIs', async () => {
    render(
      <DeployPanel
        config={config}
        nodeId="runtime-node"
        templateId="mcp-server-runtime"
        isVisible
        onClose={() => {}}
      />,
    );

    const cfn = screen.getByRole('button', {
      name: 'Download CloudFormation Template',
    });
    const python = screen.getByRole('button', { name: 'Export as Python' });
    await waitFor(() => {
      expect(cfn).toBeEnabled();
      expect(python).toBeEnabled();
    });

    fireEvent.click(cfn);
    await waitFor(() => {
      expect(
        mockAuthFetch.mock.calls.some(
          ([url]) => url === '/api/generate-cfn-template',
        ),
      ).toBe(true);
    });

    fireEvent.click(python);
    await waitFor(() => {
      expect(
        mockAuthFetch.mock.calls.some(([url]) => url === '/api/export-python'),
      ).toBe(true);
    });

    for (const endpoint of [
      '/api/generate-cfn-template',
      '/api/export-python',
    ]) {
      const call = mockAuthFetch.mock.calls.find(([url]) => url === endpoint);
      expect(call).toBeDefined();
      const body = JSON.parse((call?.[1] as RequestInit).body as string);
      expect(body.templateId).toBe('mcp-server-runtime');
      expect(body.config.protocol).toBe('MCP');
      for (const field of MODEL_ONLY_FIELDS) {
        expect(body.config).not.toHaveProperty(field);
      }
    }
  });

  it('restores an MCP deployment from its persisted protocol and deployment id', async () => {
    render(
      <DeployPanel
        config={config}
        nodeId="runtime-node"
        isVisible
        onClose={() => {}}
        restoredDeployment={{
          deploymentId: 'restored-deployment',
          runtimeId: 'restored-runtime',
          endpoint: 'restored-endpoint',
          runtimeProtocol: 'MCP',
        }}
      />,
    );

    expect(await screen.findByRole('heading', { name: 'MCP Tools' })).toBeVisible();
    await waitFor(() => {
      expect(mockDiscover).toHaveBeenCalledWith(
        'restored-deployment',
        expect.any(AbortSignal),
      );
    });
    expect(screen.queryByRole('tab', { name: /^Chat/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Triggers' })).not.toBeInTheDocument();
  });
});
