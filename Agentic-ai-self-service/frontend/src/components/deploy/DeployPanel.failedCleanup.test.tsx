import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RuntimeConfiguration } from '../../types/components';
import { DeployPanel } from './DeployPanel';
import { useWorkflowStore } from '../../store/workflowStore';

const deploymentHook = vi.hoisted(() => ({
  status: {
    state: 'error' as const,
    deploymentId: '5bb2084b-d586-46d6-a5f3-494cd24cfc89' as string | undefined,
    message: 'Deployment failed after creating Memory',
  },
  setDeploymentStatus: vi.fn(),
  handleDeploy: vi.fn(),
}));

vi.mock('./useDeployment', () => ({
  useDeployment: () => ({
    deploymentStatus: deploymentHook.status,
    setDeploymentStatus: deploymentHook.setDeploymentStatus,
    handleDeploy: deploymentHook.handleDeploy,
  }),
}));

const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

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

describe('failed deployment cleanup affordance', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useWorkflowStore.getState().resetWorkflowDocument(null);
    deploymentHook.status = {
      state: 'error',
      deploymentId: '5bb2084b-d586-46d6-a5f3-494cd24cfc89',
      message: 'Deployment failed after creating Memory',
    };
    mockAuthFetch.mockImplementation(async (url: string) => {
      if (url === '/api/runtime/5bb2084b-d586-46d6-a5f3-494cd24cfc89') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            success: true,
            message: 'Partial deployment resources deleted',
          }),
        };
      }
      if (url === '/api/settings/tags' || url === '/api/settings/tag-profiles') {
        return { ok: true, status: 200, json: async () => [] };
      }
      if (url === '/api/deploy-targets') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ enabled: false, regions: [], accounts: [] }),
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });
  });

  it('requires confirmation and cleans up by deployment UUID, never runtime ID', async () => {
    render(
      <DeployPanel
        config={config}
        nodeId="runtime-node"
        connectedTools={['memory']}
        memoryConfig={{ enabled: true }}
        isVisible
        onClose={vi.fn()}
      />,
    );

    fireEvent.click(
      screen.getByRole('button', { name: 'Clean up partial deployment' }),
    );
    expect(
      mockAuthFetch.mock.calls.some(([url]) => String(url).startsWith('/api/runtime/')),
    ).toBe(false);

    expect(
      screen.getByRole('heading', { name: 'Clean Up Partial Deployment' }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Only resources whose ownership can be verified will be removed/i),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Clean up' }));

    await waitFor(() => {
      expect(mockAuthFetch).toHaveBeenCalledWith(
        '/api/runtime/5bb2084b-d586-46d6-a5f3-494cd24cfc89',
        { method: 'DELETE' },
      );
    });
    expect(deploymentHook.setDeploymentStatus).toHaveBeenCalledWith({
      state: 'idle',
    });
  });

  it('does not offer cleanup when failure happened before a deployment ID existed', () => {
    deploymentHook.status = {
      state: 'error',
      deploymentId: undefined,
      message: 'Deployment request was rejected',
    };

    render(
      <DeployPanel
        config={config}
        nodeId="runtime-node"
        isVisible
        onClose={vi.fn()}
      />,
    );

    expect(
      screen.queryByRole('button', { name: 'Clean up partial deployment' }),
    ).not.toBeInTheDocument();
  });
});
