/**
 * AuditDashboard — Phase 5 (Loom-inspired) admin action-audit view.
 *
 * Reads GET /api/admin/audit (admin scope) and shows action counts, per-actor
 * activity, and a recent-events timeline. Only rendered for super-admins (the
 * backend gates on the `admin` scope; a non-admin gets 403 → empty state).
 */

import { useCallback, useEffect, useState } from 'react';
import { getApiClient, getErrorMessage, type AuditSummary } from '../../services/api';

export function AuditDashboard() {
  const [data, setData] = useState<AuditSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      setData(await getApiClient().getAudit(200));
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  const topActions = data ? Object.entries(data.by_action).sort((a, b) => b[1] - a[1]) : [];
  const topActors = data ? Object.entries(data.by_actor).sort((a, b) => b[1] - a[1]) : [];

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h4 className="text-sm font-semibold text-gray-800">Audit trail</h4>
          <p className="text-xs text-gray-500">Recent control-plane actions across the org (admin only).</p>
        </div>
        <button
          type="button" onClick={() => void reload()} disabled={loading}
          className="text-xs px-2 py-1 rounded border border-gray-200 hover:bg-gray-50 disabled:opacity-50"
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>
      )}

      {!data || data.total === 0 ? (
        <div className="text-xs text-gray-500">No audit events yet — perform a deploy or config change.</div>
      ) : (
        <>
          {/* Loom-study 5.2 — summary tiles + activity-over-time. */}
          <div className="grid grid-cols-3 gap-3">
            {([
              ['Events', data.total],
              ['Distinct users', data.distinct_actors ?? Object.keys(data.by_actor).length],
              ['Sessions', data.distinct_sessions ?? '—'],
            ] as const).map(([label, value]) => (
              <div key={label} className="rounded-lg border border-gray-200 p-3">
                <div className="text-[11px] text-gray-500">{label}</div>
                <div className="text-xl font-semibold text-gray-800" style={{ fontVariantNumeric: 'tabular-nums' }}>{value}</div>
              </div>
            ))}
          </div>

          {data.by_day && data.by_day.length > 0 && (
            <div className="rounded-lg border border-gray-200 p-3">
              <div className="text-xs font-semibold text-gray-800 mb-2">Activity over time</div>
              <div className="flex items-end gap-1 h-24">
                {(() => {
                  const max = Math.max(...data.by_day!.map((d) => d.count), 1);
                  return data.by_day!.map((d) => (
                    <div key={d.day} className="flex-1 flex flex-col items-center justify-end" title={`${d.day}: ${d.count}`}>
                      <div className="w-full rounded-t" style={{ height: `${(d.count / max) * 100}%`, background: 'var(--accent, #4f9cff)', minHeight: '2px' }} />
                      <div className="text-[8px] text-gray-400 mt-1 truncate w-full text-center">{d.day.slice(5)}</div>
                    </div>
                  ));
                })()}
              </div>
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-lg border border-gray-200 p-3">
              <div className="text-xs font-semibold text-gray-800 mb-2">By action</div>
              {topActions.map(([action, count]) => (
                <div key={action} className="flex justify-between text-[11px] text-gray-700 py-0.5">
                  <span className="font-mono">{action}</span><span className="font-semibold">{count}</span>
                </div>
              ))}
            </div>
            <div className="rounded-lg border border-gray-200 p-3">
              <div className="text-xs font-semibold text-gray-800 mb-2">By actor</div>
              {topActors.map(([actor, count]) => (
                <div key={actor} className="flex justify-between text-[11px] text-gray-700 py-0.5">
                  <span className="font-mono truncate max-w-[70%]" title={actor}>{actor}</span>
                  <span className="font-semibold">{count}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="rounded-lg border border-gray-200 p-3">
            <div className="text-xs font-semibold text-gray-800 mb-2">Recent events</div>
            <div className="space-y-0.5 max-h-64 overflow-auto">
              {data.events.map((e, i) => (
                <div key={i} className="flex items-center gap-2 text-[11px] text-gray-600">
                  <span className="w-36 truncate">{new Date(e.ts).toLocaleString()}</span>
                  <span className="font-mono text-cyan-700 w-32 truncate">{e.action}</span>
                  <span className="truncate flex-1" title={e.actor_sub}>{e.actor_sub}</span>
                  <span className={e.status_code >= 400 ? 'text-red-600' : 'text-gray-400'}>{e.status_code}</span>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
