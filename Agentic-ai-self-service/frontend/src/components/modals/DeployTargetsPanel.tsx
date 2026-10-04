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

type AccountTarget = {
  account_id: string;
  role_arn: string;
  runtime_role_arn: string;
  mcp_runtime_role_arn: string;
  harness_role_arn: string;
  artifact_bucket: string;
  region: string;
};

type RegionTarget = {
  region: string;
  account_id?: string | null;
  artifact_bucket?: string | null;
};

const REGIONAL_ARTIFACT_BUCKET_PATTERN =
  /^agentcore-flows-artifacts-\d{12}-[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/;

function isValidRegionalArtifactBucket(value: string): boolean {
  const bucket = value.trim();
  return bucket === ''
    || (
      bucket.length <= 63
      && !bucket.includes('..')
      && REGIONAL_ARTIFACT_BUCKET_PATTERN.test(bucket)
    );
}

export function DeployTargetsPanel() {
  const [enabled, setEnabled] = useState(false);
  const [regions, setRegions] = useState<string[]>([]);
  const [regionTargets, setRegionTargets] = useState<RegionTarget[]>([]);
  const [accounts, setAccounts] = useState<AccountTarget[]>([]);
  const [newRegion, setNewRegion] = useState('');
  const [newRegionArtifactBucket, setNewRegionArtifactBucket] = useState('');
  const [acct, setAcct] = useState({
    account_id: '',
    role_arn: '',
    runtime_role_arn: '',
    mcp_runtime_role_arn: '',
    harness_role_arn: '',
    artifact_bucket: '',
    region: '',
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const regionArtifactBucketIsValid = isValidRegionalArtifactBucket(
    newRegionArtifactBucket,
  );

  const load = useCallback(async () => {
    try {
      const cfg = await getApiClient().getDeployTargets();
      setEnabled(cfg.enabled);
      setRegions(cfg.regions);
      setRegionTargets(
        cfg.region_targets
        ?? cfg.regions.map((region) => ({ region })),
      );
      setAccounts(cfg.accounts);
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
            <div className="space-y-1 mb-2">
              {regions.map((region) => {
                const target = regionTargets.find((item) => item.region === region);
                return (
                  <div key={region} className="text-[11px] px-2 py-1 rounded bg-white/10">
                    <div className="font-mono">{region}</div>
                    <div className="text-[10px] text-gray-500 break-all">
                      {target?.artifact_bucket
                        ? `Regional artifacts: s3://${target.artifact_bucket}`
                        : 'Regional artifacts: validation required — re-register this region'}
                    </div>
                  </div>
                );
              })}
              {regions.length === 0 && <span className="text-[11px] text-gray-500">home region only</span>}
            </div>
            <div className="grid grid-cols-2 gap-2">
              <input
                className="rounded bg-black/20 border border-white/10 px-2 py-1 text-sm"
                placeholder="us-west-2" value={newRegion}
                aria-label="Region to allow"
                onChange={(e) => setNewRegion(e.target.value)}
              />
              <input
                className="rounded bg-black/20 border border-white/10 px-2 py-1 text-sm"
                placeholder="agentcore-flows-artifacts-<account>-<region> (optional)"
                value={newRegionArtifactBucket}
                aria-label="Regional runtime artifact bucket name"
                aria-describedby="regional-artifact-bucket-help"
                aria-invalid={!regionArtifactBucketIsValid}
                onChange={(e) => setNewRegionArtifactBucket(e.target.value)}
              />
            </div>
            {!regionArtifactBucketIsValid && (
              <p role="alert" className="text-[10px] text-red-400 mt-1">
                Use agentcore-flows-artifacts-&lt;12-digit platform account&gt;-&lt;suffix&gt;,
                or leave this blank to use the recommended regional default.
              </p>
            )}
            <div className="mt-2">
              <button
                type="button"
                disabled={busy || !newRegion.trim() || !regionArtifactBucketIsValid}
                onClick={() => void run(async () => {
                  await getApiClient().addDeployRegion(
                    newRegion.trim(),
                    newRegionArtifactBucket.trim() || undefined,
                  );
                  setNewRegion('');
                  setNewRegionArtifactBucket('');
                })}
                className="text-xs px-3 py-1 rounded border border-white/10 disabled:opacity-50"
              >Register &amp; validate region</button>
            </div>
            <p id="regional-artifact-bucket-help" className="text-[10px] text-gray-500 mt-1">
              Every non-home region needs an S3 code bucket in the platform account
              and that same region. Leave the bucket blank (recommended) to validate
              <code> agentcore-flows-artifacts-&lt;account&gt;-&lt;region&gt;</code>.
              An override must remain inside the
              <code> agentcore-flows-artifacts-&lt;platform-account&gt;-*</code> namespace;
              the server verifies the exact platform account.
            </p>
          </div>

          <div>
            <div className="text-xs font-semibold mb-1">Cross-account targets</div>
            {accounts.map((a) => (
              <div key={a.account_id} className="text-[11px] text-gray-400 py-1">
                <div className="font-mono">{a.account_id} · {a.region}</div>
                <div className="text-[10px] text-gray-500 break-all">
                  Runtime: {a.runtime_role_arn}
                </div>
                <div className="text-[10px] text-gray-500 break-all">
                  MCP runtime: {a.mcp_runtime_role_arn}
                </div>
                <div className="text-[10px] text-gray-500 break-all">
                  Harness: {a.harness_role_arn}
                </div>
                <div className="text-[10px] text-gray-500 break-all">
                  Artifacts: s3://{a.artifact_bucket}
                </div>
              </div>
            ))}
            <div className="grid grid-cols-3 gap-2 mt-1">
              <input className="rounded bg-black/20 border border-white/10 px-2 py-1 text-xs"
                placeholder="account id (12 digits)" value={acct.account_id}
                aria-label="Target AWS account ID"
                onChange={(e) => setAcct({ ...acct, account_id: e.target.value })} />
              <input className="rounded bg-black/20 border border-white/10 px-2 py-1 text-xs"
                placeholder="...:role/AgentCoreFlowsDeploymentRole" value={acct.role_arn}
                aria-label="Target deployment role ARN"
                onChange={(e) => setAcct({ ...acct, role_arn: e.target.value })} />
              <input className="rounded bg-black/20 border border-white/10 px-2 py-1 text-xs"
                placeholder="region" value={acct.region}
                aria-label="Target account region"
                onChange={(e) => setAcct({ ...acct, region: e.target.value })} />
            </div>
            <div className="grid grid-cols-3 gap-2 mt-2">
              <input className="rounded bg-black/20 border border-white/10 px-2 py-1 text-xs"
                placeholder="runtime role ARN (optional)"
                value={acct.runtime_role_arn}
                aria-label="Target AgentCore runtime execution role ARN"
                onChange={(e) => setAcct({ ...acct, runtime_role_arn: e.target.value })} />
              <input className="rounded bg-black/20 border border-white/10 px-2 py-1 text-xs"
                placeholder="MCP runtime role ARN (optional)"
                value={acct.mcp_runtime_role_arn}
                aria-label="Target AgentCore MCP runtime execution role ARN"
                onChange={(e) => setAcct({ ...acct, mcp_runtime_role_arn: e.target.value })} />
              <input className="rounded bg-black/20 border border-white/10 px-2 py-1 text-xs"
                placeholder="harness role ARN (optional)"
                value={acct.harness_role_arn}
                aria-label="Target AgentCore harness execution role ARN"
                onChange={(e) => setAcct({ ...acct, harness_role_arn: e.target.value })} />
            </div>
            <input className="w-full mt-2 rounded bg-black/20 border border-white/10 px-2 py-1 text-xs"
              placeholder="artifact bucket name (optional)"
              value={acct.artifact_bucket}
              aria-label="Target runtime artifact bucket name"
              onChange={(e) => setAcct({ ...acct, artifact_bucket: e.target.value })} />
            <button
              type="button" disabled={busy || !acct.account_id || !acct.role_arn || !acct.region}
              onClick={() => void run(async () => {
                await getApiClient().addDeployAccount(
                  acct.account_id,
                  acct.role_arn,
                  acct.region,
                  acct.runtime_role_arn || undefined,
                  acct.mcp_runtime_role_arn || undefined,
                  acct.harness_role_arn || undefined,
                  acct.artifact_bucket || undefined,
                );
                setAcct({
                  account_id: '',
                  role_arn: '',
                  runtime_role_arn: '',
                  mcp_runtime_role_arn: '',
                  harness_role_arn: '',
                  artifact_bucket: '',
                  region: '',
                });
              })}
              className="mt-2 text-xs px-3 py-1 rounded bg-cyan-600 text-white disabled:opacity-50"
            >Register &amp; validate account</button>
            <p className="text-[10px] text-gray-500 mt-1">
              The target needs the fixed, pathless deployment role{' '}
              <code>AgentCoreFlowsDeploymentRole</code>, plus separate pre-warmed
              model-capable Runtime, model-free MCP Runtime, and Harness execution
              roles and a regional S3 code bucket. Defaults are{' '}
              <code>AgentCoreFlowsRuntimeRole</code>,{' '}
              <code>AgentCoreFlowsMCPRuntimeRole</code>,{' '}
              <code>AgentCoreFlowsHarnessRole</code>, and{' '}
              <code>agentcore-flows-artifacts-&lt;account&gt;-&lt;region&gt;</code>.
              The execution roles and bucket may be overridden above.
            </p>
          </div>
        </>
      )}
    </div>
  );
}
