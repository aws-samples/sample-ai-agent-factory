/**
 * CostPanel — Phase 2 Gap 2B frontend.
 *
 * Displays runtime-scoped cost analytics: total cost, total input/output tokens,
 * and a per-model breakdown. Mirrors the styling of the existing DeployPanel.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  getApiClient,
  getErrorMessage,
  isNotReadyError,
  type CostSummary,
} from '../../services/api';

interface CostPanelProps {
  runtimeName: string | null;
  /** Refresh trigger — increment to force a reload (e.g. after a new deploy). */
  refreshKey?: number;
}

export function CostPanel({ runtimeName, refreshKey }: CostPanelProps) {
  const [cost, setCost] = useState<CostSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    if (!runtimeName) {
      setCost(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const api = getApiClient();
      const costData = await api.getCost(runtimeName);
      setCost(costData);
    } catch (e) {
      // Not-yet-deployed runtime returns 401/403/404 — empty state, not error (Bug 136).
      if (isNotReadyError(e)) {
        setCost(null);
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
        Deploy this agent at least once to see cost analytics.
      </div>
    );
  }

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h4 className="text-sm font-semibold text-gray-800">Cost &amp; Usage</h4>
          <p className="text-xs text-gray-500">
            Token consumption and estimated cost per model for this runtime.
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

      {loading && !cost ? (
        <div className="text-xs text-gray-500">Loading cost data…</div>
      ) : !cost || cost.total_cost === 0 ? (
        <div className="text-xs text-gray-500">
          No usage recorded yet — invoke this agent to see cost.
        </div>
      ) : (
        <>
          {/* Summary card */}
          <div className="rounded-lg border border-gray-200 px-3 py-2.5 bg-gray-50">
            <div className="flex items-center justify-between mb-2">
              <div className="text-sm font-semibold text-gray-800">
                Total Cost
              </div>
              <div className="text-base font-mono font-semibold text-gray-900">
                ${cost.total_cost.toFixed(4)}
                {cost.currency && cost.currency !== 'USD' && (
                  <span className="text-xs text-gray-500 ml-1">{cost.currency}</span>
                )}
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2 text-xs text-gray-700">
              <div>
                <span className="font-medium">Input tokens:</span>{' '}
                <span className="font-mono">{cost.total_in.toLocaleString()}</span>
              </div>
              <div>
                <span className="font-medium">Output tokens:</span>{' '}
                <span className="font-mono">{cost.total_out.toLocaleString()}</span>
              </div>
            </div>
            {cost.from_ts && cost.to_ts && (
              <div className="text-[11px] text-gray-500 mt-2 pt-2 border-t border-gray-200">
                {new Date(cost.from_ts * 1000).toLocaleString()} —{' '}
                {new Date(cost.to_ts * 1000).toLocaleString()}
              </div>
            )}
          </div>

          {/* Per-model breakdown */}
          {Object.keys(cost.by_model).length > 0 && (
            <div>
              <h5 className="text-xs font-semibold text-gray-800 mb-2">
                By Model
              </h5>
              <ul className="space-y-2">
                {Object.entries(cost.by_model).map(([modelId, usage]) => (
                  <li
                    key={modelId}
                    className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs"
                  >
                    <div className="flex items-center justify-between mb-1">
                      <code className="font-mono text-[11px] text-gray-800">
                        {modelId}
                      </code>
                      <span className="font-mono font-semibold text-gray-900">
                        ${(usage.cost ?? 0).toFixed(4)}
                      </span>
                    </div>
                    <div className="flex gap-3 text-[11px] text-gray-600">
                      <div>
                        <span className="font-medium">In:</span>{' '}
                        <span className="font-mono">
                          {(usage.input_tokens ?? 0).toLocaleString()}
                        </span>
                      </div>
                      <div>
                        <span className="font-medium">Out:</span>{' '}
                        <span className="font-mono">
                          {(usage.output_tokens ?? 0).toLocaleString()}
                        </span>
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}
