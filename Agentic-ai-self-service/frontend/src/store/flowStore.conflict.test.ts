/**
 * F-15, client half: the store names the version it saved from, and a 409
 * never overwrites either side. The user resolves it: reload theirs, or keep
 * mine (adopt the server's version as the fence and re-submit the canvas).
 */

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { updateFlowApi, getFlowApi, listFlowsApi } = vi.hoisted(() => ({
  updateFlowApi: vi.fn(),
  getFlowApi: vi.fn(),
  listFlowsApi: vi.fn(),
}));

vi.mock('../services/api', () => ({
  getApiClient: () => ({ updateFlow: updateFlowApi, getFlow: getFlowApi, listFlows: listFlowsApi }),
  getErrorMessage: (error: unknown) => {
    if (typeof error === 'object' && error !== null && 'message' in error) {
      return String((error as { message: unknown }).message);
    }
    return String(error);
  },
}));

import { useAutoSave } from '../hooks/useAutoSave';
import { createEmptyDeploymentGovernance } from '../types/workflow';
import { resetPendingSaveControllerForTests } from '../utils/pendingSave';
import { FlowSaveConflictError, useFlowStore } from './flowStore';
import { useWorkflowStore } from './workflowStore';

const FLOW_ID = 'flow-shared';
const SAVE_INTERVAL_MS = 25;

function conflict409(currentVersion: number) {
  return {
    message: 'This flow was changed elsewhere since you loaded it. Reload to see the latest version, or save again to overwrite it.',
    status: 409,
    details: {
      detail: {
        code: 'flow_version_conflict',
        message: 'This flow was changed elsewhere since you loaded it. Reload to see the latest version, or save again to overwrite it.',
        currentVersion,
        updatedAt: '2026-09-28T10:00:00+00:00',
      },
    },
  };
}

function serverFlow(version: number, viewportX: number) {
  return {
    id: FLOW_ID,
    name: 'Shared',
    workflow: {
      id: 'wf',
      name: 'Shared',
      description: '',
      version: '1.0.0',
      nodes: [],
      edges: [],
      viewport: { x: viewportX, y: 0, zoom: 1 },
      metadata: { author: 'owner', tags: [], awsRegion: 'us-east-1', deploymentStatus: 'not_deployed' as const },
      governance: createEmptyDeploymentGovernance(),
      createdAt: '2026-09-28T00:00:00Z',
      updatedAt: '2026-09-28T00:00:00Z',
    },
    deploymentStatus: 'not_deployed' as const,
    createdAt: '2026-09-28T00:00:00Z',
    updatedAt: '2026-09-28T10:00:00Z',
    version,
  };
}

describe('flowStore.saveFlow optimistic concurrency', () => {
  beforeEach(() => {
    updateFlowApi.mockReset();
    getFlowApi.mockReset();
    listFlowsApi.mockReset();
    useFlowStore.setState({
      flows: [{
        id: FLOW_ID,
        name: 'Shared',
        deploymentStatus: 'not_deployed',
        createdAt: '2026-09-28T00:00:00Z',
        updatedAt: '2026-09-28T00:00:00Z',
        version: 3,
      }],
      activeFlowId: FLOW_ID,
      activeFlowName: 'Shared',
      activeFlowVersion: 3,
      saveConflict: null,
      error: null,
    });
  });

  it('sends the version the canvas was loaded from and adopts the version the server returns', async () => {
    updateFlowApi.mockResolvedValueOnce({ flow: serverFlow(4, 0), message: 'ok' });

    await useFlowStore.getState().saveFlow(FLOW_ID, { id: 'wf' } as never);

    expect(updateFlowApi).toHaveBeenCalledWith(FLOW_ID, { workflow: { id: 'wf' }, expectedVersion: 3 });
    expect(useFlowStore.getState().activeFlowVersion).toBe(4);
    expect(useFlowStore.getState().flows[0].version).toBe(4);
    expect(useFlowStore.getState().saveConflict).toBeNull();
  });

  it('on 409 it does not retry, does not overwrite, and surfaces the conflict with the server version', async () => {
    updateFlowApi.mockRejectedValueOnce(conflict409(7));

    await expect(
      useFlowStore.getState().saveFlow(FLOW_ID, { id: 'wf' } as never),
    ).rejects.toBeInstanceOf(FlowSaveConflictError);

    expect(updateFlowApi).toHaveBeenCalledTimes(1);
    const state = useFlowStore.getState();
    expect(state.saveConflict).toEqual({
      flowId: FLOW_ID,
      serverVersion: 7,
      serverUpdatedAt: '2026-09-28T10:00:00+00:00',
      message: expect.stringContaining('changed elsewhere'),
    });
    // The local fence is NOT silently advanced: a later plain save would 409 again.
    expect(state.activeFlowVersion).toBe(3);
  });

  it('a non-409 failure is not a conflict', async () => {
    updateFlowApi.mockRejectedValueOnce({ message: 'Storage service unavailable', status: 503, details: {} });

    await expect(
      useFlowStore.getState().saveFlow(FLOW_ID, { id: 'wf' } as never),
    ).rejects.toMatchObject({ message: 'Storage service unavailable' });
    expect(useFlowStore.getState().saveConflict).toBeNull();
  });

  it('a rename is fenced too, and a stale rename refreshes the list instead of clobbering the canvas', async () => {
    updateFlowApi.mockRejectedValueOnce(conflict409(5));
    listFlowsApi.mockResolvedValueOnce({
      flows: [{ ...useFlowStore.getState().flows[0], version: 5 }],
    });

    await useFlowStore.getState().renameFlow(FLOW_ID, 'Renamed');

    expect(updateFlowApi).toHaveBeenCalledWith(FLOW_ID, { name: 'Renamed', expectedVersion: 3 });
    expect(useFlowStore.getState().activeFlowName).toBe('Shared');
    expect(useFlowStore.getState().flows[0].version).toBe(5);
    expect(useFlowStore.getState().error).toContain('changed elsewhere');
  });
});

