/**
 * Registry access model — the port of the reference registry's "Access tab"
 * (gitlab.aws.dev/omrsamer/enterprise-agentic-gateway) to OUR platform.
 *
 * The reference's guiding principle is "discovery is transparency, not
 * authorization": show EVERYTHING — including who is allowed to do what —
 * because seeing a rule does not grant it; enforcement stays 100% server-side.
 *
 * Their access model is Cedar-on-gateways. OURS is RBAC-scopes + approval-status
 * + tenant-visibility. So this helper resolves, for the SIGNED-IN caller, which
 * registry ACTIONS they may perform on a given entry, and — just as importantly —
 * WHY they can or cannot see/consume it. It is a UX affordance mirroring the real
 * server-side `require_scopes()` + visibility rules in backend/routers/registry.py;
 * it never itself grants anything.
 */

import type { RegistryEntry } from '../../../services/api';

export interface AccessRow {
  /** Human action name shown in the table. */
  action: string;
  /** The scope the backend's require_scopes() enforces for this action. */
  requiredScope: string;
  /** Whether THIS caller currently satisfies it. */
  allowed: boolean;
  /** Plain-language note — extra gating beyond the scope (approval, ownership). */
  note?: string;
}

export interface AccessSummary {
  /** Per-action allow/deny rows (the reference's per-tool rules table). */
  rows: AccessRow[];
  /** Why the caller can see this entry at all (the visibility explanation). */
  visibilityReason: string;
  /** True if the caller could see it but cannot consume it (pending, not owner). */
  cloneBlockedByApproval: boolean;
}

export interface ScopeChecker {
  /** Same contract as useScopes().hasScope — admin implies all. */
  hasScope: (...required: string[]) => boolean;
}

/**
 * Resolve the caller's capabilities against one entry. Pure — no network, no
 * hooks — so it is unit-testable and deterministic.
 */
export function resolveAccess(
  entry: RegistryEntry,
  scopes: ScopeChecker,
  isRegistryAdmin: boolean,
): AccessSummary {
  const status = entry.status || 'approved';
  const isApproved = status === 'approved';
  const isOwner = entry.is_owner;

  // Clone (consume) needs registry:read AND the entry must be approved, unless
  // the caller owns it (owners can clone their own pending drafts). Mirrors the
  // backend clone endpoint + RegistryModal.handleClone gating.
  const canReadScope = scopes.hasScope('registry:read');
  const cloneApprovalOk = isApproved || isOwner;
  const cloneAllowed = canReadScope && cloneApprovalOk;

  // Write actions need registry:write. Approve/reject additionally require the
  // registry-admin persona (a write-scoped non-admin cannot moderate).
  const canWriteScope = scopes.hasScope('registry:write');

  const rows: AccessRow[] = [
    {
      action: 'Browse & view',
      requiredScope: 'registry:read',
      allowed: canReadScope,
    },
    {
      action: 'Clone to canvas',
      requiredScope: 'registry:read',
      allowed: cloneAllowed,
      note: cloneApprovalOk
        ? undefined
        : isOwner
          ? undefined
          : 'Only approved entries are clonable by non-owners',
    },
    {
      action: 'Publish new blueprint',
      requiredScope: 'registry:write',
      allowed: canWriteScope,
    },
    {
      action: 'Edit / delete this entry',
      requiredScope: 'registry:write',
      allowed: canWriteScope && (isOwner || isRegistryAdmin),
      note: canWriteScope && !isOwner && !isRegistryAdmin
        ? 'Only the owner or a registry admin may edit/delete'
        : undefined,
    },
    {
      action: 'Approve / reject submissions',
      requiredScope: 'registry:write',
      allowed: canWriteScope && isRegistryAdmin,
      note: 'Registry-admin persona only',
    },
  ];

  const visibilityReason = explainVisibility(entry, isRegistryAdmin);

  return {
    rows,
    visibilityReason,
    cloneBlockedByApproval: canReadScope && !cloneApprovalOk,
  };
}

/** Plain-language "why you can see this" — the visibility half of the Access tab. */
function explainVisibility(entry: RegistryEntry, isRegistryAdmin: boolean): string {
  const status = entry.status || 'approved';
  if (entry.is_owner) {
    return `You can see this because you own it (status: ${status}). Owners always see their own entries, approved or not.`;
  }
  if (isRegistryAdmin && status !== 'approved') {
    return `You can see this pending/rejected entry because you are a registry admin — regular users only see approved entries from others.`;
  }
  switch (entry.visibility) {
    case 'public':
      return 'Visible to everyone: this entry is published as public and approved.';
    case 'org':
      return 'Visible because it is shared org-wide and approved, and you belong to the same organization.';
    default:
      return 'Visible to you under the current registry visibility rules.';
  }
}
