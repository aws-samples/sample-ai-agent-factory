import { beforeEach, describe, expect, it, vi } from 'vitest';

const { createFlowApi, deleteFlowApi, getFlowApi } = vi.hoisted(() => ({
  createFlowApi: vi.fn(),
  deleteFlowApi: vi.fn(),
  getFlowApi: vi.fn(),
}));

vi.mock('../services/api', () => ({
  getApiClient: () => ({
    createFlow: createFlowApi,
    deleteFlow: deleteFlowApi,
    getFlow: getFlowApi,
  }),
  getErrorMessage: (error: unknown) =>
    error instanceof Error ? error.message : String(error),
}));

import type { DeploymentGovernanceV1 } from '../types/workflow';
import { createEmptyDeploymentGovernance } from '../types/workflow';
import { useFlowStore } from './flowStore';
import { useWorkflowStore } from './workflowStore';


const GOVERNANCE_A: DeploymentGovernanceV1 = {
  version: 1,
  namingProfile: { prefix: 'ecb' },
  tags: {
    explicitValues: { owner: 'alice' },
    effectiveValues: { owner: 'alice' },
    profile: null,
    policyRevision: 'sha256:a',
  },
};
const GOVERNANCE_B: DeploymentGovernanceV1 = {
  version: 1,
  namingProfile: { prefix: 'bank' },
  tags: {
    explicitValues: { owner: 'bob' },
    effectiveValues: { owner: 'bob' },
    profile: null,
    policyRevision: 'sha256:b',
  },
};

function flow(id: string, governance: DeploymentGovernanceV1) {
  return {
    id,
    name: id,
    workflow: {
      id: `${id}-workflow`,
      name: id,
      description: '',
      version: '1.0.0',
      nodes: [],
      edges: [],
      viewport: { x: id === 'flow-a' ? 10 : 20, y: 0, zoom: 1 },
      metadata: {
        author: 'owner',
        tags: [],
        awsRegion: 'eu-west-1',
        deploymentStatus: 'not_deployed' as const,
      },
      governance,
      createdAt: '2026-09-23T00:00:00Z',
      updatedAt: '2026-09-23T00:00:00Z',
    },
    deploymentStatus: 'not_deployed' as const,
    createdAt: '2026-09-23T00:00:00Z',
    updatedAt: '2026-09-23T00:00:00Z',
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('flowStore governance hydration', () => {
  beforeEach(() => {
    createFlowApi.mockReset();
    deleteFlowApi.mockReset();
    getFlowApi.mockReset();
    useFlowStore.setState({
      flows: [],
      activeFlowId: null,
      activeFlowName: null,
      isLoading: false,
      error: null,
      navigationGeneration: 0,
      pendingFlowId: null,
    });
    useWorkflowStore.getState().resetWorkflowDocument(null);
  });

  it('loads governance with the canvas and does not mark hydration dirty', async () => {
    getFlowApi.mockResolvedValue(flow('flow-a', GOVERNANCE_A));

    await useFlowStore.getState().openFlow('flow-a');

    const document = useWorkflowStore.getState();
    expect(document.documentFlowId).toBe('flow-a');
    expect(document.viewport.x).toBe(10);
    expect(document.governance).toEqual(GOVERNANCE_A);
    expect(document.persistenceRevision).toBe(0);
  });

  it('does not let a slower flow A response overwrite newer flow B', async () => {
    const a = deferred<ReturnType<typeof flow>>();
    const b = deferred<ReturnType<typeof flow>>();
    getFlowApi.mockImplementation((id: string) => (
      id === 'flow-a' ? a.promise : b.promise
    ));

    const openA = useFlowStore.getState().openFlow('flow-a');
    const openB = useFlowStore.getState().openFlow('flow-b');
    b.resolve(flow('flow-b', GOVERNANCE_B));
    await openB;
    a.resolve(flow('flow-a', GOVERNANCE_A));
    await openA;

    expect(useFlowStore.getState().activeFlowId).toBe('flow-b');
    const document = useWorkflowStore.getState();
    expect(document.documentFlowId).toBe('flow-b');
    expect(document.viewport.x).toBe(20);
    expect(document.governance).toEqual(GOVERNANCE_B);
  });

  it('creates a legacy flow with explicit empty V1 governance', async () => {
    const created = flow('flow-new', createEmptyDeploymentGovernance());
    delete (created.workflow as { governance?: DeploymentGovernanceV1 }).governance;
    createFlowApi.mockResolvedValue({ flow: created });

    await useFlowStore.getState().createFlow('New');

    expect(useWorkflowStore.getState().governance).toEqual(
      createEmptyDeploymentGovernance(),
    );
    expect(useWorkflowStore.getState().documentFlowId).toBe('flow-new');
  });

  it('resets governance only when deleting the currently active flow', async () => {
    deleteFlowApi.mockResolvedValue(undefined);
    useFlowStore.setState({
      activeFlowId: 'flow-a',
      flows: [{
        id: 'flow-a',
        name: 'A',
        deploymentStatus: 'not_deployed',
        createdAt: '2026-09-23T00:00:00Z',
        updatedAt: '2026-09-23T00:00:00Z',
      }],
    });
    useWorkflowStore.getState().replaceWorkflowDocument(
      {
        nodes: [],
        edges: [],
        viewport: { x: 0, y: 0, zoom: 1 },
        governance: GOVERNANCE_A,
      },
      { flowId: 'flow-a', markDirty: false },
    );

    await useFlowStore.getState().deleteFlow('flow-a');

    expect(useFlowStore.getState().activeFlowId).toBeNull();
    expect(useWorkflowStore.getState().governance).toEqual(
      createEmptyDeploymentGovernance(),
    );
    expect(useWorkflowStore.getState().documentFlowId).toBeNull();
  });
});
