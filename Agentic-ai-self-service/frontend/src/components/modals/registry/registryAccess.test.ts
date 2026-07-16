import { describe, it, expect } from 'vitest';
import { resolveAccess } from './registryAccess';
import type { RegistryEntry } from '../../../services/api';

function entry(over: Partial<RegistryEntry> = {}): RegistryEntry {
  return {
    org_id: 'org-1',
    agent_slug: 'weather-agent',
    display_name: 'Weather Agent',
    description: 'x',
    tags: [],
    visibility: 'org',
    usage_count: 0,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    is_owner: false,
    status: 'approved',
    ...over,
  };
}

/** A scope checker over a fixed set (admin scope implies all, like useScopes). */
function checker(scopes: string[]) {
  const held = new Set(scopes);
  return { hasScope: (...req: string[]) => held.has('admin') || req.every((s) => held.has(s)) };
}

const row = (a: ReturnType<typeof resolveAccess>, name: string) =>
  a.rows.find((r) => r.action === name)!;

describe('resolveAccess — the RBAC port of the reference Access tab', () => {
  it('standard user (registry:read) can browse + clone approved, but NOT publish/moderate', () => {
    const a = resolveAccess(entry(), checker(['registry:read']), false);
    expect(row(a, 'Browse & view').allowed).toBe(true);
    expect(row(a, 'Clone to canvas').allowed).toBe(true);
    expect(row(a, 'Publish new blueprint').allowed).toBe(false);
    expect(row(a, 'Approve / reject submissions').allowed).toBe(false);
  });

  it('a user with NO registry scope is denied everything', () => {
    const a = resolveAccess(entry(), checker([]), false);
    expect(a.rows.every((r) => !r.allowed)).toBe(true);
  });

  it('registry admin (write + admin persona) can publish AND approve/reject', () => {
    const a = resolveAccess(entry(), checker(['registry:read', 'registry:write']), true);
    expect(row(a, 'Publish new blueprint').allowed).toBe(true);
    expect(row(a, 'Approve / reject submissions').allowed).toBe(true);
    expect(row(a, 'Edit / delete this entry').allowed).toBe(true);
  });

  it('write-scoped NON-admin cannot approve/reject nor edit others entries', () => {
    const a = resolveAccess(entry({ is_owner: false }), checker(['registry:read', 'registry:write']), false);
    expect(row(a, 'Approve / reject submissions').allowed).toBe(false);
    expect(row(a, 'Edit / delete this entry').allowed).toBe(false);
  });

  it('pending entry is NOT clonable by a non-owner, even with registry:read', () => {
    const a = resolveAccess(entry({ status: 'pending', is_owner: false }), checker(['registry:read']), false);
    expect(row(a, 'Clone to canvas').allowed).toBe(false);
    expect(a.cloneBlockedByApproval).toBe(true);
  });

  it('owner CAN clone their own pending draft', () => {
    const a = resolveAccess(entry({ status: 'pending', is_owner: true }), checker(['registry:read']), false);
    expect(row(a, 'Clone to canvas').allowed).toBe(true);
    expect(a.cloneBlockedByApproval).toBe(false);
  });

  it('admin scope implies all registry actions', () => {
    const a = resolveAccess(entry(), checker(['admin']), true);
    expect(a.rows.every((r) => r.allowed)).toBe(true);
  });

  it('explains WHY the entry is visible (owner / admin-pending / org)', () => {
    expect(resolveAccess(entry({ is_owner: true }), checker(['registry:read']), false).visibilityReason)
      .toMatch(/you own it/i);
    expect(resolveAccess(entry({ status: 'pending', is_owner: false }), checker(['registry:read']), true).visibilityReason)
      .toMatch(/registry admin/i);
    expect(resolveAccess(entry({ visibility: 'public' }), checker(['registry:read']), false).visibilityReason)
      .toMatch(/everyone/i);
  });
});
