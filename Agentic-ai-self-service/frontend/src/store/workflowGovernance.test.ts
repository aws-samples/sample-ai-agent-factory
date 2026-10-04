import { beforeEach, describe, expect, it } from 'vitest';

import {
  createEmptyDeploymentGovernance,
  type DeploymentGovernanceV1,
} from '../types/workflow';
import {
  UNBOUND_WORK_HYDRATION_ERROR,
  hydrationWouldDiscardUnboundWork,
  useWorkflowStore,
  type AgentCoreNode,
} from './workflowStore';

const runtimeNode = (id: string): AgentCoreNode =>
  ({
    id,
    type: 'runtime',
    position: { x: 0, y: 0 },
    data: {
      label: id,
      componentType: 'runtime',
      configuration: { componentType: 'runtime', name: id },
      validationStatus: 'pending',
    },
  }) as unknown as AgentCoreNode;


const GOVERNANCE: DeploymentGovernanceV1 = {
  version: 1,
  namingProfile: {
    prefix: 'ecb',
    resourceNames: { gateway: '{prefix}-{deployment}-gw' },
  },
  tags: {
    explicitValues: { 'cost:center': '4711' },
    effectiveValues: {
      'cost:center': '4711',
      'platform:application': 'payments',
    },
    profile: {
      name: 'regulated',
      updatedAt: '2026-09-23T12:00:00Z',
    },
    policyRevision: 'sha256:policy-v7',
  },
};

