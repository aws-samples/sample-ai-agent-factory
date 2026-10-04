/**
 * A dead session must not look like a switched-off feature.
 *
 * Seven components route their fetch failures through `isNotReadyError` to decide
 * between a calm empty state and a visible error. It used to return true for 401,
 * on the stated belief that "a not-yet-deployed runtime returns 401/403/404".
 * That belief is wrong about the 401, and it was verified both ways:
 *
 *   - The backend has exactly ONE 401 site, `services/auth.py` — "Caller identity
 *     not available", raised when running in Lambda with no authorizer claim.
 *     No handler returns 401 for a missing runtime, version, config or slot;
 *     those are 404s with a `detail`.
 *   - API Gateway's JWT authorizer answers before the Lambda is reached. Probed
 *     live against the deployed API: an invalid bearer AND no Authorization
 *     header at all both return `401 {"message":"Unauthorized"}`.
 *
 * So swallowing 401 protected no empty state. It only hid signed-out sessions,
 * and each of those panels then rendered "nothing configured" to a user whose
 * session had expired — the one message guaranteed to send them looking for a
 * feature flag instead of the sign-in button.
 *
 * These tests exist because the fix is a single character class in one predicate
 * and reads like a tidy-up to remove. 403 must stay (valid session, missing
 * scope, genuinely nothing there for that caller) and 401 must not come back.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const { authFetchMock } = vi.hoisted(() => ({
  authFetchMock: vi.fn(),
}));

vi.mock('../../auth/authFetch', () => ({
  authFetch: authFetchMock,
}));

import {
  SESSION_EXPIRED_MESSAGE,
  apiErrorFromResponse,
  apiRequest,
  getErrorMessage,
  getErrorStatus,
  isApiError,
  isNotReadyError,
} from './client';

const apiError = (status: number, message = 'boom') => ({ message, status });

describe('isNotReadyError', () => {
  it('treats 404 as an empty state', () => {
    expect(isNotReadyError(apiError(404, 'No evaluation config found for this runtime'))).toBe(true);
  });

  it('treats 403 as an empty state, because the session is still valid', () => {
    // The caller authenticated fine and simply lacks the scope. For these
    // read-only panels "nothing here for you" is the honest rendering.
    expect(isNotReadyError(apiError(403, 'Missing required scope'))).toBe(true);
  });

  it('does NOT treat 401 as an empty state', () => {
    // The regression this file exists for. Both real 401 bodies are checked so
    // that neither source of one can be reclassified by accident.
    expect(isNotReadyError(apiError(401, 'Unauthorized'))).toBe(false);
    expect(isNotReadyError(apiError(401, 'Caller identity not available'))).toBe(false);
  });

  it('does not treat a server error as an empty state', () => {
    // A negative control: if this ever passed, the predicate would be returning
    // true for everything and the 404/403 cases above would prove nothing.
    expect(isNotReadyError(apiError(500, 'Internal Server Error'))).toBe(false);
    expect(isNotReadyError(apiError(502, 'Bad Gateway'))).toBe(false);
  });

  it('does not treat a non-API error as an empty state', () => {
    // A thrown TypeError from a network failure has no status; guessing "not
    // ready" for it would hide genuine breakage behind an empty panel.
    expect(isNotReadyError(new TypeError('Failed to fetch'))).toBe(false);
    expect(isNotReadyError(undefined)).toBe(false);
  });
});

describe('getErrorMessage for a 401', () => {
  it('replaces the useless wire message with one naming the remedy', () => {
    // Both upstream 401 bodies say nothing actionable. The panels render this
    // string directly, so the substitution has to happen here rather than in
    // seven components.
    expect(getErrorMessage(apiError(401, 'Unauthorized'))).toBe(SESSION_EXPIRED_MESSAGE);
    expect(getErrorMessage(apiError(401, 'Caller identity not available'))).toBe(SESSION_EXPIRED_MESSAGE);
  });

  it('says what to actually do about it', () => {
    // Asserting the shape, not the wording: a message that fails to mention
    // signing in again leaves the user exactly where the old empty state did.
    expect(SESSION_EXPIRED_MESSAGE.toLowerCase()).toContain('sign in');
  });

  it('leaves every other status message alone', () => {
    // The 401 branch must not swallow messages the backend went to the trouble
    // of writing — a 404's `detail` is the only thing telling the user the
    // feature simply is not configured for this agent.
    expect(getErrorMessage(apiError(404, 'No evaluation config found for this runtime'))).toBe(
      'No evaluation config found for this runtime',
    );
    expect(getErrorMessage(apiError(403, 'Missing required scope: eval:read'))).toBe(
      'Missing required scope: eval:read',
    );
    expect(getErrorMessage(apiError(500, 'Internal Server Error'))).toBe('Internal Server Error');
  });

  it('still handles the non-API error shapes', () => {
    expect(getErrorMessage(new Error('network down'))).toBe('network down');
    expect(getErrorMessage('a bare string')).toBe('a bare string');
    expect(getErrorMessage(null)).toBe('An unknown error occurred');
  });
});

describe('the helpers these depend on', () => {
  // isNotReadyError reads the status through getErrorStatus, which reads it
  // through isApiError. If either stops recognising the error shape, every
  // assertion above passes for the wrong reason — 0 is not 401, 403 or 404, so
  // a broken type guard silently turns every case into "false".
  it('recognises the ApiError shape and reads its status', () => {
    expect(isApiError(apiError(401))).toBe(true);
    expect(getErrorStatus(apiError(401))).toBe(401);
    expect(getErrorStatus(apiError(404))).toBe(404);
  });

  it('reports 0 for something that is not an ApiError', () => {
    expect(isApiError(new Error('x'))).toBe(false);
    expect(getErrorStatus(new Error('x'))).toBe(0);
  });
});

describe('failed response body handling', () => {
  beforeEach(() => {
    authFetchMock.mockReset();
  });

  it('preserves a non-JSON API error without reading the body twice', async () => {
    authFetchMock.mockResolvedValue(
      new Response('upstream unavailable', {
        status: 502,
        statusText: 'Bad Gateway',
        headers: { 'content-type': 'application/json' },
      }),
    );

    await expect(apiRequest('/api/prompts')).rejects.toMatchObject({
      message: 'upstream unavailable',
      status: 502,
      details: 'upstream unavailable',
    });
  });

  it('normalizes a malformed JSON response for raw-response callers too', async () => {
    const error = await apiErrorFromResponse(
      new Response('{not-json', {
        status: 500,
        statusText: 'Internal Server Error',
        headers: { 'content-type': 'application/json' },
      }),
    );

    expect(error).toEqual({
      message: '{not-json',
      status: 500,
      details: '{not-json',
    });
  });
});
