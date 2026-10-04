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
      // Not-yet-deployed runtime returns 403/404 — empty state, not error (Bug 136).
      // A 401 is NOT in that set: it only ever means the session died, and calling
      // that an empty state showed "no cost data" to a signed-out user.
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

  const hasCacheMetrics = Boolean(
    cost &&
      (cost.total_cache_read !== undefined ||
        cost.total_cache_write !== undefined ||
        cost.total_input_tokens !== undefined ||
        cost.cache_reporting !== undefined),
  );
  const totalInput =
    cost?.total_input_tokens ??
    (cost
      ? cost.total_in +
        (cost.total_cache_read ?? 0) +
        (cost.total_cache_write ?? 0)
      : 0);
  const hasUsage = Boolean(
    cost &&
      (cost.total_cost !== 0 ||
        totalInput !== 0 ||
        cost.total_out !== 0),
  );
  const incompleteCacheTelemetry = cost?.cache_reporting
    ? [
        !cost.cache_reporting.cache_read_complete ? 'Cache-read' : null,
        !cost.cache_reporting.cache_write_complete ? 'Cache-write' : null,
      ].filter((label): label is string => label !== null)
    : [];

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
      ) : !cost || !hasUsage ? (
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
            {hasCacheMetrics ? (
              <div className="grid grid-cols-2 gap-2 text-xs text-gray-700">
                <div>
                  <span className="font-medium">Total input:</span>{' '}
                  <span className="font-mono">{totalInput.toLocaleString()}</span>
                </div>
                <div>
                  <span className="font-medium">Output tokens:</span>{' '}
                  <span className="font-mono">{cost.total_out.toLocaleString()}</span>
                </div>
                <div>
                  <span className="font-medium">Uncached input:</span>{' '}
                  <span className="font-mono">{cost.total_in.toLocaleString()}</span>
                </div>
                <div>
                  <span className="font-medium">Cache reads:</span>{' '}
                  <span className="font-mono">
                    {(cost.total_cache_read ?? 0).toLocaleString()}
                  </span>
                </div>
                <div>
                  <span className="font-medium">Cache writes:</span>{' '}
                  <span className="font-mono">
                    {(cost.total_cache_write ?? 0).toLocaleString()}
                  </span>
                </div>
              </div>
            ) : (
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
            )}
            {incompleteCacheTelemetry.length > 0 && (
              <div
                role="status"
                className="mt-2 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800"
              >
                {incompleteCacheTelemetry.join(' and ')} token telemetry is
                incomplete for some invocations. Estimated cost may be understated.
              </div>
            )}
            {cost.from_ts && cost.to_ts && (
              <div className="text-[11px] text-gray-500 mt-2 pt-2 border-t border-gray-200">
                {new Date(cost.from_ts * 1000).toLocaleString()} —{' '}
                {new Date(cost.to_ts * 1000).toLocaleString()}
              </div>
            )}
          </div>

          {/* Phase 4 (Loom) FinOps — owner budget spend bar. Rendered when the
              caller has an owner budget set (annotated by the cost endpoint). */}
          {cost.owner_budget && cost.owner_budget.limit > 0 && (
            <div className="rounded-lg border border-gray-200 px-3 py-2.5">
              <div className="flex items-center justify-between mb-1.5 text-xs">
                <span className="font-semibold text-gray-800">Monthly budget</span>
                <span className={
                  cost.owner_budget.status === 'over' ? 'text-red-600 font-semibold'
                  : cost.owner_budget.status === 'warn' ? 'text-amber-600 font-semibold'
                  : 'text-green-700 font-semibold'
                }>
                  ${cost.owner_budget.spend.toFixed(2)} / ${cost.owner_budget.limit.toFixed(2)}
                  {' '}({cost.owner_budget.used_pct}%)
                </span>
              </div>
              <div className="h-2 w-full rounded-full bg-gray-100 overflow-hidden">
                <div
                  className={
                    cost.owner_budget.status === 'over' ? 'h-full bg-red-500'
                    : cost.owner_budget.status === 'warn' ? 'h-full bg-amber-500'
                    : 'h-full bg-green-500'
                  }
                  style={{ width: `${Math.min(cost.owner_budget.used_pct, 100)}%` }}
                />
              </div>
              {cost.owner_budget.status === 'over' && (
                <div className="text-[11px] text-red-600 mt-1">
                  Over budget — new spend exceeds the monthly limit.
                </div>
              )}
            </div>
          )}

          {/* Per-model breakdown */}
          {Object.keys(cost.by_model).length > 0 && (
            <div>
              <h5 className="text-xs font-semibold text-gray-800 mb-2">
                By Model
              </h5>
              <ul className="space-y-2">
                {Object.entries(cost.by_model).map(([modelId, usage]) => {
                  const modelHasCacheMetrics =
                    usage.cache_read_input_tokens !== undefined ||
                    usage.cache_write_input_tokens !== undefined ||
                    usage.total_input_tokens !== undefined ||
                    usage.cache_read_complete !== undefined ||
                    usage.cache_write_complete !== undefined;
                  const modelTotalInput =
                    usage.total_input_tokens ??
                    (usage.input_tokens ?? 0) +
                      (usage.cache_read_input_tokens ?? 0) +
                      (usage.cache_write_input_tokens ?? 0);

                  return (
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
                      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-gray-600">
                        {modelHasCacheMetrics ? (
                          <>
                            <div>
                              <span className="font-medium">Total in:</span>{' '}
                              <span className="font-mono">
                                {modelTotalInput.toLocaleString()}
                              </span>
                            </div>
                            <div>
                              <span className="font-medium">Uncached:</span>{' '}
                              <span className="font-mono">
                                {(usage.input_tokens ?? 0).toLocaleString()}
                              </span>
                            </div>
                            <div>
                              <span className="font-medium">Cache reads:</span>{' '}
                              <span className="font-mono">
                                {(usage.cache_read_input_tokens ?? 0).toLocaleString()}
                              </span>
                            </div>
                            <div>
                              <span className="font-medium">Cache writes:</span>{' '}
                              <span className="font-mono">
                                {(usage.cache_write_input_tokens ?? 0).toLocaleString()}
                              </span>
                            </div>
                          </>
                        ) : (
                          <div>
                            <span className="font-medium">In:</span>{' '}
                            <span className="font-mono">
                              {(usage.input_tokens ?? 0).toLocaleString()}
                            </span>
                          </div>
                        )}
                        <div>
                          <span className="font-medium">Out:</span>{' '}
                          <span className="font-mono">
                            {(usage.output_tokens ?? 0).toLocaleString()}
                          </span>
                        </div>
                        {usage.count !== undefined && (
                          <div>
                            <span className="font-mono">
                              {usage.count.toLocaleString()}
                            </span>{' '}
                            {usage.count === 1 ? 'invocation' : 'invocations'}
                          </div>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}
