import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RuntimeConfiguration } from '../../types/components';
import { useDeployment } from './useDeployment';

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

const config: RuntimeConfiguration = {
  name: 'region-gate-runtime',
  entrypoint: 'agent.py',
  framework: 'strands_agents',
  model: { provider: 'bedrock', modelId: 'us.anthropic.claude-sonnet-5', temperature: 0.7, topP: 0.9 },
  systemPrompt: 'hi',
  deploymentType: 'direct_code_deploy',
  pythonRuntime: 'PYTHON_3_12',
  protocol: 'HTTP',
  idleTimeout: 900,
  maxLifetime: 28800,
  enableOtel: false,
  modelProvider: 'bedrock',
  multiAgentPattern: 'none',
};

function params(targetRegion: string | undefined) {
  return {
    config,
    nodeId: 'node-1',
    flowId: null,
    deploymentMode: 'runtime' as const,
    connectedTools: [],
    gatewayConfig: null,
    externalMcpServers: undefined,
    gatewayTools: [],
    templateId: null,
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
    targetAccountId: undefined,
    targetRegion,
    warmupRuntime: vi.fn(),
    onVersionsRefresh: vi.fn(),
    onTabChange: vi.fn(),
  };
}

describe('useDeployment release-Region gate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockAuthFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ success: true, runtimeId: 'runtime-1', endpoint: 'https://runtime.example.invalid' }),
    });
  });

  it('refuses an unselected target Region before any request leaves the browser', async () => {
    const { result } = renderHook(() => useDeployment(params('us-west-1')));
    await act(async () => {
      await result.current.handleDeploy();
    });
    expect(mockAuthFetch).not.toHaveBeenCalled();
    expect(result.current.deploymentStatus.state).toBe('error');
    expect(result.current.deploymentStatus.message).toContain(
      'Region us-west-1 is not a selected production release Region, so this live deployment is refused.',
    );
    expect(result.current.deploymentStatus.message).toContain('(us-west-2)');
  });

  it('still submits a selected target Region', async () => {
    const { result } = renderHook(() => useDeployment(params('eu-west-3')));
    await act(async () => {
      await result.current.handleDeploy();
    });
    const deployCall = mockAuthFetch.mock.calls.find(([url]) => url === '/api/deploy');
    expect(deployCall).toBeDefined();
    expect(JSON.parse((deployCall![1] as { body: string }).body)).toMatchObject({ targetRegion: 'eu-west-3' });
  });
});
