/**
 * LiteLLMRegistryPanel — Workstream B: LiteLLM as an alternative registry backend.
 *
 * Opt-in and reversible. The platform's own DynamoDB catalog stays the default;
 * connecting a LiteLLM proxy here does nothing until "Make authoritative" is used,
 * and "Disconnect & use platform catalog" puts it back (clearing the stored
 * connection along with the setting). Modelled on AwsRegistryPanel: propless,
 * loads its own config, degrades to a disabled state when the API is unavailable.
 *
 * Three states this panel exists to keep apart, because they have three different
 * fixes and collapsing them sends an admin to the wrong one:
 *
 *   - Not configured                 → enter a base URL and a virtual key.
 *   - Configured but UNVERIFIED      → nothing is necessarily wrong. A self-hosted
 *     LiteLLM is often only reachable inside a VPC, and the control-plane Lambda
 *     has no VPC egress, so the probe cannot reach it from here even though the
 *     deployed agent can. A rejected key or a wrong URL is refused outright at save
 *     time and never lands in this state.
 *   - Active                         → LiteLLM is the authoritative catalog AND the
 *     deploy-time approval gate. Disabling a server there blocks new deploys that
 *     wire it.
 *
 * What it deliberately does NOT do: claim LiteLLM can accept writes. It has no
 * create/update/delete API for MCP server records (registration is Admin-UI or
 * config.yaml), so projected rows are read-only and the panel says so in the same
 * words the backend's 501 uses — taken from the backend's own `capabilities()`
 * rather than restated here, so the two cannot drift.
 */

import { useCallback, useEffect, useState } from 'react';
import { getApiClient, getErrorMessage } from '../../services/api';
import type { LiteLLMRegistryConfig, LiteLLMServer } from '../../services/api';