describe('flowStore.resolveSaveConflict', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    resetPendingSaveControllerForTests();
    updateFlowApi.mockReset();
    getFlowApi.mockReset();
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    useFlowStore.setState({
      flows: [{
        id: FLOW_ID,
        name: 'Shared',
        deploymentStatus: 'not_deployed',
        createdAt: '2026-09-28T00:00:00Z',
        updatedAt: '2026-09-28T00:00:00Z',
        version: 3,
      }],
      activeFlowId: FLOW_ID,
      activeFlowName: 'Shared',
      activeFlowVersion: 3,
      saveConflict: null,
      error: null,
      isLoading: false,
      pendingFlowId: null,
    });
    useWorkflowStore.getState().replaceWorkflowDocument(
      { nodes: [], edges: [], viewport: { x: 0, y: 0, zoom: 1 }, governance: createEmptyDeploymentGovernance() },
      { flowId: FLOW_ID, markDirty: false },
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    resetPendingSaveControllerForTests();
  });

  async function editAndHitConflict(result: { current: { lastSaveError: Error | null } }) {
    updateFlowApi.mockRejectedValueOnce(conflict409(7));
    act(() => {
      useWorkflowStore.getState().setViewport({ x: 42, y: 0, zoom: 1 });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SAVE_INTERVAL_MS);
    });
    expect(result.current.lastSaveError).toBeInstanceOf(FlowSaveConflictError);
    expect(useFlowStore.getState().saveConflict?.serverVersion).toBe(7);
  }

  it('"reload" hydrates the server version over the local edit and clears the conflict', async () => {
    const { result } = renderHook(() => useAutoSave(FLOW_ID, SAVE_INTERVAL_MS));
    await editAndHitConflict(result);
    getFlowApi.mockResolvedValueOnce(serverFlow(7, 100));

    await act(async () => {
      await useFlowStore.getState().resolveSaveConflict('reload');
    });

    expect(getFlowApi).toHaveBeenCalledWith(FLOW_ID);
    expect(useWorkflowStore.getState().viewport.x).toBe(100);
    expect(useFlowStore.getState().activeFlowVersion).toBe(7);
    expect(useFlowStore.getState().saveConflict).toBeNull();
    // The reload did not itself try to save the stale canvas.
    expect(updateFlowApi).toHaveBeenCalledTimes(1);
  });

  it('"keep mine" re-submits the current canvas built on the server\'s version, through the autosave writer', async () => {
    const { result } = renderHook(() => useAutoSave(FLOW_ID, SAVE_INTERVAL_MS));
    await editAndHitConflict(result);
    updateFlowApi.mockResolvedValueOnce({ flow: serverFlow(8, 42), message: 'ok' });

    await act(async () => {
      await useFlowStore.getState().resolveSaveConflict('keep_mine');
    });

    expect(updateFlowApi).toHaveBeenCalledTimes(2);
    const [, body] = updateFlowApi.mock.calls[1];
    expect(body.expectedVersion).toBe(7);
    expect(body.workflow.viewport.x).toBe(42);
    expect(useFlowStore.getState().activeFlowVersion).toBe(8);
    expect(useFlowStore.getState().saveConflict).toBeNull();
    expect(result.current.lastSaveError).toBeNull();
  });

  it('"keep mine" that 409s again (the row moved once more) surfaces a fresh conflict, never a silent overwrite', async () => {
    const { result } = renderHook(() => useAutoSave(FLOW_ID, SAVE_INTERVAL_MS));
    await editAndHitConflict(result);
    updateFlowApi.mockRejectedValueOnce(conflict409(9));

    await act(async () => {
      await useFlowStore.getState().resolveSaveConflict('keep_mine');
    });

    expect(useFlowStore.getState().saveConflict?.serverVersion).toBe(9);
    expect(useWorkflowStore.getState().viewport.x).toBe(42);
  });
});
