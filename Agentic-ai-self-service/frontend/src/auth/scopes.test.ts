/**
 * `useScopes` — what the UI decides when it cannot tell who the caller is.
 *
 * The hook used to resolve a failed session lookup to `ALL_SCOPES` and
 * `isTypeAdmin: true`, commented "Local dev / auth failure → full access". Those are
 * two different states sharing one answer: `App` only renders behind `AuthWrapper`,
 * and `main.tsx:13` only mounts `AuthWrapper` when `VITE_COGNITO_USER_POOL_ID` is
 * set — so in the deployed app a failed lookup is a dead session, and the fallback
 * pointed a group-less end user at the builder UI instead of the chat page that now
 * reports the expired session (the F-36 family: a failure rendered as a feature).
 *
 * Two live browser arms against the deployed app could NOT reach the fallback — a
 * corrupted stored ID token, and a forced-expired token with cognito-idp blocked;
 * Amplify's Authenticator gated first and rendered the sign-in form. So this is a
 * wrong default rather than an observed breakage, and these tests are the evidence
 * that it is now right: the browser cannot show what it refuses to render.
 *
 * `AUTH_CONFIGURED` is read once at module load, so every test stubs the env and
 * re-imports rather than mutating a live binding.
 */
import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const fetchAuthSession = vi.fn();
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: () => fetchAuthSession() }));

/** Re-import the module with `VITE_COGNITO_USER_POOL_ID` set or unset. */
async function loadScopes(poolId: string | undefined) {
  vi.resetModules();
  if (poolId === undefined) {
    vi.stubEnv('VITE_COGNITO_USER_POOL_ID', '');
  } else {
    vi.stubEnv('VITE_COGNITO_USER_POOL_ID', poolId);
  }
  return import('./scopes');
}

/** An ID token whose only interesting part is the groups claim. */
const tokenWithGroups = (groups: unknown) => ({
  tokens: { idToken: { payload: { 'cognito:groups': groups } } },
});

