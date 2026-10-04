/**
 * Sign out only after the active flow's pending autosave has been flushed (F-14).
 *
 * The autosave debounce is 5 s. Signing out inside that window used to unmount
 * the hook, which cleared the timer and dropped the edit with no toast. The
 * flush runs while the session token is still valid; if it fails, the user is
 * asked before their unsaved change is abandoned.
 */

import { signOut } from 'aws-amplify/auth';
import { flushPendingSave } from '../utils/pendingSave';

export const UNSAVED_SIGN_OUT_PROMPT =
  'Your latest changes could not be saved. Sign out anyway and lose them?';

export interface SignOutAfterFlushDeps {
  flush?: () => Promise<boolean>;
  signOut?: () => Promise<unknown>;
  confirm?: (message: string) => boolean;
}

/** Resolves true when the user was signed out, false when they chose to stay. */
export async function signOutAfterFlush(deps: SignOutAfterFlushDeps = {}): Promise<boolean> {
  const flush = deps.flush ?? flushPendingSave;
  const doSignOut = deps.signOut ?? signOut;
  const confirm = deps.confirm ?? ((message: string) => window.confirm(message));

  const flushed = await flush();
  if (!flushed && !confirm(UNSAVED_SIGN_OUT_PROMPT)) {
    return false;
  }
  await doSignOut();
  return true;
}
