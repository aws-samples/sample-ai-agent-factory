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

/**
 * Extracts error message from any error type.
 */
export function getErrorMessage(error: unknown): string {
  if (isApiError(error)) {
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
 *  no versions/triggers/cost/dashboard recorded) — render an empty state. */
export function isNotReadyError(error: unknown): boolean {
  const s = getErrorStatus(error);
  return s === 401 || s === 403 || s === 404;
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
    let errorDetails: unknown;
    try {
      errorDetails = await response.json();
    } catch {
      errorDetails = await response.text();
    }

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
