/**
 * AwsRegistryPanel — Phase 6 (Loom-inspired) AWS Agent Registry federation.
 *
 * Opt-in: an admin enters an AWS registryId to federate deployed agents into
 * the org-wide AWS-native Agent Registry (with the AWS approval workflow). Also
 * offers semantic search across the registry. Degrades to a disabled state when
 * the feature is unconfigured or the API is unavailable.
 *
 * Agent Registry is GA and is its own AWS service (no longer part of
 * bedrock-agentcore). `sdk_supported: false` from /aws-config means the backend
 * bundle's boto3 predates the GA models — a different fix from a bad registryId,
 * so the two states are labelled differently below.
 *
 * Three distinct reasons federation can be unavailable, three distinct fixes, so
 * three distinct labels: the SDK is too old (redeploy), the registry could not be
 * read at all (registryId / IAM / region), or the registry is real but not yet
 * READY (wait). Collapsing the last into "unreachable" sends an admin to re-check
 * a registryId that was never wrong.
 */

import { useCallback, useEffect, useState } from 'react';
import { getApiClient, getErrorMessage } from '../../services/api';

export function AwsRegistryPanel() {
  const [enabled, setEnabled] = useState(false);
  const [available, setAvailable] = useState(false);
  const [sdkSupported, setSdkSupported] = useState(true);
  const [status, setStatus] = useState<string | null>(null);
  // False when the backend could not reconcile hit statuses against the control
  // plane and therefore omitted them — distinguishes "no badge" from "no status".
  const [statusAuthoritative, setStatusAuthoritative] = useState(true);
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
      // Older backends don't return this field; assume supported so we don't
      // show a spurious "SDK too old" warning against them.
      setSdkSupported(cfg.sdk_supported !== false);
      // null/absent = the registry could not be read; a string = it answered.
      setStatus(cfg.status ?? null);
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
      setStatusAuthoritative(r.status_authoritative !== false);
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
          : enabled || !sdkSupported ? 'text-xs text-amber-500' : 'text-xs text-gray-400'
        }>
          {enabled && available ? 'Connected'
            : !sdkSupported ? 'SDK out of date'
            : enabled && status ? `Configured (${status})`
            : enabled ? 'Configured (unreachable)' : 'Not configured'}
        </span>
      </div>

      {!sdkSupported && (
        <div className="text-[11px] text-amber-500/90">
          This deployment's AWS SDK predates the GA Agent Registry API. Redeploy the
          backend with boto3 &ge; 1.43.66 to enable federation.
        </div>
      )}

      {sdkSupported && enabled && !available && status && (
        <div className="text-[11px] text-amber-500/90">
          The registry exists but its status is {status}, so it cannot accept records
          yet. Nothing to fix — it becomes available once AWS finishes provisioning.
        </div>
      )}

      {error && <div className="text-xs text-red-400">{error}</div>}

      {!enabled ? (
        <div className="flex gap-2">
          <input
            className="flex-1 rounded bg-black/20 border border-white/10 px-2 py-1 text-sm"
            placeholder="AWS Agent Registry registryId"
            value={input}
            onChange={(e) => setInput(e.target.value)}
          />
          <button
            type="button" disabled={busy || !sdkSupported || !input.trim()} onClick={() => void enable()}
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
          {results.length > 0 && !statusAuthoritative && (
            <div className="text-[11px] text-amber-500/90">
              Approval status omitted: it could not be confirmed against the registry's
              control plane, and the search index's copy goes stale after a redeploy.
            </div>
          )}
          {results.length > 0 && (
            <div className="space-y-1 max-h-40 overflow-auto">
              {results.map((r, i) => (
                <div key={i} className="flex items-center gap-2 text-[11px]">
                  <span className="text-gray-300 truncate flex-1">
                    {String(r.displayName ?? r.name ?? r.recordArn ?? JSON.stringify(r))}
                  </span>
                  {/* recordType/status are GA response fields; preview returned neither.
                      `status` here is the backend's control-plane-reconciled value, not
                      the search index's — DELETED means de-indexing hasn't caught up. */}
                  {r.recordType != null && (
                    <span className="shrink-0 rounded bg-white/5 px-1.5 text-gray-400">
                      {String(r.recordType)}
                    </span>
                  )}
                  {r.status != null && (
                    <span className={`shrink-0 ${
                      r.status === 'APPROVED' ? 'text-green-500'
                        : r.status === 'DELETED' ? 'text-gray-500' : 'text-amber-500'
                    }`}>
                      {String(r.status)}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
