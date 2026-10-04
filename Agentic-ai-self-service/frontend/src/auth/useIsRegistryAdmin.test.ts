/**
 * `useIsRegistryAdmin` — the approver groups the pool actually creates are approvers.
 *
 * The hook recognised only `registry-admin` and `org-admin`, while the pool's RBAC
 * groups are `g-admins-registry` and `g-admins-super`. A user granted
 * `g-admins-registry` held `registry:write` yet saw no pending-review queue. The
 * list is pinned equal to the backend's by backend/tests/test_rbac_route_coverage.py.
 */
import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useIsRegistryAdmin } from './useIsRegistryAdmin';

const fetchAuthSession = vi.fn();
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: () => fetchAuthSession() }));

const withGroups = (groups: unknown) => ({
  tokens: { idToken: { payload: { 'cognito:groups': groups } } },
});

async function resolve(groups: unknown): Promise<boolean> {
  fetchAuthSession.mockResolvedValue(withGroups(groups));
  const { result } = renderHook(() => useIsRegistryAdmin());
  await waitFor(() => expect(fetchAuthSession).toHaveBeenCalled());
  // Let the effect's promise settle before reading.
  await new Promise((r) => setTimeout(r, 0));
  return result.current;
}

describe('useIsRegistryAdmin', () => {
  beforeEach(() => fetchAuthSession.mockReset());

  it.each(['g-admins-registry', 'g-admins-super', 'registry-admin', 'org-admin'])(
    '%s is a registry approver',
    async (group) => {
      expect(await resolve([group])).toBe(true);
    },
  );

  it.each([[['g-users-default', 't-user']], [['g-admins-cost']], [[]]])(
    '%j is not a registry approver',
    async (groups) => {
      expect(await resolve(groups)).toBe(false);
    },
  );

  it('reads a JSON-string groups claim', async () => {
    expect(await resolve('["t-admin","g-admins-registry"]')).toBe(true);
  });
});
