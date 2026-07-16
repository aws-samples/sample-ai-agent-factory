/**
 * DeployTargetsPanel — Phase 7 (opt-in) multi-region / multi-account deploys.
 *
 * OFF by default. An admin explicitly enables deployment targets, then
 * allowlists regions and/or registers cross-account deploy roles. When enabled,
 * the DeployPanel can offer a region/account picker; when disabled, deploys go
 * to the platform's home account+region exactly as before.
 *
 * Cross-account registration is validated server-side (the role must be
 * assumable and land in the expected account) before it's accepted.
 */

import { useCallback, useEffect, useState } from 'react';
import { getApiClient, getErrorMessage } from '../../services/api';

export function DeployTargetsPanel() {
  const [enabled, setEnabled] = useState(false);
  const [regions, setRegions] = useState<string[]>([]);
  const [accounts, setAccounts] = useState<Array<{ account_id: string; role_arn: string; region: string }>>([]);
  const [newRegion, setNewRegion] = useState('');
  const [acct, setAcct] = useState({ account_id: '', role_arn: '', region: '' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const cfg = await getApiClient().getDeployTargets();
      setEnabled(cfg.enabled); setRegions(cfg.regions); setAccounts(cfg.accounts);
    } catch {
      /* admin-only / feature optional — leave defaults */
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); await load(); }
    catch (e) { setError(getErrorMessage(e)); }
    finally { setBusy(false); }
  };

  return (
    <div className="rounded-lg border border-white/10 p-3 space-y-3 no-darkmap">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">Deployment targets (multi-region / account)</span>
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox" checked={enabled} disabled={busy}
            onChange={(e) => void run(() => getApiClient().enableDeployTargets(e.target.checked))}
          />
          {enabled ? 'Enabled' : 'Disabled (default)'}
        </label>
      </div>

      {error && <div className="text-xs text-red-400">{error}</div>}

      {!enabled ? (
        <p className="text-[11px] text-gray-500">
          Off by default — agents deploy to the platform's home account and region.
          Enable to allowlist other regions or register cross-account deploy roles.
        </p>
      ) : (
        <>
          <div>
            <div className="text-xs font-semibold mb-1">Allowed regions</div>
            <div className="flex flex-wrap gap-1 mb-2">
              {regions.map((r) => (
                <span key={r} className="text-[11px] px-2 py-0.5 rounded bg-white/10 font-mono">{r}</span>
              ))}
              {regions.length === 0 && <span className="text-[11px] text-gray-500">home region only</span>}
            </div>
            <div className="flex gap-2">
              <input
                className="flex-1 rounded bg-black/20 border border-white/10 px-2 py-1 text-sm"
                placeholder="us-west-2" value={newRegion}
                onChange={(e) => setNewRegion(e.target.value)}
              />
              <button
                type="button" disabled={busy || !newRegion.trim()}
                onClick={() => void run(async () => { await getApiClient().addDeployRegion(newRegion.trim()); setNewRegion(''); })}
                className="text-xs px-3 py-1 rounded border border-white/10 disabled:opacity-50"
              >Add region</button>
            </div>
          </div>

          <div>
            <div className="text-xs font-semibold mb-1">Cross-account targets</div>
            {accounts.map((a) => (
              <div key={a.account_id} className="text-[11px] text-gray-400 font-mono py-0.5">
                {a.account_id} · {a.region}
              </div>
            ))}
            <div className="grid grid-cols-3 gap-2 mt-1">
              <input className="rounded bg-black/20 border border-white/10 px-2 py-1 text-xs"
                placeholder="account id (12 digits)" value={acct.account_id}
                onChange={(e) => setAcct({ ...acct, account_id: e.target.value })} />
              <input className="rounded bg-black/20 border border-white/10 px-2 py-1 text-xs"
                placeholder="role arn" value={acct.role_arn}
                onChange={(e) => setAcct({ ...acct, role_arn: e.target.value })} />
              <input className="rounded bg-black/20 border border-white/10 px-2 py-1 text-xs"
                placeholder="region" value={acct.region}
                onChange={(e) => setAcct({ ...acct, region: e.target.value })} />
            </div>
            <button
              type="button" disabled={busy || !acct.account_id || !acct.role_arn || !acct.region}
              onClick={() => void run(async () => {
                await getApiClient().addDeployAccount(acct.account_id, acct.role_arn, acct.region);
                setAcct({ account_id: '', role_arn: '', region: '' });
              })}
              className="mt-2 text-xs px-3 py-1 rounded bg-cyan-600 text-white disabled:opacity-50"
            >Register &amp; validate account</button>
            <p className="text-[10px] text-gray-500 mt-1">
              Target account must have a role named <code>AgentCoreFlowsDeploymentRole</code> trusting this platform account.
            </p>
          </div>
        </>
      )}
    </div>
  );
}
