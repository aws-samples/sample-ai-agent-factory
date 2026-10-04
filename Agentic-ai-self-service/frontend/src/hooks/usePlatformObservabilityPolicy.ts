import { useCallback, useEffect, useState } from 'react';
import { authFetch } from '../auth/authFetch';
import {
  apiErrorFromResponse,
  getErrorMessage,
} from '../services/api/client';

export interface PlatformObservabilityDefaults {
  enabled: boolean;
  endpoint?: string;
  sample_rate?: number;
  service_name_prefix?: string;
}

export type PlatformObservabilityPolicyState =
  | { status: 'loading' }
  | {
      status: 'ready';
      defaults: PlatformObservabilityDefaults;
    }
  | {
      status: 'error';
      message: string;
    };

// The route's response model declares every field but `enabled` optional, and serializes an unset one as
// JSON null. Measured live 2026-10-02 on a platform without OTEL: {"enabled": false, "endpoint": null,
// "sample_rate": null, "service_name_prefix": null}. This used to accept a field only when ABSENT, so it read
// those nulls as an unreadable policy and kept Save disabled on every Runtime and Observability modal: no
// runtime could be configured from the palette. null and absent both mean unset; a wrong type still fails.
const isUnset = (value: unknown) => value === undefined || value === null;

/** The defaults, with null optionals normalized to absent, or null when the response is not a policy. */
export function parsePlatformObservabilityDefaults(
  value: unknown,
): PlatformObservabilityDefaults | null {
  if (typeof value !== 'object' || value === null) return null;

  const defaults = value as Record<string, unknown>;
  const valid =
    typeof defaults.enabled === 'boolean' &&
    (isUnset(defaults.endpoint) || typeof defaults.endpoint === 'string') &&
    (isUnset(defaults.sample_rate) ||
      (typeof defaults.sample_rate === 'number' &&
        Number.isFinite(defaults.sample_rate))) &&
    (isUnset(defaults.service_name_prefix) ||
      typeof defaults.service_name_prefix === 'string');
  if (!valid) return null;
  return {
    enabled: defaults.enabled as boolean,
    ...(isUnset(defaults.endpoint) ? {} : { endpoint: defaults.endpoint as string }),
    ...(isUnset(defaults.sample_rate) ? {} : { sample_rate: defaults.sample_rate as number }),
    ...(isUnset(defaults.service_name_prefix)
      ? {}
      : { service_name_prefix: defaults.service_name_prefix as string }),
  };
}

export function usePlatformObservabilityPolicy(
  isOpen: boolean,
  apiBaseUrl = '',
) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] =
    useState<PlatformObservabilityPolicyState>({ status: 'loading' });

  useEffect(() => {
    if (!isOpen) {
      setState({ status: 'loading' });
      return;
    }

    let cancelled = false;
    setState({ status: 'loading' });

    void (async () => {
      try {
        const response = await authFetch(
          `${apiBaseUrl}/api/observability/platform-defaults`,
        );
        if (!response.ok) {
          throw await apiErrorFromResponse(response);
        }

        const defaults = parsePlatformObservabilityDefaults(await response.json());
        if (defaults === null) {
          throw new Error(
            'The platform returned an invalid observability policy response.',
          );
        }

        if (!cancelled) {
          setState({ status: 'ready', defaults });
        }
      } catch (error) {
        if (!cancelled) {
          setState({ status: 'error', message: getErrorMessage(error) });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [apiBaseUrl, attempt, isOpen]);

  const retry = useCallback(() => {
    setState({ status: 'loading' });
    setAttempt((current) => current + 1);
  }, []);

  return { state, retry };
}
