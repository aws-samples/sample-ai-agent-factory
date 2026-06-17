/**
 * ObservabilityPanel — Phase 1 Gap 1D frontend.
 *
 * Shows CloudWatch dashboard link for a deployed runtime. Fetches the
 * dashboard URL via the backend, displays runtime metadata, and provides
 * a prominent "Open CloudWatch Dashboard" button. Handles cases where
 * the dashboard has not yet been created (first deploy pending).
 */

import { useCallback, useEffect, useState } from 'react';
import {
  getApiClient,
  getErrorMessage,
  isNotReadyError,
  type DashboardUrlSummary,
} from '../../services/api';

interface ObservabilityPanelProps {
  runtimeName: string | null;
  /** Refresh trigger — increment to force a reload (e.g. after a new deploy). */
  refreshKey?: number;
}

export function ObservabilityPanel({ runtimeName, refreshKey }: ObservabilityPanelProps) {
  const [dashboard, setDashboard] = useState<DashboardUrlSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    if (!runtimeName) {
      setDashboard(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const api = getApiClient();
      const result = await api.getDashboardUrl(runtimeName);
      setDashboard(result);
    } catch (e) {
      // 401/403/404 are expected if the runtime has never been deployed — treat
      // as a friendly empty state, not an error (Bug 136).
      if (isNotReadyError(e)) {
        setDashboard(null);
        setError(null);
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

  if (!runtimeName) {
    return (
      <div className="p-5 text-sm text-gray-500">
        Deploy this agent at least once to see observability details.
      </div>
    );
  }

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h4 className="text-sm font-semibold text-gray-800">Observability</h4>
          <p className="text-xs text-gray-500">
            CloudWatch dashboard for real-time metrics, logs, and traces.
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

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </div>
      )}

      {loading && !dashboard ? (
        <div className="text-xs text-gray-500">Loading observability details…</div>
      ) : !dashboard ? (
        <div className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-xs text-gray-600">
          Dashboard will be created on first deploy.
        </div>
      ) : (
        <div className="rounded-lg border border-gray-200 bg-white px-4 py-3 space-y-3">
          <div className="flex items-start justify-between">
            <div className="space-y-1.5">
              <div className="text-xs font-medium text-gray-800">
                {dashboard.dashboard_name}
              </div>
              <div className="text-[11px] text-gray-500 space-y-0.5">
                <div>
                  <span className="font-medium">Runtime ID:</span>{' '}
                  <code className="font-mono text-[10px] bg-gray-100 px-1 py-0.5 rounded">
                    {dashboard.runtime_id}
                  </code>
                </div>
                <div>
                  <span className="font-medium">Version:</span>{' '}
                  <code className="font-mono text-[10px]">
                    {dashboard.version_id.slice(0, 12)}
                  </code>
                </div>
              </div>
            </div>
            {dashboard.exists && (
              <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-700 text-[10px] font-medium">
                active
              </span>
            )}
          </div>

          <a
            href={dashboard.dashboard_url}
            target="_blank"
            rel="noopener noreferrer"
            className={`block w-full text-center text-sm px-4 py-2.5 rounded-lg font-medium transition-colors ${
              dashboard.exists
                ? 'bg-blue-600 text-white hover:bg-blue-700'
                : 'bg-gray-100 text-gray-400 cursor-not-allowed pointer-events-none'
            }`}
            aria-disabled={!dashboard.exists}
          >
            Open CloudWatch Dashboard ↗
          </a>

          {!dashboard.exists && (
            <p className="text-[11px] text-gray-500 text-center">
              Dashboard will be created on first deploy.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
