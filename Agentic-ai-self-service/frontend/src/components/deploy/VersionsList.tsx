/**
 * VersionsList — Phase 1 Gap 1A frontend.
 *
 * Lists every version of a friendly runtime name owned by the caller, shows
 * which version is currently in production / staging, and offers Promote /
 * Rollback actions. Mirrors the styling of the existing DeployPanel.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  getApiClient,
  getErrorMessage,
  isNotReadyError,
  type AgentVersionSummary,
  type RuntimeSlotsSummary,
} from '../../services/api';

interface VersionsListProps {
  runtimeName: string | null;
  /** Refresh trigger — increment to force a reload (e.g. after a new deploy). */
  refreshKey?: number;
}

export function VersionsList({ runtimeName, refreshKey }: VersionsListProps) {
  const [versions, setVersions] = useState<AgentVersionSummary[]>([]);
  const [slots, setSlots] = useState<RuntimeSlotsSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null); // version_id being promoted

  const reload = useCallback(async () => {
    if (!runtimeName) {
      setVersions([]);
      setSlots(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const api = getApiClient();
      const [vs, sl] = await Promise.all([
        api.listVersions(runtimeName),
        api.getSlots(runtimeName).catch(() => null), // 404 if no slots yet — fine
      ]);
      setVersions(vs);
      setSlots(sl);
    } catch (e) {
      // 404/403 means runtime not deployed yet — empty state, not error (Bug 136)
      if (isNotReadyError(e)) {
        setVersions([]);
        setSlots(null);
      } else {
        setError(getErrorMessage(e));
      }
    } finally {
      setLoading(false);
    }
  }, [runtimeName]);

  useEffect(() => {
    void reload();
  }, [reload, refreshKey]);

  const handlePromote = async (versionId: string, slot: 'production' | 'staging') => {
    if (!runtimeName) return;
    setActing(versionId);
    setError(null);
    try {
      await getApiClient().promoteVersion(runtimeName, versionId, slot);
      await reload();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setActing(null);
    }
  };

  const handleRollback = async () => {
    if (!runtimeName) return;
    setActing('__rollback__');
    setError(null);
    try {
      await getApiClient().rollbackRuntime(runtimeName);
      await reload();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setActing(null);
    }
  };

  if (!runtimeName) {
    return (
      <div className="p-5 text-sm text-gray-500">
        Deploy this agent at least once to see version history.
      </div>
    );
  }

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h4 className="text-sm font-semibold text-gray-800">Versions</h4>
          <p className="text-xs text-gray-500">
            Every deploy is a versioned snapshot. Promote or roll back without
            re-running the full pipeline.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void reload()}
          disabled={loading}
          className="text-xs px-2 py-1 rounded border border-gray-200 hover:bg-gray-50 disabled:opacity-50"
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {slots && (
        <div className="rounded-lg border border-gray-200 px-3 py-2 text-xs text-gray-700 flex items-center justify-between bg-gray-50">
          <div className="space-y-0.5">
            <div>
              <span className="font-medium">Production:</span>{' '}
              <code className="font-mono text-[11px]">
                {slots.production_version_id?.slice(0, 12) ?? '—'}
              </code>
            </div>
            <div>
              <span className="font-medium">Staging:</span>{' '}
              <code className="font-mono text-[11px]">
                {slots.staging_version_id?.slice(0, 12) ?? '—'}
              </code>
            </div>
          </div>
          <button
            type="button"
            onClick={() => void handleRollback()}
            disabled={
              acting !== null || !slots.previous_production_version_id
            }
            className="text-xs px-2.5 py-1 rounded bg-amber-50 text-amber-700 border border-amber-200 hover:bg-amber-100 disabled:opacity-40 disabled:cursor-not-allowed"
            title={
              slots.previous_production_version_id
                ? `Roll back to ${slots.previous_production_version_id.slice(0, 12)}`
                : 'No previous version to roll back to'
            }
          >
            {acting === '__rollback__' ? 'Rolling back…' : 'Rollback'}
          </button>
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </div>
      )}

      {loading && versions.length === 0 ? (
        <div className="text-xs text-gray-500">Loading versions…</div>
      ) : versions.length === 0 ? (
        <div className="text-xs text-gray-500">No versions yet.</div>
      ) : (
        <ul className="space-y-2">
          {versions.map((v) => {
            const isProduction = slots?.production_version_id === v.version_id;
            const isStaging = slots?.staging_version_id === v.version_id;
            return (
              <li
                key={v.version_id}
                className={`rounded-lg border px-3 py-2.5 text-xs space-y-1 ${
                  isProduction
                    ? 'border-emerald-300 bg-emerald-50'
                    : 'border-gray-200 bg-white'
                }`}
              >
                <div className="flex items-center gap-2">
                  <code className="font-mono text-[11px] text-gray-800">
                    {v.version_id.slice(0, 12)}
                  </code>
                  <span
                    className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${
                      v.status === 'succeeded'
                        ? 'bg-emerald-100 text-emerald-700'
                        : v.status === 'failed'
                        ? 'bg-red-100 text-red-700'
                        : 'bg-gray-100 text-gray-600'
                    }`}
                  >
                    {v.status}
                  </span>
                  {isProduction && (
                    <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-emerald-600 text-white text-[10px] font-medium">
                      production
                    </span>
                  )}
                  {isStaging && !isProduction && (
                    <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-blue-600 text-white text-[10px] font-medium">
                      staging
                    </span>
                  )}
                </div>
                <div className="text-[11px] text-gray-500">
                  {new Date(v.created_at).toLocaleString()}
                </div>
                {v.description && (
                  <div className="text-[11px] text-gray-700 italic">
                    {v.description}
                  </div>
                )}
                {v.status === 'succeeded' && !isProduction && (
                  <div className="flex gap-2 pt-1">
                    <button
                      type="button"
                      onClick={() => void handlePromote(v.version_id, 'production')}
                      disabled={acting !== null}
                      className="text-[11px] px-2 py-0.5 rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-40"
                    >
                      {acting === v.version_id ? 'Promoting…' : 'Promote to prod'}
                    </button>
                    {!isStaging && (
                      <button
                        type="button"
                        onClick={() => void handlePromote(v.version_id, 'staging')}
                        disabled={acting !== null}
                        className="text-[11px] px-2 py-0.5 rounded border border-gray-300 hover:bg-gray-50 disabled:opacity-40"
                      >
                        Stage
                      </button>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