describe('workflowStore deployment governance', () => {
  beforeEach(() => {
    useWorkflowStore.getState().resetWorkflowDocument(null);
  });

  it('stores governance as first-class persisted state', () => {
    const before = useWorkflowStore.getState().persistenceRevision;

    useWorkflowStore.getState().setGovernance(GOVERNANCE);

    const state = useWorkflowStore.getState();
    expect(state.governance).toEqual(GOVERNANCE);
    expect(state.governance).not.toBe(GOVERNANCE);
    expect(state.persistenceRevision).toBe(before + 1);
  });

  it('does not create a save revision for an equivalent governance update', () => {
    useWorkflowStore.getState().setGovernance(GOVERNANCE);
    const revision = useWorkflowStore.getState().persistenceRevision;

    useWorkflowStore.getState().setGovernance(structuredClone(GOVERNANCE));

    expect(useWorkflowStore.getState().persistenceRevision).toBe(revision);
  });

  it('merges independent tag and naming updates without stale-state loss', () => {
    useWorkflowStore.getState().setGovernance((current) => ({
      ...current,
      namingProfile: { prefix: 'ecb' },
    }));
    useWorkflowStore.getState().setGovernance((current) => ({
      ...current,
      tags: GOVERNANCE.tags,
    }));

    expect(useWorkflowStore.getState().governance).toEqual({
      version: 1,
      namingProfile: { prefix: 'ecb' },
      tags: GOVERNANCE.tags,
    });
  });

  it('hydrates canvas and governance atomically without marking them dirty', () => {
    const beforeHydration = useWorkflowStore.getState().hydrationVersion;

    useWorkflowStore.getState().replaceWorkflowDocument(
      {
        nodes: [],
        edges: [],
        viewport: { x: 12, y: 34, zoom: 1.5 },
        governance: GOVERNANCE,
      },
      { flowId: 'flow-b', markDirty: false },
    );

    const state = useWorkflowStore.getState();
    expect({
      nodes: state.nodes,
      edges: state.edges,
      viewport: state.viewport,
      governance: state.governance,
      documentFlowId: state.documentFlowId,
    }).toEqual({
      nodes: [],
      edges: [],
      viewport: { x: 12, y: 34, zoom: 1.5 },
      governance: GOVERNANCE,
      documentFlowId: 'flow-b',
    });
    expect(state.hydrationVersion).toBe(beforeHydration + 1);
    expect(state.persistenceRevision).toBe(0);
  });

  it('marks a user-initiated full replacement dirty for registry clone autosave', () => {
    useWorkflowStore.getState().replaceWorkflowDocument(
      {
        nodes: [],
        edges: [],
        viewport: { x: 0, y: 0, zoom: 1 },
        governance: createEmptyDeploymentGovernance(),
      },
      { flowId: 'flow-a', markDirty: false },
    );
    const before = useWorkflowStore.getState();

    useWorkflowStore.getState().replaceWorkflowDocument(
      {
        nodes: [],
        edges: [],
        viewport: { x: 5, y: 6, zoom: 2 },
        governance: GOVERNANCE,
      },
      { flowId: 'flow-a', markDirty: true },
    );

    const after = useWorkflowStore.getState();
    expect(after.hydrationVersion).toBe(before.hydrationVersion);
    expect(after.persistenceRevision).toBe(before.persistenceRevision + 1);
    expect(after.governance).toEqual(GOVERNANCE);
  });

  it('refuses to hydrate over content on a canvas that no flow owns', () => {
    // The live race: a template chosen while the sidebar's auto-open GET was in
    // flight; the arriving flow must not silently replace it.
    useWorkflowStore.getState().loadTemplate([runtimeNode('chosen')], [], 'tpl');
    expect(useWorkflowStore.getState().documentFlowId).toBeNull();
    expect(hydrationWouldDiscardUnboundWork(useWorkflowStore.getState())).toBe(true);

    expect(() => useWorkflowStore.getState().replaceWorkflowDocument(
      {
        nodes: [runtimeNode('restored-1'), runtimeNode('restored-2')],
        edges: [],
        viewport: { x: 0, y: 0, zoom: 1 },
        governance: GOVERNANCE,
      },
      { flowId: 'flow-a', markDirty: false },
    )).toThrow(UNBOUND_WORK_HYDRATION_ERROR);

    const state = useWorkflowStore.getState();
    expect(state.nodes.map((node) => node.id)).toEqual(['chosen']);
    expect(state.documentFlowId).toBeNull();
    expect(state.activeTemplateId).toBe('tpl');
  });

  it('still hydrates a flow switch over a bound canvas that has content', () => {
    useWorkflowStore.getState().replaceWorkflowDocument(
      {
        nodes: [runtimeNode('a-1')],
        edges: [],
        viewport: { x: 0, y: 0, zoom: 1 },
        governance: createEmptyDeploymentGovernance(),
      },
      { flowId: 'flow-a', markDirty: false },
    );
    expect(hydrationWouldDiscardUnboundWork(useWorkflowStore.getState())).toBe(false);

    useWorkflowStore.getState().replaceWorkflowDocument(
      {
        nodes: [runtimeNode('b-1'), runtimeNode('b-2')],
        edges: [],
        viewport: { x: 0, y: 0, zoom: 1 },
        governance: GOVERNANCE,
      },
      { flowId: 'flow-b', markDirty: false },
    );

    const state = useWorkflowStore.getState();
    expect(state.documentFlowId).toBe('flow-b');
    expect(state.nodes.map((node) => node.id)).toEqual(['b-1', 'b-2']);
  });

  it('refuses a user-initiated replacement while no flow is open', () => {
    expect(() => useWorkflowStore.getState().replaceWorkflowDocument(
      {
        nodes: [runtimeNode('cloned')],
        edges: [],
        viewport: { x: 0, y: 0, zoom: 1 },
        governance: GOVERNANCE,
      },
      { flowId: null, markDirty: true },
    )).toThrow(/requires an open flow/);
    expect(useWorkflowStore.getState().nodes).toEqual([]);
  });

  it('refuses to mark a replacement dirty under a different flow id', () => {
    useWorkflowStore.getState().replaceWorkflowDocument(
      {
        nodes: [],
        edges: [],
        viewport: { x: 0, y: 0, zoom: 1 },
        governance: createEmptyDeploymentGovernance(),
      },
      { flowId: 'flow-a', markDirty: false },
    );

    expect(() => useWorkflowStore.getState().replaceWorkflowDocument(
      {
        nodes: [],
        edges: [],
        viewport: { x: 0, y: 0, zoom: 1 },
        governance: GOVERNANCE,
      },
      { flowId: 'flow-b', markDirty: true },
    )).toThrow(/currently hydrated flow/);
    expect(useWorkflowStore.getState().documentFlowId).toBe('flow-a');
  });

  it('resets governance with the canvas when the active flow is deleted', () => {
    useWorkflowStore.getState().setGovernance(GOVERNANCE);
    useWorkflowStore.getState().resetWorkflowDocument(null);

    const state = useWorkflowStore.getState();
    expect(state.governance).toEqual(createEmptyDeploymentGovernance());
    expect(state.documentFlowId).toBeNull();
    expect(state.persistenceRevision).toBe(0);
  });
});
