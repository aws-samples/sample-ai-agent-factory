/**
 * Measured live on the acfe2e-p0925 stack (matrix run 20260927T155010Z, capture
 * mcp-server-gateway-target): after sign-in FlowSidebar auto-opened the most
 * recent flow; the gallery's "Use template" was clicked while that GET was still
 * in flight, so the canvas was empty, no "Replace current workflow?" confirmation
 * was shown, the template loaded, and the arriving flow (the previous session's
 * seven-node canvas) then replaced it with no error. Four earlier captures had
 * passed only because their GET resolved first.
 *
 * The rule under test: a flow may never be activated over content on a canvas
 * that no flow owns, because that content cannot be saved (useAutoSave requires
 * documentFlowId === activeFlowId) and hydration would discard it silently.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { createFlowApi, getFlowApi } = vi.hoisted(() => ({
  createFlowApi: vi.fn(),
  getFlowApi: vi.fn(),
}));

vi.mock('../services/api', () => ({
  getApiClient: () => ({ createFlow: createFlowApi, getFlow: getFlowApi }),
  getErrorMessage: (error: unknown) =>
    error instanceof Error ? error.message : String(error),
}));

import { createEmptyDeploymentGovernance } from '../types/workflow';
import { useFlowStore } from './flowStore';
import {
  UNBOUND_WORK_HYDRATION_ERROR,
  useWorkflowStore,
  type AgentCoreNode,
} from './workflowStore';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

const backendNode = (id: string) => ({
  id,
  type: 'runtime',
  position: { x: 0, y: 0 },
  data: {
    component_type: 'runtime',
    name: id,
    system_prompt: 'You are a helpful assistant.',
    framework: 'boto3',
    model: { model_id: 'us.anthropic.claude-sonnet-4-20250514-v1:0' },
  },
});

function flow(id: string, nodeIds: string[]) {
  return {
    id,
    name: id,
    workflow: {
      id: `${id}-workflow`,
      name: id,
      description: '',
      version: '1.0.0',
      nodes: nodeIds.map(backendNode),
      edges: [],
      viewport: { x: 0, y: 0, zoom: 1 },
      metadata: {
        author: 'owner',
        tags: [],
        awsRegion: 'us-east-1',
        deploymentStatus: 'not_deployed' as const,
      },
      governance: createEmptyDeploymentGovernance(),
      createdAt: '2026-09-27T00:00:00Z',
      updatedAt: '2026-09-27T00:00:00Z',
    },
    deploymentStatus: 'not_deployed' as const,
    createdAt: '2026-09-27T00:00:00Z',
    updatedAt: '2026-09-27T00:00:00Z',
  };
}

const templateNode = (id: string): AgentCoreNode =>
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

describe('flowStore auto-open versus work on an unbound canvas', () => {
  beforeEach(() => {
    getFlowApi.mockReset();
    createFlowApi.mockReset();
    useFlowStore.setState({
      flows: [],
      activeFlowId: null,
      activeFlowName: null,
      isLoading: false,
      error: null,
      pendingFlowId: null,
    });
    useWorkflowStore.getState().resetWorkflowDocument(null);
  });

  it('opens the most recent flow onto a pristine canvas', async () => {
    getFlowApi.mockResolvedValue(flow('flow-saved', ['saved-1', 'saved-2']));

    await useFlowStore.getState().openFlow('flow-saved');

    expect(useFlowStore.getState().activeFlowId).toBe('flow-saved');
    expect(useFlowStore.getState().error).toBeNull();
    const document = useWorkflowStore.getState();
    expect(document.documentFlowId).toBe('flow-saved');
    expect(document.nodes.map((node) => node.id)).toEqual(['saved-1', 'saved-2']);
  });

  it('keeps a template chosen during the open and does not activate the flow', async () => {
    const pending = deferred<ReturnType<typeof flow>>();
    getFlowApi.mockReturnValue(pending.promise);

    const opening = useFlowStore.getState().openFlow('flow-saved');
    // The user acts while the GET is in flight: the canvas is empty, so the
    // gallery shows no confirmation and loads the template directly.
    useWorkflowStore.getState().loadTemplate(
      [templateNode('chosen-1'), templateNode('chosen-2')],
      [],
      'mcp-server-gateway-target',
    );

    pending.resolve(flow('flow-saved', ['saved-1', 'saved-2', 'saved-3']));
    await opening;

    const document = useWorkflowStore.getState();
    expect(document.nodes.map((node) => node.id)).toEqual(['chosen-1', 'chosen-2']);
    expect(document.activeTemplateId).toBe('mcp-server-gateway-target');
    expect(document.documentFlowId).toBeNull();
    const flows = useFlowStore.getState();
    expect(flows.activeFlowId).toBeNull();
    expect(flows.isLoading).toBe(false);
    expect(flows.pendingFlowId).toBeNull();
    expect(flows.error).toBe(UNBOUND_WORK_HYDRATION_ERROR);
  });

  it('lists but does not activate an auto-created flow over work on an unbound canvas', async () => {
    const pending = deferred<{ flow: ReturnType<typeof flow> }>();
    createFlowApi.mockReturnValue(pending.promise);

    const creating = useFlowStore.getState().createFlow('Untitled Flow');
    useWorkflowStore.getState().loadTemplate([templateNode('chosen-1')], [], 'tpl');

    pending.resolve({ flow: flow('flow-new', []) });
    await creating;

    expect(useWorkflowStore.getState().nodes.map((node) => node.id)).toEqual(['chosen-1']);
    const flows = useFlowStore.getState();
    expect(flows.flows.map((summary) => summary.id)).toEqual(['flow-new']);
    expect(flows.activeFlowId).toBeNull();
    expect(flows.error).toBe(UNBOUND_WORK_HYDRATION_ERROR);
  });

  it('switching flows over a bound canvas is unaffected', async () => {
    getFlowApi.mockImplementation(async (id: string) => (
      id === 'flow-a' ? flow('flow-a', ['a-1']) : flow('flow-b', ['b-1', 'b-2'])
    ));

    await useFlowStore.getState().openFlow('flow-a');
    await useFlowStore.getState().openFlow('flow-b');

    expect(useFlowStore.getState().activeFlowId).toBe('flow-b');
    expect(useFlowStore.getState().error).toBeNull();
    expect(useWorkflowStore.getState().nodes.map((node) => node.id)).toEqual(['b-1', 'b-2']);
  });
});
