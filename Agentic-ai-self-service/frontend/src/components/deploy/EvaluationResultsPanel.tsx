/**
 * EvaluationResultsPanel — Phase 1 Gap 1C frontend.
 *
 * Shows the runtime's online evaluation config + recent per-evaluator scores
 * pulled from CloudWatch Logs Insights via the platform's
 * /api/runtimes/{name}/evaluations endpoint.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  getApiClient,
  getErrorMessage,
  isNotReadyError,
  type DashboardUrlSummary,
  type EvaluationConfigSummary,
  type EvaluationResultsSummary,
} from '../../services/api';

interface EvaluationResultsPanelProps {
  runtimeName: string | null;
  refreshKey?: number;
}

export function EvaluationResultsPanel({ runtimeName, refreshKey }: EvaluationResultsPanelProps) {
  const [cfg, setCfg] = useState<EvaluationConfigSummary | null>(null);
  const [cfgError, setCfgError] = useState<string | null>(null);
  const [results, setResults] = useState<EvaluationResultsSummary | null>(null);
  const [resultsError, setResultsError] = useState<string | null>(null);
  // Phase 1 Gap 1D — dashboard URL piggybacks on the same runtime resolution.
  const [dashboard, setDashboard] = useState<DashboardUrlSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [hours, setHours] = useState(24);

  const reload = useCallback(async () => {
    if (!runtimeName) return;
    setLoading(true);
    setCfgError(null);
    setResultsError(null);
    const api = getApiClient();
    try {
      setCfg(await api.getEvaluationConfig(runtimeName));
    } catch (e) {
      setCfg(null);
      // 404/403 means not deployed yet — empty state, not error
      if (!isNotReadyError(e)) {
        setCfgError(getErrorMessage(e));
      }
    }
    try {
      setResults(await api.listEvaluationResults(runtimeName, hours));
    } catch (e) {
      setResults(null);
      // 404/403 means not deployed yet — empty state, not error
      if (!isNotReadyError(e)) {
        setResultsError(getErrorMessage(e));
      }
    }
    try {
      setDashboard(await api.getDashboardUrl(runtimeName));
    } catch (e) {
      // 404 here is expected before first deploy — silently no dashboard.
      setDashboard(null);
      // Errors that aren't 404 still log via the eval results error path.
      if (!getErrorMessage(e).toLowerCase().includes('not found')) {
        console.warn('dashboard-url fetch failed:', e);
      }
    } finally {
      setLoading(false);
    }
  }, [runtimeName, hours]);

  useEffect(() => {
    void reload();
  }, [reload, refreshKey]);

  if (!runtimeName) {
    return (
      <div className="p-5 text-sm text-gray-500">
        Deploy this agent at least once to see evaluation results.
      </div>
    );
  }

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h4 className="text-sm font-semibold text-gray-800">Evaluation</h4>
          <p className="text-xs text-gray-500">
            AgentCore Online Evaluation runs builtin model-graded evaluators
            on a sample of invocations. Scores are written to CloudWatch and
            aggregated below.
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

      {/* Phase 1 Gap 1D — observability dashboard link */}
      {dashboard && (
        <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-xs flex items-center justify-between">
          <div>
            <div className="font-medium text-blue-900 mb-0.5">
              CloudWatch Dashboard
            </div>
            <div className="text-blue-800">
              Live latency, token usage, errors, tool calls.{' '}
              {!dashboard.exists && (
                <span className="text-amber-700">
                  (Dashboard hasn't been created yet — deploy this runtime to
                  generate it.)
                </span>
              )}
            </div>
            <code className="text-[10px] font-mono text-blue-700 block mt-0.5">
              {dashboard.dashboard_name}
            </code>
          </div>
          <a
            href={dashboard.dashboard_url}
            target="_blank"
            rel="noopener noreferrer"
            className={`text-xs px-2.5 py-1 rounded ${
              dashboard.exists
                ? 'bg-blue-600 text-white hover:bg-blue-700'
                : 'bg-gray-300 text-gray-600 cursor-not-allowed'
            }`}
            onClick={(e) => {
              if (!dashboard.exists) e.preventDefault();
            }}
          >
            Open in CloudWatch ↗
          </a>
        </div>
      )}

      {/* Config block */}
      {cfg ? (
        <div className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-xs space-y-1.5">
          <div className="flex items-center gap-2">
            <span className="font-medium text-gray-700">Status:</span>
            <span
              className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${
                cfg.status === 'ENABLED' || cfg.status === 'ACTIVE'
                  ? 'bg-emerald-100 text-emerald-700'
                  : 'bg-gray-200 text-gray-700'
              }`}
            >
              {cfg.status ?? '—'}
            </span>
            <span className="ml-auto font-medium text-gray-700">
              Sampling: {cfg.sampling_rate ?? '—'}%
            </span>
          </div>
          <div>
            <span className="font-medium text-gray-700">Evaluators:</span>{' '}
            {cfg.evaluators.length === 0 ? (
              <span className="text-gray-500">none</span>
            ) : (
              <span className="text-gray-700">
                {cfg.evaluators.join(', ')}
              </span>
            )}
          </div>
          <code className="text-[10px] font-mono text-gray-500">
            config_id: {cfg.config_id}
          </code>
        </div>
      ) : cfgError ? (
        cfgError.includes('not found') || cfgError.toLowerCase().includes('not found') ? (
          <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
            No evaluation config registered for this runtime. Wire an
            Evaluation node on the canvas and re-deploy to enable.
          </div>
        ) : (
          <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">
            {cfgError}
          </div>
        )
      ) : (
        <div className="text-xs text-gray-500">Loading evaluation config…</div>
      )}

      {/* Time range */}
      <div className="flex items-center gap-2 text-xs">
        <span className="text-gray-700">Time range:</span>
        {[1, 6, 24, 72, 168].map((h) => (
          <button
            key={h}
            type="button"
            onClick={() => setHours(h)}
            className={`px-2 py-0.5 rounded border ${
              hours === h
                ? 'bg-emerald-600 text-white border-emerald-600'
                : 'border-gray-200 text-gray-700 hover:bg-gray-50'
            }`}
          >
            {h < 24 ? `${h}h` : `${h / 24}d`}
          </button>
        ))}
      </div>

      {/* Results block */}
      {resultsError ? (
        <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">
          {resultsError}
        </div>
      ) : results === null ? (
        <div className="text-xs text-gray-500">Loading results…</div>
      ) : results.results.length === 0 ? (
        <div className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-xs text-gray-600">
          {results.message ?? 'No evaluation results in this window. Invoke the runtime to populate.'}
        </div>
      ) : (
        <table className="w-full text-xs border-collapse">
          <thead>
            <tr className="border-b border-gray-200 text-left text-gray-600">
              <th className="py-1.5 px-2 font-medium">Evaluator</th>
              <th className="py-1.5 px-2 font-medium text-right">Runs</th>
              <th className="py-1.5 px-2 font-medium text-right">Avg score</th>
              <th className="py-1.5 px-2 font-medium text-right">Latest</th>
            </tr>
          </thead>
          <tbody>
            {results.results.map((row) => (
              <tr key={row.eid} className="border-b border-gray-100">
                <td className="py-1.5 px-2 text-gray-800">{row.eid}</td>
                <td className="py-1.5 px-2 text-right font-mono text-gray-700">
                  {row.runs ?? '—'}
                </td>
                <td className="py-1.5 px-2 text-right font-mono text-gray-700">
                  {row.avg_score ? Number(row.avg_score).toFixed(3) : '—'}
                </td>
                <td className="py-1.5 px-2 text-right font-mono text-gray-700">
                  {row.latest_score ? Number(row.latest_score).toFixed(3) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="text-[10px] text-gray-400">
        Source: {results?.log_group_name ?? '—'}
      </div>
    </div>
  );
}
