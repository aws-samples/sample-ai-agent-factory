/**
 * F-13: a deployment row in status `failed` must end the poll and surface its
 * error_details verbatim, whatever those details say.
 *
 * Before the fix the catch around the poll rethrew only when the message
 * contained "Deployment failed" or the lowercase substring "failed". A Step
 * Functions failure whose Cause is "ValidationException: ...",
 * "AccessDeniedException ..." or "Failed to create runtime" (capital F) was
 * swallowed, the loop re-read the same failed row every 5 s for 10 minutes and
 * the user was told the deployment "timed out". The existing fixture happened to
 * contain "Deployment failed", so it passed.
 *
 * The status poll's `if (!statusResp.ok) continue` had the same shape for an
 * expired session: a 401 on every poll produced the same 10-minute silence.
 */

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RuntimeConfiguration } from '../../types/components';
import { SESSION_EXPIRED_MESSAGE } from '../../services/api/client';
import { useDeployment } from './useDeployment';

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

const setNodeExecutionStateByType = vi.fn();
vi.mock('../../store/workflowStore', () => ({
  useWorkflowStore: () => ({
    setNodeExecutionStateByType,
    resetAllExecutionStates: vi.fn(),
  }),
}));

const DEPLOYMENT_ID = '0f6d1c2e-2b8a-4d3e-9c1f-7a5b6e8d9c0a';

const config: RuntimeConfiguration = {
  name: 'failed-status',
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
  warmupRuntime: vi.fn(),
  onVersionsRefresh: vi.fn(),
  onTabChange: vi.fn(),
};

function respondWith(statusRow: () => { ok: boolean; status?: number; json?: () => Promise<unknown> }) {
  mockAuthFetch.mockImplementation(async (url: string) => {
    if (url === '/api/deploy') {
      return {
        ok: true,
        json: async () => ({ deploymentId: DEPLOYMENT_ID, status: 'pending' }),
      };
    }
    if (url === `/api/deploy/${DEPLOYMENT_ID}`) {
      return statusRow();
    }
    throw new Error(`Unexpected request: ${url}`);
  });
}

async function startAndPollOnce() {
  const { result } = renderHook(() => useDeployment(params));
  let deploy: Promise<void> | undefined;
  act(() => {
    deploy = result.current.handleDeploy();
  });
  await act(async () => {
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(5_000);
  });
  return { result, deploy: deploy! };
}

function pollCount(): number {
  return mockAuthFetch.mock.calls.filter(([url]) => url === `/api/deploy/${DEPLOYMENT_ID}`).length;
}

describe('a failed deployment row ends the poll', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it.each([
    ['ValidationException: The provided execution role ARN is not valid for this account'],
    ['AccessDeniedException when calling the CreateAgentRuntime operation: User is not authorized'],
    ['Failed to create runtime'],
  ])('surfaces %s verbatim after the first failed poll', async (errorDetails) => {
    respondWith(() => ({
      ok: true,
      json: async () => ({
        deployment_id: DEPLOYMENT_ID,
        status: 'failed',
        current_step: 'runtime_launch',
        error_details: errorDetails,
      }),
    }));

    const { result, deploy } = await startAndPollOnce();

    expect(result.current.deploymentStatus).toEqual({
      state: 'error',
      deploymentId: DEPLOYMENT_ID,
      message: errorDetails,
    });
    // One read of the failed row, not a second one 5 s later.
    expect(pollCount()).toBe(1);
    await act(async () => {
      await deploy;
    });
  });

  it('still marks the failing step node as failed', async () => {
    respondWith(() => ({
      ok: true,
      json: async () => ({
        deployment_id: DEPLOYMENT_ID,
        status: 'failed',
        current_step: 'runtime_launch',
        error_details: 'AccessDeniedException while creating the runtime',
      }),
    }));

    const { result, deploy } = await startAndPollOnce();

    expect(result.current.deploymentStatus.state).toBe('error');
    expect(setNodeExecutionStateByType).toHaveBeenCalledWith('runtime', 'failed');
    await act(async () => {
      await deploy;
    });
  });

  it('falls back to a generic message when a failed row carries no details', async () => {
    respondWith(() => ({
      ok: true,
      json: async () => ({ deployment_id: DEPLOYMENT_ID, status: 'failed' }),
    }));

    const { result, deploy } = await startAndPollOnce();

    expect(result.current.deploymentStatus.state).toBe('error');
    expect(result.current.deploymentStatus.message).toBe('Deployment failed');
    await act(async () => {
      await deploy;
    });
  });

  it('ends the poll on a 401 with the session-expired message instead of ten minutes of silence', async () => {
    respondWith(() => ({ ok: false, status: 401 }));

    const { result, deploy } = await startAndPollOnce();

    expect(result.current.deploymentStatus).toEqual({
      state: 'error',
      deploymentId: DEPLOYMENT_ID,
      message: SESSION_EXPIRED_MESSAGE,
    });
    expect(pollCount()).toBe(1);
    await act(async () => {
      await deploy;
    });
  });

  it('keeps polling through a transient 5xx and a network error', async () => {
    let poll = 0;
    respondWith(() => {
      poll += 1;
      if (poll === 1) return { ok: false, status: 502 };
      if (poll === 2) throw new TypeError('Failed to fetch');
      return {
        ok: true,
        json: async () => ({ deployment_id: DEPLOYMENT_ID, status: 'in_progress', current_step: 'CreateRuntime' }),
      };
    });

    const { result } = await startAndPollOnce();
    expect(result.current.deploymentStatus.state).toBe('deploying');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(result.current.deploymentStatus.state).toBe('deploying');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(pollCount()).toBe(3);
    expect(result.current.deploymentStatus.state).toBe('deploying');
  });
});
