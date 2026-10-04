/**
 * F-14: a debounced autosave is flushed, not cancelled, by everything that
 * would otherwise replace the canvas or end the session.
 *
 * One test per trigger: flow switch (flowStore.openFlow), flow create
 * (flowStore.createFlow), sign-out (signOutAfterFlush) and tab close
 * (pagehide / beforeunload). Each test makes an edit inside the debounce
 * window and asserts the edit reaches saveFlow under the flow it was made on,
 * before the trigger completes.
 */

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { getFlowApi, createFlowApi } = vi.hoisted(() => ({
  getFlowApi: vi.fn(),
  createFlowApi: vi.fn(),
}));

vi.mock('../services/api', () => ({
  getApiClient: () => ({ getFlow: getFlowApi, createFlow: createFlowApi }),
  getErrorMessage: (error: unknown) => (error instanceof Error ? error.message : String(error)),
}));

import { PENDING_SAVE_NOT_FLUSHED_ERROR, useFlowStore } from '../store/flowStore';

type SaveFlow = ReturnType<typeof useFlowStore.getState>['saveFlow'];
import { useWorkflowStore } from '../store/workflowStore';
import { createEmptyDeploymentGovernance } from '../types/workflow';
import { signOutAfterFlush, UNSAVED_SIGN_OUT_PROMPT } from '../auth/signOutAfterFlush';
import { hasUnsavedWork, resetPendingSaveControllerForTests } from '../utils/pendingSave';
import { useAutoSave } from './useAutoSave';

const FLOW_A = 'flow-a';
const FLOW_B = 'flow-b';
const SAVE_INTERVAL_MS = 5_000;

function summary(id: string) {
  return {
    id,
    name: id,
    deploymentStatus: 'not_deployed' as const,
    createdAt: '2026-09-28T00:00:00.000Z',
    updatedAt: '2026-09-28T00:00:00.000Z',
    version: 0,
  };
}

function serverFlow(id: string) {
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
      viewport: { x: 0, y: 0, zoom: 1 },
      metadata: { author: 'owner', tags: [], awsRegion: 'us-east-1', deploymentStatus: 'not_deployed' as const },
      governance: createEmptyDeploymentGovernance(),
      createdAt: '2026-09-28T00:00:00Z',
      updatedAt: '2026-09-28T00:00:00Z',
    },
    deploymentStatus: 'not_deployed' as const,
    createdAt: '2026-09-28T00:00:00Z',
    updatedAt: '2026-09-28T00:00:00Z',
    version: 0,
  };
}

function editViewport(x: number): void {
  const viewport = useWorkflowStore.getState().viewport;
  useWorkflowStore.getState().setViewport({ ...viewport, x });
}

function hydrate(flowId: string): void {
  useWorkflowStore.getState().replaceWorkflowDocument(
    { nodes: [], edges: [], viewport: { x: 0, y: 0, zoom: 1 }, governance: createEmptyDeploymentGovernance() },
    { flowId, markDirty: false },
  );
}