export function LiteLLMRegistryPanel() {
  const [cfg, setCfg] = useState<LiteLLMRegistryConfig | null>(null);
  const [servers, setServers] = useState<LiteLLMServer[]>([]);
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const active = cfg?.provider === 'litellm';
  const configured = cfg?.configured === true;

  const load = useCallback(async () => {
    try {
      const next = await getApiClient().getLiteLLMRegistryConfig();
      setCfg(next);
      if (next.configured) {
        try {
          const list = await getApiClient().listLiteLLMServers();
          setServers(list.servers ?? []);
        } catch {
          // A listing failure is expected for a private proxy; the unverified
          // banner already explains it. Don't overwrite a real save error.
          setServers([]);
        }
      }
    } catch {
      /* feature optional — leave the panel in its "not configured" state */
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const save = async (activate: boolean) => {
    setBusy(true); setError(null); setNotice(null);
    try {
      const next = await getApiClient().enableLiteLLMRegistry({
        base_url: baseUrl.trim(),
        api_key: apiKey.trim() || undefined,
        activate,
      });
      // Clear the key from component state as soon as it has been minted into
      // Secrets Manager — there is no reason for it to sit in the DOM afterwards.
      setApiKey('');
      setNotice(next.detail ?? null);
      await load();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const activate = async () => {
    setBusy(true); setError(null); setNotice(null);
    try {
      // Re-POST with activate — the stored key is reused via api_key_ref, so the
      // admin never has to paste it a second time to flip the switch.
      await getApiClient().enableLiteLLMRegistry({
        base_url: cfg?.base_url ?? '',
        api_key_ref: cfg?.api_key_ref ?? undefined,
        activate: true,
      });
      await load();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const revert = async () => {
    setBusy(true); setError(null);
    try {
      const res = await getApiClient().disableLiteLLMRegistry();
      setNotice(
        res.orphaned_api_key_ref
          ? `Reverted to the platform catalog. The virtual key is still stored at ${res.orphaned_api_key_ref} — it may be shared with a gateway node, so it was not deleted.`
          : 'Reverted to the platform catalog.'
      );
      await load();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const enabledCount = servers.filter((s) => s.enabled).length;
  const disabledCount = servers.length - enabledCount;

  return (
    <div className="rounded-lg border border-white/10 p-3 space-y-3 no-darkmap">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">LiteLLM Registry</span>
        <span className={
          active ? 'text-xs text-green-500'
          : configured ? 'text-xs text-amber-500' : 'text-xs text-gray-400'
        }>
          {active ? (cfg?.verified ? 'Authoritative' : 'Authoritative (unverified)')
            : configured ? 'Connected — platform catalog still authoritative'
            : 'Not configured'}
        </span>
      </div>

      {configured && !cfg?.verified && (
        <div className="text-[11px] text-amber-500/90">
          Saved, but the proxy could not be reached from the control plane. For a
          LiteLLM that only listens inside your VPC this is expected — the deployed
          agent reaches it even though this API cannot. The virtual key itself was
          accepted; a rejected key or a wrong URL is refused at save time.
        </div>
      )}

      {active && cfg?.capabilities?.notes && (
        <div className="text-[11px] text-gray-400">{cfg.capabilities.notes}</div>
      )}

      {error && <div className="text-xs text-red-400">{error}</div>}
      {notice && <div className="text-[11px] text-gray-400">{notice}</div>}

      {!configured ? (
        <div className="space-y-2">
          <input
            className="w-full rounded bg-black/20 border border-white/10 px-2 py-1 text-sm"
            placeholder="https://litellm.example.com"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
          <input
            type="password"
            autoComplete="off"
            className="w-full rounded bg-black/20 border border-white/10 px-2 py-1 text-sm"
            placeholder="LiteLLM virtual key (sk-…)"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
          <div className="flex gap-2">
            <button
              type="button"
              disabled={busy || !baseUrl.trim() || !apiKey.trim()}
              onClick={() => void save(false)}
              className="text-xs px-3 py-1 rounded border border-white/10 disabled:opacity-50"
            >
              Connect &amp; test
            </button>
            {/* Two buttons on purpose: connecting is safe and reversible, whereas
                making LiteLLM authoritative changes what every developer sees and
                which catalog gates deploys. That should be a deliberate second
                click, not a side effect of testing a URL. */}
            <button
              type="button"
              disabled={busy || !baseUrl.trim() || !apiKey.trim()}
              onClick={() => void save(true)}
              className="text-xs px-3 py-1 rounded bg-cyan-600 text-white disabled:opacity-50"
            >
              Connect &amp; make authoritative
            </button>
          </div>
          <div className="text-[11px] text-gray-500">
            The key is stored in Secrets Manager and never returned by the API.
          </div>
        </div>
      ) : (
        <>
          <div className="text-[11px] text-gray-500 font-mono truncate">{cfg?.base_url}</div>
          {servers.length > 0 && (
            <div className="text-[11px] text-gray-400" data-testid="litellm-server-counts">
              {enabledCount} enabled MCP server{enabledCount !== 1 ? 's' : ''}
              {disabledCount > 0 && (
                <>
                  {' · '}
                  {/* Named separately because it is the answer to "why was my
                      deploy just blocked?" — a disabled server is present in
                      LiteLLM but is NOT approval, so the gate refuses it. */}
                  <span className="text-amber-500/90">
                    {disabledCount} disabled (deploys wiring these are blocked)
                  </span>
                </>
              )}
            </div>
          )}
          {servers.length > 0 && (
            <div className="space-y-1 max-h-40 overflow-auto">
              {servers.map((s) => (
                <div key={s.slug} className="flex items-center gap-2 text-[11px]">
                  <span className="text-gray-300 truncate flex-1">{s.name}</span>
                  <span className={s.enabled ? 'shrink-0 text-green-500' : 'shrink-0 text-gray-500'}>
                    {s.enabled ? 'enabled' : 'disabled'}
                  </span>
                </div>
              ))}
            </div>
          )}
          <div className="flex gap-2">
            {!active && (
              <button
                type="button" disabled={busy} onClick={() => void activate()}
                className="text-xs px-3 py-1 rounded bg-cyan-600 text-white disabled:opacity-50"
              >
                Make authoritative
              </button>
            )}
            {/* "Disconnect", not just "switch back": the backend clears the stored
                connection as well as the setting, so the base URL has to be
                re-entered afterwards. Labelling it "Use platform catalog" alone
                would understate that. */}
            <button
              type="button" disabled={busy} onClick={() => void revert()}
              className="text-xs px-3 py-1 rounded border border-white/10 disabled:opacity-50"
            >
              Disconnect &amp; use platform catalog
            </button>
          </div>
        </>
      )}
    </div>
  );
}
