import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useWorkflowStore } from '../store/workflowStore';
import {
  createEmptyDeploymentGovernance,
  type DeploymentGovernanceV1,
  type WorkflowDefinition,
} from '../types/workflow';
import { WorkflowSerializer } from '../utils/serialization';
import { useWorkflowPersistence } from './useWorkflowPersistence';

const persistenceMocks = vi.hoisted(() => ({
  scheduleAutoSave: vi.fn(),
  saveNow: vi.fn(),
  cancelPendingAutoSave: vi.fn(),
  loadWorkflowFromStorage: vi.fn(),
  getStoredWorkflowId: vi.fn(),
  setStoredWorkflowId: vi.fn(),
  getWorkflow: vi.fn(),
}));

vi.mock('../utils/autoSave', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/autoSave')>();
  return {
    ...actual,
    createAutoSaveService: () => ({
      scheduleAutoSave: persistenceMocks.scheduleAutoSave,
      saveNow: persistenceMocks.saveNow,
      cancelPendingAutoSave: persistenceMocks.cancelPendingAutoSave,
    }),
    createBackendSaveFunction: vi.fn(),
    loadWorkflowFromStorage: () => persistenceMocks.loadWorkflowFromStorage(),
    getStoredWorkflowId: () => persistenceMocks.getStoredWorkflowId(),
    setStoredWorkflowId: (id: string) => persistenceMocks.setStoredWorkflowId(id),
  };
});

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>();
  return {
    ...actual,
    getApiClient: () => ({
      getWorkflow: persistenceMocks.getWorkflow,
    }),
    isApiError: () => false,
  };
});

const FLOW_ID = 'flow-governed';
const GOVERNANCE: DeploymentGovernanceV1 = {
  version: 1,
  namingProfile: { prefix: 'ecb' },
  tags: {
    explicitValues: { Environment: 'staging' },
    effectiveValues: {
      CostCenter: '4242',
      Environment: 'staging',
    },
    profile: {
      name: 'regulated',
      updatedAt: '2026-09-23T12:00:00Z',
    },
    policyRevision: 'sha256:governance-v7',
  },
};

const BACKEND_WORKFLOW: WorkflowDefinition = {
  id: FLOW_ID,
  name: 'Governed workflow',
  description: '',
  version: '1.0.0',
  nodes: [],
  edges: [],
  viewport: { x: 10, y: 20, zoom: 1.5 },
  metadata: {
    author: 'owner',
    tags: [],
    awsRegion: 'us-east-1',
    deploymentStatus: 'not_deployed',
  },
  governance: GOVERNANCE,
  createdAt: '2026-09-23T10:00:00Z',
  updatedAt: '2026-09-23T12:00:00Z',
};

describe('useWorkflowPersistence governance compatibility path', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    persistenceMocks.getStoredWorkflowId.mockReturnValue(null);
    persistenceMocks.loadWorkflowFromStorage.mockReturnValue(null);
    persistenceMocks.saveNow.mockResolvedValue({
      success: true,
      timestamp: new Date('2026-09-23T12:00:00Z'),
    });
    useWorkflowStore.getState().resetWorkflowDocument(null);
  });

  it('hydrates backend governance atomically with the canvas without scheduling a save', async () => {
    persistenceMocks.getWorkflow.mockResolvedValue(BACKEND_WORKFLOW);
    const { result } = renderHook(() => useWorkflowPersistence());
    await waitFor(() => expect(result.current.state.isRestored).toBe(true));
    persistenceMocks.scheduleAutoSave.mockClear();

    let loaded = false;
    await act(async () => {
      loaded = await result.current.loadFromBackend(FLOW_ID);
    });

    expect(loaded).toBe(true);
    const state = useWorkflowStore.getState();
    expect({
      nodes: state.nodes,
      edges: state.edges,
      viewport: state.viewport,
      governance: state.governance,
      documentFlowId: state.documentFlowId,
      persistenceRevision: state.persistenceRevision,
    }).toEqual({
      nodes: [],
      edges: [],
      viewport: BACKEND_WORKFLOW.viewport,
      governance: GOVERNANCE,
      documentFlowId: FLOW_ID,
      persistenceRevision: 0,
    });
    expect(persistenceMocks.scheduleAutoSave).not.toHaveBeenCalled();
  });

  it('restores exact governance from the local-storage fallback', async () => {
    persistenceMocks.getStoredWorkflowId.mockReturnValue(FLOW_ID);
    persistenceMocks.loadWorkflowFromStorage.mockReturnValue(
      WorkflowSerializer.serialize(
        [],
        [],
        BACKEND_WORKFLOW.viewport,
        BACKEND_WORKFLOW.metadata,
        {
          id: FLOW_ID,
          name: BACKEND_WORKFLOW.name,
          description: '',
          version: '1.0.0',
        },
        GOVERNANCE,
      ),
    );

    const { result } = renderHook(() => useWorkflowPersistence());
    await waitFor(() => expect(result.current.state.isRestored).toBe(true));

    const state = useWorkflowStore.getState();
    expect(state.governance).toEqual(GOVERNANCE);
    expect(state.viewport).toEqual(BACKEND_WORKFLOW.viewport);
    expect(state.documentFlowId).toBe(FLOW_ID);
    expect(persistenceMocks.scheduleAutoSave).not.toHaveBeenCalled();
  });

  it('treats governance-only edits as saveable state and includes governance in saveNow', async () => {
    const { result } = renderHook(() => useWorkflowPersistence());
    await waitFor(() => expect(result.current.state.isRestored).toBe(true));
    persistenceMocks.scheduleAutoSave.mockClear();

    act(() => {
      useWorkflowStore.getState().setGovernance(GOVERNANCE);
    });
    await waitFor(() => expect(persistenceMocks.scheduleAutoSave).toHaveBeenCalledTimes(1));

    const scheduledArguments = persistenceMocks.scheduleAutoSave.mock.calls[0];
    expect(scheduledArguments[5]).toEqual(GOVERNANCE);

    await act(async () => {
      await result.current.saveNow();
    });
    expect(persistenceMocks.saveNow).toHaveBeenCalledWith(
      [],
      [],
      { x: 0, y: 0, zoom: 1 },
      undefined,
      undefined,
      GOVERNANCE,
    );
  });

  it('migrates a legacy backend workflow to explicit empty V1 governance', async () => {
    persistenceMocks.getWorkflow.mockResolvedValue({
      ...BACKEND_WORKFLOW,
      governance: undefined,
    });
    const { result } = renderHook(() => useWorkflowPersistence());
    await waitFor(() => expect(result.current.state.isRestored).toBe(true));

    await act(async () => {
      await result.current.loadFromBackend(FLOW_ID);
    });

    expect(useWorkflowStore.getState().governance).toEqual(
      createEmptyDeploymentGovernance(),
    );
  });
});
