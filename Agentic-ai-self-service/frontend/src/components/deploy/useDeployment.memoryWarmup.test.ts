/**
 * A deployment warmup must not manufacture a persisted Memory conversation.
 *
 * Both generated Memory agents persist every real invocation, so the automatic
 * post-deploy "ping" used to be skipped for Memory runtimes. The ping now carries
 * an explicit `warmup: true` marker (DeployPanel.warmupRuntime) on which both
 * generated Memory entrypoints return before Memory and the model, so every
 * runtime is warmed exactly once. The marker and the zero-write behaviour are
 * pinned by DeployPanel.warmup.test.tsx and backend
 * tests/test_the_warmup_is_not_a_memory_turn.py.
 */

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
  name: 'warmup-contract',
  entrypoint: 'agent.py',
  framework: 'strands_agents',
  model: {
    provider: 'bedrock',
    modelId: 'us.anthropic.claude-sonnet-5',
    temperature: 0.7,
    topP: 0.9,
  },
  systemPrompt: 'test',
  deploymentType: 'direct_code_deploy',
  pythonRuntime: 'PYTHON_3_12',
  protocol: 'HTTP',
  idleTimeout: 900,
  maxLifetime: 28800,
  enableOtel: false,
  modelProvider: 'bedrock',
  multiAgentPattern: 'none',
};

function params(memory: boolean) {
  return {
    config,
    nodeId: 'runtime-node',
    flowId: 'saved-flow',
    deploymentMode: 'runtime' as const,
    connectedTools: memory ? ['memory'] : [],
    gatewayConfig: null,
    externalMcpServers: undefined,
    gatewayTools: [],
    templateId: null,
    identityConfig: null,
    customTools: [],
    connectors: [],
    memoryConfig: memory ? { enabled: true } : null,
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

async function finishAsyncDeployment(memory: boolean) {
  const deploymentId = '5bb2084b-d586-46d6-a5f3-494cd24cfc89';
  const runtimeId = 'warmup_contract_AbCdEf1234';
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
          runtime_id: runtimeId,
          runtime_endpoint: 'https://runtime.example.test',
          status: 'succeeded',
        }),
      };
    }
    throw new Error(`Unexpected request: ${url}`);
  });

  const hookParams = params(memory);
  const { result } = renderHook(() => useDeployment(hookParams));
  let deploy: Promise<void> | undefined;
  act(() => {
    deploy = result.current.handleDeploy();
  });
  await act(async () => {
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(5_000);
    await deploy;
  });
  return hookParams;
}

describe('post-deploy warmup and Memory', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('warms a Memory runtime once after an immediate deployment result', async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        success: true,
        runtimeId: 'memory_runtime_AbCdEf1234',
        endpoint: 'https://runtime.example.test',
      }),
    });
    const hookParams = params(true);
    const { result } = renderHook(() => useDeployment(hookParams));

    await act(async () => {
      await result.current.handleDeploy();
    });

    expect(hookParams.warmupRuntime).toHaveBeenCalledOnce();
    expect(hookParams.warmupRuntime).toHaveBeenCalledWith(
      'memory_runtime_AbCdEf1234',
      'https://runtime.example.test',
    );
  });

  it('warms a Memory runtime once after the production poll succeeds', async () => {
    const hookParams = await finishAsyncDeployment(true);

    expect(hookParams.warmupRuntime).toHaveBeenCalledOnce();
    expect(hookParams.warmupRuntime).toHaveBeenCalledWith(
      'warmup_contract_AbCdEf1234',
      'https://runtime.example.test',
    );
  });

  it('preserves production warmup for a non-Memory runtime', async () => {
    const hookParams = await finishAsyncDeployment(false);

    expect(hookParams.warmupRuntime).toHaveBeenCalledOnce();
    expect(hookParams.warmupRuntime).toHaveBeenCalledWith(
      'warmup_contract_AbCdEf1234',
      'https://runtime.example.test',
    );
  });
});
