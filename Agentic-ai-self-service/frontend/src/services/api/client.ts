/**
 * Shared API client infrastructure.
 * Provides authFetch wrapper, base URL resolution, error normalization.
 */

import { authFetch } from '../../auth/authFetch';

// ============================================================================
// Configuration
// ============================================================================

/**
 * Base URL for the backend API.
 * Can be configured via environment variable.
 */
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '';

// ============================================================================
// Types
// ============================================================================

export interface ApiError {
  message: string;
  status: number;
  details?: unknown;
}

// ============================================================================
// Error Handling
// ============================================================================

/**
 * Type guard to check if an error is an ApiError.
 */
export function isApiError(error: unknown): error is ApiError {
  return (
    typeof error === 'object' &&
    error !== null &&
    'message' in error &&
    'status' in error &&
    typeof (error as ApiError).message === 'string' &&
    typeof (error as ApiError).status === 'number'
  );
}

/** The message shown for a 401, in place of the API's bare "Unauthorized". */
export const SESSION_EXPIRED_MESSAGE =
  'Your session has expired. Sign out and sign in again to continue.';

/**
 * Extracts error message from any error type.
 */
export function getErrorMessage(error: unknown): string {
  if (isApiError(error)) {
    // A 401 is always an authentication failure, never a data condition, and the
    // two sources of one both say so uselessly: API Gateway's JWT authorizer
    // answers `{"message":"Unauthorized"}` before the Lambda is reached, and the
    // backend's single 401 site says "Caller identity not available". Neither
    // tells the user that the fix is to sign in again, so say it here rather than
    // in each of the panels that render this string.
    if (error.status === 401) {
      return SESSION_EXPIRED_MESSAGE;
    }
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  if (typeof error === 'string') {
    return error;
  }
  return 'An unknown error occurred';
}

/** HTTP status of an ApiError, or 0. */
export function getErrorStatus(error: unknown): number {
  if (isApiError(error)) {
    return error.status ?? 0;
  }
  return 0;
}

/** True when the error means "this runtime has no data yet" (not deployed, or
 *  no versions/triggers/cost/dashboard recorded) — render an empty state.
 *
 *  401 is deliberately NOT in this set, though it used to be. Nothing returns 401
 *  for "no data yet": the backend has exactly one 401 site
 *  (`services/auth.py` — "Caller identity not available", raised when running in
 *  Lambda with no authorizer claim) and API Gateway's JWT authorizer returns 401
 *  for a missing, malformed or expired token — verified live, an invalid bearer
 *  and no bearer at all both answer `401 {"message":"Unauthorized"}`. So
 *  swallowing 401 never protected an undeployed-runtime empty state; it only hid
 *  dead sessions, and every panel that used this helper rendered a calm "nothing
 *  configured" for a user who had actually been signed out. `fetchAuthSession`
 *  auto-refreshes, so this needs a revoked or expired *refresh* token to happen —
 *  rare, but then indistinguishable from the feature being switched off.
 *
 *  403 stays: the session is valid and the caller simply lacks the scope, which is
 *  a legitimate "there is nothing here for you" for these read-only panels. */
export function isNotReadyError(error: unknown): boolean {
  const s = getErrorStatus(error);
  return s === 403 || s === 404;
}

/**
 * Extracts error message from details object with various shapes.
 */
function extractErrorMessage(details: unknown, fallback: string): string {
  if (typeof details === 'string') {
    return details;
  }
  if (typeof details === 'object' && details !== null) {
    const obj = details as Record<string, unknown>;
    if (typeof obj.detail === 'string') {
      return obj.detail;
    }
    if (typeof obj.message === 'string') {
      return obj.message;
    }
    if (typeof obj.detail === 'object' && obj.detail !== null) {
      const detailObj = obj.detail as Record<string, unknown>;
      if (typeof detailObj.message === 'string') {
        return detailObj.message;
      }
      if (Array.isArray(detailObj.errors)) {
        return detailObj.errors.join(', ');
      }
    }
  }
  return fallback;
}

/**
 * Read a response body exactly once, then decode JSON when possible.
 *
 * `Response.json()` consumes the body even when JSON parsing fails. Falling
 * back to `response.text()` after that failure therefore throws "body stream
 * already read" and hides the server's real response. Error paths use this
 * helper so malformed JSON, proxy HTML, and plain-text errors remain
 * inspectable without attempting a second read.
 */
async function readResponseBody(response: Response): Promise<unknown> {
  const text = await response.text().catch(() => '');
  if (!text) return '';

  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/**
 * Builds a normalized `ApiError` from a failed `Response`.
 *
 * Exported for the callers that cannot go through `apiRequest` — `streamInvoke`
 * needs the raw `Response` to read an SSE body off it. Those callers used to
 * `throw new Error(...)` instead, which produced an error with no `status`, so
 * `getErrorMessage` fell through to the raw message and `isNotReadyError` saw 0.
 * A signed-out user was told "Agent list failed (401)" — observed live in the
 * chat sidebar. Anything that throws on a bad response must throw this shape, or
 * every status-aware behaviour in this module silently does not apply to it.
 */
export async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  const details = await readResponseBody(response);
  return {
    message: extractErrorMessage(details, response.statusText),
    status: response.status,
    details,
  };
}

// ============================================================================
// Request Helper
// ============================================================================

/**
 * Performs an authenticated API request.
 * Handles JSON serialization/deserialization and error normalization.
 */
export async function apiRequest<T>(
  endpoint: string,
  options: RequestInit = {},
  baseUrl: string = API_BASE_URL
): Promise<T> {
  const url = `${baseUrl}${endpoint}`;

  const defaultHeaders: HeadersInit = {
    'Content-Type': 'application/json',
  };

  const response = await authFetch(url, {
    ...options,
    headers: {
      ...defaultHeaders,
      ...options.headers,
    },
  });

  if (!response.ok) {
    const errorDetails = await readResponseBody(response);

    const error: ApiError = {
      message: extractErrorMessage(errorDetails, response.statusText),
      status: response.status,
      details: errorDetails,
    };
    throw error;
  }

  // Guard against non-JSON responses (e.g., CloudFront returning HTML for 404s)
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) {
    const text = await response.text();
    const error: ApiError = {
      message: 'Unexpected response from server',
      status: response.status,
      details: text,
    };
    throw error;
  }

  return response.json() as Promise<T>;
}
