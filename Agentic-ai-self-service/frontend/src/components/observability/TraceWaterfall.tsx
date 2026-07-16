/**
 * TraceWaterfall — Phase 5 (Loom-inspired) OTEL span timeline.
 *
 * Renders the parent/child span tree from GET /api/runtimes/{name}/traces as a
 * horizontal waterfall: each span is a bar positioned by offset_ms and sized by
 * duration_ms, indented by depth. Read-only; degrades to an empty state when no
 * spans are in the window (fresh/uninvoked runtime).
 */

import { useCallback, useEffect, useState } from 'react';
import { getApiClient, getErrorMessage, isNotReadyError, type TraceSpan, type TraceWaterfall as TW } from '../../services/api';

interface Props {
  runtimeName: string | null;
  refreshKey?: number;
}

function flatten(spans: TraceSpan[], acc: TraceSpan[] = []): TraceSpan[] {
  for (const s of spans) {
    acc.push(s);
    if (s.children?.length) flatten(s.children, acc);
  }
  return acc;
}

export function TraceWaterfall({ runtimeName, refreshKey }: Props) {
  const [wf, setWf] = useState<TW | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    if (!runtimeName) { setWf(null); return; }
    setLoading(true); setError(null);
    try {
      setWf(await getApiClient().getTraces(runtimeName));
    } catch (e) {
      if (isNotReadyError(e)) setWf(null);
      else setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [runtimeName]);

  useEffect(() => { void reload(); }, [reload, refreshKey]);

  if (!runtimeName) {
    return <div className="p-5 text-sm text-gray-500">Deploy and invoke this agent to see traces.</div>;
  }

  const rows = wf ? flatten(wf.spans) : [];
  const total = wf?.total_ms || 1;

  return (
    <div className="p-5 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h4 className="text-sm font-semibold text-gray-800">Trace waterfall</h4>
          <p className="text-xs text-gray-500">OTEL spans for the latest invocations (relative timing).</p>
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

      {loading && !wf ? (
        <div className="text-xs text-gray-500">Loading traces…</div>
      ) : rows.length === 0 ? (
        <div className="text-xs text-gray-500">No spans in the recent window — invoke the agent to generate a trace.</div>
      ) : (
        <div className="space-y-1">
          <div className="text-[11px] text-gray-500">total {wf?.total_ms.toFixed(1)} ms · {rows.length} spans</div>
          {rows.map((s, i) => {
            const leftPct = Math.min((s.offset_ms / total) * 100, 100);
            const widthPct = Math.max(Math.min((s.duration_ms / total) * 100, 100 - leftPct), 0.5);
            return (
              <div key={`${s.span_id}-${i}`} className="flex items-center gap-2 text-[11px]">
                <div className="w-40 truncate text-gray-700" style={{ paddingLeft: `${s.depth * 10}px` }} title={s.name}>
                  {s.name}
                </div>
                <div className="relative flex-1 h-4 bg-gray-100 rounded">
                  <div
                    className="absolute h-4 rounded bg-cyan-500/70"
                    style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                    title={`${s.offset_ms.toFixed(1)}ms +${s.duration_ms.toFixed(1)}ms`}
                  />
                </div>
                <div className="w-16 text-right font-mono text-gray-500">{s.duration_ms.toFixed(1)}ms</div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