describe('useAutoSave flushes the debounced edit (F-14)', () => {
  // Typed against the store's own signature: `tsc -b` (the build gate) type-checks test files too,
  // and an untyped vi.fn() is not assignable to the store slot (certification #35, 2026-09-30).
  let saveFlow: ReturnType<typeof vi.fn<SaveFlow>>;

  beforeEach(() => {
    vi.useFakeTimers();
    resetPendingSaveControllerForTests();
    getFlowApi.mockReset();
    createFlowApi.mockReset();
    saveFlow = vi.fn<SaveFlow>().mockResolvedValue(undefined);
    useFlowStore.setState({
      flows: [summary(FLOW_A), summary(FLOW_B)],
      activeFlowId: FLOW_A,
      activeFlowName: FLOW_A,
      activeFlowVersion: 0,
      saveConflict: null,
      isLoading: false,
      error: null,
      pendingFlowId: null,
      saveFlow,
    });
    hydrate(FLOW_A);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    resetPendingSaveControllerForTests();
  });

  it('flow switch: the edit made 1 s before opening another flow is saved under the first flow, before the second hydrates', async () => {
    getFlowApi.mockResolvedValue(serverFlow(FLOW_B));
    const { rerender } = renderHook(({ flowId }) => useAutoSave(flowId, SAVE_INTERVAL_MS), {
      initialProps: { flowId: FLOW_A as string | null },
    });

    act(() => editViewport(7));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(saveFlow).not.toHaveBeenCalled();
    expect(hasUnsavedWork()).toBe(true);

    await act(async () => {
      await useFlowStore.getState().openFlow(FLOW_B);
    });
    rerender({ flowId: FLOW_B });

    expect(saveFlow).toHaveBeenCalledTimes(1);
    const [savedId, payload] = saveFlow.mock.calls[0];
    expect(savedId).toBe(FLOW_A);
    expect(payload.viewport.x).toBe(7);
    // The flush completed before the GET: the save was filed while A was active.
    expect(saveFlow.mock.invocationCallOrder[0]).toBeLessThan(getFlowApi.mock.invocationCallOrder[0]);
    expect(useFlowStore.getState().activeFlowId).toBe(FLOW_B);
    expect(useWorkflowStore.getState().documentFlowId).toBe(FLOW_B);
    expect(useFlowStore.getState().error).toBeNull();
  });

  it('flow switch: a flush that fails refuses the switch and keeps the canvas, so nothing is lost', async () => {
    saveFlow.mockRejectedValueOnce(new Error('autosave endpoint unavailable'));
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    getFlowApi.mockResolvedValue(serverFlow(FLOW_B));
    renderHook(() => useAutoSave(FLOW_A, SAVE_INTERVAL_MS));

    act(() => editViewport(11));
    await act(async () => {
      await useFlowStore.getState().openFlow(FLOW_B);
    });

    expect(saveFlow).toHaveBeenCalledTimes(1);
    expect(getFlowApi).not.toHaveBeenCalled();
    expect(useFlowStore.getState().activeFlowId).toBe(FLOW_A);
    expect(useFlowStore.getState().error).toBe(PENDING_SAVE_NOT_FLUSHED_ERROR);
    expect(useWorkflowStore.getState().viewport.x).toBe(11);
    expect(hasUnsavedWork()).toBe(true);

    // An explicit discard is the only way past it.
    await act(async () => {
      await useFlowStore.getState().openFlow(FLOW_B, { discardUnsaved: true });
    });
    expect(useFlowStore.getState().activeFlowId).toBe(FLOW_B);
  });

  it('flow create: the pending edit is saved under the current flow before the new empty flow hydrates', async () => {
    createFlowApi.mockResolvedValue({ flow: serverFlow('flow-new') });
    renderHook(() => useAutoSave(FLOW_A, SAVE_INTERVAL_MS));

    act(() => editViewport(3));
    await act(async () => {
      await useFlowStore.getState().createFlow('Untitled Flow');
    });

    expect(saveFlow).toHaveBeenCalledTimes(1);
    expect(saveFlow.mock.calls[0][0]).toBe(FLOW_A);
    expect(saveFlow.mock.calls[0][1].viewport.x).toBe(3);
    expect(saveFlow.mock.invocationCallOrder[0]).toBeLessThan(createFlowApi.mock.invocationCallOrder[0]);
    expect(useFlowStore.getState().activeFlowId).toBe('flow-new');
  });

  it('sign-out: the pending edit is saved while the session is still valid, then sign-out proceeds', async () => {
    const amplifySignOut = vi.fn().mockResolvedValue(undefined);
    const confirm = vi.fn();
    renderHook(() => useAutoSave(FLOW_A, SAVE_INTERVAL_MS));

    act(() => editViewport(5));
    let signedOut: boolean | undefined;
    await act(async () => {
      signedOut = await signOutAfterFlush({ signOut: amplifySignOut, confirm });
    });

    expect(signedOut).toBe(true);
    expect(saveFlow).toHaveBeenCalledTimes(1);
    expect(saveFlow.mock.calls[0][1].viewport.x).toBe(5);
    expect(saveFlow.mock.invocationCallOrder[0]).toBeLessThan(amplifySignOut.mock.invocationCallOrder[0]);
    expect(confirm).not.toHaveBeenCalled();
  });

  it('sign-out: when the flush fails the user is asked, and declining keeps them signed in', async () => {
    saveFlow.mockRejectedValueOnce(new Error('autosave endpoint unavailable'));
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const amplifySignOut = vi.fn().mockResolvedValue(undefined);
    const confirm = vi.fn().mockReturnValue(false);
    renderHook(() => useAutoSave(FLOW_A, SAVE_INTERVAL_MS));

    act(() => editViewport(9));
    let signedOut: boolean | undefined;
    await act(async () => {
      signedOut = await signOutAfterFlush({ signOut: amplifySignOut, confirm });
    });

    expect(confirm).toHaveBeenCalledWith(UNSAVED_SIGN_OUT_PROMPT);
    expect(signedOut).toBe(false);
    expect(amplifySignOut).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    await act(async () => {
      signedOut = await signOutAfterFlush({ signOut: amplifySignOut, confirm });
    });
    expect(signedOut).toBe(true);
    expect(amplifySignOut).toHaveBeenCalledTimes(1);
  });

  it('tab close: pagehide fires the pending edit as a keepalive save, and beforeunload prompts while work is unsaved', async () => {
    renderHook(() => useAutoSave(FLOW_A, SAVE_INTERVAL_MS));

    // Nothing pending: the browser must not prompt.
    const quietLeave = new Event('beforeunload', { cancelable: true });
    act(() => {
      window.dispatchEvent(quietLeave);
    });
    expect(quietLeave.defaultPrevented).toBe(false);

    act(() => editViewport(13));
    const leave = new Event('beforeunload', { cancelable: true });
    act(() => {
      window.dispatchEvent(leave);
    });
    expect(leave.defaultPrevented).toBe(true);

    act(() => {
      window.dispatchEvent(new Event('pagehide'));
    });
    expect(saveFlow).toHaveBeenCalledTimes(1);
    expect(saveFlow.mock.calls[0][0]).toBe(FLOW_A);
    expect(saveFlow.mock.calls[0][1].viewport.x).toBe(13);
    expect(saveFlow.mock.calls[0][2]).toEqual({ keepalive: true });

    // The timer was consumed by pagehide: the interval elapsing must not save twice.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SAVE_INTERVAL_MS);
    });
    expect(saveFlow).toHaveBeenCalledTimes(1);
  });

  it('a hydration that was not preceded by a flush still cancels the stale timer (the fence from the earlier fix)', async () => {
    renderHook(() => useAutoSave(FLOW_A, SAVE_INTERVAL_MS));
    act(() => editViewport(2));
    act(() => {
      useFlowStore.setState({ activeFlowId: FLOW_B });
      hydrate(FLOW_B);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SAVE_INTERVAL_MS);
    });
    expect(saveFlow).not.toHaveBeenCalled();
  });
});
