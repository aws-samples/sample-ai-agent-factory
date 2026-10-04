/**
 * The canvas's validation verdict must describe the canvas that is on screen.
 *
 * Measured against the live stack before these tests existed: signing in with a
 * saved flow auto-opened it (FlowSidebar auto-opens the most recent flow), the
 * canvas rendered the flow's nodes, and WorkflowCanvas's `deploy-status-indicator`
 * read "○ Validation Pending" indefinitely — because `openFlow` used the raw
 * `setNodes`/`setEdges` setters, nothing on that path ran validation, and the
 * debounced `useValidation` hook is mounted nowhere in the app. The same hole
 * covered `addNode`, `deleteNode`, `addEdge`, `deleteEdge`, `undo` and `redo`.
 *
 * These assertions are about `validationState` being RECOMPUTED, not about any
 * particular verdict, so they keep holding when the validation rules change.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { useWorkflowStore } from './workflowStore';
import type { AgentCoreNode } from './workflowStore';

const runtimeNode = (id: string, name: string): AgentCoreNode =>
  ({
    id,
    type: 'runtime',
    position: { x: 0, y: 0 },
    data: {
      label: name,
      componentType: 'runtime',
      configuration: {
        componentType: 'runtime',
        name,
        systemPrompt: 'You are a helpful assistant.',
        framework: 'boto3',
        model: { modelId: 'us.anthropic.claude-sonnet-4-20250514-v1:0' },
      },
      validationStatus: 'pending',
    },
  }) as unknown as AgentCoreNode;

const reset = () =>
  useWorkflowStore.setState({
    nodes: [],
    edges: [],
    validationState: null,
    isReadyToDeploy: false,
    selectedNodeId: null,
    selectedEdgeId: null,
    activeTemplateId: null,
  });

describe('validation runs on every canvas mutation', () => {
  beforeEach(reset);

  it('addNode computes a verdict for the node just added', () => {
    expect(useWorkflowStore.getState().validationState).toBeNull();
    useWorkflowStore.getState().addNode(runtimeNode('n1', 'agent-one'));

    const state = useWorkflowStore.getState();
    expect(state.validationState).not.toBeNull();
    // A configured runtime is deployable, so this also pins that a dragged-on
    // node does not leave the header/indicator claiming "Validation Pending".
    expect(state.isReadyToDeploy).toBe(true);
  });

  it('deleteNode recomputes rather than leaving the previous verdict', () => {
    const store = useWorkflowStore.getState();
    store.addNode(runtimeNode('n1', 'agent-one'));
    expect(useWorkflowStore.getState().isReadyToDeploy).toBe(true);

    useWorkflowStore.getState().deleteNode('n1');
    const state = useWorkflowStore.getState();
    // 0 nodes can never be ready to deploy; a stale `true` here would enable the
    // Deploy affordance on an empty canvas.
    expect(state.isReadyToDeploy).toBe(false);
    expect(state.validationState?.nodeStates.size).toBe(0);
  });

  it('addEdge and deleteEdge recompute connection validity', () => {
    const store = useWorkflowStore.getState();
    store.addNode(runtimeNode('n1', 'agent-one'));
    store.addNode(runtimeNode('n2', 'agent-two'));

    // runtime -> runtime is not a legal connection; the verdict must say so as
    // soon as the edge exists, not after some later unrelated action.
    useWorkflowStore.getState().addEdge({
      id: 'e1',
      source: 'n1',
      target: 'n2',
      type: 'data',
    } as never);
    expect(useWorkflowStore.getState().validationState?.edgeStates.has('e1')).toBe(true);
    const errorsWithEdge = useWorkflowStore.getState().validationState?.errors.length ?? 0;

    useWorkflowStore.getState().deleteEdge('e1');
    const after = useWorkflowStore.getState().validationState;
    expect(after?.edgeStates.has('e1')).toBe(false);
    expect(after?.errors.length ?? 0).toBeLessThanOrEqual(errorsWithEdge);
  });

  it('undo and redo revalidate the graph they restore', () => {
    const store = useWorkflowStore.getState();
    store.addNode(runtimeNode('n1', 'agent-one'));
    expect(useWorkflowStore.getState().isReadyToDeploy).toBe(true);

    useWorkflowStore.getState().undo();
    expect(useWorkflowStore.getState().nodes).toHaveLength(0);
    expect(useWorkflowStore.getState().isReadyToDeploy).toBe(false);

    useWorkflowStore.getState().redo();
    expect(useWorkflowStore.getState().nodes).toHaveLength(1);
    expect(useWorkflowStore.getState().isReadyToDeploy).toBe(true);
  });
});

describe('openFlow validates the restored canvas', () => {
  beforeEach(reset);
  afterEach(() => vi.resetModules());

  it('a flow restored on sign-in does not come up as "Validation Pending"', async () => {
    const flow = {
      id: 'flow-1',
      name: 'Untitled Flow',
      deploymentStatus: 'not_deployed',
      createdAt: '2026-09-20T00:00:00Z',
      updatedAt: '2026-09-20T00:00:00Z',
      workflow: {
        nodes: [
          {
            id: 'n1',
            type: 'runtime',
            position: { x: 10, y: 20 },
            data: {
              component_type: 'runtime',
              name: 'restored-agent',
              system_prompt: 'You are a helpful assistant.',
              framework: 'boto3',
              model: { model_id: 'us.anthropic.claude-sonnet-4-20250514-v1:0' },
            },
          },
        ],
        edges: [],
        viewport: { x: 0, y: 0, zoom: 1 },
      },
    };

    vi.doMock('../services/api', () => ({
      getApiClient: () => ({ getFlow: async () => flow }),
      getErrorMessage: (e: unknown) => String(e),
    }));

    const { useFlowStore } = await import('./flowStore');
    await useFlowStore.getState().openFlow('flow-1');

    const state = useWorkflowStore.getState();
    expect(state.nodes).toHaveLength(1);
    expect(state.validationState).not.toBeNull();
    expect(state.isReadyToDeploy).toBe(true);
    // The per-node badge is computed by the same pass; 'pending' here is what the
    // user saw on every node of a restored flow.
    expect(state.nodes[0].data.validationStatus).not.toBe('pending');
  });
});
