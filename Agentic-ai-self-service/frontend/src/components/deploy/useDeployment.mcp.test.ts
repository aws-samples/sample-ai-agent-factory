import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RuntimeConfiguration } from '../../types/components';
import { useDeployment } from './useDeployment';

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

vi.mock('../../store/workflowStore', () => ({
  useWorkflowStore: () => ({
    setNodeExecutionStateByType: vi.fn(),
    resetAllExecutionStates: vi.fn(),
  }),
}));

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
  systemPrompt: 'No language model is instantiated.',
  deploymentType: 'direct_code_deploy',
  pythonRuntime: 'PYTHON_3_13',
  protocol: 'MCP',
  idleTimeout: 300,
  maxLifetime: 3600,
  enableOtel: false,
  modelProvider: 'bedrock',
  multiAgentPattern: 'none',
};

function params() {
  return {
    config,
    nodeId: 'runtime-node',
    flowId: 'saved-flow',
    deploymentMode: 'runtime' as const,
    connectedTools: [],
    gatewayConfig: null,
    externalMcpServers: undefined,
    gatewayTools: [],
    templateId: 'mcp-server-runtime',
    identityConfig: null,
    customTools: [],
    connectors: [],
    memoryConfig: null,
    evaluationConfig: null,
    policyConfig: null,
    guardrailsConfig: null,
    mcpServerConfig: null,
    knowledgeBaseConfig: null,
    observabilityConfig: null,
    a2aConfig: null,
    resourceTagState: {
      tags: {},
      profileName: null,
      profileUpdatedAt: null,
      explicitValues: {},
      policyRevision: '',
    },
    warmupRuntime: vi.fn(),
    onVersionsRefresh: vi.fn(),
    onTabChange: vi.fn(),
  };
}

describe('useDeployment standalone MCP protocol branch', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('opens MCP tools and sends no HTTP-agent warmup after synchronous success', async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        success: true,
        deploymentId: 'deployment-1',
        runtimeId: 'mcp-runtime-1',
        endpoint: 'arn:aws:bedrock-agentcore:eu-west-1:111111111111:runtime/mcp-runtime-1/runtime-endpoint/DEFAULT',
        runtimeProtocol: 'MCP',
      }),
    });
    const hookParams = params();
    const { result } = renderHook(() => useDeployment(hookParams));

    await act(async () => {
      await result.current.handleDeploy();
    });

    expect(result.current.deploymentStatus).toMatchObject({
      state: 'deployed',
      deploymentId: 'deployment-1',
      runtimeId: 'mcp-runtime-1',
      runtimeProtocol: 'MCP',
    });
    expect(hookParams.onTabChange).toHaveBeenCalledWith('tools');
    expect(hookParams.warmupRuntime).not.toHaveBeenCalled();
    const deployCall = mockAuthFetch.mock.calls.find(
      ([url]) => url === '/api/deploy',
    );
    expect(deployCall).toBeDefined();
    const body = JSON.parse(
      (deployCall?.[1] as RequestInit).body as string,
    );
    expect(body.config.protocol).toBe('MCP');
    for (const field of [
      'framework',
      'model',
      'modelProvider',
      'providerApiKeyRef',
      'providerBaseUrl',
      'systemPrompt',
      'multiAgentPattern',
      'multiAgentConfig',
    ]) {
      expect(body.config).not.toHaveProperty(field);
    }
  });

  it('uses persisted MCP protocol after asynchronous deployment completion', async () => {
    vi.useFakeTimers();
    const deploymentId = '5bb2084b-d586-46d6-a5f3-494cd24cfc89';
    mockAuthFetch.mockImplementation(async (url: string) => {
      if (url === '/api/deploy') {
        return {
          ok: true,
          json: async () => ({ deploymentId, status: 'pending' }),
        };
      }
      if (url === `/api/deploy/${deploymentId}`) {
        return {
          ok: true,
          json: async () => ({
            deployment_id: deploymentId,
            runtime_id: 'mcp-runtime-1',
            runtime_endpoint: 'runtime-endpoint',
            runtime_protocol: 'MCP',
            status: 'succeeded',
          }),
        };
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const hookParams = params();
    const { result } = renderHook(() => useDeployment(hookParams));
    let deployment: Promise<void> | undefined;

    act(() => {
      deployment = result.current.handleDeploy();
    });
    await act(async () => {
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(5_000);
      await deployment;
    });

    expect(result.current.deploymentStatus.runtimeProtocol).toBe('MCP');
    expect(hookParams.onTabChange).toHaveBeenCalledWith('tools');
    expect(hookParams.warmupRuntime).not.toHaveBeenCalled();
  });
});
