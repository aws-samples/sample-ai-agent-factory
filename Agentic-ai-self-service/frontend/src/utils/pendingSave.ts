/**
 * Registry for the one autosave writer of the active flow (F-14).
 *
 * useAutoSave debounces edits for 5 s. Anything that replaces the canvas or ends
 * the session inside that window (flow switch, flow create, sign-out, tab close)
 * used to cancel the timer, discarding the edit with no toast. The store cannot
 * import the hook, so the hook registers a controller here and the store (and
 * the sign-out button) flush through it before they hydrate or leave.
 *
 * Exactly one controller is registered at a time: the hook owns the serialised
 * save queue, and every save, flushed or scheduled, must go through that queue
 * so an older snapshot can never overtake a newer one.
 */

export interface PendingSaveController {
  /** True while an edit is debounced or a save is in flight. */
  hasUnsavedWork(): boolean;
  /**
   * Save the debounced snapshot now (if any) and wait for the queue to drain.
   * Resolves true when nothing was pending or every pending save succeeded,
   * false when a save failed (the edit is still only in the browser).
   */
  flush(): Promise<boolean>;
  /**
   * Save the current canvas now through the queue, regardless of the debounce.
   * Used to re-submit after a version conflict the user chose to overwrite.
   */
  saveNow(): Promise<boolean>;
}

let controller: PendingSaveController | null = null;

export function registerPendingSaveController(next: PendingSaveController): () => void {
  controller = next;
  return () => {
    if (controller === next) controller = null;
  };
}

export function hasUnsavedWork(): boolean {
  return controller?.hasUnsavedWork() ?? false;
}

/** Flush the active flow's pending save. True when nothing is left unsaved. */
export async function flushPendingSave(): Promise<boolean> {
  if (!controller) return true;
  return controller.flush();
}

/** Save the active flow's canvas now. False when the save failed or there is no writer. */
export async function saveActiveFlowNow(): Promise<boolean> {
  if (!controller) return false;
  return controller.saveNow();
}

/** Test seam: forget the registered controller. */
export function resetPendingSaveControllerForTests(): void {
  controller = null;
}
