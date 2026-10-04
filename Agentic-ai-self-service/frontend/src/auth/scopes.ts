/**
 * Scope-based RBAC for the UI — mirrors backend services/rbac.py.
 *
 * Reads `cognito:groups` from the ID token, maps groups → scopes using the
 * SAME table as the backend, and exposes `hasScope()` so components can hide
 * or disable actions the caller can't perform. This is a UX affordance ONLY —
 * the backend `require_scopes()` dependency is the real enforcement boundary.
 * Keep GROUP_SCOPES in sync with backend/src/app/services/rbac.py.
 */

import { useState, useEffect } from 'react';
import { fetchAuthSession } from 'aws-amplify/auth';

const RESOURCES = [
  'agent', 'registry', 'prompt', 'tag', 'cost', 'eval',
  'workspace', 'connector', 'trigger', 'hitl', 'observability', 'settings',
] as const;

export const ALL_SCOPES: string[] = [
  'invoke', 'admin',
  ...RESOURCES.map((r) => `${r}:read`),
  ...RESOURCES.map((r) => `${r}:write`),
];

const allReadWrite = () => RESOURCES.flatMap((r) => [`${r}:read`, `${r}:write`]);
const allRead = () => RESOURCES.map((r) => `${r}:read`);

// Group → scopes. MUST match backend GROUP_SCOPES.
const GROUP_SCOPES: Record<string, string[]> = {
  'g-admins-super': ['admin', 'invoke', ...allReadWrite()],
  'g-admins-registry': ['registry:read', 'registry:write'],
  'g-admins-security': [
    'settings:read',
    'settings:write',
    'observability:read',
    'tag:read',
    'tag:write',
  ],
  'g-admins-cost': ['cost:read', 'cost:write'],
  'registry-developer': ['registry:read', 'registry:write'],
  'g-users-default': [
    'invoke',
    'agent:read',
    'agent:write',
    'cost:read',
    'prompt:read',
    'registry:read',
    'tag:read',
  ],
  // Legacy groups (backward compatible)
  'org-admin': ['admin', 'invoke', ...allReadWrite()],
  'registry-admin': ['registry:read', 'registry:write'],
  editor: ['invoke', ...allReadWrite()],
  viewer: ['invoke', ...allRead()],
};

/** Parse the cognito:groups claim (array | JSON string | delimited string). */
function parseGroups(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw as string[];
  if (typeof raw === 'string' && raw) {
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return parsed as string[];
    } catch {
      /* not JSON */
    }
    return raw.replace(/[[\]]/g, '').split(/[,\s]+/).map((g) => g.trim()).filter(Boolean);
  }
  return [];
}

function scopesFromGroups(groups: string[]): Set<string> {
  const held = new Set<string>();
  for (const g of groups) (GROUP_SCOPES[g] ?? []).forEach((s) => held.add(s));
  if (held.has('admin')) return new Set(ALL_SCOPES); // admin implies all
  return held;
}

export interface ScopeState {
  scopes: Set<string>;
  isTypeAdmin: boolean; // t-admin drives which UI sections show
  loaded: boolean;
  hasScope: (...required: string[]) => boolean;
}

export type PersonaRoute = 'loading' | 'builder' | 'chat' | 'denied';

/**
 * Resolve the application surface from capabilities, not from a type label
 * alone. A standard provisioned user has agent:write and therefore belongs in
 * the builder; read+invoke-only users get Chat; unknown/zero-scope identities
 * get an explicit denial instead of being mistaken for an end-user persona.
 */
export function resolvePersonaRoute({
  loaded,
  scopes,
  isTypeAdmin,
  previewAsEndUser,
}: {
  loaded: boolean;
  scopes: ReadonlySet<string>;
  isTypeAdmin: boolean;
  previewAsEndUser: boolean;
}): PersonaRoute {
  if (!loaded) return 'loading';
  if (isTypeAdmin && previewAsEndUser) return 'chat';
  if (isTypeAdmin || scopes.has('agent:write')) return 'builder';
  if (scopes.has('agent:read') && scopes.has('invoke')) return 'chat';
  return 'denied';
}

/**
 * True when this build has a Cognito user pool configured, i.e. it is the deployed
 * app rather than a local `npm run dev` with no auth.
 *
 * `main.tsx:13` reads the SAME variable to decide whether to mount `AuthWrapper` at
 * all, so inside `App` the two are equivalent: if a pool is configured, the only
 * reason a session lookup fails is that the session is broken — never "local dev".
 * Treating those two as one value is what made `isTypeAdmin` default to `true` on a
 * failure, which is the wrong direction for an identity decision.
 */
const AUTH_CONFIGURED = Boolean(import.meta.env.VITE_COGNITO_USER_POOL_ID);

/**
 * React hook: resolve the caller's scopes from the ID token.
 *
 * Local dev (no user pool configured) → all scopes, matching the backend's
 * `_LOCAL_DEV_SUB` full-access path. A *failed* lookup in a build that HAS a pool
 * configured is a dead session, and resolves to no scopes and `isTypeAdmin: false`
 * — the least-privileged UI. Non-admins land on the chat page, which reports the
 * expired session; an admin sees the same rather than a builder whose every call
 * 401s. This is still only a UX affordance: `require_scopes()` on the backend is the
 * enforcement boundary either way, and it rejects a claimless caller with 401
 * (`services/auth.py:65`) rather than falling back to anything.
 */
export function useScopes(): ScopeState {
  const [scopes, setScopes] = useState<Set<string>>(
    () => new Set(AUTH_CONFIGURED ? [] : ALL_SCOPES),
  );
  const [isTypeAdmin, setIsTypeAdmin] = useState(!AUTH_CONFIGURED);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // Deny for a configured build, full access only when there is genuinely no auth.
    const resolveUnknown = () => {
      if (cancelled) return;
      if (AUTH_CONFIGURED) {
        setScopes(new Set<string>());
        setIsTypeAdmin(false);
      } else {
        setScopes(new Set(ALL_SCOPES));
        setIsTypeAdmin(true);
      }
      setLoaded(true);
    };
    (async () => {
      try {
        const session = await fetchAuthSession();
        const idToken = session.tokens?.idToken;
        if (cancelled) return;
        if (!idToken) {
          // Signed in with no ID token is not a state a configured build has a
          // group claim for, so it is resolved the same way as a throw.
          resolveUnknown();
          return;
        }
        const groups = parseGroups(idToken.payload['cognito:groups']);
        setScopes(scopesFromGroups(groups));
        setIsTypeAdmin(groups.includes('t-admin') || groups.includes('org-admin'));
        setLoaded(true);
      } catch {
        resolveUnknown();
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const hasScope = (...required: string[]) => {
    if (scopes.has('admin')) return true;
    return required.every((s) => scopes.has(s));
  };

  return { scopes, isTypeAdmin, loaded, hasScope };
}
