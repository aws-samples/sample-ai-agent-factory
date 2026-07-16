/**
 * RegistryEntryDetail — the detail view for one registry entry, a port of the
 * reference registry's 4-tab detail page (Description / Tools / Access / Details)
 * from gitlab.aws.dev/omrsamer/enterprise-agentic-gateway, adapted to our
 * blueprint registry:
 *
 *   Overview   → description + metadata + primary Clone action
 *   Components → the blueprint's nodes/edges (our analogue of "Tools")
 *   Access     → "what YOU can do with this entry" + why you can see it
 *                (the reference's crown-jewel Access tab, RBAC-flavored)
 *   Details    → slug, org, visibility, version, usage, timestamps, review info
 *
 * The Access tab embodies the reference's principle — "discovery is transparency,
 * not authorization": we show every action and whether the caller is allowed,
 * with a callout that seeing a row does not grant it (the server enforces).
 */

import { useEffect, useMemo, useState } from 'react';
import {
  getRegistryEntryApi,
  getErrorMessage,
  type RegistryEntry,
} from '../../../services/api';
import { useScopes } from '../../../auth/scopes';
import { resolveAccess } from './registryAccess';
import { summarizeBlueprint } from './blueprintSummary';

const TABS = ['Overview', 'Components', 'Access', 'Details'] as const;
type Tab = (typeof TABS)[number];

export interface RegistryEntryDetailProps {
  /** The list-row entry (no snapshot). Detail re-fetches to get the snapshot. */
  entry: RegistryEntry;
  isAdmin: boolean;
  cloning: boolean;
  onBack: () => void;
  onClone: (entry: RegistryEntry) => void;
}

export function RegistryEntryDetail({
  entry: listEntry,
  isAdmin,
  cloning,
  onBack,
  onClone,
}: RegistryEntryDetailProps) {
  const [tab, setTab] = useState<Tab>('Overview');
  // Full entry (with canvas_snapshot) fetched lazily; fall back to the list row.
  const [full, setFull] = useState<RegistryEntry>(listEntry);
  const [loadError, setLoadError] = useState<string | null>(null);
  const { hasScope } = useScopes();

  useEffect(() => {
    let cancelled = false;
    getRegistryEntryApi(listEntry.agent_slug)
      .then((e) => {
        if (!cancelled) setFull(e);
      })
      .catch((e) => {
        if (!cancelled) setLoadError(getErrorMessage(e));
      });
    return () => {
      cancelled = true;
    };
  }, [listEntry.agent_slug]);

  const access = useMemo(
    () => resolveAccess(full, { hasScope }, isAdmin),
    [full, hasScope, isAdmin],
  );
  const blueprint = useMemo(
    () => summarizeBlueprint(full.canvas_snapshot),
    [full.canvas_snapshot],
  );

  const status = full.status || 'approved';
  const canClone = access.rows.find((r) => r.action === 'Clone to canvas')?.allowed ?? false;

  return (
    <div className="flex flex-col h-full">
      {/* Breadcrumb */}
      <div className="px-6 pt-4 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
        <button
          type="button"
          onClick={onBack}
          className="hover:underline"
          style={{ color: 'var(--accent)' }}
        >
          ← Registry
        </button>
        <span className="mx-1.5">/</span>
        <span>{full.display_name}</span>
      </div>

      {/* Title + primary action */}
      <div className="px-6 pt-2 pb-3 flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-lg font-semibold tracking-tight truncate" style={{ color: 'var(--color-text-primary)' }}>
            {full.display_name}
          </h2>
          <div className="flex items-center gap-2 mt-1">
            <StatusBadge status={status} />
            <VisibilityBadge visibility={full.visibility} />
            {full.is_owner && <MiniBadge className="bg-amber-100 text-amber-700">owner</MiniBadge>}
          </div>
        </div>
        <button
          type="button"
          onClick={() => onClone(full)}
          disabled={cloning || !canClone}
          title={canClone ? 'Clone this blueprint to your canvas' : 'You cannot clone this entry (see the Access tab)'}
          className="px-3 py-1.5 text-xs font-semibold bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed transition-all whitespace-nowrap"
        >
          {cloning ? 'Cloning…' : 'Clone to canvas'}
        </button>
      </div>

      {/* Tabs */}
      <div className="px-6 flex gap-1 border-b" style={{ borderColor: 'var(--color-border)' }}>
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className="px-3.5 py-2 text-sm border-b-2 -mb-px transition-colors"
            style={{
              color: tab === t ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
              borderBottomColor: tab === t ? 'var(--accent)' : 'transparent',
            }}
          >
            {t}
          </button>
        ))}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-6 py-4">
        {loadError && (
          <div className="mb-3 px-3 py-2 rounded-lg border border-red-200 bg-red-50 text-xs text-red-700">
            Couldn't load full details: {loadError}
          </div>
        )}
        {tab === 'Overview' && <OverviewTab entry={full} />}
        {tab === 'Components' && <ComponentsTab blueprint={blueprint} hasSnapshot={!!full.canvas_snapshot} />}
        {tab === 'Access' && <AccessTab access={access} status={status} />}
        {tab === 'Details' && <DetailsTab entry={full} blueprint={blueprint} />}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------------
