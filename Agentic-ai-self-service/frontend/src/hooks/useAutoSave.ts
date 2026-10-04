/**
 * Hook for auto-saving the active flow workflow at a debounced interval.
 * Requirements: 6.1, 6.2, 6.3
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useWorkflowStore } from '../store/workflowStore';
import { useFlowStore } from '../store/flowStore';
import { buildFlowSavePayload } from '../utils/flowSavePayload';
import { registerPendingSaveController } from '../utils/pendingSave';

/**
 * Return value of {@link useAutoSave}.
 *
 * Audit issue #8: previously the hook returned `void` and silently swallowed
 * save errors (only flowStore.error got set, which is overwritten by every
 * other flow operation). Consumers can now read `lastSaveError` to render
 * an autosave-specific banner/toast, and call `clearLastSaveError()` to
 * dismiss it.
 */
export interface UseAutoSaveResult {
  lastSaveError: Error | null;
  clearLastSaveError: () => void;
}

const DEFAULT_INTERVAL = 5000;

/**
 * Subscribes directly to workflowStore changes and debounces saves
 * to flowStore.saveFlow() when an active flow is set.
 * Converts React Flow nodes/edges to backend-compatible format with snake_case keys.
 *
 * The debounced edit is never simply dropped (F-14). The hook registers a
 * pending-save controller so that a flow switch, a flow create and sign-out
 * flush it first (flowStore / signOutAfterFlush call `flushPendingSave`), a
 * `pagehide` fires it with a keepalive request, and `beforeunload` shows the
 * browser's leave-page prompt while work is unsaved. A hydration that arrives
 * WITHOUT a preceding flush still cancels the timer: at that point the canvas
 * already holds the other flow, so there is nothing correct left to save.
 *
 * Returns {@link UseAutoSaveResult} so callers can render autosave-specific
 * error UI. Backwards-compatible: existing callers that ignore the return
 * value still work.
 */
