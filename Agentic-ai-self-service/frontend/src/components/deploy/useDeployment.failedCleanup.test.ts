/**
 * F-56: a failed asynchronous deployment must remain a teardown handle.
 *
 * Runtime creation can fail after Memory, IAM roles, code bundles, or gateway
 * resources already exist. In that case there is no runtime ID, so the
 * deployment UUID returned by POST /api/deploy is the only identifier the
 * product can use to invoke its manifest-driven cleanup route. Losing it when
 * polling transitions to `error` makes the UI offer only "Retry Deployment"
 * while the partial AWS estate remains live.
 */

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RuntimeConfiguration } from '../../types/components';
import { useDeployment } from './useDeployment';

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

const resetAllExecutionStates = vi.fn();
vi.mock('../../store/workflowStore', () => ({
  useWorkflowStore: () => ({
    setNodeExecutionStateByType: vi.fn(),
    resetAllExecutionStates,
  }),
}));

const FAILED_DEPLOYMENT = '5bb2084b-d586-46d6-a5f3-494cd24cfc89';

const config: RuntimeConfiguration = {
  name: 'partial-failure',
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

const params = {
  config,
  nodeId: 'runtime-node',
  flowId: 'saved-flow',
  deploymentMode: 'runtime' as const,
  connectedTools: ['memory'],
  gatewayConfig: null,
  externalMcpServers: undefined,
  gatewayTools: [],
  templateId: null,
  identityConfig: null,
  customTools: [],
  connectors: [],
  memoryConfig: { enabled: true },
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

describe('failed deployment cleanup identity', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    mockAuthFetch.mockImplementation(async (url: string) => {
      if (url === '/api/deploy') {
        return {
          ok: true,
          json: async () => ({
            deploymentId: FAILED_DEPLOYMENT,
            status: 'pending',
          }),
        };
      }
      if (url === `/api/deploy/${FAILED_DEPLOYMENT}`) {
        return {
          ok: true,
          json: async () => ({
            deployment_id: FAILED_DEPLOYMENT,
            status: 'failed',
            current_step: 'CreateMemory',
            error_details: 'Deployment failed after creating Memory',
          }),
        };
      }
      throw new Error(`Unexpected request: ${url}`);
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('preserves the deployment UUID when polling reports failure', async () => {
    const { result } = renderHook(() => useDeployment(params));

    let deploy: Promise<void> | undefined;
    act(() => {
      deploy = result.current.handleDeploy();
    });

    await act(async () => {
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(5_000);
      await deploy;
    });

    expect(result.current.deploymentStatus).toMatchObject({
      state: 'error',
      deploymentId: FAILED_DEPLOYMENT,
    });
    expect(result.current.deploymentStatus.runtimeId).toBeUndefined();
  });
});
