/**
 * RegistryModal — Phase 2 Gap 2A agent registry with two-persona approval workflow.
 *
 * ADMIN (registry-admin or org-admin group): see all entries, pending review tab,
 * approve/reject submissions.
 * DEVELOPER (others): see approved entries + own pending entries, no approve/reject UI.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  searchRegistryApi,
  cloneFromRegistryApi,
  approveRegistryApi,
  rejectRegistryApi,
  getErrorMessage,
  type RegistryEntry,
  type GeneratedCanvasSpec,
} from '../../services/api';
import { useIsRegistryAdmin } from '../../auth/useIsRegistryAdmin';

// ============================================================================
// Props
// ============================================================================

export interface RegistryModalProps {
  isOpen: boolean;
  onClose: () => void;
  onClone?: (snapshot: GeneratedCanvasSpec) => void;
}

// ============================================================================
// Component
// ============================================================================

export function RegistryModal({ isOpen, onClose, onClone }: RegistryModalProps) {
  const isAdmin = useIsRegistryAdmin();
  const [scope, setScope] = useState<'all' | 'mine' | 'public' | 'pending'>('all');
  const [query, setQuery] = useState('');
  const [entries, setEntries] = useState<RegistryEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cloning, setCloning] = useState<string | null>(null);
  const [approving, setApproving] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const results = await searchRegistryApi({ q: query || undefined, scope });
      setEntries(results);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [query, scope]);

  useEffect(() => {
    if (isOpen) {
      void load();
    }
  }, [isOpen, load]);

  const handleClone = async (entry: RegistryEntry) => {
    if (!onClone) return;
    // Only allow cloning approved entries (or own entries for the owner)
    const status = entry.status || 'approved';
    if (status !== 'approved' && !entry.is_owner) {
      setError('Only approved agents can be cloned');
      return;
    }
    setCloning(entry.agent_slug);
    setError(null);
    try {
      const result = await cloneFromRegistryApi(entry.agent_slug);
      onClone(result.canvas_snapshot);
      onClose();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setCloning(null);
    }
  };

  const handleApprove = async (slug: string) => {
    setApproving(slug);
    setError(null);
    try {
      await approveRegistryApi(slug);
      await load(); // refresh list
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setApproving(null);
    }
  };

  const handleReject = async (slug: string) => {
    setRejecting(slug);
    setError(null);
    try {
      await rejectRegistryApi(slug, rejectReason || undefined);
      setRejectReason('');
      await load();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setRejecting(null);
    }
  };

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    void load();
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Overlay */}
      <div
        className="absolute inset-0 bg-black/40"
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Panel */}
      <div className="relative rounded-xl w-full max-w-4xl max-h-[85vh] flex flex-col" style={{ background: 'var(--color-surface-elevated)', boxShadow: 'var(--elevation-4)', border: '1px solid var(--color-border)' }}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b" style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-subtle)' }}>
          <div>
            <h2 className="text-lg font-semibold tracking-tight" style={{ color: 'var(--color-text-primary)' }}>
              Agent Registry {isAdmin && <span className="text-xs font-normal" style={{ color: 'var(--accent)' }}>(Admin)</span>}
            </h2>
            <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-secondary)' }}>
              {isAdmin
                ? 'Browse, approve, and manage published agent blueprints'
                : 'Browse and clone published agent blueprints to your canvas'}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 transition-colors"
            aria-label="Close"
          >
            <svg
              className="w-5 h-5"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              viewBox="0 0 24 24"
            >
              <path d="M6 18L18 6M6 6l12 12" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        {/* Search bar */}
        <div className="px-6 py-4 border-b border-gray-200 bg-gray-50">
          <form onSubmit={handleSearch} className="flex gap-2">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search by name or description..."
              className="flex-1 px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
            <select
              value={scope}
              onChange={(e) => setScope(e.target.value as typeof scope)}
              className="px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent bg-white"
            >
              <option value="all">All visible</option>
              <option value="mine">My agents</option>
              <option value="public">Public</option>
              {isAdmin && <option value="pending">Pending review</option>}
            </select>
            <button
              type="submit"
              disabled={loading}
              className="px-4 py-2 text-sm font-medium bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {loading ? 'Searching...' : 'Search'}
            </button>
          </form>
        </div>

        {/* Error banner */}
        {error && (
          <div className="mx-6 mt-4 px-3 py-2 rounded-lg border border-red-200 bg-red-50 text-xs text-red-700">
            {error}
          </div>
        )}

        {/* Content area */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {loading && entries.length === 0 ? (
            <div className="text-sm text-gray-500">Loading agents...</div>
          ) : entries.length === 0 ? (
            <div className="text-center py-16">
              <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-gray-100 flex items-center justify-center">
                <svg className="w-8 h-8 text-gray-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
                </svg>
              </div>
              <div className="text-sm font-semibold text-gray-700 mb-1">
                No agents found
              </div>
              <div className="text-xs text-gray-500">
                {query
                  ? 'Try a different search term or scope'
                  : scope === 'pending'
                  ? 'No pending submissions'
                  : 'No published agents match your scope. Publish your first agent after deploying!'}
              </div>
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3">
              {entries.map((entry) => {
                const status = entry.status || 'approved';
                const canClone = status === 'approved' || entry.is_owner;
                const showApprovalActions = isAdmin && status === 'pending';

                return (
                  <div
                    key={entry.agent_slug}
                    className="border border-gray-200 rounded-lg p-4 bg-white hover:border-gray-300 transition-colors"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <h3 className="text-sm font-semibold text-gray-900 tracking-tight">
                            {entry.display_name}
                          </h3>
                          {/* Status badge */}
                          <span
                            className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wide ${
                              status === 'pending'
                                ? 'bg-amber-100 text-amber-700'
                                : status === 'approved'
                                ? 'bg-emerald-100 text-emerald-700'
                                : 'bg-red-100 text-red-700'
                            }`}
                          >
                            {status}
                          </span>
                          {/* Visibility badge */}
                          <span
                            className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wide ${
                              entry.visibility === 'public'
                                ? 'bg-green-100 text-green-700'
                                : entry.visibility === 'org'
                                ? 'bg-blue-100 text-blue-700'
                                : 'bg-gray-100 text-gray-600'
                            }`}
                          >
                            {entry.visibility}
                          </span>
                          {entry.is_owner && (
                            <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 text-[10px] font-medium uppercase tracking-wide">
                              owner
                            </span>
                          )}
                        </div>
                        <p className="text-xs text-gray-600 mb-2 line-clamp-2">
                          {entry.description || 'No description provided.'}
                        </p>
                        {entry.tags.length > 0 && (
                          <div className="flex flex-wrap gap-1 mb-2">
                            {entry.tags.map((tag) => (
                              <span
                                key={tag}
                                className="inline-flex items-center px-1.5 py-0.5 rounded bg-gray-100 text-gray-700 text-[10px]"
                              >
                                {tag}
                              </span>
                            ))}
                          </div>
                        )}
                        {entry.rejection_reason && (
                          <div className="text-xs text-red-600 mb-2 italic">
                            Rejected: {entry.rejection_reason}
                          </div>
                        )}
                        <div className="flex items-center gap-3 text-[10px] text-gray-500">
                          <span style={{ fontVariantNumeric: 'tabular-nums' }}>
                            {entry.usage_count} clone{entry.usage_count !== 1 ? 's' : ''}
                          </span>
                          {entry.source_runtime_name && (
                            <span className="font-mono text-[9px]">
                              {entry.source_runtime_name}
                            </span>
                          )}
                          <span>
                            {new Date(entry.updated_at).toLocaleDateString()}
                          </span>
                        </div>
                      </div>
                      <div className="flex flex-col gap-2">
                        {/* Clone button */}
                        <button
                          type="button"
                          onClick={() => void handleClone(entry)}
                          disabled={cloning !== null || !onClone || !canClone}
                          className="px-3 py-1.5 text-xs font-semibold bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed transition-all duration-150 whitespace-nowrap flex items-center gap-1.5"
                          title={canClone ? 'Clone this agent to your canvas' : 'Only approved agents can be cloned'}
                          aria-label={`Clone ${entry.display_name} to canvas`}
                        >
                          {cloning === entry.agent_slug ? (
                            <>
                              <svg className="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                              </svg>
                              Cloning...
                            </>
                          ) : (
                            <>
                              <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                                <path d="M12 5v14M5 12h14" />
                              </svg>
                              Clone
                            </>
                          )}
                        </button>
                        {/* Admin approve/reject buttons */}
                        {showApprovalActions && (
                          <>
                            <button
                              type="button"
                              onClick={() => void handleApprove(entry.agent_slug)}
                              disabled={approving !== null}
                              className="px-3 py-1.5 text-xs font-semibold bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-40 disabled:cursor-not-allowed transition-all duration-150 whitespace-nowrap"
                              title="Approve this submission"
                              aria-label={`Approve ${entry.display_name}`}
                            >
                              {approving === entry.agent_slug ? 'Approving...' : 'Approve'}
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                const reason = prompt('Rejection reason (optional):');
                                if (reason !== null) {
                                  setRejectReason(reason);
                                  void handleReject(entry.agent_slug);
                                }
                              }}
                              disabled={rejecting !== null}
                              className="px-3 py-1.5 text-xs font-semibold bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-40 disabled:cursor-not-allowed transition-all duration-150 whitespace-nowrap"
                              title="Reject this submission"
                              aria-label={`Reject ${entry.display_name}`}
                            >
                              {rejecting === entry.agent_slug ? 'Rejecting...' : 'Reject'}
                            </button>
                          </>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
