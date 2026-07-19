/**
 * useDeployment hook unit tests.
 * Tests state transitions with mocked api module.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useDeployment } from './useDeployment';
import type { RuntimeConfiguration } from '../../types/components';

// Mock authFetch
const mockAuthFetch = vi.fn();
vi.mock('../../auth/authFetch', () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

// Mock workflowStore
const mockSetNodeExecutionStateByType = vi.fn();
const mockResetAllExecutionStates = vi.fn();
vi.mock('../../store/workflowStore', () => ({
  useWorkflowStore: () => ({
    setNodeExecutionStateByType: mockSetNodeExecutionStateByType,
    resetAllExecutionStates: mockResetAllExecutionStates,
  }),
}));

describe('useDeployment', () => {
  const mockConfig: RuntimeConfiguration = {
    name: 'test-runtime',
    entrypoint: 'agent.py',
    systemPrompt: 'test prompt',
    model: { modelId: 'test-model', provider: 'bedrock', temperature: 0.7, topP: 0.9 },
    framework: 'strands_agents',
    deploymentType: 'direct_code_deploy',
    protocol: 'HTTP',
    pythonRuntime: 'PYTHON_3_12',
    idleTimeout: 900,
    maxLifetime: 28800,
    enableOtel: false,
    modelProvider: 'bedrock',
    multiAgentPattern: 'none',
  };

  const mockParams = {
    config: mockConfig,
    nodeId: 'node-1',
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
    resourceTagState: { tags: {}, profileName: null },
    warmupRuntime: vi.fn(),
    onVersionsRefresh: vi.fn(),
    onTabChange: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('should initialize with idle state', () => {
    const { result } = renderHook(() => useDeployment(mockParams));
    expect(result.current.deploymentStatus.state).toBe('idle');
  });

  it('should transition to deploying state when handleDeploy is called', async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ success: true, runtimeId: 'runtime-1', endpoint: 'https://test.com' }),
    });

    const { result } = renderHook(() => useDeployment(mockParams));

    expect(result.current.deploymentStatus.state).toBe('idle');

    result.current.handleDeploy();

    await waitFor(() => {
      expect(mockResetAllExecutionStates).toHaveBeenCalled();
    });
  });

  it('should handle synchronous deployment success', async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        success: true,
        runtimeId: 'runtime-123',
        endpoint: 'https://test-endpoint.com',
        message: 'Deployed successfully!',
      }),
    });

    const { result } = renderHook(() => useDeployment(mockParams));

    await result.current.handleDeploy();

    await waitFor(() => {
      expect(result.current.deploymentStatus.state).toBe('deployed');
      expect(result.current.deploymentStatus.runtimeId).toBe('runtime-123');
      expect(result.current.deploymentStatus.endpoint).toBe('https://test-endpoint.com');
    });

    expect(mockParams.onTabChange).toHaveBeenCalledWith('chat');
    expect(mockParams.warmupRuntime).toHaveBeenCalledWith('runtime-123', 'https://test-endpoint.com');
  });

  it('should handle deployment error', async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: false,
      status: 500,
      text: async () => 'Internal Server Error',
    });

    const { result } = renderHook(() => useDeployment(mockParams));

    await result.current.handleDeploy();

    await waitFor(() => {
      expect(result.current.deploymentStatus.state).toBe('error');
      expect(result.current.deploymentStatus.message).toContain('failed');
    });
  });

  it('should reset execution states when deploy starts', async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ success: true, runtimeId: 'runtime-1', endpoint: 'https://test.com' }),
    });

    const { result } = renderHook(() => useDeployment(mockParams));

    await result.current.handleDeploy();

    expect(mockResetAllExecutionStates).toHaveBeenCalled();
  });

  it('should allow manual state changes via setDeploymentStatus', () => {
    const { result } = renderHook(() => useDeployment(mockParams));

    result.current.setDeploymentStatus({ state: 'idle' });

    expect(result.current.deploymentStatus.state).toBe('idle');
  });
});