// Tabs
// ----------------------------------------------------------------------------

function OverviewTab({ entry }: { entry: RegistryEntry }) {
  return (
    <div className="space-y-4">
      <Section title="Description">
        <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
          {entry.description || 'No description provided.'}
        </p>
      </Section>
      {entry.tags.length > 0 && (
        <Section title="Tags">
          <div className="flex flex-wrap gap-1.5">
            {entry.tags.map((t) => (
              <span key={t} className="inline-flex items-center px-2 py-0.5 rounded bg-gray-100 text-gray-700 text-xs">
                {t}
              </span>
            ))}
          </div>
        </Section>
      )}
      {entry.rejection_reason && (
        <div className="px-3 py-2 rounded-lg border border-red-200 bg-red-50 text-xs text-red-700 italic">
          Rejected: {entry.rejection_reason}
        </div>
      )}
    </div>
  );
}

function ComponentsTab({
  blueprint,
  hasSnapshot,
}: {
  blueprint: ReturnType<typeof summarizeBlueprint>;
  hasSnapshot: boolean;
}) {
  if (!hasSnapshot) return <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>Loading blueprint…</p>;
  if (blueprint.components.length === 0)
    return <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>This blueprint has no components.</p>;
  return (
    <div className="space-y-4">
      <Section title={`${blueprint.components.length} components`}>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left" style={{ color: 'var(--color-text-secondary)' }}>
              <th className="py-1.5 pr-3 font-medium">Type</th>
              <th className="py-1.5 pr-3 font-medium">Name</th>
              <th className="py-1.5 font-medium">Config</th>
            </tr>
          </thead>
          <tbody>
            {blueprint.components.map((c) => (
              <tr key={c.id} className="border-t" style={{ borderColor: 'var(--color-border)' }}>
                <td className="py-1.5 pr-3">
                  <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-blue-50 text-blue-700 text-[11px] font-medium">
                    {c.type}
                  </span>
                </td>
                <td className="py-1.5 pr-3 font-medium" style={{ color: 'var(--color-text-primary)' }}>{c.label}</td>
                <td className="py-1.5" style={{ color: 'var(--color-text-secondary)' }}>{c.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
      {blueprint.wiring.length > 0 && (
        <Section title={`${blueprint.wiring.length} connections`}>
          <ul className="text-sm space-y-1" style={{ color: 'var(--color-text-secondary)' }}>
            {blueprint.wiring.map((w, i) => (
              <li key={i} className="font-mono text-[12px]">
                {w.sourceLabel} <span style={{ color: 'var(--accent)' }}>→</span> {w.targetLabel}
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}

function AccessTab({
  access,
  status,
}: {
  access: ReturnType<typeof resolveAccess>;
  status: string;
}) {
  return (
    <div className="space-y-4">
      <Section title="What you can do with this entry">
        <p className="text-xs mb-3" style={{ color: 'var(--color-text-secondary)' }}>
          {access.visibilityReason}
        </p>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left" style={{ color: 'var(--color-text-secondary)' }}>
              <th className="py-1.5 pr-3 font-medium">Action</th>
              <th className="py-1.5 pr-3 font-medium">Required scope</th>
              <th className="py-1.5 pr-3 font-medium">You</th>
              <th className="py-1.5 font-medium">Note</th>
            </tr>
          </thead>
          <tbody>
            {access.rows.map((r) => (
              <tr key={r.action} className="border-t" style={{ borderColor: 'var(--color-border)' }}>
                <td className="py-1.5 pr-3" style={{ color: 'var(--color-text-primary)' }}>{r.action}</td>
                <td className="py-1.5 pr-3">
                  <code className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>{r.requiredScope}</code>
                </td>
                <td className="py-1.5 pr-3">
                  {r.allowed ? (
                    <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-emerald-700">✓ allowed</span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-red-600">✕ denied</span>
                  )}
                </td>
                <td className="py-1.5 text-xs" style={{ color: 'var(--color-text-secondary)' }}>{r.note || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      {access.cloneBlockedByApproval && (
        <div className="px-3 py-2 rounded-lg border border-amber-200 bg-amber-50 text-xs text-amber-800">
          This entry is <strong>{status}</strong> — it becomes clonable once a registry admin approves it.
        </div>
      )}

      <div className="px-3 py-2 rounded-lg text-xs" style={{ background: 'rgba(79,156,255,.08)', border: '1px solid var(--accent)', color: 'var(--color-text-secondary)' }}>
        This is informational. Access is enforced server-side by the API
        (<code>require_scopes()</code> + tenant-visibility rules). Seeing an action
        here does not grant it — a request you aren't scoped for is rejected by the
        backend. Scopes come from your Cognito group membership.
      </div>
    </div>
  );
}

function DetailsTab({
  entry,
  blueprint,
}: {
  entry: RegistryEntry;
  blueprint: ReturnType<typeof summarizeBlueprint>;
}) {
  const rows: Array<[string, string | undefined | null]> = [
    ['Slug', entry.agent_slug],
    ['Organization', entry.org_id],
    ['Visibility', entry.visibility],
    ['Status', entry.status || 'approved'],
    ['Version', entry.latest_version_id],
    ['Components', String(blueprint.components.length)],
    ['Connections', String(blueprint.wiring.length)],
    ['Clones', String(entry.usage_count)],
    ['Source runtime', entry.source_runtime_name],
    ['Created', entry.created_at ? new Date(entry.created_at).toLocaleString() : undefined],
    ['Updated', entry.updated_at ? new Date(entry.updated_at).toLocaleString() : undefined],
    ['Reviewed by', entry.reviewed_by],
    ['Reviewed at', entry.reviewed_at ? new Date(entry.reviewed_at).toLocaleString() : undefined],
  ];
  return (
    <Section title="Details">
      <table className="w-full text-sm">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k} className="border-t" style={{ borderColor: 'var(--color-border)' }}>
              <th className="py-1.5 pr-3 text-left font-medium w-44 align-top" style={{ color: 'var(--color-text-secondary)' }}>{k}</th>
              <td className="py-1.5 break-all" style={{ color: 'var(--color-text-primary)' }}>{v || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

// ----------------------------------------------------------------------------
// Small presentational helpers
// ----------------------------------------------------------------------------

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border p-4" style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-subtle)' }}>
      <h3 className="text-sm font-semibold mb-2" style={{ color: 'var(--color-text-primary)' }}>{title}</h3>
      {children}
    </div>
  );
}

function MiniBadge({ className, children }: { className: string; children: React.ReactNode }) {
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wide ${className}`}>
      {children}
    </span>
  );
}

function StatusBadge({ status }: { status: string }) {
  const cls =
    status === 'pending'
      ? 'bg-amber-100 text-amber-700'
      : status === 'approved'
        ? 'bg-emerald-100 text-emerald-700'
        : 'bg-red-100 text-red-700';
  return <MiniBadge className={cls}>{status}</MiniBadge>;
}

function VisibilityBadge({ visibility }: { visibility: RegistryEntry['visibility'] }) {
  const cls =
    visibility === 'public'
      ? 'bg-green-100 text-green-700'
      : visibility === 'org'
        ? 'bg-blue-100 text-blue-700'
        : 'bg-gray-100 text-gray-600';
  return <MiniBadge className={cls}>{visibility}</MiniBadge>;
}