export function useAutoSave(
  flowId: string | null,
  interval: number = DEFAULT_INTERVAL,
): UseAutoSaveResult {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The body of the pending timer, kept so a flush can run it before the
  // interval elapses. Null when no edit is debounced.
  const pendingFireRef = useRef<((options?: { keepalive?: boolean }) => void) | null>(null);
  // Serialize writes. Without this, a slow older request can finish after a
  // newer request and overwrite the server with stale canvas/governance state.
  const saveQueueRef = useRef<Promise<boolean>>(Promise.resolve(true));
  const inFlightRef = useRef(0);
  const lastSaveFailedRef = useRef(false);

  const [lastSaveError, setLastSaveError] = useState<Error | null>(null);
  const clearLastSaveError = useCallback(() => setLastSaveError(null), []);

  useEffect(() => {
    const clearPendingTimer = () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      pendingFireRef.current = null;
    };

    // No active persisted document means there is nothing this hook may write.
    if (!flowId) {
      clearPendingTimer();
      return undefined;
    }

    const enqueue = (workflow: unknown, options?: { keepalive?: boolean }): Promise<boolean> => {
      const { saveFlow } = useFlowStore.getState();
      const persist = async (): Promise<boolean> => {
        inFlightRef.current += 1;
        try {
          await saveFlow(flowId, workflow as never, options);
          lastSaveFailedRef.current = false;
          // Successful save: clear any previously surfaced auto-save error
          // so a transient network blip doesn't leave a stale banner.
          setLastSaveError((prev) => (prev === null ? prev : null));
          return true;
        } catch (err: unknown) {
          // Audit issue #8: surface auto-save failures to the consumer
          // so it can render a dedicated toast/banner. flowStore.error
          // is shared with every other flow operation and gets clobbered;
          // this state is autosave-specific.
          const error = err instanceof Error ? err : new Error(String(err));
          // Log so the failure is also visible in dev tools / observability.
          console.error('[useAutoSave] flow save failed', error);
          lastSaveFailedRef.current = true;
          setLastSaveError(error);
          return false;
        } finally {
          inFlightRef.current -= 1;
        }
      };
      // Start synchronously when the queue is idle: on `pagehide` this gets the
      // request (and its token read) under way inside the event handler, before
      // the page's task queue is torn down. Otherwise chain behind the save in
      // flight so an older snapshot can never overtake a newer one.
      saveQueueRef.current = inFlightRef.current === 0
        ? persist()
        : saveQueueRef.current.then(persist, persist);
      return saveQueueRef.current;
    };

    // The canvas as it is right now, or null when this hook may not write it.
    const currentSnapshot = () => {
      const workflowState = useWorkflowStore.getState();
      const { activeFlowId } = useFlowStore.getState();
      if (activeFlowId !== flowId || workflowState.documentFlowId !== flowId) return null;
      return buildFlowSavePayload(flowId, workflowState);
    };

    const hasUnsavedWork = () =>
      pendingFireRef.current !== null || inFlightRef.current > 0 || lastSaveFailedRef.current;

    const controller = {
      hasUnsavedWork,
      flush: async (): Promise<boolean> => {
        const fire = pendingFireRef.current;
        if (fire) {
          clearPendingTimer();
          fire();
        } else if (lastSaveFailedRef.current) {
          // Nothing debounced, but the last attempt failed: the edit is still
          // only in the browser, so try once more before letting go of it.
          const workflow = currentSnapshot();
          if (workflow) enqueue(workflow);
        }
        return saveQueueRef.current;
      },
      saveNow: async (): Promise<boolean> => {
        clearPendingTimer();
        const workflow = currentSnapshot();
        if (!workflow) return false;
        return enqueue(workflow);
      },
    };
    const unregister = registerPendingSaveController(controller);

    const unsubscribe = useWorkflowStore.subscribe(
      (state, prevState) => {
        // Hydration is not a user edit. It also invalidates any timer captured for
        // the previous flow, which is the fence that prevents flow B from being
        // written into flow A after a fast switch.
        if (state.hydrationVersion !== prevState.hydrationVersion) {
          clearPendingTimer();
          return;
        }

        // persistenceRevision advances only for user-visible persisted state,
        // including governance-only edits. Validation/execution UI churn is ignored.
        if (state.persistenceRevision === prevState.persistenceRevision) return;

        const flowState = useFlowStore.getState();
        if (state.documentFlowId !== flowId || flowState.activeFlowId !== flowId) return;

        clearPendingTimer();
        const scheduledHydrationVersion = state.hydrationVersion;
        const scheduledPersistenceRevision = state.persistenceRevision;
        const fire = (options?: { keepalive?: boolean }) => {
          timerRef.current = null;
          pendingFireRef.current = null;
          const workflowState = useWorkflowStore.getState();
          const { activeFlowId } = useFlowStore.getState();
          if (activeFlowId !== flowId || workflowState.documentFlowId !== flowId) return;
          if (
            workflowState.hydrationVersion !== scheduledHydrationVersion
            || workflowState.persistenceRevision !== scheduledPersistenceRevision
          ) {
            return;
          }
          enqueue(buildFlowSavePayload(flowId, workflowState), options);
        };
        pendingFireRef.current = fire;
        timerRef.current = setTimeout(() => fire(), interval);
      }
    );

    // Tab close / navigation away. `pagehide` is the last reliable event: fire
    // the debounced edit now with a keepalive request so the browser lets it
    // finish after the page is gone. It goes to the same origin (/api) through
    // the same authFetch, so the bearer token travels exactly where it always
    // does. `beforeunload` shows the browser's own leave-page prompt while work
    // is unsaved, which covers the case where the keepalive request could not
    // be issued in time.
    const onPageHide = () => {
      const fire = pendingFireRef.current;
      if (!fire) return;
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      fire({ keepalive: true });
    };
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!hasUnsavedWork()) return;
      event.preventDefault();
      // Legacy browsers read returnValue; modern ones only need preventDefault.
      event.returnValue = '';
    };
    window.addEventListener('pagehide', onPageHide);
    window.addEventListener('beforeunload', onBeforeUnload);

    return () => {
      unsubscribe();
      window.removeEventListener('pagehide', onPageHide);
      window.removeEventListener('beforeunload', onBeforeUnload);
      unregister();
      // Unmounting with an edit still debounced: the flow is still active and
      // the canvas still holds the edit, so save it rather than drop it. The
      // fences inside `fire` keep this from writing a canvas that has already
      // been replaced.
      const fire = pendingFireRef.current;
      clearPendingTimer();
      if (fire) fire();
    };
  }, [flowId, interval]);

  return { lastSaveError, clearLastSaveError };
}
