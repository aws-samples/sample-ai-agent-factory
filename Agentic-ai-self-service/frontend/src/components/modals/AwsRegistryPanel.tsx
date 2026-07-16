/**
 * AwsRegistryPanel — Phase 6 (Loom-inspired) AWS Agent Registry federation.
 *
 * Opt-in: an admin enters an AWS registryId to federate deployed agents into
 * the org-wide AWS-native Agent Registry (with the AWS approval workflow). Also
 * offers semantic search across the registry. Degrades to a disabled state when
 * the feature is unconfigured or the (public-preview) API is unavailable.
 */

import { useCallback, useEffect, useState } from 'react';
import { getApiClient, getErrorMessage } from '../../services/api';

export function AwsRegistryPanel() {
  const [enabled, setEnabled] = useState(false);
  const [available, setAvailable] = useState(false);
  const [registryId, setRegistryId] = useState('');
  const [input, setInput] = useState('');
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<Array<Record<string, unknown>>>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadConfig = useCallback(async () => {
    try {
      const cfg = await getApiClient().getAwsRegistryConfig();
      setEnabled(cfg.enabled);
      setAvailable(cfg.available);
      setRegistryId(cfg.registry_id ?? '');
    } catch {
      /* feature optional — leave disabled */
    }
  }, []);

  useEffect(() => { void loadConfig(); }, [loadConfig]);

  const enable = async () => {
    setBusy(true); setError(null);
    try {
      await getApiClient().enableAwsRegistry(input.trim());
      await loadConfig();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const search = async () => {
    setBusy(true); setError(null);
    try {
      const r = await getApiClient().searchAwsRegistry(query.trim());
      setResults(r.results ?? []);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-white/10 p-3 space-y-3 no-darkmap">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">AWS Agent Registry</span>
        <span className={
          enabled && available ? 'text-xs text-green-500'
          : enabled ? 'text-xs text-amber-500' : 'text-xs text-gray-400'
        }>
          {enabled && available ? 'Connected' : enabled ? 'Configured (unreachable)' : 'Not configured'}
        </span>
      </div>

      {error && <div className="text-xs text-red-400">{error}</div>}

      {!enabled ? (
        <div className="flex gap-2">
          <input
            className="flex-1 rounded bg-black/20 border border-white/10 px-2 py-1 text-sm"
            placeholder="AWS registryId (public preview)"
            value={input}
            onChange={(e) => setInput(e.target.value)}
          />
          <button
            type="button" disabled={busy || !input.trim()} onClick={() => void enable()}
            className="text-xs px-3 py-1 rounded bg-cyan-600 text-white disabled:opacity-50"
          >
            Enable
          </button>
        </div>
      ) : (
        <>
          <div className="text-[11px] text-gray-500 font-mono truncate">{registryId}</div>
          <div className="flex gap-2">
            <input
              className="flex-1 rounded bg-black/20 border border-white/10 px-2 py-1 text-sm"
              placeholder="Search registered agents…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') void search(); }}
            />
            <button
              type="button" disabled={busy || !query.trim()} onClick={() => void search()}
              className="text-xs px-3 py-1 rounded border border-white/10 disabled:opacity-50"
            >
              Search
            </button>
          </div>
          {results.length > 0 && (
            <div className="space-y-1 max-h-40 overflow-auto">
              {results.map((r, i) => (
                <div key={i} className="text-[11px] text-gray-300 truncate">
                  {String(r.name ?? r.recordArn ?? JSON.stringify(r))}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
