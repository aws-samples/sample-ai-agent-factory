/**
 * When the agent list fails, the sidebar must say so — once, and without a spinner.
 *
 * `agents` starts as `null` and stays `null` when the fetch rejects, so the
 * "Loading…" branch (`agents === null`) stayed mounted next to the error. Driving
 * the deployed app with a real API Gateway 401 rendered both at once:
 *
 *     Your agents
 *     Agent list failed (401)
 *     Loading…
 *
 * A spinner that never resolves reads as "still working", which is the opposite of
 * what had happened, and it sat directly under the failure notice. The same shape
 * was fixed once before in EvaluationResultsPanel ("Loading evaluation config…"
 * forever), so this is the second instance of one pattern: a null sentinel used as
 * both "not fetched yet" and "fetch failed".
 *
 * These tests assert what the user sees, not the state variable, because the bug
 * was never in the state — `agentError` was set correctly all along.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const listMyAgentsApi = vi.fn();
const streamInvokeApi = vi.fn();

vi.mock('../../services/api', () => ({
  listMyAgentsApi: (...a: unknown[]) => listMyAgentsApi(...a),
  streamInvokeApi: (...a: unknown[]) => streamInvokeApi(...a),
  // The real one, so the 401 -> session-message substitution is exercised rather
  // than stubbed. Mocking it would let the assertions pass with the fix removed.
  getErrorMessage: (e: unknown) =>
    e && typeof e === 'object' && (e as { status?: number }).status === 401
      ? 'Your session has expired. Sign out and sign in again to continue.'
      : String((e as { message?: string })?.message ?? e),
}));

vi.mock('aws-amplify/auth', () => ({ signOut: vi.fn() }));

// jsdom implements no layout, so `Element.scrollTo` is simply absent and the
// component's scroll-to-bottom effect throws during mount. A harness gap, not a
// product defect — without this every assertion below fails for the wrong reason.
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = () => {};
}

const { ChatPage } = await import('./ChatPage');

afterEach(() => {
  listMyAgentsApi.mockReset();
  streamInvokeApi.mockReset();
});

describe('ChatPage when the agent list fails', () => {
  it('shows the error and no longer shows a spinner beside it', async () => {
    listMyAgentsApi.mockRejectedValue({ message: 'Unauthorized', status: 401 });
    render(<ChatPage />);

    await waitFor(() => expect(screen.getByText(/session has expired/i)).toBeTruthy());
    // The regression: this used to be present at the same time as the error.
    expect(screen.queryByText('Loading…')).toBeNull();
  });

  it('does not also claim the user has no agents', async () => {
    // `agents` is null, not [], so the empty state should not render either —
    // asserted so a fix that sets `agents = []` on failure (swapping one wrong
    // message for another) does not pass.
    listMyAgentsApi.mockRejectedValue({ message: 'Unauthorized', status: 401 });
    render(<ChatPage />);

    await waitFor(() => expect(screen.getByText(/session has expired/i)).toBeTruthy());
    expect(screen.queryByText('No deployed agents yet.')).toBeNull();
  });

  it('still shows the spinner while the fetch is genuinely in flight', async () => {
    // The negative control. Without it, `{false && …}` would pass both tests
    // above while removing the loading indicator entirely.
    listMyAgentsApi.mockReturnValue(new Promise(() => {}));
    render(<ChatPage />);

    expect(await screen.findByText('Loading…')).toBeTruthy();
    expect(screen.queryByText(/session has expired/i)).toBeNull();
    expect(screen.getByRole('textbox', { name: 'Message your agent' })).toBeDisabled();
  });

  it('shows the empty state, not the spinner, when the caller really has none', async () => {
    listMyAgentsApi.mockResolvedValue([]);
    render(<ChatPage />);

    expect(await screen.findByText('No deployed agents yet.')).toBeTruthy();
    expect(screen.queryByText('Loading…')).toBeNull();
    expect(screen.queryByText(/session has expired/i)).toBeNull();
  });

  it('lists the agents on success', async () => {
    listMyAgentsApi.mockResolvedValue([
      { deployment_id: 'd-1', runtime_id: 'rt-1', status: 'succeeded' },
    ]);
    render(<ChatPage />);

    // By role, not by text: on success the runtime name also appears in the chat
    // header ("Chat with rt-1"), so a bare text query matches two nodes and fails
    // for a reason that has nothing to do with the sidebar.
    expect(await screen.findByRole('button', { name: 'rt-1' })).toBeTruthy();
    expect(screen.queryByText('Loading…')).toBeNull();
  });
});