beforeEach(() => {
  fetchAuthSession.mockReset();
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('a session that cannot be resolved, in a build that HAS auth configured', () => {
  it('grants no scopes and is not a type admin when the session lookup throws', async () => {
    const { useScopes } = await loadScopes('us-east-1_NKN5eM40q');
    fetchAuthSession.mockRejectedValue(new Error('NotAuthorizedException: Refresh Token has expired'));

    const { result } = renderHook(() => useScopes());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    expect(result.current.isTypeAdmin).toBe(false);
    expect(result.current.scopes.size).toBe(0);
    // The specific one that matters: `admin` short-circuits `hasScope` entirely.
    expect(result.current.hasScope('admin')).toBe(false);
    expect(result.current.hasScope('agent:write')).toBe(false);
  });

  it('grants no scopes when the session resolves but carries no ID token', async () => {
    const { useScopes } = await loadScopes('us-east-1_NKN5eM40q');
    fetchAuthSession.mockResolvedValue({ tokens: {} });

    const { result } = renderHook(() => useScopes());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    expect(result.current.isTypeAdmin).toBe(false);
    expect(result.current.scopes.size).toBe(0);
  });

  it('does not report loaded=true while still resolving', async () => {
    // The negative control for the two above. `App.tsx:517` routes on
    // `scopesLoaded && !isTypeAdmin`, so a hook that flipped `loaded` early would
    // bounce an admin through the chat page on every mount — and both tests above
    // would still pass.
    const { useScopes } = await loadScopes('us-east-1_NKN5eM40q');
    fetchAuthSession.mockReturnValue(new Promise(() => {}));

    const { result } = renderHook(() => useScopes());
    await new Promise((r) => setTimeout(r, 20));
    expect(result.current.loaded).toBe(false);
    expect(result.current.scopes.size).toBe(0);
    expect(result.current.isTypeAdmin).toBe(false);
  });
});

describe('local dev, with no user pool configured', () => {
  it('keeps full access when the session lookup throws', async () => {
    // `App` is rendered without `AuthWrapper` in this build (main.tsx:31), so there
    // is no session to fail — and the backend grants `local-dev` full access to
    // match. Narrowing this would break `npm run dev`.
    const { useScopes, ALL_SCOPES } = await loadScopes(undefined);
    fetchAuthSession.mockRejectedValue(new Error('Auth UserPool not configured'));

    const { result } = renderHook(() => useScopes());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    expect(result.current.isTypeAdmin).toBe(true);
    expect(result.current.scopes.size).toBe(ALL_SCOPES.length);
    expect(result.current.hasScope('admin')).toBe(true);
  });

  it('keeps full access when there is no ID token', async () => {
    const { useScopes } = await loadScopes(undefined);
    fetchAuthSession.mockResolvedValue({ tokens: undefined });

    const { result } = renderHook(() => useScopes());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    expect(result.current.isTypeAdmin).toBe(true);
    expect(result.current.hasScope('admin')).toBe(true);
  });
});

describe('a real token still drives the groups table', () => {
  it('t-admin is a type admin', async () => {
    const { useScopes } = await loadScopes('us-east-1_NKN5eM40q');
    fetchAuthSession.mockResolvedValue(tokenWithGroups(['t-admin', 'g-users-default']));

    const { result } = renderHook(() => useScopes());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    expect(result.current.isTypeAdmin).toBe(true);
    expect(result.current.hasScope('invoke')).toBe(true);
  });

  it('a group-less user gets no scopes and is not a type admin', async () => {
    // The state the deployed probe user is actually in. It must be reached from a
    // *successful* lookup, so it cannot be confused with the failure path above.
    const { useScopes } = await loadScopes('us-east-1_NKN5eM40q');
    fetchAuthSession.mockResolvedValue(tokenWithGroups([]));

    const { result } = renderHook(() => useScopes());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    expect(result.current.isTypeAdmin).toBe(false);
    expect(result.current.scopes.size).toBe(0);
  });

  it('g-admins-super implies every scope', async () => {
    const { useScopes, ALL_SCOPES } = await loadScopes('us-east-1_NKN5eM40q');
    fetchAuthSession.mockResolvedValue(tokenWithGroups(['g-admins-super']));

    const { result } = renderHook(() => useScopes());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    expect(result.current.scopes.size).toBe(ALL_SCOPES.length);
    // `g-admins-super` is not a *type* group, so it does not by itself pick the
    // builder UI — only `t-admin`/`org-admin` do. Pinning this so the two axes
    // (which UI, which actions) do not get collapsed back into one.
    expect(result.current.isTypeAdmin).toBe(false);
    expect(result.current.hasScope('anything-at-all')).toBe(true);
  });

  it('parses the groups claim when Cognito sends it as a delimited string', async () => {
    const { useScopes } = await loadScopes('us-east-1_NKN5eM40q');
    fetchAuthSession.mockResolvedValue(tokenWithGroups('[t-admin g-users-default]'));

    const { result } = renderHook(() => useScopes());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    expect(result.current.isTypeAdmin).toBe(true);
    expect(result.current.hasScope('invoke')).toBe(true);
  });

  it('an unrecognized group grants nothing rather than defaulting open', async () => {
    const { useScopes } = await loadScopes('us-east-1_NKN5eM40q');
    fetchAuthSession.mockResolvedValue(tokenWithGroups(['some-future-group']));

    const { result } = renderHook(() => useScopes());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    expect(result.current.scopes.size).toBe(0);
    expect(result.current.isTypeAdmin).toBe(false);
  });

  it('maps the provisioned governance and publisher groups exactly', async () => {
    const { useScopes } = await loadScopes('us-east-1_NKN5eM40q');
    fetchAuthSession.mockResolvedValue(tokenWithGroups([
      'g-users-default',
      'g-admins-security',
      'registry-developer',
    ]));

    const { result } = renderHook(() => useScopes());
    await waitFor(() => expect(result.current.loaded).toBe(true));

    expect(result.current.hasScope('agent:write')).toBe(true);
    expect(result.current.hasScope('tag:read', 'tag:write')).toBe(true);
    expect(result.current.hasScope('registry:read', 'registry:write')).toBe(true);
    expect(result.current.hasScope('admin')).toBe(false);
  });
});

describe('persona routing', () => {
  it('routes standard builders by agent:write, not by t-admin membership', async () => {
    const { resolvePersonaRoute } = await loadScopes('pool');
    expect(resolvePersonaRoute({
      loaded: true,
      scopes: new Set(['agent:read', 'agent:write', 'invoke']),
      isTypeAdmin: false,
      previewAsEndUser: false,
    })).toBe('builder');
  });

  it('routes only read+invoke identities to chat and zero-scope identities to denial', async () => {
    const { resolvePersonaRoute } = await loadScopes('pool');
    expect(resolvePersonaRoute({
      loaded: true,
      scopes: new Set(['agent:read', 'invoke']),
      isTypeAdmin: false,
      previewAsEndUser: false,
    })).toBe('chat');
    expect(resolvePersonaRoute({
      loaded: true,
      scopes: new Set(),
      isTypeAdmin: false,
      previewAsEndUser: false,
    })).toBe('denied');
  });

  it('keeps unresolved identities behind a neutral loading gate', async () => {
    const { resolvePersonaRoute } = await loadScopes('pool');
    expect(resolvePersonaRoute({
      loaded: false,
      scopes: new Set(),
      isTypeAdmin: false,
      previewAsEndUser: false,
    })).toBe('loading');
  });

  it('allows only a type admin to enter the explicit chat preview', async () => {
    const { resolvePersonaRoute } = await loadScopes('pool');
    expect(resolvePersonaRoute({
      loaded: true,
      scopes: new Set(['agent:write']),
      isTypeAdmin: true,
      previewAsEndUser: true,
    })).toBe('chat');
    expect(resolvePersonaRoute({
      loaded: true,
      scopes: new Set(['agent:write']),
      isTypeAdmin: false,
      previewAsEndUser: true,
    })).toBe('builder');
  });
});
