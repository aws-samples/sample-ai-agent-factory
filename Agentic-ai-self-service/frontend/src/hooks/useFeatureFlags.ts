import { useCallback, useEffect, useState } from 'react';
import { authFetch } from '../auth/authFetch';
import { apiErrorFromResponse, getErrorMessage } from '../services/api/client';

/**
 * Platform feature flags, read once per signed-in session from GET /api/config/features.
 *
 * Twin of backend/src/app/services/feature_flags.py: a flag is on only when the backend says
 * literal `true` for a name this build knows. Everything else is off, including the whole set
 * while the request is loading or failed, so a flagged surface can never flash on, and a
 * backend outage hides optional surfaces instead of exposing them. The backend answers the
 * same 404 as a nonexistent route for a flagged-off route, so hiding the surface here is a
 * courtesy, never the control.
 */

/** Every flag this frontend build knows. Mirror of KNOWN_FEATURE_FLAGS in the backend. */
export const KNOWN_FEATURE_FLAGS = ['platformFlagsCanary', 'specV2Read'] as const;
export type FeatureFlagName = (typeof KNOWN_FEATURE_FLAGS)[number];

export type FeatureFlagSource = 'ssm' | 'env' | 'missing' | 'invalid' | 'error';

export interface FeatureFlags {
  features: Record<FeatureFlagName, boolean>;
  source: FeatureFlagSource;
  parameter: string;
  ignored: string[];
  ttlSeconds: number;
  refreshedAt: string;
  descriptions: Record<string, string>;
}

export type FeatureFlagsState =
  | { status: 'loading' }
  | { status: 'ready'; flags: FeatureFlags }
  | { status: 'error'; message: string };

const SOURCES: readonly FeatureFlagSource[] = ['ssm', 'env', 'missing', 'invalid', 'error'];

export function allFlagsOff(): Record<FeatureFlagName, boolean> {
  return Object.fromEntries(KNOWN_FEATURE_FLAGS.map((name) => [name, false])) as Record<
    FeatureFlagName,
    boolean
  >;
}

/**
 * The response, with unknown names dropped and non-boolean values read as off, or null when
 * the body is not a feature-flags response at all (a proxy error page, an older backend).
 */
export function parseFeatureFlags(value: unknown): FeatureFlags | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  const body = value as Record<string, unknown>;
  if (typeof body.features !== 'object' || body.features === null || Array.isArray(body.features)) {
    return null;
  }
  if (typeof body.source !== 'string' || !SOURCES.includes(body.source as FeatureFlagSource)) {
    return null;
  }
  const features = allFlagsOff();
  for (const name of KNOWN_FEATURE_FLAGS) {
    features[name] = (body.features as Record<string, unknown>)[name] === true;
  }
  const ignored = Array.isArray(body.ignored)
    ? body.ignored.filter((item): item is string => typeof item === 'string')
    : [];
  const descriptions =
    typeof body.descriptions === 'object' && body.descriptions !== null && !Array.isArray(body.descriptions)
      ? Object.fromEntries(
          Object.entries(body.descriptions as Record<string, unknown>).filter(
            (entry): entry is [string, string] => typeof entry[1] === 'string',
          ),
        )
      : {};
  return {
    features,
    source: body.source as FeatureFlagSource,
    parameter: typeof body.parameter === 'string' ? body.parameter : '',
    ignored,
    ttlSeconds: typeof body.ttlSeconds === 'number' && Number.isFinite(body.ttlSeconds) ? body.ttlSeconds : 0,
    refreshedAt: typeof body.refreshedAt === 'string' ? body.refreshedAt : '',
    descriptions,
  };
}

/** Whether `name` is on in `state`; off while loading, after an error, and for unknown names. */
export function isFeatureEnabled(state: FeatureFlagsState, name: FeatureFlagName): boolean {
  return state.status === 'ready' && state.flags.features[name] === true;
}

export function useFeatureFlags(apiBaseUrl = '') {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<FeatureFlagsState>({ status: 'loading' });

  useEffect(() => {
    let cancelled = false;
    setState({ status: 'loading' });

    void (async () => {
      try {
        const response = await authFetch(`${apiBaseUrl}/api/config/features`);
        if (!response.ok) {
          throw await apiErrorFromResponse(response);
        }
        const flags = parseFeatureFlags(await response.json());
        if (flags === null) {
          throw new Error('The platform returned an invalid feature-flags response.');
        }
        if (!cancelled) setState({ status: 'ready', flags });
      } catch (error) {
        if (!cancelled) setState({ status: 'error', message: getErrorMessage(error) });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [apiBaseUrl, attempt]);

  const refresh = useCallback(() => {
    setState({ status: 'loading' });
    setAttempt((current) => current + 1);
  }, []);

  const enabled = useCallback((name: FeatureFlagName) => isFeatureEnabled(state, name), [state]);

  return { state, enabled, refresh };
}
