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
  'g-admins-security': ['settings:read', 'settings:write', 'observability:read'],
  'g-admins-cost': ['cost:read', 'cost:write'],
  'g-users-default': ['invoke', 'agent:read', 'cost:read', 'prompt:read', 'registry:read'],
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

/**
 * React hook: resolve the caller's scopes from the ID token.
 * Local dev / auth failure → all scopes (matches backend local-dev full access).
 */
export function useScopes(): ScopeState {
  const [scopes, setScopes] = useState<Set<string>>(new Set(ALL_SCOPES));
  const [isTypeAdmin, setIsTypeAdmin] = useState(true);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const session = await fetchAuthSession();
        const idToken = session.tokens?.idToken;
        if (cancelled) return;
        if (!idToken) {
          // No token (local dev) → keep full access.
          setLoaded(true);
          return;
        }
        const groups = parseGroups(idToken.payload['cognito:groups']);
        setScopes(scopesFromGroups(groups));
        setIsTypeAdmin(groups.includes('t-admin') || groups.includes('org-admin'));
        setLoaded(true);
      } catch {
        // Auth failure / local dev → full access, matches backend.
        if (!cancelled) {
          setScopes(new Set(ALL_SCOPES));
          setIsTypeAdmin(true);
          setLoaded(true);
        }
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
