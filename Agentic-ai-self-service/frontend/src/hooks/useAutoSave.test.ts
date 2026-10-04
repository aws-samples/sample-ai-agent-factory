import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useFlowStore } from '../store/flowStore';
import { useWorkflowStore } from '../store/workflowStore';
import {
  createEmptyDeploymentGovernance,
  type DeploymentGovernanceV1,
} from '../types/workflow';
import { useAutoSave } from './useAutoSave';

const FLOW_ID = 'flow-autosave-test';
const SAVE_INTERVAL_MS = 25;
const GOVERNANCE: DeploymentGovernanceV1 = {
  version: 1,
  namingProfile: { prefix: 'ecb' },
  tags: {
    explicitValues: { owner: 'alice' },
    effectiveValues: { owner: 'alice' },
    profile: { name: 'regulated', updatedAt: '2026-09-23T12:00:00Z' },
    policyRevision: 'sha256:v7',
  },
};

function changeWorkflowViewport(x: number): void {
  const viewport = useWorkflowStore.getState().viewport;
  useWorkflowStore.getState().setViewport({ ...viewport, x });
}

async function elapseSaveInterval(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(SAVE_INTERVAL_MS);
  });
}

describe('useAutoSave persistence failures', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useFlowStore.setState({
      activeFlowId: FLOW_ID,
      activeFlowName: 'Autosave test',
      flows: [{
        id: FLOW_ID,
        name: 'Autosave test',
        deploymentStatus: 'not_deployed',
        createdAt: '2026-09-22T00:00:00.000Z',
        updatedAt: '2026-09-22T00:00:00.000Z',
      }],
      error: null,
    });
    useWorkflowStore.getState().replaceWorkflowDocument(
      {
        nodes: [],
        edges: [],
        viewport: { x: 0, y: 0, zoom: 1 },
        governance: createEmptyDeploymentGovernance(),
      },
      { flowId: FLOW_ID, markDirty: false },
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('surfaces a rejected save, permits dismissal, and clears the warning after recovery', async () => {
    const failure = new Error('autosave endpoint unavailable');
    const saveFlow = vi.fn()
      .mockRejectedValueOnce(failure)
      .mockResolvedValueOnce(undefined);
    useFlowStore.setState({ saveFlow });
    vi.spyOn(console, 'error').mockImplementation(() => undefined);

    const { result } = renderHook(() => useAutoSave(FLOW_ID, SAVE_INTERVAL_MS));

    // The first real user edit must save. Hydration is ignored explicitly by its
    // version marker; there is no generic "drop the first event" heuristic.
    act(() => changeWorkflowViewport(2));
    await elapseSaveInterval();

    expect(result.current.lastSaveError).toBe(failure);
    expect(saveFlow).toHaveBeenCalledTimes(1);

    act(() => result.current.clearLastSaveError());
    expect(result.current.lastSaveError).toBeNull();

    act(() => changeWorkflowViewport(3));
    await elapseSaveInterval();

    expect(saveFlow).toHaveBeenCalledTimes(2);
    expect(result.current.lastSaveError).toBeNull();
  });

  it('auto-saves a governance-only edit with the exact canonical V1 payload', async () => {
    const saveFlow = vi.fn().mockResolvedValue(undefined);
    useFlowStore.setState({ saveFlow });

    renderHook(() => useAutoSave(FLOW_ID, SAVE_INTERVAL_MS));

    act(() => useWorkflowStore.getState().setGovernance(GOVERNANCE));
    await elapseSaveInterval();

    expect(saveFlow).toHaveBeenCalledTimes(1);
    const [savedFlowId, workflow] = saveFlow.mock.calls[0];
    expect(savedFlowId).toBe(FLOW_ID);
    expect(workflow.governance).toEqual(GOVERNANCE);
  });

  it('does not save a flow hydration back to the server', async () => {
    const saveFlow = vi.fn().mockResolvedValue(undefined);
    useFlowStore.setState({ saveFlow });

    renderHook(() => useAutoSave(FLOW_ID, SAVE_INTERVAL_MS));
    act(() => {
      useWorkflowStore.getState().replaceWorkflowDocument(
        {
          nodes: [],
          edges: [],
          viewport: { x: 100, y: 200, zoom: 2 },
          governance: GOVERNANCE,
        },
        { flowId: FLOW_ID, markDirty: false },
      );
    });
    await elapseSaveInterval();

    expect(saveFlow).not.toHaveBeenCalled();
  });

  it('saves a directly opened active flow even before the sidebar list is loaded', async () => {
    const saveFlow = vi.fn().mockResolvedValue(undefined);
    useFlowStore.setState({ saveFlow, flows: [] });

    renderHook(() => useAutoSave(FLOW_ID, SAVE_INTERVAL_MS));
    act(() => changeWorkflowViewport(4));
    await elapseSaveInterval();

    expect(saveFlow).toHaveBeenCalledTimes(1);
    expect(saveFlow.mock.calls[0][0]).toBe(FLOW_ID);
  });

  it('cancels flow A pending data when flow B hydrates', async () => {
    const saveFlow = vi.fn().mockResolvedValue(undefined);
    useFlowStore.setState({
      saveFlow,
      flows: [
        {
          id: 'flow-a',
          name: 'A',
          deploymentStatus: 'not_deployed',
          createdAt: '2026-09-22T00:00:00.000Z',
          updatedAt: '2026-09-22T00:00:00.000Z',
        },
        {
          id: 'flow-b',
          name: 'B',
          deploymentStatus: 'not_deployed',
          createdAt: '2026-09-22T00:00:00.000Z',
          updatedAt: '2026-09-22T00:00:00.000Z',
        },
      ],
      activeFlowId: 'flow-a',
    });
    useWorkflowStore.getState().replaceWorkflowDocument(
      {
        nodes: [],
        edges: [],
        viewport: { x: 0, y: 0, zoom: 1 },
        governance: createEmptyDeploymentGovernance(),
      },
      { flowId: 'flow-a', markDirty: false },
    );

    const { rerender } = renderHook(
      ({ flowId }) => useAutoSave(flowId, SAVE_INTERVAL_MS),
      { initialProps: { flowId: 'flow-a' as string | null } },
    );

    act(() => changeWorkflowViewport(9));
    act(() => {
      useFlowStore.setState({ activeFlowId: 'flow-b' });
      useWorkflowStore.getState().replaceWorkflowDocument(
        {
          nodes: [],
          edges: [],
          viewport: { x: 20, y: 0, zoom: 1 },
          governance: GOVERNANCE,
        },
        { flowId: 'flow-b', markDirty: false },
      );
    });
    rerender({ flowId: 'flow-b' });
    await elapseSaveInterval();
    expect(saveFlow).not.toHaveBeenCalled();

    act(() => changeWorkflowViewport(21));
    await elapseSaveInterval();

    expect(saveFlow).toHaveBeenCalledTimes(1);
    expect(saveFlow.mock.calls[0][0]).toBe('flow-b');
    expect(saveFlow.mock.calls[0][1].governance).toEqual(GOVERNANCE);
  });

  it('serializes writes so an older slow save cannot overwrite a newer snapshot', async () => {
    let resolveFirst!: () => void;
    let resolveSecond!: () => void;
    const first = new Promise<void>((resolve) => { resolveFirst = resolve; });
    const second = new Promise<void>((resolve) => { resolveSecond = resolve; });
    const saveFlow = vi.fn()
      .mockImplementationOnce(() => first)
      .mockImplementationOnce(() => second);
    useFlowStore.setState({ saveFlow });

    renderHook(() => useAutoSave(FLOW_ID, SAVE_INTERVAL_MS));

    act(() => changeWorkflowViewport(1));
    await elapseSaveInterval();
    expect(saveFlow).toHaveBeenCalledTimes(1);
    expect(saveFlow.mock.calls[0][1].viewport.x).toBe(1);

    act(() => changeWorkflowViewport(2));
    await elapseSaveInterval();
    // The second snapshot is queued, not sent concurrently.
    expect(saveFlow).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveFirst();
      await first;
      await Promise.resolve();
    });
    expect(saveFlow).toHaveBeenCalledTimes(2);
    expect(saveFlow.mock.calls[1][1].viewport.x).toBe(2);

    await act(async () => {
      resolveSecond();
      await second;
    });
  });
});
